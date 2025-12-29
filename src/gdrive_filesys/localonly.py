import os
import shutil

from pathlib import Path
from gdrive_filesys.api import symlink, readlink
from gdrive_filesys import common
from gdrive_filesys.log import logger
from gdrive_filesys.config import config

from pathlib import Path

class LocalOnly:
    def __init__(self):
        self.localonlyDir = os.path.join(Path.home(), '.gdrive-filesys', 'localonly')
        os.makedirs(self.localonlyDir, exist_ok=True) 

    def isInLocalOnlyConfig(self, path: str) -> bool:        
        name = os.path.basename(path)
        localOnly = name in config.getLocalOnlyDirs()
        if localOnly:
            logger.info(f'localonly.isInLocalOnlyConfig: {path}')
        
        return localOnly   

    def createDirSymLink(self, path: str) -> None:                 
        targetPath = os.path.join(self.localonlyDir, path[1:])
        logger.info(f'localonly.createDirSymLink: path={path} targetPath={targetPath}') 
        os.makedirs(targetPath)     
        localId = common.generateLocalId(path, 'symlink', 'localonly symlink', localOnly=True)
        symlink.execute(path, localId, targetPath)
            
    def deleteDirSymLink(self, path: str) -> None:        
        targetPath = readlink.execute(path)
        if targetPath == None or targetPath.startswith(self.localonlyDir) == False:            
            return
        logger.info(f'localonly.deleteDirSymLink: path={path} targetPath={targetPath}')
        shutil.rmtree(targetPath, ignore_errors=True)

    def deleteAll(self) -> None:
        logger.info(f'localonly.deleteAll')
        for entry in os.listdir(self.localonlyDir):
            entryPath = os.path.join(self.localonlyDir, entry)
            shutil.rmtree(entryPath, ignore_errors=True)

localonly = LocalOnly()