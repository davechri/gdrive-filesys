import copy
import json
import os
from gdrive_filesys import common, metrics
from gdrive_filesys.cache import metadata, db

from gdrive_filesys.log import logger

DIRECTORY_PREFIX = 'directory'

class Directory:
    def __init__(self, gdId: str, localId: str, path: str, name: str, gdParentId, localParentId, localOnly: bool):        
        self.gdId = gdId
        self.localId = localId
        self.path = path
        self.name = name       
        self.gdParentId = gdParentId
        self.localParentId = localParentId
        self.localOnly = localOnly

class Directories:
    def __init__(self):        
        self.directoriesByPath: dict[str,Directory] = {} # key=path, value=Directory
        self.directoriesByGdId: dict[str,Directory] = {} # key=id, value=Directory
        self.directoriesByLocalId: dict[str,Directory] = {} # key=id, value=Directory

        self.localOnlyDirectoriesByPath: dict[str,Directory] = {} # key=path, value=Directory
        self.localOnlyDirectoriesByLocalId: dict[str,Directory] = {} # key=id, value=Directory

    def key(self, localId: str) -> str:
        return f'{DIRECTORY_PREFIX}:{localId}'

    def size(self) -> int:
        return len(self.directoriesByPath) + len(self.localOnlyDirectoriesByPath)

    def putDb(self, directory: Directory):
        key = self.key(directory.localId)
        value = {
            'gd_id': directory.gdId,
            'local_id': directory.localId,
            'path': directory.path,
            'name': directory.name,
            'gd_parent_id': directory.gdParentId,
            'local_parent_id': directory.localParentId,
            'local_only': directory.localOnly
        }
        db.cache.put(key, bytes(json.dumps(value), encoding='utf-8'), DIRECTORY_PREFIX)

    def getAllDirectories(self) -> list[Directory]:
        return list(self.directoriesByPath.values()) + list(self.localOnlyDirectoriesByPath.values())

    def populateFromDb(self):
        logger.info('directories.populateFromDb')
        directoriesByPath: dict[str,Directory] = {} # key=path, value=Directory
        directoriesByGdId: dict[str, Directory] = {} # key=id
        directoriesByLocalId: dict[str, Directory] = {} # key=id

        doUpdate = False
        it = db.cache.getIterator()
        doUpdate = False
        for key, value in it(prefix=bytes(DIRECTORY_PREFIX, encoding='utf-8')):
            d = json.loads(value) 
            directory = Directory(
                d['gd_id'], 
                d['local_id'], 
                d['path'], 
                d['name'], 
                d['gd_parent_id'], 
                d['local_parent_id'],
                d['local_only']
            )       
            logger.debug(f'directories.populateFromDb: {d}')
            if directory.localOnly:
                self.localOnlyDirectoriesByPath[directory.path] = directory
                self.localOnlyDirectoriesByLocalId[directory.localId] = directory
            else:
                directoriesByPath[directory.path] = directory
                directoriesByGdId[directory.gdId] = directory
                directoriesByLocalId[directory.localId] = directory
                doUpdate = True
        if doUpdate:
            self.update(directoriesByPath, directoriesByGdId, directoriesByLocalId)       

    def populate(self):
        """
        Populates the directory entries by fetching all Google Drive folders using the Drive API.
        Retrieves all folders from the user's Google Drive, constructs their hierarchical paths,
        and builds two dictionaries for fast lookup  by path 
        Raises:
            Any exceptions raised by the Google Drive API client.
        """
        logger.info('directories.populate')
        dirFiles = []
        page_token = None
        service = common.getApiClient()
        while True:
            metrics.counts.incr('directories_network')
            # pylint: disable=maybe-no-member
            response = (
                service.files()
                    .list(
                        q="mimeType='application/vnd.google-apps.folder'",
                        spaces="drive",
                        fields="nextPageToken, files(id, name, parents, modifiedTime, trashed)",
                        pageToken=page_token,
                        pageSize=1000,
                        orderBy='createdTime'
                )
                .execute()
            )
            files = response.get("files", [])            
            dirFiles.extend(files)
            page_token = response.get("nextPageToken", None)
            if page_token is None:
                break

        if len(dirFiles) > 0:
            it = db.cache.getIterator()
            for key, value in it(prefix=bytes(DIRECTORY_PREFIX, encoding='utf-8')):
                d = json.loads(value) 
                if not d.get('local_only', False):                    
                    db.cache.delete(key, DIRECTORY_PREFIX)

        directoriesByPath: dict[str,Directory] = {} # key=path, value=Directory
        directoriesByGdId: dict[str, Directory] = {} # key=id
        directoriesByLocalId: dict[str, Directory] = {} # key=id

        if not dirFiles:
            logger.warning('directories.populate: No directories found from Drive API.')
            return
        # Add root dir path
        parents = dirFiles[0].get('parents')  
        if (dir := self.directoriesByPath.get('/')) is not None:
            localId = dir.localId
        elif (localId := metadata.cache.getattr_get_local_id('/')) is not None:
            pass
        else:
            localId = common.generateLocalId('/', 'dir', 'directories.populate', localOnly=False)  
        directory = Directory(parents[0], localId, '/', '/', None, None, localOnly=False)
        self.putDb(directory)
        directoriesByPath['/'] = directory  
        directoriesByGdId[directory.gdId] = directory      
        directoriesByLocalId[directory.localId] = directory
        logger.debug('directories.populate: %s gd_id=%s local_id=%s', directory.path, directory.gdId, directory.localId)

        for file in dirFiles:           
            if file.get('trashed'):
                metrics.counts.incr('readdir_trashed')                
                continue
            gdId = file.get('id')
            name = file.get('name')
            parents = file.get('parents')  

            if parents == None or len(parents) == 0: 
                logger.warning(f'directories.populate: no parents skipped: {file}')
                continue
            gdParentId = parents[0]
            if not gdParentId in directoriesByGdId:
                logger.warning(f'directories.populate: parent directory not found for gdParentId={gdParentId}, dir={file}')
                continue

            parent = directoriesByGdId[gdParentId]
            if parent.path == '/':
                path = '/' + name
            else:
                path = parent.path + '/' + name

            currentDirectory = self.getDirectoryByGdId(gdId)

            # Make sure to reuse existing local IDs for directories already known:
            if currentDirectory is not None:
                localId = currentDirectory.localId
                # Directory path has changed, update it in the cache:
                if currentDirectory.path != path:
                    metrics.counts.incr('directories_path_changed')
                    logger.info(f'directories.populate: path changed from {currentDirectory.path} to {path}')                    
            elif (localId := metadata.cache.getattr_get_local_id(gdId)) is not None:
                pass
            else:
                localId = common.generateLocalId(path, 'dir', 'directories.populate', localOnly=False) 

            directory = Directory(gdId, localId, path, name, gdParentId, parent.localId, localOnly=False)

            # Directory renamed by another computer?
            if currentDirectory != None and currentDirectory.path != directory.path:
                metadata.cache.renameMetadata(currentDirectory.path, directory.path, currentDirectory.localId) 

            self.putDb(directory)
            directoriesByPath[path] = directory
            directoriesByGdId[gdId] = directory
            directoriesByLocalId[localId] = directory
           
        self.update(directoriesByPath, directoriesByGdId, directoriesByLocalId)

    def update(self, directoriesByPath: dict[str,Directory], directoriesByGdId: dict[str,Directory], directoriesByLocalId: dict[str,Directory]):
        if self.directoriesByPath != directoriesByPath:            
            # atomic update:
            self.directoriesByPath = directoriesByPath
            self.directoriesByGdId = directoriesByGdId
            self.directoriesByLocalId = directoriesByLocalId
            metrics.counts.incr('directories_updated')

    def getDirectoryByPath(self, path: str) -> Directory | None:
        """
        Retrieves a Directory object corresponding to the given path.
        Args:
            path (str): The path of the directory to retrieve.
        Returns:
            Directory: The Directory object if found; otherwise, None.
        """
        if path in self.directoriesByPath:
            return self.directoriesByPath[path]
        elif path in self.localOnlyDirectoriesByPath:
            return self.localOnlyDirectoriesByPath[path]
        else:
            metrics.counts.incr('directories_by_path_miss')
            return None

    def getDirectoryByLocalId(self, localId: str) -> Directory | None:
        if localId in self.directoriesByLocalId:
            return self.directoriesByLocalId[localId]
        elif localId in self.localOnlyDirectoriesByLocalId:
            return self.localOnlyDirectoriesByLocalId[localId]
        else:
            metrics.counts.incr('directories_by_local_id_miss')
            return None
        
    def getDirectoryByGdId(self, gdId: str) -> Directory | None:
        if gdId in self.directoriesByGdId:
            return self.directoriesByGdId[gdId]
        else:
            metrics.counts.incr('directories_by_gd_id_miss')
            return None
        
    def getParentDirectory(self, path: str) -> Directory | None:
        parentDir = self.getDirectoryByPath(os.path.dirname(path))        
        metrics.counts.incr('directories_get_parent_miss') if parentDir == None else None 
        return parentDir
    
    def renameDirectory(self, oldPath: str, newPath:str, recursive: bool=True):
        directory = self.getDirectoryByPath(oldPath)
        
        if directory is not None:
            # Update the directory's path and name
            directory.path = newPath
            directory.name = os.path.basename(newPath)

            # Update the directoriesByPath and directoriesByGdId mappings
            if directory.localOnly:
                self.localOnlyDirectoriesByPath[newPath] = directory
                del self.localOnlyDirectoriesByPath[oldPath]
                self.localOnlyDirectoriesByLocalId[directory.localId] = directory
                dbp = copy.deepcopy(self.localOnlyDirectoriesByPath) 
            else:
                self.directoriesByPath[newPath] = directory
                del self.directoriesByPath[oldPath]
                self.directoriesByGdId[directory.gdId] = directory
                self.directoriesByLocalId[directory.localId] = directory
                dbp = copy.deepcopy(self.directoriesByPath) 

            for path, dir in dbp.items():
                if dir.localParentId == directory.localId and path.startswith(oldPath + '/'):
                    subDirNewPath = newPath + path[len(oldPath):]
                    self.renameDirectory(path, subDirNewPath, recursive=False)

            metrics.counts.incr('directories_rename')
            logger.info('directories.rename: old=%s new=%s gd_id=%s local_id=%s gdParentId=%s local_parent_id=%s', oldPath, newPath, directory.gdId, directory.localId, directory.gdParentId, directory.localParentId)
        else:
            metrics.counts.incr('directories_rename_not_found')
            logger.info('directories.rename: old=%s new=%s', oldPath, newPath)

    def addDirectory(self, path: str, gdId: str|None, localId: str, gdParentId: str, localParentId: str, localOnly: bool):
        name = os.path.basename(path)
        directory = Directory(gdId, localId, path, name, gdParentId, localParentId, localOnly=localOnly)        
        
        if localOnly:
            self.localOnlyDirectoriesByPath[path] = directory
            self.localOnlyDirectoriesByLocalId[localId] = directory
        else:
            self.directoriesByPath[path] = directory  
            self.directoriesByLocalId[localId] = directory

        if gdId is not None:           
            self.directoriesByGdId[gdId] = directory   

        self.putDb(directory)     
        logger.info('directories.add: %s gd_id=%s local_id=%s gdParentId=%s local_parent_id=%s', path, directory.gdId, directory.localId, directory.gdParentId, directory.localParentId)
        metrics.counts.incr('directories_add')

    def deleteDirectoryByLocalId(self, path: str, localId: str):        
        directory = self.getDirectoryByLocalId(localId)

        if directory is not None:
            # Remove the directory fronnm the mappings
            if directory.localOnly:
                del self.localOnlyDirectoriesByPath[directory.path]
                del self.localOnlyDirectoriesByLocalId[directory.localId]
            else:
                del self.directoriesByPath[directory.path]
                del self.directoriesByLocalId[directory.localId]
                if directory.gdId != None:
                    del self.directoriesByGdId[directory.gdId]

            metrics.counts.incr('directories_delete')
            logger.info('directories.delete: %s gd_id=%s local_id=%s gdParentId=%s local_parent_id=%s', directory.path, directory.gdId, directory.localId, directory.gdParentId, directory.localParentId) 
            db.cache.delete(self.key(directory.localId), DIRECTORY_PREFIX)
        else:
            logger.warning('directories.delete: directory not found: %s %s', path, localId)
            metrics.counts.incr('directories_delete_not_found')  

store: Directories = Directories()