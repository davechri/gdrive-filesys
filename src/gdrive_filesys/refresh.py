
import threading
import time

from gdrive_filesys import common, directories, eventq, gdcreate, gddelete, log, metrics 
from gdrive_filesys.api import api
from gdrive_filesys.cache import refreshcache
from gdrive_filesys.log import logger

from googleapiclient.errors import HttpError

from fuse import FuseOSError

class Refresh:
    def __init__(self):       
        self.event: threading.Event
        self.refreshRunning: bool = False
        self.exceptionCount: int = 0
        
    def start(self):
        threading.Thread(target=self.refreshThread, daemon=True).start() 

    def trigger(self):
        """Triggers the refresh thread to start the refresh process by setting the event flag."""       
        self.event.set()       
     
    def refreshThread(self):
        common.threadLocal.operation = 'refresh'
        common.threadLocal.path = None
        metrics.counts.incr('refresh_thread_started')
        self.event = threading.Event()

        lastRefreshTime = 0
        while True:
            try:
                metrics.counts.incr('refresh_wait')                
                self.event.wait()              
                metrics.counts.incr('refresh_start')
                metrics.counts.startExecution('refresh')

                eventq.queue.executeEvents() # Execute any pending events first
                 
                # Update cached data at least every common.updateinterval seconds
                elapsed = time.time() - lastRefreshTime
                if elapsed > common.updateinterval:  
                    metrics.counts.incr('refresh', int(elapsed))
                                            
                    lastRefreshTime =  time.time() + common.updateinterval
                
                    if gddelete.manager.activeThreadCount > 0 or gddelete.manager.queue.qsize() > 0 or gdcreate.manager.activeThreadCount > 0 or gdcreate.manager.queue.qsize() > 0:
                        metrics.counts.incr('refresh_delay_for_gddelete')
                        logger.debug('refresh: delaying refresh due to active gddelete operations')
                    else:
                        logger.info('--> refresh: refreshing all files and directories')                
                        self.refreshRunning = True
                        directories.store.populate()              
                        refreshcache.refreshAll() # Refresh all files                
                        metrics.counts.incr('refresh_complete')
                        logger.info('<-- refresh: refresh complete')
            except Exception as e:
                if isinstance(e, HttpError):
                    logger.error(f"refresh HttpError in refreshThread: {e}")
                    metrics.counts.incr('refresh_httperror') 
                elif isinstance(e, TimeoutError):
                    logger.error(f"refresh TimeoutError in refreshThread: {e}")
                    metrics.counts.incr('refresh_timeout_error') 
                elif isinstance(e, FuseOSError):
                    logger.error(f"refresh FuseOSError in refreshThread: {e}")
                    metrics.counts.incr('refresh_fuse_error') 
                else:
                    self.exceptionCount += 1
                    metrics.counts.incr('refresh_exception')
                    raisedBy = log.exceptionRaisedBy(e)
                    logger.exception(f'refreshThread: {raisedBy}')
                    logger.info('<-- refresh: refresh failed')
            finally:
                self.refreshRunning = False
                metrics.counts.endExecution('refresh')
                self.event.clear()

thread: Refresh = Refresh()