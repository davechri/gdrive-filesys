import errno
from os import path
from fuse import FuseOSError

from gdrive_filesys import common, metrics
from gdrive_filesys import directories
from gdrive_filesys.cache import metadata, refreshcache

from googleapiclient.errors import HttpError

from gdrive_filesys.cache import db
from gdrive_filesys.log import logger

def execute(path: str) -> list[str]:
    """
    Lists the names of files and directories within the specified Google Drive path.
    Args:
        path (str): The path to the directory in Google Drive. Defaults to the current directory (".")
    Returns:
        list[str]: A list of file and directory names within the specified path, including '.' and '..'.
    Raises:
        FuseOSError: If the specified directory does not exist or cannot be accessed.
        HttpError: If an HTTP error occurs during the API request.
    """  
    logger.debug(f'API.readdir: path={path}')
    if common.offline:
        metrics.counts.incr('readdir_offline')
        raise FuseOSError(errno.ENETDOWN) 

    st = refreshcache.refreshDirectory(path)
    if st != None:
        dirEntries = metadata.cache.readdir(path)
        if dirEntries != None:
            return list(dirEntries.keys())

    raise FuseOSError(errno.ENOENT)