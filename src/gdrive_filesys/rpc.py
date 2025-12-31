

import json
import math
import humanize
import stat
import threading
import time
import subprocess
from xmlrpc.server import SimpleXMLRPCServer
from datetime import datetime

from gdrive_filesys import common, eventq, filesystem, gddownload, gdupload, metrics, attr, directories, refresh
from gdrive_filesys.cache import db, mem
from gdrive_filesys.log import logger
from gdrive_filesys import log
from gdrive_filesys import gddelete, gdcreate
from gdrive_filesys.cache import metadata, data
from gdrive_filesys.localonly import localonly

class RpcServer:
    def __init__(self):
        self.server: SimpleXMLRPCServer | None = None

    def start(self):
        logger.info('Starting RPC server thread')
        threading.Thread(target=self.rpcServerThread, daemon=True).start() 

    def stop(self):
        self.server.shutdown() if self.server != None else None 

    def rpcServerThread(self):
        common.threadLocal.operation = 'rpcserver'
        common.threadLocal.path = None
        metrics.counts.incr('rpcserver_thread_started')
        try:
            logger.info(f'RPC.rpcServerThread starting on port {common.RPC_SERVER_PORT}')
            self.server = SimpleXMLRPCServer(("localhost", common.RPC_SERVER_PORT), allow_none=True)
            self.server.register_function(self.eventqueue, "eventqueue")
            self.server.register_function(self.metadata, 'metadata')
            self.server.register_function(self.directories, 'directories')
            self.server.register_function(self.unread, "unread")
            self.server.register_function(self.status, "status")
            self.server.serve_forever()
        except Exception as e:
            raisedBy = log.exceptionRaisedBy(e)
            logger.exception(f'RPC.rpcServerThread exception: {raisedBy}')
            metrics.counts.incr('rpcserver_thread_exception')

    def status(self) -> str:
        common.threadLocal.path = None
        metrics.counts.incr('rpc_status')             
        output: list[str] = []
        try:            
            class EventQueue:               
                totalEvents: int = 0
                failedCount: int = 0
                retryCount: int = 0
                eventTypes: dict[str,int] = dict()
            eventQueue = EventQueue()

            class Counts:
                dirs: int = 0
                files: int = 0
                links: int = 0
                fileBytes: int = 0
                cacheBytes: int = 0
                localOnly: int = 0           
            
            counts = {'local': Counts(), 'remote': Counts()}

            it = db.cache.getIterator()
            for key, value in it(prefix=bytes(mem.GETATTR, encoding='utf-8')):
                key = str(key, 'utf-8')                
                d = json.loads(value)
                mode = d.get('st_mode', 0)  
                localId = d.get('local_id')
                size = d.get('st_size', 0)
                key = 'local' if d.get('local_only', False) else 'remote'
                if localId.find('file') != -1:
                    counts[key].files += 1
                    counts[key].fileBytes += size 
                    counts[key].cacheBytes += data.cache.getCachedFileSize(None, localId, attr.Stat.newFromDict(d))
                    if mode & stat.S_IFLNK == stat.S_IFLNK:
                        logger.error(f'File cannot be a symlink: localId={localId} path={attr.Stat.newFromDict(d).getPath()} mode={oct(mode)}')
                elif (localId.find('dir') != -1):
                    counts[key].dirs += 1
                elif (localId.find('symlink') != -1):
                    counts[key].links += 1      
                    if mode & stat.S_IFLNK != stat.S_IFLNK:
                        logger.error(f'Symlink must have S_IFLNK set: localId={localId} path={attr.Stat.newFromDict(d).getPath()} mode={oct(mode)}')
                    
            for key, value in it(prefix=bytes(eventq.EVENT_PREFIX, encoding='utf-8')): 
                eventQueue.totalEvents += 1
                keyObj = eventq.queue.parseKey(key)
                eventKey = f'{keyObj.fromOperation}_{keyObj.event}' if keyObj.event not in keyObj.fromOperation else keyObj.fromOperation
                if eventKey not in eventQueue.eventTypes:
                    eventQueue.eventTypes[eventKey] = 0
                eventQueue.eventTypes[eventKey] += 1                
                valueObj = eventq.queue.parseValue(value)
                eventQueue.failedCount += valueObj.failedCount
                eventQueue.retryCount += valueObj.retryCount

            failedCount = ''
            if eventQueue.failedCount > 0:
                failedCount = f' failed={eventQueue.failedCount}'
            retryCount = ''
            if eventQueue.retryCount > 0:
                retryCount = f' retry={eventQueue.retryCount}'
            eventCounts = ''
            for eventType, count in eventQueue.eventTypes.items():
                eventCounts += f'{eventType}={count} '

            now = datetime.now().strftime('%H:%M:%S')
            state = 'OFFLINE' if common.offline else 'ONLINE'
            
            eventQueueStr = ''
            if eventQueue.totalEvents > 0 or eventQueue.failedCount > 0 or eventQueue.retryCount > 0:
                eventQueueStr = f'\n\tEVENT_QUEUE:  total={eventQueue.totalEvents}{failedCount}{retryCount} {eventCounts}'

            gdDownloadStr = ''
            downloadExceptionsStr = ''
            if gddownload.manager.exceptionCount > 0:
                downloadExceptionsStr = f'exceptions={gddownload.manager.exceptionCount}'
            if gddownload.manager.activeThreadCount > 0 or gddownload.manager.downloadQueue.qsize() > 0 or gddownload.manager.exceptionCount > 0:
                gdDownloadStr = f'\n\tGB_DOWNLOAD:  qsize={gddownload.manager.downloadQueue.qsize()} active={gddownload.manager.activeThreadCount} {downloadExceptionsStr}'

            gdUploadStr = ''
            uploadExceptionsStr = ''
            if gdupload.manager.exceptionCount > 0:
                uploadExceptionsStr = f'exceptions={gdupload.manager.exceptionCount}'
            if gdupload.manager.activeThreadCount > 0 or gdupload.manager.uploadQueue.qsize() > 0 or gdupload.manager.exceptionCount > 0:
                gdUploadStr = f'\n\tGB_UPLOAD:    qsize={gdupload.manager.uploadQueue.qsize()} active={gdupload.manager.activeThreadCount} {uploadExceptionsStr}'

            dbcreatestr = ''
            if gdcreate.manager.activeThreadCount > 0 or gdcreate.manager.queue.qsize() > 0 or gdcreate.manager.exceptionCount > 0 or len(gdcreate.manager.pendingCreates) > 0:
                gdcreateExceptionsStr = ''
                if gdcreate.manager.exceptionCount > 0:
                    gdcreateExceptionsStr = f'exceptions={gdcreate.manager.exceptionCount}'
                dbcreatestr = f'\n\tGB_CREATE:    qsize={gdcreate.manager.queue.qsize()} active={gdcreate.manager.activeThreadCount} pending={len(gdcreate.manager.pendingCreates)} {gdcreateExceptionsStr}'

            gddeleteStr = ''
            if gddelete.manager.activeThreadCount > 0 or gddelete.manager.queue.qsize() > 0 or gddelete.manager.exceptionCount > 0:
                gddeleteExceptionsStr = ''
                if gddelete.manager.exceptionCount > 0:
                    gddeleteExceptionsStr = f'exceptions={gddelete.manager.exceptionCount}'
                gddeleteStr = f'\n\tGD_DELETE:    qsize={gddelete.manager.queue.qsize()} active={gddelete.manager.activeThreadCount} {gddeleteExceptionsStr}'

            refreshRunningStr = ''
            refreshExceptionsStr = ''
            if refresh.thread.exceptionCount > 0:
                refreshExceptionsStr = f'exceptions={ refresh.thread.exceptionCount}'
            refreshRunningStr = ''
            if refresh.thread.refreshRunning:
                refreshRunningStr = 'running'
            if refresh.thread.refreshRunning or refresh.thread.exceptionCount > 0:
                refreshRunningStr = f'\n\tREFRESH:      {refreshRunningStr}{refreshExceptionsStr}'

            filesystemStatsStr = ''
            stats = filesystem.stats.capture()
            statFieldsStr = ''
            for key, value in stats.items():
                if value > 0:
                    statFieldsStr += f'{key}={value} '
            if statFieldsStr != '':
                filesystemStatsStr = f'\n\tFILESYS OPS:  {statFieldsStr}'

            localDirsStr = ''
            result = subprocess.run(["du", "-s", localonly.localonlyDir], capture_output=True, text=True)
            size = result.stdout.split('/')[0]
            dir = result.stdout[len(size):].strip()
            if int(size) > 4:
                localDirsStr = f'\n\tLOCAL_ONLY:   {dir}={humanize.naturalsize(int(size)*1024)}'

            localOnlyStr = ''
            if counts['local'].dirs > 0 or counts['local'].files > 0 or counts['local'].links > 0:
                localOnlyStr = f'\n\tGITIGNORE:    dirs={str(counts["local"].dirs):5} files={str(counts["local"].files):8} links={str(counts["local"].links):3}  cached={humanize.naturalsize(counts["local"].cacheBytes):8}  total={humanize.naturalsize(counts["local"].fileBytes)}'

            googleDriveStr = f'\n\tGOOGLE_DRIVE: dirs={str(counts["remote"].dirs):5} files={str(counts["remote"].files):8} links={str(counts["remote"].links):3}  cached={humanize.naturalsize(counts["remote"].cacheBytes):8}  total={humanize.naturalsize(counts["remote"].fileBytes)}'
                                                
            output.append(f'{now} {state}:{filesystemStatsStr}{eventQueueStr}{dbcreatestr}{gddeleteStr}{gdDownloadStr}{gdUploadStr}{refreshRunningStr}{localDirsStr}{localOnlyStr}{googleDriveStr}')

        except Exception as e:
            raisedBy = log.exceptionRaisedBy(e)
            logger.exception(f'RPC.status exception: {raisedBy}')
            metrics.counts.incr('rpc_status_exception')
            raisedBy = log.exceptionRaisedBy(e)
            logger.exception(f'<-- rpc_status: {raisedBy}')            
            output.append(str(e))
            output.append('See error details in ~/gdrive_filesys/error.log')

        return output
    
    def eventqueue(self) -> str:        
        metrics.counts.incr('rpc_eventqueue')       
        output: list[str] = []
        try:
            totalEvents = 0           
            it = db.cache.getIterator()
            for key, value in it(prefix=bytes(eventq.EVENT_PREFIX, encoding='utf-8')):
                totalEvents += 1
                key = str(key, 'utf-8')
                d = json.loads(value)
                output.append(f'{key} {d}')              
            output.append(f'Events={totalEvents}')         
        except Exception as e:
            raisedBy = log.exceptionRaisedBy(e)
            logger.exception(f'RPC.eventqueue exception: {raisedBy}')
            metrics.counts.incr('rpc_eventqueue_exception')
            raisedBy = log.exceptionRaisedBy(e)
            logger.exception(f'<-- rpc_eventqueue: {raisedBy}')            
            output.append(str(e))
            output.append('See error details in ~/gdrive_filesys/error.log')

        return output

    def metadata(self) -> str:       
        metrics.counts.incr('rpc_metadata')       
        output: list[str] = []
        try:
            totalFiles = 0
            totalDirectories = 0
            totalLinks = 0
            totalInvalid = 0
            it = db.cache.getIterator()
            for key, value in it(prefix=bytes(mem.GETATTR, encoding='utf-8')):
                key = str(key, 'utf-8')
                d = json.loads(value)
                st = attr.Stat.newFromDict(d)
                localId = d.get('local_id')
                mode = d.get('st_mode',0)
                path = st.getPath()
                
                msg = '' if not localId in gddownload.manager.errorsByLocalId else f'ERROR: {gddownload.manager.errorsByLocalId[localId]}'

                st = attr.Stat.newFromDict(d)
                output.append(f'local_id={d.get("local_id", None)} path={st.getPath()} {d} {msg}')
                if localId != key.split('-',1)[1]:
                    output.append(f'    localId MISMATCH: getattr.{localId} != key.{key.split('-')[1]}')                        
                
                if (mode & stat.S_IFDIR):
                    dirEntries = metadata.cache.readdir(path)
                    if dirEntries is None:
                        output.append(f'   readdir: path={path} NOT FOUND!')
                    else:
                        #output.append(f'   readdir={dirEntries}')
                        for name, localId in dirEntries.items():
                            entrySt = metadata.cache.getattr_by_id(localId)
                            if entrySt == None:                             
                                output.append(f'   readdir: dir={path} name={name} local_id={localId} NOT FOUND!')

                if (mode & stat.S_IFLNK == stat.S_IFLNK):
                    totalLinks += 1
                elif (mode & stat.S_IFREG == stat.S_IFREG):
                    totalFiles += 1
                elif (mode & stat.S_IFDIR): 
                    totalDirectories += 1                
                else:
                    totalInvalid += 1

            output.append(f'TOTAL: files={totalFiles} directories={totalDirectories}, symlinks={totalLinks}')          
        except Exception as e:
            raisedBy = log.exceptionRaisedBy(e)
            logger.exception(f'RPC.metadata exception: {raisedBy}')
            metrics.counts.incr('rpc_metadata_exception')
            raisedBy = log.exceptionRaisedBy(e)
            logger.exception(f'<-- rpc_metadata: {raisedBy}')
            output.append(str(e))
            output.append('See error details in ~/gdrive_filesys/error.log')

        return output

    def directories(self) -> str:        
        metrics.counts.incr('rpc_directories')    
        output: list[str] = []
        try:
            for dir in directories.store.getAllDirectories():                
                path = dir.path 
                
                output.append(f'ByPath: path={path} name={dir.name} gd_id={dir.gdId} local_id={dir.localId} gd_parent_id={dir.gdParentId} local_parent_id={dir.localParentId}')
                key = directories.store.key(dir.localId)
                dirBytes = db.cache.get(key, directories.DIRECTORY_PREFIX) 
                if dirBytes == None:
                    output.append(f'ERROR: Directory not found in db: {key}')
                    raise Exception(f'Directory not found in db: {key}')
                if dir.gdId != None:
                    dirByGdId = directories.store.getDirectoryByGdId(dir.gdId)
                    if dirByGdId is not dir:
                        output.append(f'ByGdId: path={dirByGdId.path} name={dirByGdId.name} gd_id={dirByGdId.gdId} local_id={dirByGdId.localId} gd_parent_id={dirByGdId.gdParentId} local_parent_id={dirByGdId.localParentId}')
                        raise Exception(f'Directory lookup by gdId failed for gdId={dir.gdId} path={path}')
                dirByLocalId = directories.store.getDirectoryByLocalId(dir.localId)
                if dirByLocalId is not dir:
                    output.append(f'ByLocalId: path={dirByLocalId.path} name={dirByLocalId.name} gd_id={dirByLocalId.gdId} local_id={dirByLocalId.localId} gd_parent_id={dirByLocalId.gdParentId} local_parent_id={dirByLocalId.localParentId}')
                    raise Exception(f'Directory lookup by localId failed for localId={dir.localId} path={path}')                    
          
        except Exception as e:
            raisedBy = log.exceptionRaisedBy(e)
            logger.exception(f'RPC.directories exception: {raisedBy}')
            metrics.counts.incr('rpc_directories_exception')
            raisedBy = log.exceptionRaisedBy(e)
            logger.exception(f'<-- rpc_directories: {raisedBy}')
            output.append(str(e))
            output.append('See error details in ~/gdrive_filesys/error.log')

        return output
    
    def unread(self) -> str:
        
        metrics.counts.incr('rpc_unread')       
        output: list[str] = []
        try:
            totalFiles = 0
            totalBlocks = 0
            unreadFiles = 0            
            unreadBlocks = 0            

            it = db.cache.getIterator()
            for key, value in it(prefix=bytes(mem.GETATTR, encoding='utf-8')):
                key = str(key, 'utf-8')
                d = json.loads(value)
                mode = d.get('st_mode', 0)  
                localId = d.get('local_id')              
                if not (mode & stat.S_IFREG == stat.S_IFREG):
                    continue  
                st = attr.Stat.newFromDict(d)
                path = st.getPath()              
                
                totalFiles += 1                    
                size = d.get('st_size', 0)                    
                if size > 0:                        
                    unreadBlockCount = data.cache.getUnreadBlockCount(path, localId, st)
                    if unreadBlockCount > 0:                            
                        unreadFiles += 1
                        unreadBlocks += unreadBlockCount

                        msg = f'UNREAD BLOCKS={unreadBlockCount}' if not localId in gddownload.manager.errorsByLocalId else f'ERROR: {gddownload.manager.errorsByLocalId[localId]}'
                        output.append(f'local_id={localId} path={st.getPath()} {msg}')

                    totalBlocks += math.ceil(size/ common.BLOCK_SIZE)
            output.append(f'TOTAL: files={totalFiles} blocks={totalBlocks}, UNREAD: files={unreadFiles} blocks={unreadBlocks}')
    
        except Exception as e:
            raisedBy = log.exceptionRaisedBy(e)
            logger.exception(f'RPC.unread exception: {raisedBy}')
            metrics.counts.incr('rpc_unread_exception')
            raisedBy = log.exceptionRaisedBy(e)
            logger.exception(f'<-- rpc_unread: {raisedBy}')
            output.append(str(e))
            output.append('See error details in ~/gdrive_filesys/error.log')

        return output

class RpcClient:
    def __init__(self):
        import xmlrpc.client
        self.client = xmlrpc.client.ServerProxy(f'http://localhost:{common.RPC_SERVER_PORT}/', allow_none=True)
    
    def eventqueue(self):
        output = self.client.eventqueue()
        for line in output:
            print(line+'\n')

    def unread(self):
        output = self.client.unread()
        for line in output:
            print(line+'\n')

    def metadata(self):
        output = self.client.metadata()
        for line in output:
            print(line+'\n')

    def directories(self):
        output = self.client.directories()
        for line in output:
            print(line+'\n')

    def status(self):
        while True:
            output = self.client.status()
            for line in output:
                line = line.replace('OFFLINE', '\033[1m\033[31mOFFLINE\033[00m')
                line = line.replace('ONLINE', '\033[1m\033[32mONLINE\033[00m')
                line = line.replace('retry', '\033[1m\033[33mretry\033[00m')
                line = line.replace('failed', '\033[1m\033[31mfailed\033[00m')
                line = line.replace('exceptions', '\033[1m\033[31mexceptions\033[00m')
                print(line)
            time.sleep(10)

server: RpcServer = RpcServer()
client: RpcClient = RpcClient()