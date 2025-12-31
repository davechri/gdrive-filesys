import io
from fuse import FuseOSError
import errno
import stat

from gdrive_filesys import common, eventq, metrics, gddelete
from gdrive_filesys.cache import metadata
from gdrive_filesys.log import logger

def execute(path: str) -> None:  
    logger.debug(f'API.rmdir: path={path}')

    st = metadata.cache.getattr(path)
    if st is None:
        raise FuseOSError(errno.ENOENT) 
    
    if st.st_mode & stat.S_IFDIR == 0:
        metrics.counts.incr('rmdir_not_directory')
        raise FuseOSError(errno.ENOTDIR)

    dirEntries =metadata.cache.readdir(path)
    if dirEntries is not None and len(dirEntries) > 2:
        metrics.counts.incr('rmdir_not_empty')
        raise FuseOSError(errno.ENOTEMPTY)
    
    if not st.local_only:
        metrics.counts.incr('rmdir_enqueue_event')
        eventq.queue.enqueueDirEvent(path, st.local_id, st.gd_id)            
    else:
        logger.debug(f'API.rmdir: path={path} is local only, not removing directory from Google Drive')
        metrics.counts.incr('rmdir_local_only')

    metadata.cache.deleteMetadata(path, st.local_id, 'API.rmdir: directory removed')
       
def gdDelete(path: str, localId: str, gdId: str):    
    metrics.counts.incr('rmdir_network')            
    gddelete.manager.enqueue(path, localId=localId, gdId=gdId)
         
        
    