
import os
from pathlib import Path
import shutil
import tomllib

from gdrive_filesys import common
from gdrive_filesys.log import logger


class Config:
    def __init__(self):
        configPath = os.path.join(common.dataDir, 'config.toml')
        if not os.path.exists(configPath): 
            self.localonlyDir = os.path.join(Path.home(), '.gdrive-filesys', 'config.toml')
            defaultConfigPath = Path(__file__).resolve().parent / "../default_config.toml"
            shutil.copy(defaultConfigPath, configPath)
            
        with open(configPath, 'rb') as f:
            config = tomllib.load(f)
            logger.debug(f'config: {config}')
            self.localOnlyDirs = config['local_only']

    def getLocalOnlyDirs(self) -> list[str]:
        return self.localOnlyDirs
    
config = Config()