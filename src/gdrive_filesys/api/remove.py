import os
from fuse import FuseOSError
import errno
import stat

from gdrive_filesys import common, eventq, metrics, gddelete
from gdrive_filesys.cache import data, metadata
from gdrive_filesys.log import logger
from gdrive_filesys.localonly import localonly

def execute(path: str) -> None:
   
    st = metadata.cache.getattr(path)
    if st is None:
        raise FuseOSError(errno.ENOENT)    
    
    if not st.local_only:
        metrics.counts.incr('remove_enqueue_event')
        eventq.queue.enqueueFileEvent(path, st.local_id, st.gd_id)
    else:
        logger.debug(f'API.remove: path={path} is local only, not removing from Google Drive')
        metrics.counts.incr('remove_local_only')
        if st.st_mode & stat.S_IFLNK == stat.S_IFLNK:
            localonly.deleteDirSymLink(path)        
           
    data.cache.deleteAll(path, st.local_id)

def gdDelete(path: str, localId: str, gdId: str) -> None:
    for timeout in common.apiTimeoutRange():
        try:  
            metrics.counts.incr('remove_network')  
            # service = common.getApiClient(timeout)
            # service.files().delete(fileId=gdId).execute()
            gddelete.manager.enqueue(path, localId=localId, gdId=gdId)
            break
        except TimeoutError as e:
            logger.error(f'remove timeout {e}')
            metrics.counts.incr('remove_network_timeout')  
            if common.isLastAttempt(timeout):
                raise  