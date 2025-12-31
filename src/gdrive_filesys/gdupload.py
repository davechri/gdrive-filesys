
import math
import os
import errno
import queue
import threading
from time import time

from fuse import FuseOSError

from gdrive_filesys import common, gitignore, lock, metrics
from gdrive_filesys import log
from gdrive_filesys.api import writefile
from gdrive_filesys.cache import db, metadata, data
from gdrive_filesys.log import logger

from googleapiclient.errors import HttpError

UPLOAD = 'upload'

class Upload:
    def __init__(self):
        self.uploadQueue = queue.Queue()
        self.activeThreadCount = 0 
        self.exceptionCount = 0
    
    def init(self):                     
        self.uploadDir = os.path.join(common.dataDir, 'upload')
        os.makedirs(self.uploadDir, exist_ok=True)
        files = os.listdir(self.uploadDir)
        for f in files:
            fullPath =  os.path.join(self.uploadDir, f)
            if os.path.isfile(fullPath):
                os.remove(fullPath)
        it = db.cache.getIterator()
        for key, value in it(prefix=bytes(UPLOAD, 'utf-8')):
            key = str(key, 'utf-8')
            db.cache.delete(key, UPLOAD)
    
    def write(self, path:str, buf: bytes, offset: int) -> int:
        st = metadata.cache.getattr(path)
        if st is None:
            raise FuseOSError(errno.ENOENT)
        metrics.counts.incr(f'gdupload_put_block')
        data.cache.putData(path, st.local_id, offset, buf)
        
        db.cache.put(f'{UPLOAD}:{st.local_id}', b'1', UPLOAD)
        
        metadata.cache.getattr_increase_size(path, offset + len(buf))        
      
        return len(buf)
    
    def isFlushPending(self, path: str, localId: str) -> bool:
        return db.cache.get(f'{UPLOAD}:{localId}', UPLOAD) is not None

    def flush(self, path: str) -> int:
        if common.offline:
            metrics.counts.incr('gdupload_flush_offline')
            return 0
        
        st = metadata.cache.getattr(path)
        if st is None:          
            metrics.counts.incr(f'gdupload_flush_noattr')
            return 0
        
        if st.gd_id == None:
            return 0

        if db.cache.get(f'{UPLOAD}:{st.local_id}', UPLOAD) == None:         
            metrics.counts.incr(f'gdupload_flush_nopending')
            return 0

        if not data.cache.isEntireFileCached(path, st.local_id, st):       
            metrics.counts.incr('gdupload_flush_file_is not_cached')
            return 0
        
        if gitignore.parser.isIgnored(path):
            logger.debug(f'gdupload.flush: {path} is ignored by .gitignore, skipping upload')
            metrics.counts.incr('gdupload_flush_ignored_by_gitignore')
            return 0
                
        localPath = None
        sentBytes = 0
        try:
            with lock.get(path):              
                flushFileToRemote = db.cache.get(f'{UPLOAD}:{st.local_id}', UPLOAD) is not None
                if flushFileToRemote:
                    db.cache.delete(f'{UPLOAD}:{st.local_id}', UPLOAD)       
                else:
                    metrics.counts.incr(f'gdupload_flush_noop')
                    return 0

            self.enqueueUploadQueue(path, st.local_id)            
        finally:            
            if localPath is not None:               
                os.remove(localPath)                
        return 0
    
    def start(self):         
        self.stopped = False
        for i in range(common.NUMBER_OF_FILE_READER_THREADS):
            threading.Thread(target=self.uploadThread, args=(i+1,), daemon=True).start()

    def stop(self):        
        logger.info('gdupload.stop')
        self.stopped = True

    def enqueueUploadQueue(self, path: str, localId: str):
        self.uploadQueue.put((path, localId))

    def uploadThread(self, number: int):
        
        common.threadLocal.operation = 'gdupload_%d' % number
        common.threadLocal.path = None
        while not self.stopped:            
            (path, localId) = self.uploadQueue.get()            

            common.threadLocal.path = (path,)
            metrics.counts.incr('gdupload_dequeued')
            metrics.counts.startExecution('gdupload_%d' % number)
            try:
                self.activeThreadCount += 1
                logger.info('-> gdupload.uploadThread %s local_id=%s', path, localId)                

                st = metadata.cache.getattr_by_id(localId)
                if st is None:
                    logger.warning(f'gdupload.uploadThread: {path} noop no attr for local_id={localId}')
                    continue

                localPath = os.path.join(self.uploadDir, f'{localId}-{time()}')
                fileSize = data.cache.copyDataToFile(path, localId, st, localPath)

                metrics.counts.incr(f'gdupload_flush_wrote_local_file_bytes', fileSize)

                sentBytes = writefile.execute(path, localId, localPath, st)
                
                metrics.counts.incr(f'gdupload_flush_network_bytes_sent', sentBytes) 
                
                if fileSize != sentBytes:
                    logger.warning('gdupload.flush: %s local_id=%s local file size %d differs from sent bytes %d', path, localId, fileSize, sentBytes)
                    metrics.counts.incr(f'gdupload_flush_size_mismatch')
                    raise FuseOSError(errno.EIO)
                
            except Exception as e:
                if isinstance(e, HttpError):
                    metrics.counts.incr('gdupload_httperror')
                    logger.error(f"<-- uploadThread: HttpError writing file {path}: {e}")
                elif isinstance(e, TimeoutError):
                    metrics.counts.incr('gdupload_timeouterror')
                    logger.error(f'<-- uploadThread: TimeoutError writing file {path}: {e}')
                else:
                    self.exceptionCount += 1
                    metrics.counts.incr('gdupload_exception')
                    raisedBy = log.exceptionRaisedBy(e)
                    logger.exception(f"<-- uploadThread: exception writing file {path}: {raisedBy}")
            finally:
                self.activeThreadCount -= 1
                metrics.counts.endExecution('gdupload_%d' % number)
                logger.info('<- uploadThread %s local_id=%s', path, localId)

manager = Upload()