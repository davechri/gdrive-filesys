import errno
import os

from gdrive_filesys import common, metrics, attr
from gdrive_filesys.log import logger
from gdrive_filesys.cache import metadata

from googleapiclient.http import MediaFileUpload

from fuse import FuseOSError

def execute(path: str, localId: str, filePath: str, st: attr.Stat) -> int: 

    if common.offline:
        metrics.counts.incr('write_offline')
        raise FuseOSError(errno.ENETDOWN)
    
    st = metadata.cache.getattr_by_id(localId)
    if st is None:
        raise FuseOSError(errno.ENOENT)

    for timeout in common.apiTimeoutRange():
        try:
            metrics.counts.incr('write_network')
            service = common.getApiClient()
            
            media = MediaFileUpload(filePath, mimetype=st.mime_type, resumable=True)

            fileMetadata = {
                'name': os.path.basename(path)
            }
            file = service.files().update(fileId=st.gd_id, body=fileMetadata, media_body=media, fields='id,name,size,createdTime,modifiedTime').execute()
            break
        except TimeoutError as e:
            logger.error(f'writefile timed out: {e}')
            metrics.counts.incr('write_network_timeout') 
            if common.isLastAttempt(timeout):
                raise   
                    
    st.st_size = int(file.get('size', 0))
    st.st_ctime = attr.Stat.unixTime(file.get('createdTime'))
    st.st_mtime = attr.Stat.unixTime(file.get('modifiedTime'))    
    metadata.cache.getattr_save(path, st.toDict())
    return int(file.get('size', 0))