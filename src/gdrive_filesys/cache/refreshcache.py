import json
import os.path
import errno
import time
import stat

from fuse import FuseOSError

from googleapiclient.errors import HttpError

from gdrive_filesys import directories, eventq, gddelete, lock
from gdrive_filesys.cache import mem, metadata
from gdrive_filesys import common, gddownload, metrics, attr, gdupload
from gdrive_filesys.cache import data, db
from gdrive_filesys.log import logger

def refreshAll():
    return _refresh(None)

def refreshNewFiles():
    return _refresh(None, newOnly=True)

def refreshDirectory(path: str):
    return _refresh(path)

def _refresh(path: str|None, newOnly: bool=False) -> attr.Stat|None:

    if common.offline:
        metrics.counts.incr('refreshcache_offline')
        raise FuseOSError(errno.ENOENT)    
   
    if path != None:
        operation = 'stat files in parent directory'
    else:
        operation = 'stat all files and directories'

   
    if path == '/':
        directory = directories.store.getDirectoryByPath(path)
        if directory == None:
            raise FuseOSError(errno.ENOENT)         
        else:
            gdId = directory.gdId

        service = common.getApiClient()
        try:
            # pylint: disable=maybe-no-member
            response = (
                service.files()
                    .get(
                        fileId=gdId,
                        fields="id, name, createdTime, modifiedTime, size, capabilities(canEdit), appProperties, trashed, mimeType, shortcutDetails, parents"
                )
                .execute()
            )               
            files = [response]               
        except HttpError as error:   
            if path != None:         
                if error.status_code == 404:                
                    metadata.cache.getattr_save(path, {})                
            raise error        
        
        st = metadata.cache.getattr(path)
        if st != None:
            return st
        
        driveName = files[0].get('name')
     
        metrics.counts.incr('refreshcache_root_is_' + driveName)
        files[0]['name'] = '/'
        files[0]['mimeType'] = 'application/vnd.google-apps.folder'

        type =  'dir'
        localId = common.generateLocalId(path, type, 'refreshcache._cacheMissingFiles', localOnly=False)
        st = attr.Stat.newFromFile(files[0], localId, type)       
        metadata.cache.getattr_save(path, st.toDict())
        
        return st
    
    if eventq.eventCount > 0:
        metrics.counts.incr('refreshcache_delay_dir_refresh_for_pending_events')
        logger.debug('refreshcache._refreshDirEntries: delaying directory refresh due to pending events')
        return 

    if gddelete.manager.activeThreadCount > 0 or gddelete.manager.queue.qsize() > 0:
        metrics.counts.incr('refreshcache_delay_dir_refresh_for_gddelete')
        logger.debug('refreshcache._refreshDirEntries: delaying directory refresh due to active gddelete operations')
        return   

    # If the path is None, get all files
    if path == None:
        query = "trashed = false"    
    else:
        # get all files in the parent directory, and cache all the files
        directory = directories.store.getDirectoryByPath(path)
        if directory == None:
            raise FuseOSError(errno.ENOENT)         
        else:
            gdId = directory.gdId
        
        query = f"'{gdId}' in parents and trashed = false'"

    dirNameToFileByGdId: dict[str, dict[str,any]] = None
    filesMap: dict[str, dict] = {}
    files = []

    page_token = None
    service = common.getApiClient()        
    while True:
        try:   
            metrics.counts.incr('refreshcache_get_files_network')                 
            # pylint: disable=maybe-no-member
            response = (
                service.files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields="nextPageToken, files(id, name, createdTime, modifiedTime, size, capabilities(canEdit), appProperties, trashed, mimeType, shortcutDetails, parents)",
                        pageToken=page_token,
                        pageSize=1000,
                        orderBy='createdTime'
                )
                .execute()
            )                                

                            
            if dirNameToFileByGdId == None:
                dirNameToFileByGdId = {}
            for file in response.get("files", []):
              
                if file.get('trashed'):
                    metrics.counts.incr('refreshcache_skip_trashed')  
                    continue

                if file.get('mimeType') == 'application/vnd.google-apps.folder':
                    if file.get('id') not in dirNameToFileByGdId:
                        dirNameToFileByGdId[file.get('id')] = {}   

                parentIds = file.get('parents', [])
                if len(parentIds) == 0:
                    metrics.counts.incr('refreshcache_skip_no_parents')  
                    continue

                for parentId in parentIds:                            
                    if parentId not in dirNameToFileByGdId:
                        dirNameToFileByGdId[parentId] = {}                            
                    dirNameToFileByGdId[parentId][file.get('name')] = file   

                filesMap[file.get('id')] = file
                files.append(file)
            
            page_token = response.get("nextPageToken", None)
            if page_token is None:
                break
        except HttpError as error:
            if error.status_code == 404:
                raise FuseOSError(errno.ENOENT)
            raise error
    
    _refreshDirEntries(dirNameToFileByGdId)   

    if path == None:
        if newOnly:            
            _refreshFiles(filesMap, dirNameToFileByGdId)
    else:
        st = metadata.cache.getattr(path)
        if st == None:
           raise FuseOSError(errno.ENOENT)
        return st
                
    return None

