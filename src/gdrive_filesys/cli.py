#!/usr/bin/env python 

import os.path
import subprocess

from gdrive_filesys import filesystem, log, common, oauth
from pathlib import Path
from gdrive_filesys import rpc
from gdrive_filesys.api import api

def main():
    
    common.threadLocal.operation = 'cli'
    common.threadLocal.path = None
    import argparse
    import argparse
    parser = argparse.ArgumentParser(description='Access google drive as a local filesystem.')
    subparsers = parser.add_subparsers(dest='operation', help='Available operations')

    # mount
    mount_parser = subparsers.add_parser('mount', help='Mount google drive filesystem')
    mount_parser.add_argument('mountpoint', help='local mount point (eg, ~/mnt)')
    mount_parser.add_argument('--debug', help='run in debug mode', action='store_true')
    mount_parser.add_argument('--verbose', help='run in verbose debug mode', action='store_true')
    mount_parser.add_argument('--pathfilter', type=str, help='path to filter for logging', default=None)
    mount_parser.add_argument('--updateinterval', type=int, help=f'Update interval in seconds for updating google drive and refreshing cached data (default is {common.UPDATE_INTERVAL})', default=common.UPDATE_INTERVAL)
    mount_parser.add_argument('--clearcache', help='clear the cache', action='store_true')
    mount_parser.add_argument('--downloadall', help='Download all files for offline access', action='store_true')
    
    # unmount
    unmount_parser = subparsers.add_parser('unmount', help='Unmount google drive filesystem')
    unmount_parser.add_argument('mountpoint', help='local mount point (eg, ~/mnt)')

    # rpc operations
    subparsers.add_parser('status', help='Dump status every 10 seconds')   
    subparsers.add_parser('eventqueue', help='Dump event queue')
    subparsers.add_parser('metadata', help='Dump metadata')
    subparsers.add_parser('directories', help='Dump directories')  
    subparsers.add_parser('unread', help='Dump unread data blocks')

    args = parser.parse_args()

    if not os.path.exists(common.dataDir):
        os.mkdir(common.dataDir)

    if args.operation == None:
        parser.print_help()
        exit(1)

    if args.operation == 'unmount':
        rc = subprocess.run(['fusermount', '-u', args.mountpoint]).returncode
        if rc == 0:
            print('unmount successful')
        exit(rc)     

    if args.operation == 'mount':
        common.debug = args.debug 
        common.verbose = args.verbose 
        common.pathfilter = args.pathfilter
        common.updateinterval = args.updateinterval
        common.mountpoint = args.mountpoint
        common.downloadall = args.downloadall
      
        log.Log().setupConfig(debug=args.debug, verbose=args.verbose) 
        filesystem.gdrive_filesys(args)
    else:
        try:       
            if args.operation == 'unread':
                rpc.client.unread()
            elif args.operation == 'eventqueue':
                rpc.client.eventqueue()
            elif args.operation == 'metadata':
                rpc.client.metadata()
            elif args.operation == 'directories':
                rpc.client.directories()
            elif args.operation == 'status':
                rpc.client.status()
        except Exception as e:
            print('RPC connection failed.')
            print('Make sure that your google drive filesystem is mounted.')

if __name__ == '__main__':
    main()