import os
import uuid
from pathlib import Path
from enum import Enum

import threading
import googleapiclient
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

from gdrive_filesys import oauth
from gdrive_filesys.log import logger

API_TIMEOUT = 20
UPDATE_INTERVAL = 60
BLOCK_SIZE = 131072
NUMBER_OF_FILE_READER_THREADS = 5
RPC_SERVER_PORT = 11111

SHORTCUT_MIME_TYPE = 'application/vnd.google-apps.shortcut'

dataDir = os.path.join(Path.home(), '.gdrive-filesys')
debug = False
verbose = False
pathfilter: str|None = None
updateinterval = UPDATE_INTERVAL
mountpoint = None
offline = False
offlineode = False
xattrEnabled = False

# Google Documents: application/vnd.google-apps.document
# Google Spreadsheets: application/vnd.google-apps.spreadsheet
# Google Drawings: application/vnd.google-apps.drawing
# Google Presentations (Slides): application/vnd.google-apps.presentation 
def mimeTypeCannotBeConverted(mimeType: str) -> bool:
    return mimeType == 'application/vnd.google-apps.document' or \
              mimeType == 'application/vnd.google-apps.spreadsheet' or \
                mimeType == 'application/vnd.google-apps.drawing' or \
                    mimeType == 'application/vnd.google-apps.presentation'

def generateLocalId(path: str, type: str, context: str, localOnly: bool) -> str:
    if localOnly:
        prefix = 'localonly-' + type
    else:
        prefix = 'local-' + type
    localId = f'{prefix}-{os.path.basename(path) if path != "/" else "root"}-{str(uuid.uuid4())}'
    logger.debug('Generated localId=%s for path=%s type=%s in context=%s', localId, path, type, context)
    return localId

def isInLocalOnlyConfigLocalId(localId: str) -> bool:
    return localId.startswith('localonly-')

class ApiClient:
    """
    ApiClient is a class responsible for managing API credentials and service connections.
    Attributes:
        creds: Stores authentication credentials required for API access.
        service: Represents the API service client instance.
    """
    def __init__(self):
        self.creds = None
        self.service = None 

def apiTimeoutRange():
    return range(API_TIMEOUT, API_TIMEOUT*4, API_TIMEOUT) 
def isLastAttempt(timeout: int) -> bool:
    return timeout >= API_TIMEOUT*3       

apiClientsByThread: dict[int, ApiClient] = {}  

def getApiClient(timeout: int = API_TIMEOUT) -> 'googleapiclient.discovery.Resource':
    """
    Retrieves a thread-local Google Drive API client service instance.
    This function ensures that each thread has its own instance of the API client.
    If the credentials have changed, it rebuilds the service with the new credentials.
    Returns:
        googleapiclient.discovery.Resource: The Google Drive API service instance for the current thread.
    """
    threadId = threading.get_native_id()
    if threadId in apiClientsByThread:
        apiClient = apiClientsByThread[threadId]
    else:
        apiClient = ApiClient()        
        apiClientsByThread[threadId] = apiClient

    oauthCreds = oauth.creds.get()
    if apiClient.creds != oauthCreds:
        oauth.creds.refreshIfExpired()        
        apiClient.service = build("drive", "v3", credentials=oauthCreds, cache_discovery=False)
        apiClient.service._http.timeout = timeout  # type: ignore
        
    apiClient.creds = oauthCreds

    return apiClient.service

threadLocal = threading.local()