def _refreshDirEntries(dirNameToFileByGdId: dict[str, dict[str,any]]):
    root = directories.store.getDirectoryByPath('/')
    _refreshDirEntry(root.gdId, dirNameToFileByGdId.get(root.gdId, {}))

    for gdId, nameToFile in dirNameToFileByGdId.items():
        if gdId != root.gdId:
            _refreshDirEntry(gdId, nameToFile)
               
def _refreshDirEntry(gdId: str, nameToFile: dict[str,any]):    
    updateRequired = False
    directory = directories.store.getDirectoryByGdId(gdId)        
    if directory == None:
        metrics.counts.incr('refreshcache_directory_not_found')
        logger.debug(f'refreshcache._refreshDirEntries: directory not found for gdId={gdId}')
        return
            
    dirEntries: dict[str,str] = {}  
    for name, file in nameToFile.items():
        path = os.path.join(directory.path, name)
        st = metadata.cache.getattr(path)
        if st != None:
            metrics.counts.incr('refreshcache_direntry_is_cached')
            dirEntries[name] = st.local_id
        else:
            metrics.counts.incr('refreshcache_direntry_not_cached')
            type = _getFileType(file)
            localId = None   
            if type == 'dir':    
                subDirectory = directories.store.getDirectoryByPath(path) 
                if subDirectory == None:
                    metrics.counts.incr('refreshcache_subdirectory_not_found')
                    logger.debug(f'refreshcache._refreshDirEntries: subdirectory not found for path={path} {name}: {file}')
                    continue                                 
                localId = subDirectory.localId  
            if localId == None: 
                localId = common.generateLocalId(path, type, 'refreshcache._cacheMissingFiles', localOnly=False)
            st = attr.Stat.newFromFile(file, localId, type)                
            metadata.cache.getattr_save(path, st.toDict())

            if st.file_name.find('/') != -1 and st.local_parent_id != None:
                logger.warning('refreshcache._cacheMissingFiles: replacing / with underscore (_) %s', st.toDict())
                st.file_name.replace('/', '_')
            
            if common.mimeTypeCannotBeConverted(st.mime_type):  
                _convertToDesktopFile(path, st) 
                name = st.file_name

            dirEntries[name] = localId
            updateRequired = True

    dirEntries['.'] = directory.localId 
    if directory.localParentId != None:
        dirEntries['..'] = directory.localParentId       
                
    deleteAttrList: list[attr.Stat] = []
    with lock.get(directory.path):
        curDirEntries = metadata.cache.readdir(directory.path)
        if curDirEntries == None:
            updateRequired = True
        else:
            for name in dirEntries:
                if name not in curDirEntries:
                    metrics.counts.incr('refreshcache_readdir_new_entry')
                    updateRequired = True   
                    break
            for name, localId in curDirEntries.items():
                if name not in dirEntries:
                    st = metadata.cache.getattr_by_id(localId)
                    if st != None and (st.local_only or st.st_ctime + common.UPDATE_INTERVAL < time.time()):
                        dirEntries[name] = st.local_id
                        metrics.counts.incr('refreshcache_readdir_localonly_entry')
                    else: 
                        metrics.counts.incr('refreshcache_readdir_remove_entry')  
                        if st != None:
                            deleteAttrList.append(st)                     
                        updateRequired = True                

        if eventq.eventCount > 0:
            metrics.counts.incr('refreshcache_delay_dir_refresh_for_pending_events')
            logger.debug('refreshcache._refreshDirEntries: delaying directory refresh due to pending events')
            return 

        if gddelete.manager.activeThreadCount > 0 or gddelete.manager.queue.qsize() > 0:
            metrics.counts.incr('refreshcache_delay_dir_refresh_for_gddelete')
            logger.debug('refreshcache._refreshDirEntries: delaying directory refresh due to active gddelete operations')
            return                        
                    
        if updateRequired:
            metrics.counts.incr('refreshcache_readdir_save')
            metadata.cache.readdir_save(directory.path, dirEntries)  

    for st in deleteAttrList:
        path = os.path.join(directory.path, st.file_name)
        data.cache.deleteAll(path, st.local_id)
            
def _convertToDesktopFile(path: str, st: attr.Stat):
    if not st.file_name.endswith('.desktop'):
        c = metadata.cache.getattr(path)    
        if c != None and c.file_name.endswith('.desktop'):
            # .desktop entry already exists
            return

        desktopEntry = f"""[Desktop Entry]
Type=Link
Name={st.file_name}
URL=https://docs.google.com/document/d/{st.gd_id}/edit?usp=drivesdk
"""
        st.st_size = len(desktopEntry)
        metadata.cache.deleteMetadata(path, st.local_id, 'refreshcache._convertToDesktopFile: replace with .desktop entry')
        path += '.desktop'
        st.file_name += '.desktop'      
        metadata.cache.getattr_save(path, st.toDict())
        parentPath = os.path.dirname(path)
        metadata.cache.readdir_add_entry(parentPath, st.file_name, st.local_id)        
        data.cache.putData(path, st.local_id, 0, bytes(desktopEntry, 'utf-8'))

