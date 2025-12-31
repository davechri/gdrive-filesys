
import stat
import json
import traceback

from googleapiclient.errors import HttpError

from gdrive_filesys import common, gdcreate, metrics, gdupload
from gdrive_filesys.cache import db, metadata
from gdrive_filesys.api import create, remove, chmod, mkdir, symlink, truncate, rmdir, rename
from gdrive_filesys.log import logger
from gdrive_filesys import log, gitignore

FILE_EVENT = 'file'
DIR_EVENT = 'dir'
RENAME_EVENT = 'rename'
SYMLINK_EVENT = 'symlink'
CHMOD_EVENT = 'chmod'
TRUNCATE_EVENT = 'truncate'

EVENT_PREFIX = 'event'
EVENT_SEQ_NUM_KEY = '_eventseq'

eventCount = 0

class Key:
    def __init__(self, event: str, fromOperation: str, seqNum: int):
        self.event = event
        self.fromOperation = fromOperation
        self.seqNum = seqNum

class Value:
    def __init__(self, path: str, path2: str|None, localId: str, gdId: str|None, failedCount: int, retryCount: int):
        self.path = path
        self.path2 = path2
        self.localId = localId
        self.gdId = gdId
        self.failedCount = failedCount
        self.retryCount = retryCount

class RetryException(Exception):
    def __init__(self, message: str):
        super().__init__(message)

