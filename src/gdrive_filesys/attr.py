import os
from pathlib import Path
import time
import stat
import math
import fuse

from gdrive_filesys import common, directories
from gdrive_filesys import mimetype
from gdrive_filesys.log import logger
from dateutil import parser as dp

DEFAULT_DIR_MODE                 = 0o755
DEFAULT_FILE_MODE_WRITE          = 0o644
DEFAULT_FILE_MODE_NON_READ_ONLY  = 0o444

ST_SIZE_DIR = 4096

class Stat:
    """
    Simple class to hold stat attributes.
    """
    def __init__(self, size: int, mode: int, ctime: float, mtime: float, fileName: str, mimeType: str, gdId: str|None, localId: str, localParentId: str|None, targetId: str|None=None, localOnly: bool=False):          
        st = os.stat(Path.home()) 
        uid = st.st_uid
        gid = st.st_gid    

        self.file_name = fileName
        self.gd_id = gdId 
        self.local_id = localId       
        self.local_parent_id = localParentId
        self.target_id = targetId
        self.mime_type = mimeType

        self.st_size = size

        self.st_blocks = math.ceil(size / 512)
        self.st_blksize = 512

        self.st_mode = mode
        self.gd_mode = mode if gdId != None else None
        self.st_ctime = ctime
        self.st_mtime = mtime        

        self.st_uid = uid   
        self.st_gid = gid       
        self.st_atime = math.floor(time.time())
        
        self.local_only = localOnly

    def getPath(self) -> str|None:
        if self.file_name == '/':            
            return '/'
        else:
            parentDir = directories.store.getDirectoryByLocalId(self.local_parent_id)
            if parentDir == None:                
                logger.warning('Stat.getPath: parent directory not found for %s', self.toDict())
                return None
            dir = parentDir.path
            if dir != '/':
                dir += '/'
            return dir + self.file_name
        
    def isDir(self) -> bool:
        return stat.S_ISDIR(self.st_mode)
        
    @staticmethod
    def unixTime(ts: str|int|float) -> float:
        if ts == None:
            logger.error('Stat.unixTime: time is None')
            ts = time.time()
        if isinstance(ts, (int, float)):
            return int(ts) 
        return math.floor(dp.parse(ts).timestamp())

    @staticmethod
    def newFromFile(file: any, localId: str, type: str) -> 'Stat':
        mode = Stat._getMode(file)
        size = file.get('size', 0) if file.get('mimeType') != 'application/vnd.google-apps.folder' else ST_SIZE_DIR
        size = int(size)
        ctime = Stat.unixTime(file.get('createdTime'))
        mtime = Stat.unixTime(file.get('modifiedTime'))        
        gdId = file.get('id')        
        parentId = file.get('parents')[0] if file.get('parents') != None else None
        mimeType = file.get('mimeType')
        appProperties = file.get('appProperties')
        shortcut = file.get('shortcutDetails', None)
        targetId = None
        if shortcut != None:           
            targetId = shortcut.get('targetId', None)
        elif mode & stat.S_IFLNK == stat.S_IFLNK:
            if appProperties != None and appProperties.get('targetPath') != None:
                targetPath = appProperties.get('targetPath')
                targetId = targetPath
        name = file.get('name') 

        if parentId is not None and parentId.startswith('local'):
            parentDirectory = directories.store.getDirectoryByLocalId(parentId) 
        else:  
            parentDirectory = directories.store.getDirectoryByGdId(parentId) 
        localParentId = parentDirectory.localId if parentDirectory != None else None
        if localParentId == None and parentId != None:
            raise Exception(f'Stat.newFromFile: Parent directory not found for parentId={parentId} name={name}')
        if mimeType == 'application/vnd.google-apps.folder':
            directory = directories.store.getDirectoryByGdId(gdId)
            localId = directory.localId
        else:
            path = parentDirectory.path + '/' + name if parentDirectory.path != '/' else '/' + name
            localId = common.generateLocalId(path, type, 'attr.newFromFile', localOnly=False) if localId == None else localId    
        st = Stat(size, mode, ctime, mtime, name, mimeType, gdId, localId, localParentId, targetId)

        return st   
    
    @staticmethod
    def _getMode(file) -> int:
        appProperties = file.get('appProperties')
        mimeType = file.get('mimeType')
        if appProperties != None and 'mode' in appProperties:
            mode = int(appProperties.get('mode'))            
        else:
            if file.get('mimeType') == 'application/vnd.google-apps.folder':
                mode = DEFAULT_DIR_MODE
            else:
                capabilities = file.get('capabilities')
                if not common.mimeTypeCannotBeConverted(mimeType) and capabilities.get('canEdit', False) == True:
                    mode = DEFAULT_FILE_MODE_WRITE
                else:
                    mode = DEFAULT_FILE_MODE_NON_READ_ONLY 

                if mimeType in mimetype.executable_types:
                    mode |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

        if mimeType == 'application/vnd.google-apps.folder':                       
            mode = (stat.S_IFDIR | mode)                
        elif file.get('mimeType') == common.SHORTCUT_MIME_TYPE:
            mode = (stat.S_IFLNK | mode)
        else:
            mode = (stat.S_IFREG | mode)
        
        return mode      

    @staticmethod
    def newFromDict(dict: dict[str, any]) -> 'Stat':
        return Stat(
            size=dict.get('st_size', 0),
            mode=dict.get('st_mode', 0),
            ctime=dict.get('st_ctime', 0),
            mtime=dict.get('st_mtime', 0),
            fileName=dict.get('file_name', ''),
            mimeType=dict.get('mime_type', ''),
            gdId=dict.get('gd_id'),
            localId=dict.get('local_id'),
            localParentId=dict.get('local_parent_id'),
            targetId=dict.get('target_id', None),
            localOnly=dict.get('local_only', False)
        )

    def toDict(self) -> dict[str, any]:
        return dict((key, getattr(self, key)) for key in (
            'st_atime', 'st_gid', 'st_mode', 'gd_mode', 'st_ctime', 'st_mtime', 'st_size', 'st_blocks', 'st_blksize', 'st_uid', 'gd_id', 'local_id', 'local_parent_id', 'file_name', 'mime_type', 'target_id', 'local_only'))
    