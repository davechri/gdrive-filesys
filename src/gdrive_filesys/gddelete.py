
import math
from pathlib import Path
import os
import queue
import threading

from gdrive_filesys import metrics, lock, common
from gdrive_filesys import log
from gdrive_filesys.api import api
from googleapiclient.errors import HttpError

from gdrive_filesys.cache import data, metadata
from gdrive_filesys.log import logger

class GdDelete:   
    def __init__(self):       
        self.stopped = False        
        self.queue = queue.Queue() 
        self.activeThreadCount = 0
        self.exceptionCount = 0  
            
    def start(self):           
        self.stopped = False
        for i in range(common.NUMBER_OF_FILE_READER_THREADS):
            threading.Thread(target=self.worker, args=(i+1,), daemon=True).start()

    def stop(self):       
        logger.info('gddelete.stop')
        self.stopped = True

    def enqueue(self, path: str, localId: str,gdId: str):        
        self.queue.put((path, localId, gdId))

    def worker(self, number: int):        
        common.threadLocal.operation = 'gddelete_%d' % number
        common.threadLocal.path = None
        while not self.stopped:            
            (path, localId, gdId) = self.queue.get()
           
            with lock.get(path):
                common.threadLocal.path = (path,)
                metrics.counts.incr('gddelete_dequeued')
                metrics.counts.startExecution('gddelete_%d' % number)
                try:
                    self.activeThreadCount += 1
                    logger.info('--> workerThread %s local_id=%s', path, localId)

                    for timeout in common.apiTimeoutRange():
                        try:                            
                            service = common.getApiClient(common.API_TIMEOUT)
                            service.files().delete(fileId=gdId).execute()
                            break
                        except TimeoutError as e:
                            logger.error(f'rmdir timeout {e}')
                            metrics.counts.incr('rmdir_network_timeout') 
                            if common.isLastAttempt(timeout):
                                raise
                    
                    logger.info('<-- workerThread %s local_id=%s', path, localId)
                
                except Exception as e:
                    if isinstance(e, HttpError):
                        metrics.counts.incr('gddelete_httperror')
                        logger.error(f"<-- workerThread: HttpError deleting file {path} local_id={localId}: {e}")
                    elif isinstance(e, TimeoutError):
                        metrics.counts.incr('gddelete_timeouterror')
                        logger.error(f'<-- workerThread: TimeoutError deleting file {path} local_id={localId}: {e}')
                    else:
                        self.exceptionCount += 1
                        metrics.counts.incr('gddelete_exception')
                        raisedBy = log.exceptionRaisedBy(e)
                        logger.exception(f"<-- workerThread: exception deleting file {path} local_id={localId}: {raisedBy}")
                finally:
                    self.activeThreadCount -= 1
                    metrics.counts.endExecution('gddelete_%d' % number)
                
manager: GdDelete = GdDelete()