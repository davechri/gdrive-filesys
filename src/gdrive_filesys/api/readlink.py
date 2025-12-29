
import os
from gdrive_filesys import common, directories, metrics, attr
from gdrive_filesys.cache import metadata, db, mem
from gdrive_filesys.log import logger
from gdrive_filesys import log
from gdrive_filesys import api

from fuse import FuseOSError
import errno

def execute(path: str) -> str:
    logger.debug(f'API.readlink: path={path}')
           
    st = metadata.cache.getattr(path)
    if st == None:
        st = api.interface.lstat(path)        
        targetId = st.target_id
    else:
        targetId = st.target_id
        
    baseDir = os.path.dirname(path)

    targetSt = None
    targetPath = None
    if targetId.startswith('/'):        
        targetSt = metadata.cache.getattr(targetId)
        if targetSt == None: 
            # Target path is in different file system?     
            if os.path.exists(targetId):
                targetPath = targetId
                return targetPath
            else:
                logger.warning(f'API.readlink: {path} targetId={targetId} target file not found')
                metrics.counts.incr('readlink_target_file_not_found_by_path')
                return os.path.relpath(targetId, baseDir)
    else:
        for key, value in mem.cache.map.items():
            if key.startswith(mem.GETATTR):
                if isinstance(value, dict):
                    if value.get('gd_id') == targetId:
                        targetSt = attr.Stat.newFromDict(value)
                        break  
        if targetSt == None:
            logger.error(f'API.readlink: {path} targetId={targetId} target file not found by gd_id')
            metrics.counts.incr('readlink_target_file_not_found_by_gd_id')
            return None    

    if targetSt != None:
        if targetSt.local_parent_id == None:
            targetPath = targetSt.file_name
        else:
            parentDir = directories.store.getDirectoryByLocalId(targetSt.local_parent_id)
            if parentDir == None:
                logger.error(f'API.readlink: {path} targetId={targetId} parent directory not found')
                metrics.counts.incr('readlink_target_parent_dir_not_found')
                return None
            targetPath = parentDir.path + '/' if parentDir.path != '/' else '/'
            targetPath += targetSt.file_name
        if st.isDir():
            baseDir = path 

    logger.debug(f'API.readlink: {path} baseDir={baseDir} targetPath={targetPath}')   
    return os.path.relpath(targetPath, baseDir)      
    