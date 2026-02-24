import errno
import os
from fuse import FuseOSError

from gdrive_filesys import attr, directories, metrics
from gdrive_filesys.api import chmod, create, readdir, remove, rename, rmdir, truncate, writefile, utime, symlink, mkdir, getxattr, chown, readlink
from gdrive_filesys.cache import refreshcache, metadata

GOOGLE_DRIVE_URL = 'https://www.googleapis.com/drive/v3'
WINDOW_SIZE = 1073741824 
   
class API:
    """
    API interfaces with Google Drive when there is connectivity to Google Drive.    
    """
    def __init__(self):
        pass
        
    def create(self, path: str, mode: int) -> None:             
        id = create.execute(path, mode)
        
    def close(self) -> None:
        pass
    def getxattr(self, path: str) -> dict[str, any]:        
        return getxattr.execute(path)
    def link(self, source: bytes | str, dest: bytes | str) -> None|str:
        raise FuseOSError(errno.EOPNOTSUPP)       
    def listxattr(self, path: str) -> list[str]:        
        return list(getxattr.execute(path).keys())
    def readdir(self, path: str = ".") -> list[str]:        
        return readdir.execute(path)
    def remove(self, path: bytes | str) -> None:
        remove.execute(path)
    unlink = remove
    def rename(self, oldpath: bytes | str, newpath: bytes | str) -> None:        
        rename.execute(oldpath, newpath)
    def mkdir(self, path: bytes | str, mode: int = 511) -> None:        
        mkdir.execute(path, mode)
        
    def rmdir(self, path: bytes | str) -> None:
        rmdir.execute(path)
    def lstat(self, path: bytes | str) -> attr.Stat:
        if directories.store.getDirectoryByPath(path) != None:
            return refreshcache.refreshDirectory(str(path))
        else:
            parentDirectory = directories.store.getParentDirectory(path)
            if parentDirectory == None:
                return None
            refreshcache.refreshDirectory(parentDirectory.path)
            return metadata.cache.getattr(path)
       
    def symlink(self, source: bytes | str, dest: bytes | str) -> None|str: 
        if not dest.startswith('/'):
            dest = os.path.normpath(os.path.join(os.path.dirname(source), dest))
            
        return symlink.execute(source, sourceLocalId=None, target=dest)
    def chmod(self, path: bytes | str, mode: int) -> None:
        st = metadata.cache.getattr(path)
        if st == None:
            raise FuseOSError(errno.ENOENT)
        return chmod.execute(path, st.local_id, mode, st)
    def chown(self, path: bytes | str, uid: int, gid: int) -> None:
        return chown.execute(path, uid, gid)
    def utime(self, path: bytes | str, times: tuple[float, float] | None) -> None:
        id = metadata.cache.getattr_get_local_id(path)
        return utime.execute(path, id, times)
    def truncate(self, path: bytes | str, size: int) -> None:
        st = metadata.cache.getattr(path)
        truncate.execute(path, st.local_id, size, st)
    def readlink(self, path: bytes | str) -> str | None:  
        return readlink.execute(path) 
   
interface = API()