class EventQueue:
    def init(self):
        seqNum = db.cache.get(EVENT_SEQ_NUM_KEY, EVENT_PREFIX) 
        if seqNum == None:
            seqNum = 0
        else:
            seqNum = int(str(seqNum, 'utf-8'))       
        self.seqNum = seqNum
        global eventCount
        it = db.cache.getIterator()
        for key, value in it(prefix=bytes(EVENT_PREFIX, 'utf-8')):
            eventCount += 1
        logger.info(f'init: seqNum={self.seqNum} eventCount={eventCount}')
    
    def key(self, event: str, fromOperation: str, seqNum: int) -> str:
        return f'{EVENT_PREFIX}:{seqNum:010d}:{event.ljust(10)}:{fromOperation.ljust(10)}'
    
    def enqueueFileEvent(self, path: str, localId: str, gdId: str|None) -> None:
        self.enqueueEvent(path, None, localId, gdId, FILE_EVENT)

    def enqueueDirEvent(self, path: str, localId: str, gdId: str|None) -> None:
        self.enqueueEvent(path, None, localId, gdId, DIR_EVENT)

    def enqueueRenameEvent(self, oldpath: str, newPath: str, localId: str, gdId: str|None) -> None:
        if newPath is None:
            raise Exception(f'enqueueRenameEvent: {oldpath} newPath cannot be None')
        self.enqueueEvent(oldpath, newPath, localId, gdId, RENAME_EVENT)

    def enqueueSymlinkEvent(self, path: str,  localId: str, gdId: str|None) -> None:
        self.enqueueEvent(path, None, localId, gdId, SYMLINK_EVENT)

    def enqueueChmodEvent(self, path: str, localId: str, gdId: str|None) -> None:
        self.enqueueEvent(path, None, localId, gdId, CHMOD_EVENT)
    def enqueueTruncateEvent(self, path: str, localId: str, gdId: str|None) -> None:
        self.enqueueEvent(path, None, localId, gdId, TRUNCATE_EVENT)

    def enqueueEvent(self, path: str, path2: str|None, localId: str, gdId: str|None, event: str) -> None:
        fromOperation = common.threadLocal.operation
        metrics.counts.incr(f'enqueue_{fromOperation}_{event}_event')
        self.seqNum += 1
        logger.info(f'enqueue {event} event: {path} {path2} local_id={localId} gd_id={gdId} seqNum={self.seqNum}')
        data = {
            'path': path,
            'path2': path2,
            'local_id': localId,
            'gd_id': gdId
        }
        db.cache.put(self.key(event, fromOperation, self.seqNum), bytes(json.dumps(data), 'utf-8'), EVENT_PREFIX)
        db.cache.put(EVENT_SEQ_NUM_KEY, bytes(str(self.seqNum), 'utf-8'), EVENT_PREFIX)
        global eventCount
        eventCount += 1
    
    def parseKey(self, key: bytes) -> Key:
        key = str(key, 'utf-8')
        (_, seqNum, event, fromOperation) = key.split(':', 4)
        return Key(event.strip(), fromOperation.strip(), int(seqNum))
    
    def parseValue(self, value: bytes) -> Value:
        data = json.loads(value)
        path = data.get('path')
        path2 = data.get('path2')
        localId = data.get('local_id')
        gdId = data.get('gd_id')
        failedCount = data.get('failed_count', 0)
        retryCount = data.get('retry_count', 0)
        return Value(path, path2, localId, gdId, failedCount, retryCount)

    def executeEvents(self) -> None:
        metrics.counts.incr('execute_events')
        logger.debug('executeEvents: start')
        saveOperation = common.threadLocal.operation
        savePath = common.threadLocal.path
        try:
            common.threadLocal.operation = 'eventq.py'
            common.threadLocal.path = None
            files: list[tuple[str, str]] = []
            it = db.cache.getIterator()
            for key, value in it(prefix=bytes(EVENT_PREFIX, 'utf-8')):
                key = str(key, 'utf-8')
                (_, seqNum, event, fromOperation) = key.split(':', 4)
                event = event.strip()
                fromOperation = fromOperation.strip()
                data = json.loads(value)
                path = data.get('path')
                path2 = data.get('path2')
                localId = data.get('local_id')
                gdId = data.get('gd_id')
                failedCount = data.get('failed_count', 0)  
                retryCount = data.get('retry_count', 0)              
                exception = None
                try:                   
                    common.threadLocal.path = (path,)                    
                    if event == FILE_EVENT:
                        common.threadLocal.operation = f'{fromOperation}_file_event'
                        self.executeFileEvent(path, localId, gdId, seqNum, fromOperation)
                    elif event == DIR_EVENT:
                        common.threadLocal.operation = f'{fromOperation}_dir_event'
                        self.executeDirEvent(path, localId, gdId, seqNum, fromOperation)  
                    elif event == RENAME_EVENT:  
                        op = fromOperation + '_' if fromOperation != 'rename' else ''
                        common.threadLocal.operation = f'{op}rename_event'
                        self.executeRenameEvent(path, path2,localId, gdId, seqNum, fromOperation)               
                    elif event == SYMLINK_EVENT:
                        op = fromOperation + '_' if fromOperation != 'symlink' else ''
                        common.threadLocal.operation = f'{op}symlink_event'
                        self.executeSymlinkEvent(path, localId, gdId, seqNum, fromOperation)  
                    elif event == CHMOD_EVENT:
                        op = fromOperation + '_' if fromOperation != 'chmod' else ''
                        common.threadLocal.operation = f'{op}chmod_event'
                        self.executeChmodEvent(path, localId, gdId, seqNum, fromOperation)
                    elif event == TRUNCATE_EVENT:
                        op = fromOperation + '_' if fromOperation != 'truncate' else ''
                        common.threadLocal.operation = f'{op}truncate_event'
                        self.executeTruncateEvent(path, localId, gdId, seqNum, fromOperation)   
                    else:
                        logger.error(f'executeEvents: unknown event {event} for path={path} local_id={localId} gd_id={gdId} seqNum={seqNum}')
                except Exception as e:                    
                    raisedBy = log.exceptionRaisedBy(e) 
                    if isinstance(e, RetryException):
                        logger.warning(f'executeEvents retry exception: event={event} path={path} path2={path2} local_id={localId} gd_id={gdId} seqNum={seqNum} {raisedBy}')    
                        metrics.counts.incr('eventqueue_retry_exception')                
                    else:                   
                        logger.exception(f'executeEvents exception: event={event} path={path} local_id={localId} gd_id={gdId} seqNum={seqNum} {raisedBy}')
                        metrics.counts.incr('eventqueue_exception')

                    if isinstance(e, HttpError):
                        metrics.counts.incr('eventqueue_http_'+str(e.resp.status))
                        if e.resp.status == 404:
                            logger.error(f'HttpError 404 - dropping event: event={event} path={path} local_id={localId} gd_id={gdId} seqNum={seqNum}')
                        else:
                            exception = e                            
                    else:
                        exception = e                        
                finally:
                    if exception == None:
                        db.cache.delete(self.key(event, fromOperation, int(seqNum)), EVENT_PREFIX) 
                        global eventCount
                        eventCount -= 1
                    else:
                        metrics.counts.incr('eventqueue_requeue_event')
                        if isinstance(exception, RetryException):
                            retryCount += 1
                        else:
                            failedCount += 1
                        data = {
                            'path': path,
                            'path2': path2,
                            'local_id': localId,
                            'gd_id': gdId,
                            'failed_count': failedCount,
                            'retry_count': retryCount
                        }
                        db.cache.put(self.key(event, fromOperation, int(seqNum)), bytes(json.dumps(data), 'utf-8'), EVENT_PREFIX)                               
        except Exception as e:
            raisedBy = log.exceptionRaisedBy(e)
            logger.exception(f'executeEvents: exception {raisedBy}')
            metrics.counts.incr('eventqueue_exception')            
        finally:
            common.threadLocal.operation = saveOperation
            common.threadLocal.path = savePath
            
    def executeDirEvent(self, path: str, localId: str, gdId: str|None, seqNum, fromOperation) -> None:
        metrics.counts.incr(f'execute_{fromOperation}DirEvent')
        logger.info(f'execute: path={path} local_id={localId} gd_id={gdId} seqNum={seqNum}')        
        st = metadata.cache.getattr(path, localId)
        if st != None:
            if st.local_only:
                logger.warning(f'execute: path={path} is local only, not creating on Google Drive')
            elif gitignore.parser.isIgnored(path):
                logger.info(f'execute: path={path} is gitignored, not creating on Google Drive')
                metadata.cache.changeToLocalOnly(path, localId, st)
            elif st.gd_id == None:               
                logger.info(f'mkdir --> {path} local_id={localId}  gd_id={gdId} mode={oct(st.st_mode)}')
                metrics.counts.incr('execute_create_dir')
                gdId = mkdir.execute(path, st.st_mode, runAsync=False)
                logger.info(f'mkdir <-- {path} local_id={localId} gd_id={gdId}')
            else:
                logger.info(f'dir has google drive id - noop: {path} local_id={localId}')
        else: 
            if gdId == None: 
                metrics.counts.incr('execute_dir_was_deleted')
                logger.info(f'noop directory was deleted: {path} local_id={localId} gd_id={gdId}')            
            else:
                logger.info(f'rmdir --> {path} local_id={localId} gd_id={gdId}')
                metrics.counts.incr('execute_delete_dir')
                rmdir.gdDelete(path, localId, gdId)
                logger.info(f'rmdir <-- {path} local_id={localId} gd_id={gdId}') 

    def executeFileEvent(self, path: str, localId: str, gdId: str|None, seqNum, fromOperation) -> None:
        metrics.counts.incr(f'execute_{fromOperation}FileEvent')
        logger.info(f'execute: path={path} local_id={localId} gd_id={gdId} seqNum={seqNum}')
        st = metadata.cache.getattr(path, localId)
        if st != None:
            if st.local_only:
                logger.warning(f'execute: path={path} is local only, not creating on Google Drive')
            elif gitignore.parser.isIgnored(path):
                logger.info(f'execute: path={path} is gitignored, not creating on Google Drive')
                metadata.cache.changeToLocalOnly(path, localId, st)
            elif st.gd_id == None:
                gdcreate.manager.enqueue(path, localId)  
                return              
            else:
                logger.info(f'file has google drive id - noop: {path} local_id={localId} gd_id={st.gd_id}')
            logger.info(f'flush --> {path} local_id={localId} gd_id={st.gd_id}')
            metrics.counts.incr('execute_flush')
            size = gdupload.manager.flush(path)
            logger.info(f'flush <-- {path} local_id={localId} gd_id={st.gd_id} size={size}')
        else:
            if gdId == None:
                metrics.counts.incr('execute_file_was_deleted')
                logger.info(f'noop file was deleted or renamed: {path} local_id={localId}')
            else:
                logger.info(f'delete --> {path} local_id={localId} gd_id={gdId}')
                metrics.counts.incr('execute_delete_file')
                remove.gdDelete(path, localId, gdId)
                logger.info(f'delete <-- {path} local_id={localId} gd_id={gdId}')
    
    
    def executeRenameEvent(self, oldPath: str, newPath:str, localId: str, gdId: str|None, seqNum, fromOperation) -> None:
        metrics.counts.incr(f'execute_{fromOperation}RenameEvent')
        logger.info(f'execute: oldPath={oldPath} newPath={newPath} local_id={localId} gd_id={gdId} seqNum={seqNum}')        
        st = metadata.cache.getattr(newPath, localId)
        if st != None:
            if st.local_only:
                logger.warning(f'execute: newPath={newPath} is local only, not creating on Google Drive')               
            elif st.gd_id == None:
                if gdcreate.manager.pendingCreates.get(localId) != None:                    
                    raise RetryException(f'executeRenameEvent: create pending for local_id={localId}, retrying later')                
                    
                logger.info(f'execute: create non-existing file with new path {newPath} local_id={localId} gd_id={gdId}')
                if st.st_mode & stat.S_IFDIR:
                    self.executeDirEvent(newPath, localId, gdId, seqNum, fromOperation)
                elif st.st_mode & stat.S_IFREG == stat.S_IFREG:
                    self.executeFileEvent(newPath, localId, gdId, seqNum, fromOperation)
                elif st.st_mode & stat.S_IFLNK == stat.S_IFLNK:
                    self.executeSymlinkEvent(newPath, localId, gdId, seqNum, fromOperation)
            else:
                if newPath != st.getPath():
                    logger.error(f'executeRenameEvent: newPath {newPath} does not match st.getPath() {st.getPath()} for local_id={localId} gd_id={gdId}')
                    return
                logger.info(f'rename --> old={oldPath} new={newPath} local_id={localId}  gd_id={st.gd_id}')
                metrics.counts.incr('execute_rename')
                rename.gdRename(oldPath, newPath, st)    
                logger.info(f'rename <-- old={oldPath} new={newPath} local_id={localId}  gd_id={st.gd_id}')        
        else:            
            metrics.counts.incr('execute_rename_was_deleted')
            logger.info(f'noop rename was deleted: old={oldPath} new={newPath} local_id={localId} gd_id={gdId}')
            
    def executeSymlinkEvent(self, path: str, localId: str, gdId: str|None, seqNum, fromOperation) -> None:
        metrics.counts.incr(f'execute_{fromOperation}SymlinkEvent')
        logger.info(f'execute: path={path} local_id={localId} gd_id={gdId} seqNum={seqNum}')
        st = metadata.cache.getattr(path, localId)
        if st != None:
            if st.local_only:
                logger.warning(f'execute: path={path} is local only, not creating on Google Drive')
            elif gitignore.parser.isIgnored(path):
                logger.info(f'execute: path={path} is gitignored, not creating on Google Drive')
                metadata.cache.changeToLocalOnly(path, localId, st)
            elif st.gd_id == None:
                gdcreate.manager.enqueue(path, localId)
            else:
                logger.info(f'symlink google drive id - noop: {path} local_id={localId} gd_id={st.gd_id}')
        else:
            if gdId == None:
                metrics.counts.incr('execute_symlink_was_deleted')
                logger.info(f'noop symlink was deleted or renamed: {path} local_id={localId} gd_id={gdId}')
            else:
                logger.info(f'remove --> {path} local_id={localId} gd_id={gdId}')
                metrics.counts.incr('execute_delete_symlink')
                remove.gdDelete(path, localId, gdId)
                logger.info(f'remove <-- {path} local_id={localId} gd_id={gdId}')

    def executeChmodEvent(self, path: str, localId: str, gdId: str|None, seqNum, fromOperation) -> None:
        metrics.counts.incr(f'execute_{fromOperation}ChmodEvent')
        logger.info(f'execute: path={path} local_id={localId} gd_id={gdId} seqNum={seqNum}')
        st = metadata.cache.getattr(path, localId)
        if st != None:
            if st.local_only:
                logger.warning(f'execute: path={path} is local only, not changing mode on Google Drive')   
            logger.info(f'chmod --> {path} local_id={localId} gd_id={gdId} mode={oct(st.st_mode)}')
            metrics.counts.incr('execute_chmod')
            chmod.execute(path, localId, st.st_mode, st, runAsync=False)
            logger.info(f'chmod <-- {path} local_id={localId} gd_id={gdId}')
        else:
            logger.info(f'chmod google drive id - noop: {path} local_id={localId} gd_id={gdId}')

    def executeTruncateEvent(self, path: str, localId: str, gdId: str|None, seqNum, fromOperation) -> None:
        metrics.counts.incr(f'execute_{fromOperation}TruncateEvent')
        logger.info(f'execute: path={path} local_id={localId} gd_id={gdId} seqNum={seqNum}')
        st = metadata.cache.getattr(path, localId)
        if st != None:
            if st.local_only:
                logger.warning(f'execute: path={path} is local only, not truncating on Google Drive')
            elif st.gd_id == None:
                raise RetryException(f'executeTruncateEvent: path={path} local_id={localId} has no gd_id, retrying later')
            else:
                logger.info(f'truncate --> {path} local_id={localId} gd_id={gdId} size={st.st_size}')
                metrics.counts.incr('execute_truncate')
                truncate.execute(path, localId, st.st_size, st, runAsync=False)
                logger.info(f'truncate <-- {path} local_id={localId} gd_id={gdId}')
        else:
            logger.info(f'truncate google drive id - noop: {path} local_id={localId} gd_id={gdId}')

queue = EventQueue()