import errno
from fuse import FuseOSError

from gdrive_filesys import common, metrics
from gdrive_filesys.cache import metadata

_cache: dict[str, dict[str, any]] = {}

def execute(path: str) -> dict[str, any]:
    if common.offline:
        metrics.counts.incr('getxattr_offline')
        raise FuseOSError(errno.ENETDOWN)    
    
    fileId = metadata.cache.getattr(path)
    if fileId is None:
        metrics.counts.incr('getxattr_enoent')
        raise FuseOSError(errno.ENOENT)
    
    if fileId in _cache:        
        file = _cache[fileId]
    else:
        metrics.counts.incr('getxattr_network')
        service = common.getApiClient()
        
        file = (
            service.files()
                .get(
                    fileId=fileId,
                    fields="*"
            )
            .execute()
        )
        _cache[fileId] = file

    dic: dict[str, str] = {}
    for key in file.keys():
        value = file.get(key)
        if isinstance(value, dict):
            for subkey, subvalue in value.items():
                if isinstance(subvalue, bool) and subvalue is False:
                    continue                    
                dic[f'{key}.{subkey}'] = str(subvalue, 'utf-8') if isinstance(subvalue, bytes) else str(subvalue)
        else:
            if isinstance(value, bool) and value is False:
                continue
            dic[key] = str(value, 'utf-8') if isinstance(value, bytes) else str(value)
            
    return dic
    
        