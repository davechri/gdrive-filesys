
import math
from pathlib import Path
import queue
import stat
import threading

from gdrive_filesys import metrics, lock, common, gdupload
from gdrive_filesys import log
from gdrive_filesys.api import mkdir, create, symlink
from googleapiclient.errors import HttpError

from gdrive_filesys.cache import data, metadata
from gdrive_filesys.log import logger

class GdCreate:   
    def __init__(self):       
        self.stopped = False        
        self.queue = queue.Queue() 
        self.activeThreadCount = 0
        self.exceptionCount = 0 
        self.pendingCreates: dict[str, bool] = {}        
            
    def start(self):           
        self.stopped = False
        for i in range(common.NUMBER_OF_FILE_READER_THREADS):
            threading.Thread(target=self.worker, args=(i+1,), daemon=True).start()

    def stop(self):       
        logger.info('gdcreate.stop')
        self.stopped = True

    def enqueue(self, path: str, localId: str):        
        self.pendingCreates[localId] = True  
        self.queue.put((path, localId))

    def worker(self, number: int):        
        common.threadLocal.operation = 'gdcreate_%d' % number
        common.threadLocal.path = None
        while not self.stopped:            
            (path, localId) = self.queue.get()
           
            with lock.get(path):
                common.threadLocal.path = (path,)
                metrics.counts.incr('gdcreate_dequeued')
                metrics.counts.startExecution('gdcreate_%d' % number)
                try:
                    self.activeThreadCount += 1
                    logger.info('--> workerThread %s local_id=%s', path, localId)

                    for timeout in common.apiTimeoutRange():
                        try:                            
                            st = metadata.cache.getattr_by_id(localId)
                            if st == None:
                                logger.error(f'gdcreate: {path} noop already deleted local_id={localId}')
                                break

                            currentPath = st.getPath()  # Ensure path is set
                            if path != currentPath:
                                logger.warning(f'gdcreate: {path} path changed to {currentPath} local_id={localId}, using updated path')
                                path = currentPath

                            if st.st_mode & stat.S_IFREG == stat.S_IFREG:
                                create.execute(path, st.st_mode, localId, runAsync=False)
                            elif st.st_mode & stat.S_IFLNK == stat.S_IFLNK:
                                symlink.execute(path, localId, st.target_id, runAsync=False)
                            self.pendingCreates.pop(localId)
                            break
                        except TimeoutError as e:
                            logger.error(f'gdcreate timeout {e}')
                            metrics.counts.incr('gdcreate_network_timeout') 
                            if common.isLastAttempt(timeout):
                                raise
                    
                    logger.info('<-- workerThread %s local_id=%s', path, localId)
                
                except Exception as e:
                    if isinstance(e, HttpError):
                        metrics.counts.incr('gdcreate_httperror')
                        logger.error(f"<-- workerThread: HttpError creating file {path} local_id={localId}: {e}")
                    elif isinstance(e, TimeoutError):
                        metrics.counts.incr('gdcreate_timeouterror')
                        logger.error(f'<-- workerThread: TimeoutError creating file {path} local_id={localId}: {e}')
                    else:
                        self.exceptionCount += 1
                        metrics.counts.incr('gdcreate_exception')
                        raisedBy = log.exceptionRaisedBy(e)
                        logger.exception(f"<-- workerThread: exception creating file {path} local_id={localId}: {raisedBy}")
                finally:
                    self.activeThreadCount -= 1
                    metrics.counts.endExecution('gdcreate_%d' % number)

            logger.info(f'flush --> {path} local_id={localId}')
            metrics.counts.incr('execute_flush')
            size = gdupload.manager.flush(path)
            logger.info(f'flush <-- {path} local_id={localId} size={size}')
                
manager: GdCreate = GdCreate()