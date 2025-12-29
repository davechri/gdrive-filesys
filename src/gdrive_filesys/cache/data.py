import math
from gdrive_filesys import common, metrics, attr
from gdrive_filesys.cache import db, mem, metadata
from gdrive_filesys.log import logger
from gdrive_filesys.api import readchunk

DATA = 'data'

class Key:
    def __init__(self, localId: str, blockNumber: int, offset: int, size: int):
        self.localId = localId
        self.blockNumber = blockNumber
        self.offset = offset
        self.size = size
        
DEL = '//' # key delimiter

class Data:
    def __init__(self):
        pass

    def key(self, id: str, offset: int, size: int) -> str: 
        blockNumber = math.floor(offset/common.BLOCK_SIZE) + 1       
        return f'{DATA}{DEL}{id}{DEL}{blockNumber:06d}{DEL}{offset:010d}{DEL}{size}'
    
    def parseKey(self, key: bytes|str) -> Key:
        if isinstance(key, bytes):
            key = str(key, 'utf-8')
        tokens = key.split(DEL)
        return Key(tokens[1], int(tokens[2]), int(tokens[3]), int(tokens[4]))
    
    def prefixBlockNumber(self, localId: str, offset: int):
        blockNumber = math.ceil(offset/common.BLOCK_SIZE) + 1
        return f'{DATA}{DEL}{localId}{DEL}{blockNumber:06d}{DEL}'
    
    def prefixLocalId(self, localId: str):         
        return f'{DATA}{DEL}{localId}{DEL}'

    def isEntireFileCached(self, path: str, localId: str, st: attr.Stat):
        isCoherent = self.coherencyCheck(path, localId, st)
        if not isCoherent:          
            metrics.counts.incr('data_entire_file_not_cached_coherency_failed')
            return False
        metrics.counts.incr('data_entire_file_coherency_ok')
        return True
    
    def getUnreadBlockCount(self, path: str, localId: str, st: attr.Stat) -> int:
        
        blockMap = bytearray(math.ceil(st.st_size / common.BLOCK_SIZE))
        
        it = db.cache.getIterator()
        for key, _ in it(prefix=bytes(self.prefixLocalId(localId), encoding='utf-8')):
            k = self.parseKey(key)
            blockMap[k.blockNumber - 1] = 1

        count = blockMap.count(0)        
        logger.debug(f'data.cache.getUnreadBlockCount: {path} {localId} unread blocks={count} of {len(blockMap)} size={st.st_size}')
        return count
    
    def getCachedFileSize(self, path: str, localId: str, st: attr.Stat) -> int:
        size = 0
        it = db.cache.getIterator()
        for key, _ in it(prefix=bytes(self.prefixLocalId(localId), encoding='utf-8')):
            k = self.parseKey(key)
            size += k.size
        return size


    def findNextUncachedBlockOffset(self, path: str, localId: str, st: attr.Stat, reverse: bool) -> int | None:
        if st.st_size == 0:
            return None
        
        blockMap = bytearray(math.ceil(st.st_size / common.BLOCK_SIZE))
        
        it = db.cache.getIterator()
        for key, _ in it(prefix=bytes(self.prefixLocalId(localId), encoding='utf-8')):
            k = self.parseKey(key)
            blockMap[k.blockNumber - 1] = 1
       
        index = blockMap.find(0) if not reverse else blockMap.rfind(0)
        if index != -1:
            return (index * common.BLOCK_SIZE) # offset          
        return None    

    def coherencyCheck(self, path: str, id: str, st: attr.Stat) -> bool:  
        errors = list[str]()    
        it = db.cache.getIterator() 
        offset = 0    
        blockNumber = 0
        blockSize = 0
        prevKey = None
        for key, value in it(prefix=bytes(self.prefixLocalId(id), encoding='utf-8')):
            logger.debug(f'data.cache.coherencyCheck: checking {path} {key} file_size={st.st_size} offset={offset} value_size={len(value)}')
            k = self.parseKey(key)
            if k.size != len(value):
                errors.append(f'{prevKey} {key} size {k.size} does not match data size {len(value)}')
                break
            if k.offset > offset:
                errors.append(f'{prevKey} {key} {k.offset-offset} bytes are missing at offset {offset}')
                break
            if k.offset < offset:
                errors.append(f'{prevKey} {key} {offset-k.offset} overlapping bytes at offset {offset}')
                break
            offset = k.size + k.offset
            if k.blockNumber == 0:
                errors.append(f'{prevKey} {key} blockNumber cannot be zero')
                break
        
            if k.blockNumber != blockNumber:
                if k.blockNumber - 1 != blockNumber:
                    errors.append(f'{prevKey} {key} blockNumber {k.blockNumber} is not sequential after blockNumber {blockNumber}')
                    break
                if k.offset != (k.blockNumber-1)*common.BLOCK_SIZE:
                    errors.append(f'{prevKey} {key} blockNumber {k.blockNumber} offset {k.offset} is not expected block boundary offset {(blockNumber)*common.BLOCK_SIZE}')
                    break
                if blockNumber > 0:
                    if blockSize != common.BLOCK_SIZE and offset + k.size != st.st_size:
                        errors.append(f'{prevKey} {key} block size {blockSize} is not full block size {common.BLOCK_SIZE}')
                        break
                blockNumber = k.blockNumber
                blockSize = 0
            blockSize += k.size
            prevKey = key

        if len(errors) == 0 and offset < st.st_size:
            errors.append(f'Total cached size {offset} does not match expected file size {st.st_size}')

        if len(errors) > 0:            
            logger.error(f'data.cache.coherencyCheck: {path} {id} {st.toDict()}\n {'\n'.join(errors)}')

        return len(errors) == 0

    def getBlock(self, id: str, offset: int) -> list[bytes] | None: 
        output: list[bytes] = []       
        it = db.cache.getIterator()
        for _, value in it(prefix=bytes(self.prefixBlockNumber(id, offset), encoding='utf-8')):
            output.append(value)
        return output if len(output) > 0 else None
    
    def copyDataToFile(self, path: str, localId: str, st: attr.Stat, localFilePath: str) -> int: 
        fileSize = 0
        with open(localFilePath, 'wb') as f:
            it = db.cache.getIterator()
            offset = 0            
            for key, value in it(prefix=bytes(self.prefixLocalId(localId), encoding='utf-8')):
                k = self.parseKey(key)
                if k.offset != offset:
                    self.coherencyCheck(path, localId, st)
                    raise ValueError(f'data.cache.copyDataToFile: {path} local_id={localId} Inconsistent data offset for key={key} expected={offset} actual={k.offset}')
                if k.size != len(value):
                    self.coherencyCheck(path, localId, st)
                    raise ValueError(f'data.cache.copyDataToFile: {path} local_id={localId} Inconsistent data size for key={key} expected={k.size} actual={len(value)}')
                offset += k.size
                f.write(value) 
                fileSize += len(value)
        logger.debug('data.cache.copyDataToFile: %s local_id=%s wrote %d bytes to %s', path, localId, fileSize, localFilePath)
        if fileSize < st.st_size:
            self.coherencyCheck(path, localId, st)
            raise ValueError(f'data.cache.readAll: {path} local_id={localId} Returned data size {fileSize} is not expected data size {st.st_size}')
        if fileSize > st.st_size:
            metrics.counts.incr(f'data.cache_copy_updated_size_to_{fileSize}')
            logger.warning(f'data.cache.copyDataToFile: {path} local_id={st.local_id} Cached file size {fileSize} is larger than attr file size {st.st_size}, updated metadata')
            st.st_size = fileSize
            metadata.cache.getattr_increase_size(path, fileSize)            
        return fileSize

    def read(self, path: str, localId: str, offset: int, size: int, st: attr.Stat) -> bytes: 
        output = bytearray()           
        it = db.cache.getIterator()
        
        blockStart = offset-offset%common.BLOCK_SIZE
        if blockStart >= st.st_size:
            self.coherencyCheck(path, localId, st)
            raise ValueError(f'data.cache.read: {path} local_id={localId} Read offset {offset} beyond EOF {st.st_size}') 
        
        while True:
            blockSize = 0
            key = None
            k = None
            prevKey = None
            prevK = None
            for key, value in it(prefix=bytes(self.prefixBlockNumber(localId, blockStart), encoding='utf-8')):
                k = self.parseKey(key)
                key = str(key, 'utf-8')
                logger.debug(f'data.cache.read cache: {path} key={key}')
                blockSize += k.size               
                if offset + len(output) >= k.offset and offset + len(output) < k.offset + len(value):
                    if prevK != None and k.offset != prevK.offset + prevK.size:
                        raise ValueError(f'data.cache.read: {path} Missing data prevKey={prevKey} key={key}')  
                    start = offset-k.offset if offset > k.offset else 0
                    end = min(start + k.size, start + size-len(output))
                    output += value[start:end]

                    logger.debug(f'data.cache.read cache: {path} Copied {end-start} bytes from key={key} offset={offset} size={size} output_size={len(output)}')
                    if len(output) == st.st_size or len(output) == size:
                        return output
                    prevKey = key
                    prevK = k
                elif k.offset >= offset + len(output):
                    return output
           
            if blockSize > 0 or st.local_only:
                # last chunk of data is before the end of the block
                if k.offset + k.size < blockStart + common.BLOCK_SIZE:
                    logger.debug(f'data.cache.read cache: {path} key={key} Finished reading {len(output)} available bytes from cached blocks at offset={blockStart} for requested offset={offset} size={size}')
                    return output           
            else: # If we did not find any data in cache, read from network
                readLen = min(common.BLOCK_SIZE, st.st_size-blockStart)
                block = readchunk.execute(path, st.gd_id, st.mime_type, readLen, blockStart)
                metrics.counts.incr('data_read_network_block')
                if len(block) != readLen:
                    raise ValueError(f'data.cache.read: Unexpected block size {len(block)} expected {readLen}')
                
                blockSize = len(block)                
                
                start = offset-blockStart if offset > blockStart else 0
                end = min(start + len(block), start + size-len(output))
                output += block[start:end]

                logger.debug(f'data.cache.read network: {path} local_id={localId} Copied {end-start} {start}:{end} bytes from network block offset={blockStart} block_size={len(block)} output_size={len(output)} file_size={st.st_size}')

                self.putData(path, localId, blockStart, block) # cache the data block

                if len(output) == size:
                    return output
                
            if blockSize > common.BLOCK_SIZE:                
                raise ValueError(f'data.cache.read: {path} local_id={localId} Read block size {blockSize} exceeds block size {common.BLOCK_SIZE}')                
            if len(output) + offset == st.st_size:               
                metrics.counts.incr('data_read_eof')
                return output
            if blockSize != common.BLOCK_SIZE:
                raise ValueError(f'data.cache.read: {path} local_id={localId} Incomplete block size {blockSize} at offset {blockStart} for file size={st.st_size} before EOF {st.toDict()}')      
            if len(output) >= size:
                raise ValueError(f'data.cache.read: {path} local_id={localId} Read size {len(output)} exceeds expected size {size}')
            
            blockStart += common.BLOCK_SIZE # move to next block

    def truncate(self, localId: str, maxSize: int) -> list[bytes]:
        output: list[bytes] = []
        it = db.cache.getIterator()
        currentSize = 0
        for key, value in it(prefix=bytes(self.prefixLocalId(localId), encoding='utf-8')):
            k = self.parseKey(key)
            if k.offset == currentSize and currentSize < maxSize:
                if currentSize + len(value) > maxSize:
                    output.append(value[:(maxSize-currentSize)])  
                    currentSize = maxSize              
                else:
                    output.append(value)
                    currentSize += len(value)
            else:
                db.cache.delete(key, DATA)
        return output

    def putData(self, path: str, localId: str, offset: int, data: bytes) -> None:      
        metrics.counts.incr('data_putdata')
        # metrics.counts.incr(f'data_putdata_size_{len(data)}')
        blockSize = len(data)  
        blockStart = 0
        blockEnd = min(common.BLOCK_SIZE-(offset%common.BLOCK_SIZE), blockSize)  
        blockOffset = offset    
        while True:            
            blockData = data[blockStart:blockEnd]
            self.deleteBlock(path, localId, blockOffset, len(blockData)) # delete old data block
            key = self.key(localId, blockOffset, len(blockData))
            metrics.counts.incr('data_putdata_block')
            logger.debug('data.cache.putData: %s local_id=%s key=%s size=%d', path, localId, key, len(blockData))
            db.cache.put(key, blockData, DATA)  
            blockSize -= len(blockData)
            if blockSize == 0:
                break          
            blockStart = blockEnd
            blockEnd = min(blockStart + common.BLOCK_SIZE, blockStart + blockSize)  
            blockOffset += len(blockData) 

    def deleteBlock(self, path:str, id: str, offset: int, size: int) -> None:
        it = db.cache.getIterator()
        for key, _ in it(prefix=bytes(self.prefixBlockNumber(id, offset), encoding='utf-8')):
            k = self.parseKey(key)
            if k.offset >= offset and k.offset < offset + size:
                logger.debug('data.cache.deleteBlock: %s local_id=%s key=%s size=%d', path, k.localId, key, k.size)
                db.cache.delete(key, DATA)

    def deleteByID(self, path:str, id: str) -> None:
        it = db.cache.getIterator()
        for key, _ in it(prefix=bytes(self.prefixLocalId(id), encoding='utf-8')):
            db.cache.delete(key, DATA)
     
        metrics.counts.incr('data_deletebyid')

    def deleteAll(self, path: str, localId: str|None):
        if localId != None:
            self.deleteByID(path, localId)
        metadata.cache.deleteMetadata(path, localId, 'data.cache.deleteAll: delete all data and metadata')

cache = Data()