    

import logging
import os
from pathlib import Path
import traceback

from gdrive_filesys import color, common

_GDRIVE_FS     = 'gdrive-filesys'
_GDRIVE_METRICS     = 'gdrive-filesys.metrics'

_CONNECTION  = 'urllib3.connectionpool'
_FUSE        = 'fuse'
_GOOGLE_CACHE    = 'googleapiclient.discovery_cache'
_GOOGLE_DISCOVERY = 'googleapiclient.discovery'
_GOOGLE_HTTP = 'googleapiclient.http'

class ThreadId:
    def __init__(self):
        self.num = 0
        self.idToNum: dict[str,int] = dict()
    def toNum(self, id):
        if not id in self.idToNum:
            self.num = self.num + 1
            self.idToNum[id] = self.num
        return self.idToNum[id]

threadId = ThreadId()

class TidFilter(logging.Filter):
    def filter(self, record):
        record.thread = threadId.toNum(record.thread)

        if record.levelname == 'INFO':
            record.levelname = color.green(record.levelname)
        elif record.levelname == 'WARNING':
            record.levelname = color.yellow(record.levelname)
        elif record.levelname == 'ERROR':
            record.levelname = color.red(record.levelname)
        elif record.levelname == 'DEBUG':
            record.levelname = color.cyan(record.levelname)
        
        if hasattr(common.threadLocal, 'operation') and (record.name == _GDRIVE_FS):
            status = color.red('OFFLINE ') if common.offline else color.green('ONLINE ')
            record.name = status + color.cyan(common.threadLocal.operation+":")
        else:
            record.name = color.cyan(record.name )
        return True
    
class HttpFilter(logging.Filter):
    def filter(self, record): 
        if record.levelno >= logging.ERROR:
            s = logging.Formatter().format(record)             
            return s.find('HttpError') != -1
        return False

class ErrorFilter(logging.Filter):
    def filter(self, record):         
        if record.levelno >= logging.ERROR:
            s = logging.Formatter().format(record)               
            return s.find('HttpError') == -1 and s.find('ConnectionError') == -1
        return False
    
class FuseLogFilter(logging.Filter):
    def filter(self, record):
        return True
    
class PathFilter(logging.Filter):
    def filter(self, record): 
        if common.pathfilter == None:
            return True   
        if common.threadLocal.path == None:
            return False 
        for path in common.threadLocal.path:
            if isinstance(path, str) and path.find(common.pathfilter) != -1:
                return True       
        return False

class Log:
    def __init__(self):
        self.logDir = common.dataDir
        if not os.path.exists(self.logDir):
            os.makedirs(self.logDir)
        self.formatter = logging.Formatter('%(asctime)s %(levelname)s TID=%(thread)d %(name)s %(message)s')

    def setupConfig(self, debug: bool, verbose: bool):    
        """
        Configures logging for the application based on the debug flag.
        - If `debug` is True:            
            - Configures the root logger for DEBUG level with a specific format.
        - Sets up error logging for all major loggers (MAIN, API, METADATA, DATA, FUSE):
            - Adds a FileHandler for error logs to each logger.
            - Sets logger level to ERROR if not in debug mode.
        - Sets up metrics logging:
            - Adds a FileHandler for metrics logs to the METRICS logger.
            - Sets METRICS logger level to DEBUG.
        Args:
            debug (bool): If True, enables debug-level logging; otherwise, restricts logging to errors.
        verbose (bool): If True, enables verbose logging; otherwise, restricts logging to errors.
        Side Effects:
            Modifies logger configurations and adds file handlers for error and metrics logging.
        """
        ## debug logging
        if debug or verbose:            
            logging.getLogger(_FUSE).setLevel(logging.WARNING)            
            logging.basicConfig(
                format='%(asctime)s %(levelname)s TID=%(thread)d %(name)s %(message)s',
                datefmt='%H:%M:%S',
                level=logging.DEBUG if verbose else logging.INFO                
            ) 

        # error logging
        for name in [_GDRIVE_FS, _FUSE, _CONNECTION, _GOOGLE_CACHE, _GOOGLE_HTTP, _GOOGLE_DISCOVERY]:
            logger = logging.getLogger(name)

            httpErrorHandler = logging.FileHandler(os.path.join(self.logDir, 'httpError.log'), mode='w')
            httpErrorHandler.setFormatter(self.formatter) 
            httpErrorHandler.setLevel(logging.ERROR)
            httpErrorHandler.addFilter(HttpFilter())
            logger.addHandler(httpErrorHandler)

            errorHandler = logging.FileHandler(os.path.join(self.logDir, 'error.log'), mode='w')
            errorHandler.setFormatter(self.formatter) 
            errorHandler.setLevel(logging.ERROR)
            errorHandler.addFilter(ErrorFilter())
            logger.addHandler(errorHandler) 

            if not debug and not verbose:
                logger.setLevel(logging.ERROR)   

         # metrics logging
        metricsHandler = logging.FileHandler(os.path.join(self.logDir, 'metrics.log'), mode='w')
        metricsHandler.setFormatter(self.formatter)             
        metricsLogger = logging.getLogger(_GDRIVE_METRICS)
        metricsLogger.addHandler(metricsHandler)
        metricsLogger.setLevel(logging.INFO) 

logger = logging.getLogger(_GDRIVE_FS)
metricsLogger = logging.getLogger(_GDRIVE_METRICS)

for name in [_GDRIVE_FS, _GDRIVE_METRICS, _CONNECTION, _GOOGLE_CACHE, _GOOGLE_HTTP, _GOOGLE_DISCOVERY]:
    l = logging.getLogger(name)
    l.addFilter(TidFilter())
    l.addFilter(PathFilter()) if name != _GDRIVE_METRICS else None

def exceptionRaisedBy(e:Exception) -> str:
    traceback_details = traceback.extract_tb(e.__traceback__)
    # The last element in traceback_details corresponds to the point where the exception was raised.
    filename = os.path.basename(traceback_details[-1].filename)
    line_number = traceback_details[-1].lineno
    function_name = traceback_details[-1].name
    raised = f'raised by={filename}:{line_number} {function_name}() {e}'
    return raised