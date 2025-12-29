import io
import re
import errno

from fuse import FuseOSError

from gdrive_filesys import common,metrics
from gdrive_filesys.log import logger

def execute(path: str, gdId: str, mimeType:str, size: int, offset: int) -> bytes:
    if common.offline:
        metrics.counts.incr('readchunk_offline')
        raise FuseOSError(errno.ENETDOWN)
    
    metrics.counts.incr('read_network')
    service = common.getApiClient()  
    if(re.match('^application/vnd\.google-apps\..+', mimeType)):  
        request = service.files().export_media(fileId=gdId, mimeType=mimeType)
    else:
        request = service.files().get_media(fileId=gdId)    
    request.headers["Range"] = "bytes={}-{}".format(offset, offset+size-1)
    fh = io.BytesIO(request.execute())
    buf = fh.getvalue()
    logger.debug(f'API.readchunk: {path} gdId={gdId} mimeType={mimeType} size={size} offset={offset} -> {len(buf)} bytes')
    return buf     