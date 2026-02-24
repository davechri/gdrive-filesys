import threading
import time

import requests

from google.auth.transport.requests import Request
from googleapiclient.errors import HttpError
from google.auth.exceptions import TransportError, TimeoutError

from gdrive_filesys import common, eventq, metrics, refresh, oauth
from gdrive_filesys.api import api
from gdrive_filesys.log import logger

class Heartbeat:
    def __init__(self):     
        self.heartbeatStarted = False
        self.heartbeatStopped = False
        
    def start(self):
        """
        Starts the heartbeat thread if it has not already been started and has not been stopped.
        Increments the 'heartbeat_start' metric counter and sets the heartbeatStarted flag to True.
        Spawns a new thread to run the heartbeatThread method.
        Once stopped, the heartbeat thread cannot be restarted.
        """
        if self.heartbeatStopped:
            logger.warning("Cannot restart heartbeat thread after it has been stopped.")
            return
        if not self.heartbeatStarted:
            metrics.counts.incr('heartbeat_start')
            self.heartbeatStarted = True
            threading.Thread(target=self.heartbeatThread, daemon=True).start()

    def heartbeatThread(self): 
        """
        Periodically checks the status of the Google Drive API to maintain an online/offline state.
        This thread performs the following actions:
        - Increments a metric counter when started.
        - Continuously runs until `self.heartbeatStopped` is set to True.
        - Sleeps for 10 seconds between checks.
        - Refreshes credentials if expired and a refresh token is available.
        - Sends a GET request to the Google Drive API to check connectivity.
        - Updates the offline/online status and increments corresponding metrics.
        - Refreshes cached directory data at intervals defined by `common.updateinterval`.
        - Logs errors and updates offline status if the API request fails.
        """
        common.threadLocal.operation = 'heartbeat'
        common.threadLocal.path = None
        metrics.counts.incr('heartbeatThread')        
        failCount = 0
        try:
            while True:
                if self.heartbeatStopped:
                    metrics.counts.incr('heartbeat_stopped')
                    break
                time.sleep(10)

                try:
                    oauth.creds.refreshIfExpired()

                    headers = {
                        "Authorization": f"Bearer {oauth.creds.get().token}",
                        "Content-Type": "application/json"
                    }

                    response = requests.get(api.GOOGLE_DRIVE_URL+"/files?pageSize=1", headers=headers, timeout=10)
                    if response.status_code == 200:
                        failCount = 0
                        if common.offline:
                            metrics.counts.incr('heartbeat_online')
                            common.apiClientsByThread.clear() # Clear cached API clients
                            common.offline = False
                            logger.info("Google Drive API is reachable again, switching to online mode")                         

                        refresh.thread.trigger()                        
                    else: 
                        logger.error(f"Error: {response.status_code}, {response.text}") 
                        if not common.offline:
                            failCount += 1
                            if failCount >= 2:
                                logger.info("Setting offline due to repeated errors from Google Drive API")
                                common.offline = True
                                metrics.counts.incr('heartbeat_offline')
                except (ConnectionError, TransportError, TimeoutError, HttpError, requests.RequestException) as e:                    
                    if not common.offline:
                        logger.info(f"Cannot connect to google drive: {e}")
                        failCount += 1
                        if failCount >= 2:
                            logger.info("Setting offline due to connection errors")
                            common.offline = True                            
                            metrics.counts.incr('heartbeat_offline')
        except Exception as e:
            if isinstance(e, HttpError):
                metrics.counts.incr('heartbeat_httperror')
                logger.error(f"HttpError in heartbeatThread: {e}")
            elif isinstance(e, TimeoutError):
                metrics.counts.incr('heartbeat_timeout_error')
                logger.error(f"TimeoutError in heartbeatThread: {e}")
            else:
                metrics.counts.incr('heartbeat_exception')
                logger.error(f"Exception in heartbeatThread: {e}")

    def stop(self):
        """
        Stops the heartbeat process.
        This method sets the `heartbeatStopped` flag to True, logs the stop event,
        and increments the 'heartbeat_stop' metric counter.
        Note: Once stopped, the heartbeat thread cannot be restarted; this is enforced by the start() method.
        """
        logger.info('heartbeat_stop called')
        metrics.counts.incr('heartbeat_stop')
        self.heartbeatStopped = True

monitor: Heartbeat = Heartbeat()