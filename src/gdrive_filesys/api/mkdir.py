
import errno

import os
import stat
import time
from gdrive_filesys import common, eventq, gitignore, metrics, attr

from gdrive_filesys import directories
from gdrive_filesys.log import logger

from fuse import FuseOSError

from gdrive_filesys.cache import metadata
from gdrive_filesys.localonly import localonly

def execute(path: str, mode: int, runAsync: bool=True) -> str:
    logger.debug(f'API.mkdir: path={path} mode={oct(mode)} runAsync={runAsync}')

    if not runAsync and common.offline:
        raise Exception(f'API.mkdir: cannot mkdir while offline {path}')

    if path == '/':
        raise FuseOSError(errno.ENOENT)
    
    # Check if directory already exists and not called by eventq.queue.enqueueDirEvent
    if directories.store.getDirectoryByPath(path) != None and runAsync == True:       
        raise FuseOSError(errno.EEXIST)
    
    parentDirectory = directories.store.getParentDirectory(path) 
    if parentDirectory == None:
        raise FuseOSError(errno.ENOENT)

    if localonly.isInLocalOnlyConfig(path):
        logger.debug(f'API.mkdir: localOnly {path}')
        if not parentDirectory.localOnly:
            localonly.createDirSymLink(path)
            return None
        
    localOnly = False
    localId = metadata.cache.getattr_get_local_id(path)
    if localId == None:
        localOnly = gitignore.parser.isIgnored(path)
        localId = common.generateLocalId(path, 'dir', 'API.mkdir', localOnly=localOnly)
        
    name = os.path.basename(path)  
    mode2 = stat.S_IFDIR | mode
    
    gdId = None       
    if not runAsync and not localOnly:        
        for timeout in common.apiTimeoutRange():
            try:  
                metrics.counts.incr('mkdir_network')
                if parentDirectory.gdId == None:                   
                    raise Exception('API.mkdir: parentDirectory.gdId is None for path=%s', path)
                
                fileMetadata = {
                    'name': name,
                    'parents': [parentDirectory.gdId],
                    'mimeType': 'application/vnd.google-apps.folder',        
                    'appProperties': {
                        'mode': mode2
                    }
                }
                logger.debug('API.mkdir: fileMetadata=%s', fileMetadata)
                service = common.getApiClient(timeout)
                file = service.files().create(body=fileMetadata, fields='id,createdTime,modifiedTime').execute()
                gdId = file.get('id')
                break
            except TimeoutError as e:
                logger.error(f'mkdir timeout: {e}')
                metrics.counts.incr('mkdir_network_timeout')
                if common.isLastAttempt(timeout):
                    raise
    else:
        file = {
            'id': None,
            'createdTime': 0,
            'modifiedTime': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()),
        }
        
        if localOnly:            
            metrics.counts.incr('mkdir_localonly')
        else:            
            metrics.counts.incr('mkdir_enqueue_event')        
            eventq.queue.enqueueDirEvent(path, localId, gdId=None)
        
   
    st = attr.Stat(attr.ST_SIZE_DIR, 
                    mode2,
                    attr.Stat.unixTime(file.get('createdTime')),
                    attr.Stat.unixTime(file.get('modifiedTime')),
                    name, 
                    'application/vnd.google-apps.folder',
                    gdId,
                    localId,
                    parentDirectory.localId,
                    targetId=None,
                    localOnly=localOnly
                    )
    d = st.toDict()
    metadata.cache.getattr_save(path, d)

    parentPath = parentDirectory.path
    metadata.cache.readdir_add_entry(parentPath, st.file_name, localId)
        
    directories.store.addDirectory(path, st.gd_id, st.local_id, parentDirectory.gdId, st.local_parent_id, localOnly)

    metadata.cache.readdir_add_entry(path, '.', st.local_id)
    metadata.cache.readdir_add_entry(path, '..', st.local_parent_id)
    
    return gdId
