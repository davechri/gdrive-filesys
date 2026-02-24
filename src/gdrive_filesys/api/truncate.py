
import errno
import math
import os
import tempfile

from gdrive_filesys import common, eventq, metrics, attr
from gdrive_filesys.cache import data, metadata
from gdrive_filesys.log import logger

from googleapiclient.http import MediaFileUpload

from fuse import FuseOSError

from gdrive_filesys.cache import db

def execute(path: str, localId: str, size: int, st: attr.Stat, runAsync: bool=True) -> int: 
    logger.debug(f'API.truncate: path={path} localId={localId} size={size} runAsync={runAsync}')
    if not runAsync and common.offline:
        raise Exception(f'API.truncate: cannot truncate while offline {path}')

    st = metadata.cache.getattr(path, localId)
    if st == None:
        raise FuseOSError(errno.ENOENT)    
     
    if not runAsync:        
        metrics.counts.incr('truncate_network')
        
        filePath = None
        for timeout in common.apiTimeoutRange():
            try:
                if size == 0:
                    filePath = '/dev/null'
                    st.st_size = size
                    metadata.cache.getattr_save(path, st.toDict())
                    data.cache.deleteByID(path, localId)
                else:
                    tempFile = tempfile.NamedTemporaryFile(delete=False, prefix="gdrive_filesys", suffix=".tmp")
                    tempFile.close()
                    filePath = tempFile.name
                    with open(filePath, 'wb') as f:
                        fsize = 0
                        for block in data.cache.truncate(localId, size):
                            f.write(block)
                            fsize += len(block)

                        if fsize < size:
                            f.write(bytearray(size-fsize))
                            fsize = size

                    st.st_size = size
                    metadata.cache.getattr_save(path, st.toDict())
                    
                media = MediaFileUpload(filePath, mimetype=st.mime_type)
                fileMetadata = {
                    'name': os.path.basename(path)
                }
                service = common.getApiClient(timeout)    
                file = service.files().update(fileId=st.gd_id, body=fileMetadata, media_body=media, fields='id,name,size,createdTime,modifiedTime').execute()

                st.st_mtime = attr.Stat.unixTime(file.get('modifiedTime'))    
                st.st_ctime = attr.Stat.unixTime(file.get('createdTime'))
                metadata.cache.getattr_save(path, st.toDict())
                if size > 0:
                    os.remove(filePath)
                break
            except TimeoutError as e:
                logger.error(f'truncate timeout {e}')
                metrics.counts.incr('truncate_network_timeout')
                if common.isLastAttempt(timeout):
                    raise 
    elif st.local_only:
        logger.debug(f'API.truncate: path={path} is local only, not truncating on Google Drive')
        metrics.counts.incr('truncate_local_only')
    else:
        metrics.counts.incr('truncate_enqueue_event')
        eventq.queue.enqueueTruncateEvent(path, localId, st.gd_id)
    
    st.st_size = size
    metadata.cache.getattr_save(path, st.toDict())
    if size == 0:
        data.cache.deleteByID(path, localId)

    return size
    