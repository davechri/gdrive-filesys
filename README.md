# gdrive-filesys

**gdrive-filesys is a Linux or macOS filesystem for Google Drive.**

gdrive-filesys is a network filesystem that gives your computer direct access to your
files on Google Drive.  gdrive-filesys allows you to use files on Google Drive as if they
were files on your local computer.

gdrive-filesys has been optimized to allow git repos to be stored on google drive:
```sh
gdrive-filesys mount ~/mnt
cd ~/mnt
git clone <repo>
```

gdrive-filesys by default performs an on-demand download of files when your computer
attempts to use them.  

## Key features

gdrive-filesys has several nice features that make it significantly more useful than
other Google Drive clients:

- **The default is to only download files when you use them.** The deafult is to only
  download a file if you (or a program on your computer) uses that file. You don't need
  to wait hours for a sync client to sync your entire Google Drive account to your
  local computer.

- **Downloading all files from Google Drive.** You can automaticaly download all files to
  to your local filesystem.  This will ensure that all files can be accessed offline when your computer is disconnected from the internet. 

- **Bidirectional sync.** gdrive-filesys will only download a file when you access a file
  that has been changed remotely on Google Drive. If you somehow simultaneously
  modify a file both locally on your computer and also remotely on Google Drive,
  gdrive-filesys will keep the most recently modified file.  The default is to check Google
  Drive once every minute for any remotely changed files.

- **Can be used offline.** Files you've opened previously will be available even
  if your computer has no access to the internet.  While your computer is disconnected
  from the internet, you can continue to update the local filesystem and when you
  reconnect to the internet, the locally updated files will be uploaded to google drive. 
    
- **Fast.** gdrive-filesys asynchronously performs network requests to Google Drive once every
  minute. gdrive-filesys caches both filesystem metadata and file contents both in memory and on-disk.  Filesytem operations will always be fast unless a large file must be read from Google Drive.

- **gitignore files.** Files that match a `.gitignore` file are only cached locally and are
  not uploaded to Google Drive.  This means that Python virtual environment packages in `.venv`
  and nodejs packages in `node_modules` are only installed locally.  
  
 **Local Only directories.** Directories can be configued to only store files locally in your native filesystem to improve small file performance.  The `~/.gdrive-filesys/config.toml` file is used to configure local only directories.  The`node_modules` directory is configured as a local only directory so node packages are stored locally in your native filesystem, and `npm install` will run very fast.

 This is the default `config.toml' file:
 ```json
### This is the configuration file.
### It should be placed in $HOME/.gdrive-filesys/config.toml

# Directory names that are only created localy and not synced to Google Drive.
# Files in local only directories are stored in the native filessystem for fast
# updates.  Install directories are good candidates to be stored locally.
local_only = [
    "node_modules",    
]
 ```
 
## Quick start

### Install FUSE

#### OSX

On Mac OSX, gdrive-filesys requires [osxfuse](https://osxfuse.github.io/) and [pkg-config](http://macappstore.org/pkg-config/):

```bash
$ brew update; brew install pkg-config; brew tap homebrew/cask; brew install --cask osxfuse
```

#### Ubuntu / Debian

On Ubuntu / Debian, gdrive-filesys requires [libfuse-dev](https://packages.ubuntu.com/disco/libfuse-dev), [libssl-dev](https://packages.ubuntu.com/disco/libssl-dev) and [pkg-config](https://packages.ubuntu.com/disco/pkg-config):

```bash
sudo apt-get install -y fuse3 libfuse-dev libssl-dev pkg-config
```

#### Fedora

On Fedora, gdrive-filesys requires gcc, fuse3-devel, and pkg-config:

```bash
sudo dnf install -y gcc fuse3-devel pkg-config
```

### Google Drive Authentication
1) Follow these [instuctions](https://developers.google.com/workspace/guides/create-credentials) to create the required `credentials.json` file. 
2) Download the `credentials.json` file and place it in your `~/.gdrive-filesys/` directory.

### Install gdrive-filesys:

### MacOS

Install pipx:
```sh
brew install pipx
pipx ensurepath
sudo pipx ensurepath --global # optional to allow pipx actions with --global argument
pipx install gdrive-filesys
```

#### Ubuntu / Debian

```sh
sudo apt update
sudo apt install pipx
pipx ensurepath
sudo pipx ensurepath --global # optional to allow pipx actions with --global argument
pipx install gdrive-filesys
```

#### Fedora

```sh
sudo dnf install pipx
pipx ensurepath
sudo pipx ensurepath --global # optional to allow pipx actions with --global argument
pipx install gdrive-filesys
```

### Mount Filesystem

Create mountpoint directory:
```sh
MOUNTPOINT=~/mnt
mkdir $MOUNTPOINT
```

Mount Filesystem:
```sh
gdrive-filesys mount $MOUNTPOINT # add --downloadall to sync all files
```

Notes:
- It's recommended to run it as user, not as root.  For this to work the mountpoint must be owned by the user.
- The cache update interval defaults to 1 minutes, and can be set with the --updateinterval option.

### Unmount Filesystem

```sh
gdrive-filesys unmount $MOUNTPOINT
```
or
```sh
fusermount -u $MOUNTPOINT
```

### Display gdrive-filesys activity:

```sh
gdrive-filesys status
```

## Building gdrive-filesys yourself

Create Virtual Environment:

```sh
cd gdrive-filesys
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

