import errno
import os
import copy
import threading
import tomllib
import traceback

from gdrive_filesys import common, directories, gddownload, eventq, gdcreate, metrics, gddelete, oauth, refresh, heartbeat, rpc, error
from gdrive_filesys.api import api

from fuse import FUSE, FuseOSError, Operations

from gdrive_filesys import gdupload
from gdrive_filesys.cache import mem, metadata, refreshcache
from gdrive_filesys.cache import db
from gdrive_filesys.log import logger
from gdrive_filesys import log
from gdrive_filesys.localonly import localonly


from googleapiclient.errors import HttpError

class filesystemStats:
    def __init__(self):
        self.init = 0
        self.destroy = 0
        self.create = 0
        self.listxattr = 0
        self.getattr = 0
        self.readdir = 0
        self.read = 0
        self.write = 0
        self.flush = 0
        self.unlink = 0
        self.mkdir = 0
        self.rmdir = 0
        self.rename = 0
        self.symlink = 0
        self.truncate = 0
        self.chmod = 0
        self.chown = 0
        self.utimens = 0
        self.readlink = 0
        self.statFs = 0
        self.exceptions = 0
        
    def capture(self):
        stats = copy.deepcopy(self.__dict__)
        for key, value in stats.items():           
            if key in previousStats.__dict__:
                stats[key] = value - previousStats.__dict__[key]
            else:
                stats[key] = value
        previousStats.__dict__ = copy.deepcopy(self.__dict__)
        return stats

previousStats = filesystemStats()
stats = filesystemStats()

