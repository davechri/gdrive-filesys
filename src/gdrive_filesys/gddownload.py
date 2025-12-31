
import math
from pathlib import Path
import os
import queue
import threading

from gdrive_filesys import metrics, attr, common
from gdrive_filesys import log
from gdrive_filesys.api import api
from googleapiclient.errors import HttpError

from gdrive_filesys.cache import data, metadata
from gdrive_filesys.log import logger

READ_FRONT = False
READ_BACK = True

class Download:
    """
    Handles local caching of file data for gdrive-filesys.    
    """
     
    def __init__(self):               
        self.downloadQueue = queue.Queue() 
        self.activeThreadCount = 0
        self.exceptionCount = 0
        self.errorsByLocalId: dict[str, str] = dict()

    def statvfs(self, path: str):
        """
        Retrieves filesystem statistics for the given path.
        Args:
            path (str): The path for which to retrieve filesystem statistics.
        Returns:
            os.statvfs_result: An object containing filesystem statistics for the specified path.        
        Notes:            
            - Returns None if the path does not exist.
        """       
        st = os.statvfs('/')        
        return st    

    def read(self, path, size, offset, readEntireFile: bool, queueEnd: bool=READ_FRONT) -> bytes:
        """
        Reads data from a cached file, fetching missing blocks from a remote source if necessary.
        Args:
            path (str): The file path to read from.
            size (int): The number of bytes to read.
            offset (int): The offset in the file to start reading from.
            fh (int): File handle (unused).
        Returns:
            bytes: The data read from the file.
        Raises:
            Exception: If an error occurs during reading or fetching blocks.
        Notes:
            - If the file or its directory does not exist, it is created and truncated to the expected size.
            - The method uses a block map to track which blocks are cached locally.
            - Missing blocks are fetched from Google Drive and written to the local cache.
            - The method may queue the file for further reading if not all blocks are cached.
        """
        st = metadata.cache.getattr(path)
        if st == None:
            st = api.interface.lstat(path)
            localId = st.local_id
            mimeType = st.mime_type
        else:
            localId = st.local_id
            mimeType = st.mime_type

        try:
            buf = data.cache.read(path, localId, offset, size, st)                   
        except Exception as e:            
            self.errorsByLocalId[localId] = str(e)
            raise e

        # If the entire file is being read and all blocks are not cached, queue it for background reading
        if readEntireFile:
            count = data.cache.getUnreadBlockCount(path, st.local_id, st)
            if count > 0:
                if queueEnd == READ_FRONT or count > 1:
                    self.downloadQueue.put((path, st.local_id, queueEnd))            
                    metrics.counts.incr('gddownload_read_enqueue_downloadqueue')                
            else:
                metrics.counts.incr('gddownload_read_all_blocks_cached')       
       
        return bytes(buf)
    
    def start(self):
        """
        Starts the file reader thread by setting the 'stopped' flag to False and launching a new thread
        that runs the 'downloadThread' method.
        """         
        self.stopped = False
        for i in range(common.NUMBER_OF_FILE_READER_THREADS):
            threading.Thread(target=self.downloadThread, args=(i+1,), daemon=True).start()

    def stop(self):
        """
        Stops the data processing by setting the stopped flag to True and logging the action.
        This method is typically called to gracefully halt ongoing operations.
        """
        logger.info('gddownload.stop')
        self.stopped = True

    def enqueueDownloadQueue(self, path: str, st: attr.Stat):
        """
        Adds a file path to the file reader queue for processing.
        This method places the specified file path into the queue, allowing
        the file reader thread to pick it up and read unread blocks from the file.
        Args:
            path (str): The file path to be added to the queue.
        """
        count = data.cache.getUnreadBlockCount(path, st.local_id, st)
        if count > 0:
            self.downloadQueue.put((path, st.local_id, READ_BACK))
        if count > 1:
            self.downloadQueue.put((path, st.local_id, READ_FRONT))

    def downloadThread(self, number: int):
        """
        Continuously processes file paths from the downloadQueue, reading unread blocks from each file.
        For each file path retrieved from the queue:
        - Retrieves the block map indicating which blocks have been read.
        - Searches for the first unread block (where blockMap[i] == 0).
        - If an unread block is found, reads the block using the `read` method and increments the 'downloadThread' metric.
        - If all blocks have been read, logs that all blocks are read for the file.
        The thread runs until the `self.stopped` flag is set.
        """
        common.threadLocal.operation = 'gddownload_%d' % number
        common.threadLocal.path = None
        while not self.stopped:            
            (path, localId, queueEnd) = self.downloadQueue.get()
           
            common.threadLocal.path = (path,)
            metrics.counts.incr('gddownload_dequeued')
            metrics.counts.startExecution('gddownload_%d' % number)
            try:
                self.activeThreadCount += 1
                logger.info('--> downloadThread %s local_id=%s queueEnd=%s', path, localId, 'front' if queueEnd==READ_FRONT else 'back')
                
                st = metadata.cache.getattr_by_id(localId)
                if st == None:
                    metrics.counts.incr('gddownload_file_deleted')
                    logger.warning('downloadThread: file was deleted %s local_id=%s', path, localId)
                    continue
                
                localId = st.local_id
                        
                offset = data.cache.findNextUncachedBlockOffset(path, localId, st, reverse=queueEnd)
                if offset is None:
                    metrics.counts.incr('gddownload_all_blocks_cached')
                    logger.info('<-- downloadThread %s local_id=%s all blocks cached', path, localId)
                    continue

                size = common.BLOCK_SIZE                
                self.read(path, size, offset, readEntireFile=True, queueEnd=queueEnd)
                
                metrics.counts.incr('gddownload_block_read'+('_front' if queueEnd==READ_FRONT else '_back'))
                metrics.counts.incr('gddownload_bytes_read'+('_front' if queueEnd==READ_FRONT else '_back'), size)

                logger.info('<-- downloadThread %s local_id=%s size=%s offset=%s', path, localId, size, offset)
                
            except Exception as e:
                if isinstance(e, HttpError):
                    metrics.counts.incr('gddownload_httperror')
                    logger.error(f"<-- downloadThread: HttpError reading file {path} local_id={localId}: {e}")
                elif isinstance(e, TimeoutError):
                    metrics.counts.incr('gddownload_timeouterror')
                    logger.error(f'<-- downloadThread: TimeoutError reading file {path} local_id={localId}: {e}')
                else:
                    self.exceptionCount += 1
                    metrics.counts.incr('gddownload_exception')
                    raisedBy = log.exceptionRaisedBy(e)
                    logger.exception(f"<-- downloadThread: exception reading file {path} local_id={localId}: {raisedBy}")
            finally:
                self.activeThreadCount -= 1
                metrics.counts.endExecution('gddownload_%d' % number)
                
manager: Download = Download()