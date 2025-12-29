
import os
from pathlib import Path
import shutil
import tomllib

from gdrive_filesys import common, log
from gdrive_filesys.log import logger

class Config:
    def __init__(self):
        try:
            configPath = os.path.join(common.dataDir, 'config.toml')
            if not os.path.exists(configPath): 
                self.localonlyDir = os.path.join(Path.home(), '.gdrive-filesys', 'config.toml')
                defaultConfigPath = Path(__file__).resolve().parent / "default_config.toml"
                shutil.copy(defaultConfigPath, configPath)
                
            with open(configPath, 'rb') as f:
                config = tomllib.load(f)
                logger.debug(f'config: {config}')
                self.localOnlyDirs = config['local_only']            
        except Exception as e:
            self.localOnlyDirs = ['node_modules',]
            raisedBy = log.exceptionRaisedBy(e)  
            logger.exception(f'Config.__init__: exception loading config: {raisedBy}')            

    def getLocalOnlyDirs(self) -> list[str]:
        return self.localOnlyDirs
    
config = Config()
