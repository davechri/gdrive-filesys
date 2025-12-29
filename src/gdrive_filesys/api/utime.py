from datetime import datetime
from time import time
import errno
from venv import logger

from fuse import FuseOSError

from gdrive_filesys import common, eventq, metrics, attr
from gdrive_filesys.cache import metadata

def execute(path: str, id: str, times) -> int:
    
    if times is not None:
        (atime, mtime) = times
    else:
        now = time()
        (atime, mtime) = (now, now)

    st = metadata.cache.getattr(path, id) 
    if st is None:
        raise FuseOSError(errno.ENOENT)
    
    st.st_atime = int(atime)
    st.st_mtime = int(mtime)
    metadata.cache.getattr_save(path, st.toDict())

    if not st.local_only:
        eventq.queue.enqueueFileEvent(path, id, st.gd_id)
        metrics.counts.incr('utime_enqueue_event') 
    else:
        logger.debug(f'API.utime: path={path} is local only, not updating Google Drive')
        metrics.counts.incr('utime_local_only') 
    
    return 0