def _refreshFiles(filesMap: dict[str, dict], dirNameToFileByGdId: dict[str, list[str]]):

    downloadQueueSize = gddownload.manager.downloadQueue.qsize()
    metrics.counts.incr('refreshcache_refreshfiles_download_qsize', downloadQueueSize)   
    it = db.cache.getIterator()
    for key, value in it(prefix=bytes(mem.GETATTR, encoding='utf-8'), fill_cache=False):
        if eventq.eventCount > 0:
            metrics.counts.incr('refreshcache_delay_file_refresh_for_pending_events')
            logger.debug('refreshcache._refreshFiles: delaying file refresh due to pending events')
            return
        
        if gddelete.manager.activeThreadCount > 0 or gddelete.manager.queue.qsize() > 0:
            metrics.counts.incr('refreshcache_delay_dir_refresh_for_gddelete')
            logger.debug('refreshcache._refreshDirEntries: delaying directory refresh due to active gddelete operations')
            return      
        
        d = json.loads(value)
        stCache = attr.Stat.newFromDict(d)
        mimeType = stCache.mime_type
        if common.mimeTypeCannotBeConverted(mimeType):
            metrics.counts.incr('refreshcache_refreshfiles_skip_cannot_convert')
            continue
        # this file or directory was deleted?           
        path = stCache.getPath()
        if path == None:
            metrics.counts.incr('refreshcache_refreshfiles_skip_no_path')
            continue
        if path == '/':
            metrics.counts.incr('refreshcache_refreshfiles_skip_root')
            continue
        if stCache.local_only:
            metrics.counts.incr('refreshcache_refreshfiles_skip_local_only')
            continue
        if stCache.gd_id == None:
            metrics.counts.incr('refreshcache_refreshfiles_skip_no_gdid')
            continue
        if stCache.st_ctime + common.UPDATE_INTERVAL < time.time():
            metrics.counts.incr('refreshcache_refreshfiles_skip_just_created')
            continue
        if stCache.gd_id not in filesMap:
            metrics.counts.incr('refreshcache_refreshFiles_gd_file_deleted')
            data.cache.deleteAll(path, stCache.local_id) 
            continue           

        file = filesMap[stCache.gd_id]       
        fileSt = attr.Stat.newFromFile(file, stCache.local_id, _getFileType(file))
        if stCache.st_mtime < fileSt.st_mtime:             
            if (stCache.st_mode & stat.S_IFREG) and stCache.st_ctime != 0:
                metrics.counts.incr('refreshcache_refreshfiles_delete_stale')
                logger.info(f'refreshcache._refreshFiles: deleting stale data for {path} {key.decode()} ctime={stCache.st_ctime} mtime={stCache.st_mtime} new_mtime={fileSt.st_mtime}')
                data.cache.deleteByID(path, stCache.local_id)

            filePath = fileSt.getPath()
            if filePath != path:
                metrics.counts.incr('refreshcache_refreshfiles_rename')                
                metadata.cache.renameMetadata(path, filePath, stCache.local_id)            
             
            metadata.cache.getattr_save(filePath, fileSt.toDict())             
            stCache = fileSt

        if (stCache.st_mode & stat.S_IFREG == stat.S_IFREG):            
            if downloadQueueSize == 0:                
                _downloadFile(path, stCache)
            else:
                metrics.counts.incr('refreshcache_download_skip_queue_busy')

def _downloadFile(path: str, stCache: attr.Stat):
    if not common.downloadall:
        metrics.counts.incr('refreshcache_download_skip_not_downloadall')
        return
    if stCache.st_size == 0:
        metrics.counts.incr('refreshcache_download_skip_empty_file')
        return
    if gdupload.manager.isFlushPending(path, stCache.local_id):
        metrics.counts.incr('refreshcache_download_skip_is_flush_pending')
        return
    count = data.cache.getUnreadBlockCount(path, stCache.local_id, stCache)
    if count == 0:
        metrics.counts.incr('refreshcache_download_skip_no_unread_blocks')
        return
    
    metrics.counts.incr('refreshcache_download_enqueue')

    gddownload.manager.enqueueDownloadQueue(path, stCache)

def _getFileType(file: dict) -> str:
    appProperties = file.get('appProperties')
    type = 'dir' if file.get('mimeType') == 'application/vnd.google-apps.folder' else 'file'
    type = 'desktop' if common.mimeTypeCannotBeConverted(file.get('mimeType')) else type
    type = 'symlink' if file.get('mimeType') == 'application/vnd.google-apps.shortcut' or appProperties != None and appProperties.get('targetPath') != None else type     
    return type

