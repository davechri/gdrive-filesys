
import os
import time
import errno
import stat
from gdrive_filesys import common, directories, eventq, gitignore, metrics, attr

from fuse import FuseOSError

from gdrive_filesys.cache import metadata
from gdrive_filesys.log import logger

def execute(source: str, sourceLocalId: str | None, target: str, runAsync: bool=True) -> str:    
    logger.debug(f'API.symlink: source={source} sourceLocalId={sourceLocalId} target={target} runAsync={runAsync}')
    if not runAsync and common.offline:
        raise Exception(f'API.symlink: cannot symlink while offline {source}')
    
    parentDirectory = directories.store.getParentDirectory(source)
    if parentDirectory == None:
        logger.error(f'API.symlink: parentDirectory is None for source={source}')
        raise FuseOSError(errno.ENOENT)
    
    localOnly = sourceLocalId != None and common.isInLocalOnlyConfigLocalId(sourceLocalId) or gitignore.parser.isIgnored(source)
    localId = sourceLocalId
    if localId is None:    
        localId = metadata.cache.getattr_get_local_id(source)
        if localId == None:
            localId = common.generateLocalId(source, 'symlink', 'API.symlink', localOnly=False)

    name = source.split('/')[-1]
    mimeType = common.SHORTCUT_MIME_TYPE

    mode = stat.S_IFLNK | 0o511   
   
    gdId = None    
    if not runAsync and not localOnly:
        targetGdId = None
        shortCutDetails = None
        targetSt = metadata.cache.getattr(target)        
        if targetSt == None:
            if target.startswith('/'):
                logger.warning(f'API.symlink: target={target} exists in another filesystem, creating symlink without target gd_id') 
                mimeType = 'text/plain'
            else:
                logger.warning(f'API.symlink: {source} target={target} does not exist in Google Drive filesystem')
                return None        
        elif targetSt.gd_id == None:            
            metrics.counts.incr('symlink_enqueue_event_no_gdid')  
            logger.warning(f'API.symlink: target={target} has no gd_id, enqueueing event instead of creating symlink now') 
            eventq.queue.enqueueSymlinkEvent(source, localId, gdId=None)
            return None
        else:
            targetGdId = targetSt.gd_id
            shortCutDetails = {
                'targetId': targetGdId
            }
            
        for timeout in common.apiTimeoutRange():
            try:
                metrics.counts.incr('symlink_network')
                if parentDirectory.gdId == None:
                    raise Exception(f'API.symlink: parentDirectory.gdId is None for source={source}')
                
                fileMetadata = {
                    'name': name,
                    'parents': [parentDirectory.gdId],
                    'mimeType': mimeType,
                    'chunkSize': common.BLOCK_SIZE,
                    'appProperties': {
                        'mode': mode,
                        'targetPath': target,
                    },
                    'shortcutDetails': shortCutDetails
                }
                logger.debug('API.symlink: fileMetadata=%s', fileMetadata)
                service = common.getApiClient(timeout)
                file = service.files().create(
                    body=fileMetadata, 
                    fields='id, name, createdTime, modifiedTime, size, capabilities(canEdit), appProperties, trashed, mimeType, shortcutDetails, parents'
                ).execute()
                gdId = file.get('id')
                break
            except TimeoutError as e:
                logger.error(f'symlink timed out: {e}')
                metrics.counts.incr('symlink_network_timeout')
                if common.isLastAttempt(timeout):
                    raise                 
    else:            
        file = {
            'id': None,
            'name': name,
            'createdTime': 0,
            'modifiedTime': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()),
            'size': 0,
            'parents': [parentDirectory.localId],
            'mimeType': mimeType,            
            'appProperties': {
                'mode': mode
            },
            'shortcutDetails': {
                'targetId': target
            },
            'capabilities': {'canEdit': True},
            'trashed': False
        }
        if not localOnly:
            metrics.counts.incr('symlink_enqueue_event')   
            eventq.queue.enqueueSymlinkEvent(source, localId, gdId=None)
        else:
            metrics.counts.incr('symlink_localonly')
    
    st = attr.Stat.newFromFile(file, localId, 'symlink')  
    st.local_only = localOnly 
    if st.target_id == None:
        raise Exception(f'API.symlink: target_id is None after creation target={target} file={file}')
    metadata.cache.getattr_save(source, st.toDict())

    parentPath = parentDirectory.path
    metadata.cache.readdir_add_entry(parentPath, name, st.local_id)
   
    return gdId
    