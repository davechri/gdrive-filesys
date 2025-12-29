
import errno
import stat
from venv import logger
from gdrive_filesys import common, metrics

from googleapiclient.http import MediaFileUpload

from gdrive_filesys import directories

from fuse import FuseOSError

from gdrive_filesys.cache import metadata
from gdrive_filesys.log import logger

def execute(path:str, uid: int, gid: int) -> None:
    logger.debug(f'API.chown: path={path} uid={uid} gid={gid}')
    st = metadata.cache.getattr(path) 
    if st is not None:   
        if uid != -1:
            st.st_uid = uid
            metrics.counts.incr(f'chown_uid_{uid}')
        if gid != -1:
            st.st_gid = gid
            metrics.counts.incr(f'chown_gid_{gid}')
        
        metadata.cache.getattr_save(path, st.toDict())
    else:
        metrics.counts.incr('chown_enoent')
        raise FuseOSError(errno.ENOENT)
    
    
        