Mount filesystem:

```sh
$ ./src/gdrive_filesys/cli.py $MOUNTPOINT

```

### Running tests

The best way to test is to clone a repo, and install, build and run the application.

Mount Filesystem:
```sh
gdrive-filesys mount $MOUNTPOINT
```

Build and run web application:
```bash
cd $MOUNTPOINT
git clone https://github.com/allproxy/allproxy.git
npm install
npm run build
npm start
```

Run python application:
```bash
cd $MOUNTPOINT
git clone https://github.com/davechri/gdrive-filesys.git
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
./src/gdrive_filesys/cli.py status
```

## Troubleshooting

If the filesystem appears to hang, its possible the fileystem has crashed. To resolve this:
- **Remount:** `fusermount -u $MOUNTPOINT` and remount `gdrive-filesys mount $MOUNTPOINT`
- **Clear the local cache:** `fusermount -u $MOUNTPOINT` and remount `gdrive-filesys mount $MOUNTPOINT --clearcache`
- **Restart your computer**

### Logging

- **Error Log:** error logs are logged to `~/.gdrive-filesys/error.log`
- **HTTP Log:** HTTP errors are logged to `~/.gdrive-filesys/http.log`

#### Debug Logging

For debug logging use the --debug option.  The --verbose option will produce more verbose logging.
```sh
gdrive-filesys mount $MOUNTPOINT --debug &> ~/path_to_my_log_file
```

### Display gdrive-filesys status

Filesystem statistics are displayed every 10 seconds.

```sh
gdrive-filesys status
```
```sh
18:11:12 ONLINE:
	LOCAL_ONLY:   /home/dave/.gdrive-filesys/localonly=969.2 kB
	GITIGNORE:    dirs=0     files=0        links=2    cached=0 Bytes   total=0 Bytes
	GOOGLE_DRIVE: dirs=495   files=3109     links=5    cached=7.3 MB    total=121.1 MB
18:11:24 ONLINE:
	FILESYS OPS:  getattr=424 readdir=32 read=290 flush=249 readlink=4 
	LOCAL_ONLY:   /home/dave/.gdrive-filesys/localonly=969.2 kB
	GITIGNORE:    dirs=0     files=0        links=2    cached=0 Bytes   total=0 Bytes
	GOOGLE_DRIVE: dirs=495   files=3109     links=5    cached=7.3 MB    total=121.1 MB
18:11:36 ONLINE:
	FILESYS OPS:  getattr=311 readdir=18 read=63 flush=63 readlink=644 
	LOCAL_ONLY:   /home/dave/.gdrive-filesys/localonly=969.2 kB
	GITIGNORE:    dirs=0     files=0        links=2    cached=0 Bytes   total=0 Bytes
	GOOGLE_DRIVE: dirs=495   files=3109     links=5    cached=7.3 MB    total=121.1 MB
```

### Dump Internal State

Display metadata:
```sh
gdrive-filesys metadata
```

Display directory entries:
```sh
gdrive-filesys directories
```

Diaplay all unread files:
```sh
gdrive-filesys unread
```

Display the event queue:
```sh
gdrive-filesys eventqueue
```