class gdrive_filesys(Operations):
    """
    FileSystem implements the FUSE filesystem operations for gdrive-filesys.
    This class provides methods corresponding to standard filesystem operations,
    such as reading, writing, creating, deleting files and directories, as well as
    managing metadata and caching. It interacts with the underlying API manager,
    metadata cache, data cache, and other subsystems to provide a seamless
    filesystem interface backed by Google Drive.
    Attributes:
        debug (bool): Debug mode flag.
        log (Logger): Logger instance for logging operations.        
        heartbeat (Keepalive): Manages heartbeat signals for the connection.
    Methods:
        init(path): Initializes the filesystem and subsystems.
        chmod(path, mode): Changes the permissions of a file or directory.
        chown(path, uid, gid): Changes the owner and group of a file or directory.
        create(path, mode): Creates a new file with the specified mode.
        destroy(path): Cleans up resources and stops subsystems.
        getattr(path, fh=None): Retrieves file or directory attributes.
        statfs(path): Returns filesystem statistics.
        mkdir(path, mode): Creates a new directory.
        read(path, size, offset, fh): Reads data from a file.
        readdir(path, fh): Lists the contents of a directory.
        readlink(path): Reads the target of a symbolic link.
        rename(old, new): Renames a file or directory.
        rmdir(path): Removes a directory.
        symlink(target, source): Creates a symbolic link.
        truncate(path, length, fh=None): Truncates a file to a specified length.
        unlink(path): Removes a file.
        utimens(path, times=None): Updates the access and modification times of a file.
        write(path, buf, offset, fh): Writes data to a file.
    """                            
    def __init__(self, args):        
        common.threadLocal.operation = 'filesys_init'
        common.threadLocal.path = None
       
        db.cache = db.Db(clearcache=args.clearcache)
        if args.clearcache:
            localonly.deleteAll()

        gddownload.manager = gddownload.Download()

        if not oauth.creds.init():            
            exit(1)

        api.interface # verify connection to host

        try:
            fuse = FUSE(
                self,
                args.mountpoint,
                foreground=args.debug,
                nothreads=False,
                allow_other=True,
                big_writes=True,
                max_read=common.BLOCK_SIZE, # Set max read size (e.g., 128KB)
                max_write=common.BLOCK_SIZE, # Set max write size (e.g., 128KB)
            )
        except Exception as e:
            print(f'Ensure that no terminal is referencing the mountpoint directory {args.mountpoint},')
            print('and that the mountpoint directory exists and is empty.')
            print('To unmount filesystem: gdrive-filesys unmount {}'.format(args.mountpoint))

    def _handleException(self, name: str, logStr: str, e: Exception):       
        raisedBy = log.exceptionRaisedBy(e)
        
        if isinstance(e,  FuseOSError):            
            if e.errno == errno.ENOENT:
                logger.info(f'{logStr} {raisedBy}')
            else:
                logger.warning(f'{logStr} {raisedBy}')
            metrics.counts.incr(f'{name}_{os.strerror(e.errno)}')
            raise e
        elif isinstance(e,  HttpError):
            #logger.error(f'{log} {raised} {e}')
            logger.exception(f'{logStr} {raisedBy}')
            metrics.counts.incr(f'{name}_httperror_'+str(e.status_code)) 
            raise FuseOSError(error.httpToErrno(e.status_code))
        elif isinstance(e, Exception):
            stats.exceptions += 1
            logger.exception(f'{logStr} {raisedBy}')
            metrics.counts.incr(f'{name}_except')
            raise FuseOSError(errno.EINVAL)
     
    def init(self, path):
        stats.init += 1
        common.threadLocal.operation = 'init'
        common.threadLocal.path = (path,)
        try:
            metrics.counts.startExecution('init')
            logger.info('--> %s', path)
            directories.store.populateFromDb()
            mem.cache.initMemCache()
            metrics.counts.incr('init')
            metrics.counts.start()
            log.Log().setupConfig(common.debug, common.verbose)

            heartbeat.monitor.start()
            refresh.thread.start()
            gddownload.manager.start() 
            gdupload.manager.init()
            gdupload.manager.start()
            gdcreate.manager.start()
            gddelete.manager.start()
            eventq.queue.init()
            rpc.server.start()           

            if not common.offline:
                directories.store.populate()
                refreshcache.refreshDirectory('/') # refresh root directory

                def refreshAllThread(path=None, newOnly=True):
                    refreshcache.refreshAll(path=None, newOnly=True) # Refresh new files      

                threading.Thread(target=refreshAllThread, daemon=True).start()  
                
            logger.info('<-- %s', path)                 
        except Exception as e:
            self._handleException('init', f'<-- {path}', e)            
        finally:
            metrics.counts.endExecution('init')
         
    def chmod(self, path, mode):
        stats.chmod += 1
        common.threadLocal.operation = 'chmod'
        common.threadLocal.path = (path,)
        try: 
            metrics.counts.startExecution('chmod')
            logger.info('--> %s %s', path, oct(mode))   
            metrics.counts.incr('chmod')
            api.interface.chmod(path, mode)
            logger.info('<-- %s %s', path, oct(mode))
        except Exception as e:
            self._handleException('chmod', f'<-- {path} {oct(mode)}', e)
        finally:
            metrics.counts.endExecution('chmod')

    def chown(self, path, uid, gid):
        stats.chown += 1
        common.threadLocal.operation = 'chown'
        common.threadLocal.path = (path,)
        try:
            metrics.counts.startExecution('chown')
            logger.info('--> %s %s %s', path, uid, gid) 
            metrics.counts.incr('chown')
            api.interface.chown(path, uid, gid)  
            logger.info('<-- %s', path)
        except Exception as e:
            self._handleException('chown', f'<-- {path} {uid} {gid}', e)
        finally:
            metrics.counts.endExecution('chown')
        
    def create(self, path, mode):  
        stats.create += 1
        common.threadLocal.operation = 'create'
        common.threadLocal.path = (path,)
        try:
            metrics.counts.startExecution('create')
            logger.info('--> %s %s', path, oct(mode))  
            metrics.counts.incr('create')
            api.interface.create(path, mode)                    
            logger.info('<-- %s %s', path, oct(mode))             
            return 0
        except Exception as e:
            self._handleException('create', f'<-- {path} {oct(mode)}', e)            
        finally:
            metrics.counts.endExecution('create') 

    def getxattr(self, path, name, position=0) -> str:
        stats.getattr += 1
        common.threadLocal.operation = 'getxattr'
        common.threadLocal.path = (path,)
        try:
            metrics.counts.startExecution('getxattr')
            logger.info('--> %s %s', path, name)
            metrics.counts.incr('getxattr')
            if common.xattrEnabled: 
                attrs = api.interface.getxattr(path)
            else:
                attrs = {}
            logger.info('<-- %s %s %s', path, name, attrs)
            return attrs[name].encode('utf-8') if name in attrs else b''
        except Exception as e:
            self._handleException('getxattr', f'<-- {path}', e)
        finally:
            metrics.counts.endExecution('getxattr')

    def flush(self, path, fh):
        stats.flush += 1
        common.threadLocal.operation = 'flush'
        common.threadLocal.path = (path,)
        try:
            metrics.counts.startExecution('flush')
            logger.info('--> %s', path)
            metrics.counts.incr('flush')
            rc = gdupload.manager.flush(path)
            logger.info('<-- %s rc=%d', path, rc)
            return rc
        except Exception as e:
            self._handleException('flush', f'<-- {path}', e)
        finally:
            metrics.counts.endExecution('flush')     

    def destroy(self, path): 
        stats.destroy += 1
        common.threadLocal.operation = 'destroy' 
        common.threadLocal.path = (path,)
        try:
            metrics.counts.startExecution('destroy')
            logger.info('--> %s', path)  
            metrics.counts.incr('destroy')     
            api.interface.close()        
            logger.info('<-- %s', path)
        except Exception as e:
            self._handleException('destroy', f'<-- {path}', e)
        finally:
            metrics.counts.stop()
            heartbeat.monitor.stop()
            gddownload.manager.stop()
            gdupload.manager.stop()
            gdcreate.manager.stop()
            gddelete.manager.stop()
            rpc.server.stop()
            metrics.counts.endExecution('destroy')
           
    def getattr(self, path, fh=None):
        stats.getattr += 1
        common.threadLocal.operation = 'getattr'
        common.threadLocal.path = (path,)
        try:
            metrics.counts.startExecution('getattr')
            logger.info('--> %s', path)
            metrics.counts.incr('getattr')
            
            d = metadata.cache.getattr(path, localId=None,returnDict=True)
            if d != None: 
                c = copy.deepcopy(d)
                c['st_mode'] = oct(c['st_mode'])              
                logger.info('<-- %s %s', path, c)
                return d # cache hit
            if path != '/':
                raise FuseOSError(errno.ENOENT)                                                   
           
            st = api.interface.lstat(path) 
            if st == None:
                raise FuseOSError(errno.ENOENT)
            d = st.toDict()
            c = copy.deepcopy(d)
            c['st_mode'] = oct(c['st_mode'])
            logger.info('<-- %s %s', path, c)
            return d           
            
        except Exception as e:
            self._handleException('getattr', f'<-- {path}', e)
        finally:
            metrics.counts.endExecution('getattr')     
        
    def statfs(self, path):
        stats.statFs += 1
        common.threadLocal.operation = 'statfs' 
        common.threadLocal.path = (path,)
        try:
            metrics.counts.startExecution('statfs')
            logger.info('--> %s', path) 
            metrics.counts.incr('statfs')      
            stv = gddownload.manager.statvfs(path)        
            dic = dict((key, getattr(stv, key)) for key in ('f_bavail', 'f_bfree',
                'f_blocks', 'f_bsize', 'f_favail', 'f_ffree', 'f_files', 'f_flag',
                'f_frsize', 'f_namemax'))
            dic['f_bsize'] = common.BLOCK_SIZE
            dic['f_frsize'] = common.BLOCK_SIZE
            logger.info('<-- %s %s', path, dic)  
            return dic       
        except Exception as e:
            self._handleException('statfs', f'<-- {path}', e)
        finally:
            metrics.counts.endExecution('statfs')

    def listxattr(self, path) -> list[str]:
        stats.listxattr += 1
        common.threadLocal.operation = 'listxattr'
        common.threadLocal.path = (path,)
        try:
            metrics.counts.startExecution('listxattr')
            logger.info('--> %s', path)
            metrics.counts.incr('listxattr')
            if common.xattrEnabled: 
                attrs = api.interface.listxattr(path)
            else:               
                attrs = []
            logger.info('<-- %s %s', path, attrs)
            return attrs
        except Exception as e:
            self._handleException('listxattr', f'<-- {path}', e)
        finally:
            metrics.counts.endExecution('listxattr')

    def mkdir(self, path, mode):
        stats.mkdir += 1
        common.threadLocal.operation = 'mkdir'
        common.threadLocal.path = (path,)
        try: 
            metrics.counts.startExecution('mkdir')
            logger.info('--> %s %s', path, oct(mode)) 
            metrics.counts.incr('mkdir')
            api.interface.mkdir(path, mode)
            logger.info('<-- %s %s', path, oct(mode))        
        except Exception as e:
            self._handleException('mkdir', f'<-- {path} {oct(mode)}', e)
        finally:
            metrics.counts.endExecution('mkdir')

    def read(self, path, size, offset, fh): 
        stats.read += 1 
        common.threadLocal.operation = 'read'
        common.threadLocal.path = (path,)
        try:
            metrics.counts.startExecution('read')
            logger.info('--> %s size=%d offset=%d', path, size, offset)
            metrics.counts.incr('read')

            buf = gddownload.manager.read(path, size, offset, readEntireFile=False)

            logger.info('<-- %s size=%d', path, len(buf))
            return buf
        except Exception as e:
            self._handleException('read', f'<-- {path} {size} {offset}', e)           
        finally:
            metrics.counts.endExecution('read')
        
    def readdir(self, path, fh):
        stats.readdir += 1
        common.threadLocal.operation = 'readdir'
        common.threadLocal.path = (path,)
        try:
            metrics.counts.startExecution('readdir')
            logger.info('--> %s', path)
            metrics.counts.incr('readdir')
            dirEntries = metadata.cache.readdir(path)
            if dirEntries != None:
                logger.info('<-- %s %s', path, dirEntries)
                return list(dirEntries.keys())
            raise FuseOSError(errno.ENOENT)           
        except Exception as e:
            self._handleException('readdir', f'<-- {path}', e)            
        finally:
            metrics.counts.endExecution('readdir')

    def readlink(self, path):
        stats.readlink += 1
        common.threadLocal.operation = 'readlink'
        common.threadLocal.path = (path,)
        try:
            metrics.counts.startExecution('readlink')
            logger.info('--> %s', path)
            metrics.counts.incr('readlink')
            link = metadata.cache.readlink(path)
            if link == None:        
                link = api.interface.readlink(path)
                if link != None:
                    metadata.cache.readlink_save(path, link)
            if link == None:
                raise FuseOSError(errno.ENOENT)
            logger.info('<-- %s %s', path, link)
            return link        
        except Exception as e:
            self._handleException('readlink', f'<-- {path}', e)
        finally:
            metrics.counts.endExecution('readlink')

    def rename(self, old, new):
        stats.rename += 1
        common.threadLocal.operation = 'rename'
        common.threadLocal.path = (old, new)
        try:
            metrics.counts.startExecution('rename')
            logger.info('--> %s %s', old, new) 
            metrics.counts.incr('rename')
            api.interface.rename(old, new)
            logger.info('<-- %s %s', old, new)        
        except Exception as e:
            self._handleException('rename', f'<-- {old} {new}', e)            
        finally:
            metrics.counts.endExecution('rename')

    def rmdir(self, path): 
        stats.rmdir += 1
        common.threadLocal.operation = 'rmdir' 
        common.threadLocal.path = (path,)
        try:
            metrics.counts.startExecution('rmdir')
            logger.info('--> %s', path)   
            metrics.counts.incr('rmdir')
            api.interface.rmdir(path)
            logger.info('<-- %s', path)
        except Exception as e:
            self._handleException('rmdir', f'<-- {path}', e)
        finally:
            metrics.counts.endExecution('rmdir')

    def symlink(self, source, target): 
        stats.symlink += 1
        common.threadLocal.operation = 'symlink'
        common.threadLocal.path = (source, target)
        try:
            metrics.counts.startExecution('symlink')
            logger.info('--> %s %s', source, target)   
            metrics.counts.incr('symlink')        
            api.interface.symlink(source, target)
            logger.info('<-- %s %s', source, target) 
        except Exception as e:
            self._handleException('symlink'f'<-- {source} {target}', e)
        finally:
            metrics.counts.endExecution('symlink')

    def truncate(self, path, length, fh=None): 
        stats.truncate += 1
        common.threadLocal.operation = 'truncate'
        common.threadLocal.path = (path,)
        try:
            metrics.counts.startExecution('truncate')
            logger.info('--> %s %d', path, length)  
            metrics.counts.incr('truncate') 
            api.interface.truncate(path, length)
            logger.info('<-- %s %d', path, length)
        except Exception as e:
            self._handleException('truncate', f'<-- {path} {length}', e)
        finally:
            metrics.counts.endExecution('truncate')

    def unlink(self, path): 
        stats.unlink += 1 
        common.threadLocal.operation = 'unlink'
        common.threadLocal.path = (path,)
        try: 
            metrics.counts.startExecution('unlink')
            logger.info('--> %s', path)    
            metrics.counts.incr('unlink')
            api.interface.unlink(path)
            logger.info('<-- %s', path)        
        except Exception as e:
            self._handleException('unlink', f'<-- {path}', e)
        finally:
            metrics.counts.endExecution('unlink')

    def utimens(self, path, times=None): 
        stats.utimens += 1
        common.threadLocal.operation = 'utimens'  
        common.threadLocal.path = (path,)
        try:
            metrics.counts.startExecution('utimens')
            logger.info('--> %s', path) 
            metrics.counts.incr('utimens')
            api.interface.utime(path, times)
            logger.info('<-- %s', path)        
        except Exception as e:
            self._handleException('utimens', f'<-- {path}', e)
        finally:
            metrics.counts.endExecution('utimens')

    def write(self, path, buf, offset, fh): 
        stats.write += 1
        common.threadLocal.operation = 'write'
        common.threadLocal.path = (path,)
        try:  
            metrics.counts.startExecution('write')
            logger.info('--> %s size=%d offset=%d', path, len(buf), offset)
            metrics.counts.incr('write')   
            gdupload.manager.write(path, buf, offset) 
            logger.info('<-- %s %d', path, len(buf))
            return len(buf)         
        except Exception as e:
            self._handleException('write', f'<-- {path} {offset}', e)            
        finally:
            metrics.counts.endExecution('write') 