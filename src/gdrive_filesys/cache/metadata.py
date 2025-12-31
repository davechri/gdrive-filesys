
import copy
import os
import stat

from gdrive_filesys import attr, gitignore, lock
from gdrive_filesys.cache import db, mem
from gdrive_filesys.log import logger

import json

from gdrive_filesys import metrics
from gdrive_filesys import directories

from fuse import FuseOSError

NEGATIVE = 'negative'

class Metadata:
    '''
    Metadata cache for getattr, readdir and read link operations.
    '''

    def __init__(self):        
        pass
       
    def key(self, localId: str, operation: str):
        return '{}-{}'.format(operation, localId)   

    def changeToLocalOnly(self, path: str, localId: str, st: attr.Stat):        
        if st.local_only:
            logger.debug(f'metadata.changeToLocalOnly: path={path} localId={localId} already local only')
            return
        st.local_only = True
        logger.info(f'metadata.changeToLocalOnly: path={path} localId={localId} marked as local only')
        self.getattr_save(path, st.toDict())
        if st.st_mode & stat.S_IFDIR:
            directory = directories.store.getDirectoryByLocalId(localId)  
            directories.store.deleteDirectoryByLocalId(path, localId)          
            directories.store.addDirectory(directory.path, 
                                        directory.gdId,
                                            directory.localId, 
                                            directory.gdParentId, 
                                            directory.localParentId,
                                            localOnly=True)
           
    def renameMetadata(self, oldPath: str, newPath: str, localId: str):

        with lock.get(oldPath):
            dirEntries = self.readdir(os.path.dirname(oldPath))
            if dirEntries != None:            
                oldName = os.path.basename(oldPath)
                if oldName in dirEntries:
                    del dirEntries[oldName]
            self.readdir_save(os.path.dirname(oldPath), dirEntries)

            newDirEntries = self.readdir(os.path.dirname(newPath))
            if newDirEntries != None:   
                newDirEntries[os.path.basename(newPath)] = localId
                self.readdir_save(os.path.dirname(newPath), newDirEntries)

        if directories.store.getDirectoryByPath(oldPath) != None:
            directories.store.renameDirectory(oldPath, newPath)

        for op in [mem.GETATTR, mem.READDIR, mem.READLINK]:
            key = self.key(localId, op)
            
            if mem.cache.exists(key):
                logger.debug(f'metadata.renameMetadata: renaming mem cache {key}')
                value = mem.cache.get(key, op)
                if op == mem.GETATTR:
                    value['file_name'] = os.path.basename(newPath)              
                mem.cache.put(key, value, op)                               
                metrics.counts.incr(op+f'_mem_rename')
            
            value = db.cache.get(key, op)
            if value is not None:
                logger.debug(f'metadata.renameMetadata: renaming db cache {key}')
                if op == mem.GETATTR:
                    d = json.loads(value)
                    d['file_name'] = os.path.basename(newPath)
                    value = json.dumps(d)                    
                    value = bytes(value, 'utf-8')
                db.cache.put(key, value, op)
                metrics.counts.incr(op+f'_db_rename')

    def deleteMetadata(self, path, localId, reason: str):        
        logger.info('metadata.deleteMetadata: %s local_id=%s reason=%s', path, localId, reason)
        for op in [mem.GETATTR, mem.READDIR, mem.READLINK]:
            key = self.key(localId, op)
            
            value = db.cache.get(key, op)
            if value is not None:
                metrics.counts.incr(op+f'_db_delete')
                db.cache.delete(key, op) 
            if mem.cache.exists(key):
                mem.cache.delete(key, op)
                metrics.counts.incr(op+f'_mem_delete')            
            
        parentPath = os.path.dirname(path)
        with lock.get(parentPath):            
            dirEntries = self.readdir(parentPath)
            if dirEntries != None:
                name = os.path.basename(path)
                if name in dirEntries:
                    del dirEntries[name]
                    self.readdir_save(parentPath, dirEntries)

        directories.store.deleteDirectoryByLocalId(path, localId)
        
    # 'st_atime', 'st_gid', 'st_mode', 'st_mtime', 'st_size', 'st_uid'    
    def getattr(self, path, localId:str=None, returnDict: bool=False)-> dict | attr.Stat| None:
        """
        Retrieves the cached metadata attributes for the specified path.        
        Args:
            path (str): The file or directory path for which to retrieve metadata: 
        Returns:
            dict: The metadata attributes for the specified path.
        """
        if path == '/':
            if localId == None:
                root = directories.store.getDirectoryByPath('/')
                if root == None:
                    logger.debug(f'metadata.getattr: root directory not found!')
                    return None 
                localId = root.localId
        else:
            name = os.path.basename(path)
            dirEntries = self.readdir(os.path.dirname(path)) 
            if dirEntries == None:
                logger.debug('metadata.getattr: %s parent directory %s not found in readdir cache', path, os.path.dirname(path))
                return None  
            elif name not in dirEntries:
                logger.debug('metadata.getattr: %s %s not found in readdir cache parent directory %s', path, name, os.path.dirname(path))
                return None  
            else:                
                direntryLocalId = dirEntries.get(name)
                if localId != None and localId != direntryLocalId:
                    logger.warning('metadata.getattr: %s %s localId mismatch: %s != %s', path, name, localId, direntryLocalId)
                    return None
                localId = direntryLocalId
                   
        d = self._readCache(path, localId, mem.GETATTR)
        if returnDict:
            return d
        return attr.Stat.newFromDict(d) if d != None else None  

    def getattr_by_id(self, id: str) -> attr.Stat| None:
        """
        Retrieves the cached metadata attributes for the specified file ID.
        Args:
            id (str): The file ID for which to retrieve metadata:
        Returns:
            dict | attr.Stat | None: The metadata attributes if found, otherwise None.
        """
        d = self._readCache(None, id, mem.GETATTR)
        if d == {}:
            return None        
        return attr.Stat.newFromDict(d) if d != None else None

    def getattr_save(self, path, d: dict):
        """
        Saves the given metadata attributes for the specified path in the cache.
        This method updates the cache with the provided attributes, regardless of whether
        it is empty or not, as long as it is not None.
        Args:
            path (str): The file or directory path for which metadata is being saved.
            dic (dict): The metadata attributes to save. If empty, it is still saved.
        """       
        if d == None:
            raise ValueError('getattr_save: missing required parameter "attr"')
        
        localId = d.get('local_id') 
        if localId == None:
            metrics.counts.incr('getattr_save_no_id')
            logger.error('metadata.getattr_save: missing local_id in attr for path=%s attr=%s', path, d)
            return
            
        self._updateCache(path, localId, mem.GETATTR, d) 

        if d.get('file_name') == '.gitignore':
            gitignore.parser.addRepo(os.path.dirname(path), attr.Stat.newFromDict(d))       
        
    def getattr_increase_size(self, path: str, newSize: int) -> None:
        """
        Increases the size attribute of the specified path by the given size increase.
        Args:
            path (str): The file or directory path whose size attribute is to be increased.
            newSize (int): The new size to set for the size attribute.
        Returns:
            None
        """       
        st = self.getattr(path)
        if st == None:
            metrics.counts.incr('getattr_increase_size_noattr')
            return
        
        if newSize <= st.st_size:
            metrics.counts.incr('getattr_increase_size_noop')
            return
        logger.debug('metadata.getattr_increase_size: %s increased size from %d to %d', path, st.st_size, newSize)
        metrics.counts.incr('getattr_increase_size')
        st.st_size = newSize
        self.getattr_save(path, st.toDict())

    def getattr_get_local_id(self, path: str) -> str | None:
        """
        Retrieves the file ID for the specified path from the cache.
        Args:
            path (str): The file or directory path for which to retrieve the ID.
        Returns:
            str | None: The file ID if available, otherwise None.
        """
        st = self.getattr(path)
        if st == None:
            return None
        return st.local_id

    def readdir(self, path)-> dict[str,str]|None: # key=name, value=local_id
        """
        Returns a list of directory entries for the given path from the cache.        
        Args:
            path (str): The directory path to read.
        Returns:
            dict[str,str]: A dictionary of directory entry names and their IDs.
        """
        directory = directories.store.getDirectoryByPath(path)
        if directory == None:
            metrics.counts.incr('readdir_no_directory')
            return None
        return self._readCache(path, directory.localId, mem.READDIR)       

    def readdir_save(self, path, dirEntries: dict[str,str]): # key=name, value=id
        """
        Saves the directory listing for the specified path to the cache.
        Args:
            path (str): The path of the directory whose contents are being saved.
            s (dict[str,str]): A dictionary of directory entries (filenames or subdirectory names).
        Returns:
            None
        """  
        localIdMap = dict[str, str]()
        for name, localId in dirEntries.items():
            if localId == None:
                metrics.counts.incr('readdir_save_no_local_id')
                raise ValueError(f'readdir_save: missing localId for entry {name} in directory {path}')
            if localId in localIdMap:
                metrics.counts.incr('readdir_save_duplicate_local_id')
                logger.error(f'readdir_save: duplicate localId {localId} for entries {name} and {localIdMap[localId]} in directory {path}')   
                raise ValueError(f'readdir_save: duplicate localId {localId} for entries {name} and {localIdMap[localId]} in directory {path}')
            localIdMap[localId] = name
        directory = directories.store.getDirectoryByPath(path)   
        if directory == None:
            metrics.counts.incr('readdir_save_no_directory')
            raise ValueError(f'readdir_save: directory not found for path {path}')     
        self._updateCache(path, directory.localId, mem.READDIR, dirEntries)

    def readdir_add_entry(self, path: str, name: str, localId: str):
        """
        Adds a new directory entry to the cache for the specified path.
        Args:
            path (str): The path of the directory to which the entry is being added.
            name (str): The name of the new directory entry.
            localId (str): The ID of the new directory entry.
        Returns:
            None
        """
        with lock.get(path):
            dirEntries = self.readdir(path)
            if dirEntries == None:
                dirEntries = dict[str, str]()
            dirEntries[name] = localId
            self.readdir_save(path, dirEntries)

    def readlink(self, path:str) -> str | None:
        """
        Returns the target of a symbolic link for the given path from the cache.       
        Args:
            path (str): The path to the symbolic link.
        Returns:
            str | None: The target of the symbolic link if available, otherwise None.
        """
        name = os.path.basename(path)
        dirEntries = self.readdir(os.path.dirname(path)) 
        if dirEntries == None or not name in dirEntries:
            None
        
        localId = dirEntries.get(name)
        return self._readCache(path, localId, mem.READLINK)

    def readlink_save(self, path:str, link: str):  
        """
        Saves the symbolic link information for the specified path in the cache.
        Args:
            path (str): The file system path for which the symbolic link information is to be saved.
            link (str, optional): The target of the symbolic link. Defaults to None.
        Returns:
            None
        """
        name = os.path.basename(path)
        dirEntries = self.readdir(os.path.dirname(path)) 
        if dirEntries == None or not name in dirEntries:
            None
        
        localId = dirEntries.get(name)
        self._updateCache(path, localId, mem.READLINK, link)

    def _updateCache(self, path, localId: str|None, operation, d: dict | str | bytearray):
        key = self.key(localId, operation)        
        logger.info(f'metadata._updateCache.{operation}: {path} {key} {len(d) if len(d) > 50 else d}')

        if d == None:
            raise ValueError('Cannot cache None value')

        key = self.key(localId, operation)

        s = json.dumps(d)
        db.cache.put(key, bytes(s, encoding='utf-8'), operation)

        mem.cache.put(key, d, operation)
        metrics.counts.incr(operation+f'_updatecache')

    def _readCache(self, path, localId: str|None, operation) -> dict | str | bytearray:

        key = self.key(localId, operation)

        buf = mem.cache.get(key, operation)
        if buf == NEGATIVE:
            metrics.counts.incr(operation+'_negative')
            return None
        if buf != None:            
            if operation == mem.GETATTR:
                c = copy.deepcopy(buf)
                if 'st_mode' in c:
                    c['st_mode'] = oct(c['st_mode'])
            return buf

        metrics.counts.incr(f'{operation}_mem_miss')

        buf = db.cache.get(key, operation)

        if buf != None:
            d = json.loads(buf)

            mem.cache.put(key, d, operation)            
            if operation == mem.GETATTR:
                c = copy.deepcopy(d)
                if 'st_mode' in c:
                    c['st_mode'] = oct(c['st_mode'])
            else:
                c = buf
            
            return 
        else:            
            mem.cache.put(key, NEGATIVE, operation)
            metrics.counts.incr(operation+'_negative_cache')

        logger.info('metadata._readCache.%s: %s %s: cache miss', operation, path, key)
        return None
    
cache: Metadata = Metadata()