import io
import os
import stat
from fuse import FuseOSError
import errno

from gdrive_filesys import attr, common, directories, eventq, metrics
from gdrive_filesys.cache import metadata, data
from gdrive_filesys.log import logger

def execute(oldpath: str, newpath: str, runAsync: bool=True) -> None: 
    logger.debug(f'API.rename: oldpath={oldpath} newpath={newpath} runAsync={runAsync}')

    if not runAsync and common.offline:
        raise Exception(f'API.rename: cannot rename while offline {oldpath} to {newpath}')

    if oldpath == '/' or newpath == '/':
        metrics.counts.incr('rename_root_is_invalid')
        raise FuseOSError(errno.EINVAL)
    
    stOld = metadata.cache.getattr(oldpath)
    if stOld == None:
        raise FuseOSError(errno.ENOENT)  
    
    stNew = metadata.cache.getattr(newpath)
    if stNew is not None:
        if stNew.st_mode &  stat.S_IFDIR:
            if stOld.local_only:
                logger.debug(f'API.rename: newpath={newpath} is local only, not removing existing directory from Google Drive')
                metrics.counts.incr('rename_delete_existing_dir_local_only')
            elif runAsync:
                metrics.counts.incr('rename_delete_existing_dir_enqueue')
                eventq.queue.enqueueDirEvent(newpath, stNew.local_id, stNew.gd_id)
            else:
                metrics.counts.incr('rename_delete_existing_dir')
                service = common.getApiClient()
                request = service.files().delete(fileId=stNew.local_id)
                request.execute()            
        else:
            if stNew.local_only:
                logger.debug(f'API.rename: newpath={newpath} is local only, not removing existing file from Google Drive')
                metrics.counts.incr('rename_delete_existing_file_local_only')
            elif runAsync:
                if stNew.st_mode & stat.S_IFLNK == stat.S_IFLNK:
                    metrics.counts.incr('rename_delete_existing_symlink_enqueue')
                    eventq.queue.enqueueSymlinkEvent(newpath, stNew.local_id, stNew.gd_id)
                else:
                    metrics.counts.incr('rename_delete_existing_file_enqueue')
                    eventq.queue.enqueueFileEvent(newpath, stNew.local_id, stNew.gd_id)
            else:
                metrics.counts.incr('rename_delete_existing_file')
                service = common.getApiClient()
                request = service.files().delete(fileId=stNew.local_id)
                request.execute()

        data.cache.deleteAll(newpath, stNew.local_id)
        
    metadata.cache.renameMetadata(oldpath, newpath, stOld.local_id)

    if not runAsync:             
        metrics.counts.incr('rename_network')            
        gdRename(oldpath, newpath, stOld)        
    elif not stOld.local_only:   
        metrics.counts.incr('rename_enqueue_event')
        eventq.queue.enqueueRenameEvent(oldpath, newpath, stOld.local_id, stOld.gd_id)
    else:
        logger.debug(f'API.rename: oldpath={oldpath} is local only, not renaming on Google Drive')
        metrics.counts.incr('rename_local_only')

def gdRename(oldpath: str, newpath: str, st: attr.Stat) -> None:  
    
    addParents = removeParents = None

    if os.path.dirname(oldpath) != os.path.dirname(newpath):        
        parentDirectory = directories.store.getDirectoryByLocalId(st.local_id) 
        if parentDirectory == None:
            metrics.counts.incr('rename_new_parent_dir_does_not_exist')
            raise FuseOSError(errno.EINVAL)
        addParents = parentDirectory.gdId

        parentDirectory = directories.store.getDirectoryByLocalId(st.local_parent_id) 
        if parentDirectory == None:
            metrics.counts.incr('rename_old_parent_dir_does_not_exist')
            raise FuseOSError(errno.EINVAL)
        removeParents = parentDirectory.gdId

    for timeout in common.apiTimeoutRange():
        try:
            service = common.getApiClient()
            fileMetadata = {        
                'name': os.path.basename(newpath)      
            }
            logger.debug('API.rename: oldpath=%s newpath=%s local_id=%s gd_id=%s %s', oldpath, newpath, st.local_id, st.gd_id, fileMetadata)
            service = common.getApiClient(timeout)
            file =service.files().update(fileId=st.gd_id, 
                                addParents=addParents, removeParents=removeParents, body=fileMetadata, fields='id,name,createdTime,modifiedTime'
                                ).execute()
            break
        except TimeoutError as e:
            logger.error(f'rename timeout: {e}')
            metrics.counts.incr('rename_network_timeout')
            if common.isLastAttempt(timeout):
                raise 
               
    st.st_mtime = attr.Stat.unixTime(file.get('modifiedTime'))    
    st.st_ctime = attr.Stat.unixTime(file.get('createdTime'))
    metadata.cache.getattr_save(newpath, st.toDict())