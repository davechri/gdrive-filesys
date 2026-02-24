
import errno
import stat
from gdrive_filesys import common, eventq, metrics, attr

from googleapiclient.http import MediaFileUpload

from gdrive_filesys.log import logger

from fuse import FuseOSError

from gdrive_filesys.cache import metadata

def execute(path:str, localId: str, mode: int, st: attr.Stat, runAsync: bool=True) -> None:
    logger.debug(f'API.chmod: path={path} localId={localId} mode={oct(mode)} runAsync={runAsync}')
    if not runAsync and common.offline:
        raise Exception(f'API.chmod: cannot chmod while offline {path}')
        
    gdId = st.gd_id
    oldMode = st.st_mode
    st.st_mode = mode | ((stat.S_IFDIR | stat.S_IFLNK | stat.S_IFREG) & st.st_mode)
    if oldMode == st.st_mode and runAsync:
        logger.info(f'API.chmod: path={path} mode is unchanged {oct(mode)}')
        return
    metrics.counts.incr(f'chmod_{oct(oldMode)}_to_{oct(st.st_mode)}')
    metadata.cache.getattr_save(path, st.toDict())
    
    if st.local_only:
        logger.debug(f'API.chmod: path={path} is local only, not updating Google Drive')
        metrics.counts.incr('chmod_local_only')  
    elif runAsync:
        metrics.counts.incr('chmod_enqueue_event')
        eventq.queue.enqueueChmodEvent(path, localId, gdId)    
    else:
        for timeout in common.apiTimeoutRange():
            try:
                metrics.counts.incr('chmod_network')
                if gdId == None:
                    logger.error('API.chmod: gdId is None for path=%s', path)
                    raise FuseOSError(errno.ENOENT)                           
                fileMetadata = { 
                    'appProperties': {
                        'mode': mode
                    }
                }
                service = common.getApiClient(timeout)
                file = service.files().update(fileId=gdId, body=fileMetadata, fields='id,createdTime,modifiedTime').execute()
                st.st_mtime = attr.Stat.unixTime(file.get('modifiedTime'))    
                st.st_ctime = attr.Stat.unixTime(file.get('createdTime'))
                st.gd_mode = mode
                metadata.cache.getattr_save(path, st.toDict())   
                break             
            except TimeoutError as e:
                logger.error(f'chmod timed out: {e}')            
                metrics.counts.incr('chmod_network_timeout')
                if common.isLastAttempt(timeout):
                    raise 
                       
        
        