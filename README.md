# BlockOps

BlockOps is a local Minecraft Java server manager for Windows and macOS. Its browser dashboard creates Vanilla, Fabric, and Forge servers; installs the correct Java runtime; safely starts and stops worlds; manages mods and configuration; shows players and logs; makes scheduled backups; and connects the active server through playit.gg.

The dashboard is private to the computer (`127.0.0.1:8765`) and protected by a random local launch token. Worlds and account credentials are excluded from Git.

## Install from GitHub

No system Python or Java installation is required. The first-run installer downloads a private Python runtime, and BlockOps later downloads the exact Java runtime each Minecraft version needs.

### Windows 10/11

1. On GitHub, choose **Code → Download ZIP**, then extract the ZIP completely. Do not run the app from inside the ZIP preview.
2. Double-click `setup.bat`.
3. If Windows asks whether PowerShell may run the trusted Astral `uv` installer, allow it. Administrator access is not required.
4. BlockOps opens in your default browser. Later, use `BlockOps.bat`.

### macOS 12 or newer

1. On GitHub, choose **Code → Download ZIP**, then extract it, or clone the repository with Git.
2. Double-click `setup.command`.
3. If macOS blocks it, Control-click `setup.command`, choose **Open**, and confirm once. Later, launch `BlockOps.app` or `BlockOps.command`.
4. If a ZIP extractor removed executable permissions, open Terminal in this folder and run:

   ```sh
   chmod +x setup.command BlockOps.command server-manager *.command BlockOps.app/Contents/MacOS/BlockOps
   ./setup.command
   ```

Setup is safe to run again. It reuses the private runtime and verifies the app before opening it. Developer tests are not required for installation, so a release copy without the `tests` folder still installs normally.

## First-time connection setup

Before creating the first server, BlockOps opens a guided connection setup:

1. On Windows, choose **Install Playit**. BlockOps downloads the signed official portable agent. On macOS, use the official Playit download link, install the app, and return to BlockOps.
2. Choose **Connect Account**, open the one-time **Claim Playit Agent** link, and approve this computer on playit.gg. Account credentials are entered only on Playit's site.
3. Open **Playit Tunnels**, create a **Minecraft Java** tunnel targeting `127.0.0.1:25565`, and confirm that step in BlockOps.
4. Choose **Create My First Server**, enter a Minecraft version, select Vanilla/Fabric/Forge, and choose RAM limits. BlockOps validates the version and downloads Minecraft plus the correct Java runtime. Forge requires an exact Forge version; Fabric can select the latest stable loader automatically.

The **Setup Guide** reports installation, account, and tunnel status separately and provides recovery guidance when a download, firewall, antivirus, permission, or agent-start problem occurs. Existing installations with servers are never blocked by this newly introduced first-run gate.

## Terminal tools

The graphical dashboard is recommended, but direct commands are available:

| Task | macOS | Windows |
| --- | --- | --- |
| Open manager | `./server-manager` | `server-manager.bat` |
| Create server | `./server-manager create instance --name Survival --minecraft 1.21.8 --loader fabric` | `server-manager.bat create instance --name Survival --minecraft 1.21.8 --loader fabric` |
| Start | `./server-manager start instance --profile survival` | `server-manager.bat start instance --profile survival` |
| Console | `./server-manager console` | `server-manager.bat console` |
| Stop safely | `./server-manager stop instance` | `server-manager.bat stop instance` |

On macOS, `Server Manager.command`, `Server Console.command`, and `Minecraft Server Dashboard.command` are Finder-friendly launchers.

## What is and is not stored in Git

Git contains the application, launchers, tests, and portable examples. It intentionally excludes:

- worlds, player data, profiles, logs, crash reports, mods, and server JARs;
- downloaded Python/Java/Playit runtimes and install caches;
- Playit credentials, dashboard tokens, and machine-specific profile data;
- backups and BlockOps configuration-edit recovery copies.

To move an existing world, first create a matching server in BlockOps and stop it. Replace its world folder only while it is offline. To move a live BlockOps installation between computers, safely stop the server and transfer the full profile directory separately; never try to merge a running Minecraft world through Git.

## Backups and safety

New servers default to one compressed world backup every 30 minutes and retain the newest 10, deleting the oldest when an eleventh is created. Existing servers keep their previously stored schedule. Each server's frequency, retention, compression, empty-server behavior, and safe-stop snapshot option can be changed under **Backups → Backup Policy**.

You can also drag a BlockOps `.tar.gz` world backup onto the Backups screen. BlockOps uploads and validates it first, then asks whether to replace the server's current progress. Applying any backup creates a fresh safety snapshot, safely stops a running server, restores with rollback on failure, and restarts it. Live snapshots use `save-off` and `save-all flush`, then restore automatic saving.

The stop action sends `save-all flush` and `stop`, waiting up to 125 seconds. It will not silently force-kill a server that has not completed its save.

## Developer verification

BlockOps uses only the Python standard library. With Python 3.10+ installed:

```sh
python3 -m unittest discover -s tests -t . -v
```

GitHub Actions runs the same tests on Windows and macOS for every push and pull request. Network access is required only for initial setup and first-time downloads of Minecraft, Java, loaders, and Playit.

Downloads are obtained from the official Astral, Mojang, Fabric, Forge, Eclipse Adoptium, Azul, and `playit-cloud/playit-agent` endpoints.
