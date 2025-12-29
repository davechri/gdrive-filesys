
import errno
import os
import stat
import time
from gdrive_filesys import common, directories, eventq, gitignore, metrics, attr
from gdrive_filesys.log import logger

from gdrive_filesys import mimetype

from fuse import FuseOSError

from gdrive_filesys.cache import metadata

def execute(path: str, mode: int, localId: str|None = None, runAsync: bool=True) -> None:
    """
    Creates a new file at the specified path with the given mode.
    Args:
        path (str): The path where the new file will be created.
        mode (int): The file mode (permissions) for the new file.
    Returns:
        str: The ID of the newly created file in Google Drive.
    Raises:
        FuseOSError: If an error occurs during file creation.
    """ 
    logger.debug(f'API.create: path={path} mode={oct(mode)} localId={localId} runAsync={runAsync}')

    if not runAsync and common.offline:
        raise Exception(f'API.create: cannot create while offline {path}')

    if path == '/':
        raise FuseOSError(errno.EINVAL)
   
    parentDirectory = directories.store.getParentDirectory(path)    
    if parentDirectory == None:
        raise FuseOSError(errno.ENOENT)

    st = metadata.cache.getattr(path, localId)
    if st != None:
        localId = st.local_id
        if st.gd_id != None:
            metrics.counts.incr('create_truncate')
            if not runAsync:
                service = common.getApiClient()
                service.files().update(fileId=st.gd_id, media_body=bytes(), fields='id,name,size').execute()
            elif not st.local_only:
                metrics.counts.incr('create_truncate_enqueue_event')
                eventq.queue.enqueueTruncateEvent(path, st.local_id, st.gd_id)
            else:
                logger.debug(f'API.create: path={path} is local only, not truncating on Google Drive')
                metrics.counts.incr('create_truncate_local_only')
            st.st_size = 0
            metadata.cache.getattr_save(path, st.toDict())
            return
    
    localOnly = False
    if localId == None:
        if gitignore.parser.isIgnored(path):
            logger.debug(f'API.create: {path} is ignored by .gitignore, creating as local only')
            localOnly = True
        
        localId = common.generateLocalId(path, 'file', 'API.create', localOnly=localOnly)
    
    name = os.path.basename(path)
    (root, ext) = os.path.splitext(name)
    mimeType = mimetype.types_map.get(ext.lower(), 'text/plain')

    mode = stat.S_IFREG | mode
    if mimeType in mimetype.executable_types:
        mode |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH    
   
    gdId = None       
    if not runAsync and not localOnly:
        for timeout in common.apiTimeoutRange():
            try:
                metrics.counts.incr('create_network')  
                if parentDirectory.gdId == None:                    
                    raise Exception('API.create: parentDirectory.gdId is None for path=%s', path)

                fileMetadata = {
                    'name': name,
                    'parents': [parentDirectory.gdId],
                    'mimeType': mimeType,
                    'chunkSize': common.BLOCK_SIZE,
                    'appProperties': {
                        'mode': mode
                    }        
                }
                logger.debug('API.create: fileMetadata=%s', fileMetadata)
                service = common.getApiClient(timeout)
                file = service.files().create(
                    body=fileMetadata, 
                    fields='id, name, createdTime, modifiedTime, size, capabilities(canEdit), appProperties, trashed, mimeType, shortcutDetails, parents'
                ).execute()
                gdId = file.get('id')
                break
            except TimeoutError as e:
                logger.error(f'create timed out: {e}')
                metrics.counts.incr('create_network_timeout')
                if common.isLastAttempt(timeout):
                    raise
    else:
        metrics.counts.incr('create_enqueue_event')        
        file = {
            'id': None,
            'name': name,
            'createdTime': 0,
            'modifiedTime': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()),
            'size': 0,
            'capabilities': {'canEdit': True},
            'appProperties': {'mode': mode},
            'trashed': False,
            'mimeType': mimeType,
            'parents': [parentDirectory.localId]
        }
       
        if localOnly:            
            metrics.counts.incr('create_localonly')
        else:
            eventq.queue.enqueueFileEvent(path, localId, gdId=None)
            logger.debug('API.create enqueue event: file=%s', file)       

    st = attr.Stat.newFromFile(file, localId, 'file') 
    st.local_only = localOnly
    metadata.cache.getattr_save(path, st.toDict())

    parentPath = parentDirectory.path
    metadata.cache.readdir_add_entry(parentPath, file.get('name'), localId)
    
    return gdId
    