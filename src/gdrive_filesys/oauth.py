
import os
import sys
from gdrive_filesys.log import logger
from gdrive_filesys import common, metrics

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


class Creds:
    def __init__(self):
        self.creds: Credentials|None = None
    
    def get(self) -> Credentials|None:
        return self.creds
   
    def refreshIfExpired(self) -> None:
        if self.creds.expired and self.creds.refresh_token:            
            self.creds.refresh(Request())
            logger.info(f'oauth.refreshIfExpired: credentials refreshed')
            metrics.counts.incr('oauth_refreshed')
        
    def init(self) -> bool: # False, if credentials.json file is missing
        SCOPES = [
            "https://www.googleapis.com/auth/drive.metadata",
            "https://www.googleapis.com/auth/drive",
        ]

        credentialsJsonPath = os.path.join(common.dataDir, 'credentials.json')  
        if not os.path.exists(credentialsJsonPath):
            logger.error(f'oauth.init: {credentialsJsonPath} does not exist')
            setupUrl = 'https://developers.google.com/workspace/drive/api/quickstart/python'
            metrics.counts.incr('oauth_credentials_missing')
            print('credentials.json file not found in {}'.format(common.dataDir), file=sys.stderr)
            print('\nPlease create OAuth 2.0 Client IDs credentials in Google Cloud Console,')
            print('download the credentials.json file and place it in {}'.format(common.dataDir))
            print(f'\nFor more information, see {setupUrl}')
            return False
        tokenPath = os.path.join(common.dataDir, 'token.json')  

        self.creds = None
        # The file token.json stores the user's access and refresh tokens, and is
        # created automatically when the authorization flow completes for the first
        # time.
        if os.path.exists(tokenPath):
            logger.info(f'oauth.init: create credentials from token')
            metrics.counts.incr('oauth_token_exists')
            self.creds = Credentials.from_authorized_user_file(tokenPath, SCOPES)        
        try:
            # If there are no (valid) credentials available, let the user log in.
            if not self.creds or not self.creds.valid:
                if self.creds and self.creds.expired and self.creds.refresh_token:
                    logger.info(f'oauth.init: refreshing credentials')
                    metrics.counts.incr('oauth_refreshing')
                    self.creds.refresh(Request())
                else:
                    metrics.counts.incr('oauth_creating')
                    flow = InstalledAppFlow.from_client_secrets_file(
                        credentialsJsonPath, SCOPES
                    )
                    self.creds = flow.run_local_server(port=0)
                    # Save the credentials for the next run
                    logger.info(f'oauth.init: save credentials for the next run')
                    with open(tokenPath, "w") as token:
                        token.write(self.creds.to_json())
            return True     
        except Exception as e:
            logger.error(f'oauth.init: {e}')
            metrics.counts.incr('oauth_init_exception')
            print('Authentication failed!')
            print('Only cached readonly content can be accessed.')
            print('Once the remote google drive is available, read/write access will be restored.')
            common.offline = True
            return True             
        
creds: Creds = Creds()