# BlockOps

BlockOps is a local game-server manager for Windows and macOS. Its browser dashboard creates Minecraft Java (Vanilla, Fabric, and Forge), Minecraft Bedrock, and Palworld Dedicated Servers; safely starts and stops worlds; manages configuration; shows logs; makes scheduled backups; and connects Java/Palworld through playit.gg. Bedrock starts locally and has a separate, manual NetherNet/Playit connection guide.

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

Palworld has no native macOS dedicated-server build. On a Mac, BlockOps downloads the official Linux depot with Valve's native macOS SteamCMD and runs it in the open-source `thijsvanloef/palworld-server-docker` runtime. Install and start [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/) before creating a Palworld profile. Apple Silicon Macs use the container's native ARM64/Box64 path; 16 GB is the recommended Palworld limit, and the runtime includes a clock-safety workaround needed by Palworld 1.0 under ARM emulation.

## Bedrock prerequisites (macOS and Windows)

Install and start Docker Desktop using Linux containers before creating a Bedrock instance. Windows requires Docker Desktop's WSL 2 backend (enable virtualization and install/update WSL as described in the [Windows installation guide](https://docs.docker.com/desktop/setup/install/windows-install/)). macOS uses Docker's Linux VM; allow file sharing for the BlockOps folder. Allocate enough Docker VM memory for the server's configured maximum plus overhead; 4 GB is the default Bedrock container limit.

BlockOps runs Mojang's **official Linux Bedrock Dedicated Server**, downloaded on first start, inside the pinned open-source [itzg/minecraft-bedrock-server](https://github.com/itzg/docker-minecraft-bedrock-server) wrapper. This wrapper is not an official Mojang Docker image. The server is x86-64: Apple Silicon uses `linux/amd64` emulation. For better performance, select Apple Virtualization Framework and enable Rosetta for x86/amd64 in Docker Desktop; Docker VMM currently lacks Rosetta support. Emulation is slower and depends on Docker/macOS compatibility. See [Docker's VMM documentation](https://docs.docker.com/desktop/features/vmm/) and the [official BDS download/system requirements](https://www.minecraft.net/en-us/download/server/bedrock).

The initial start needs Internet access for the Docker image and Mojang download. Creating a Minecraft server accepts the [Minecraft EULA](https://www.minecraft.net/eula). Later starts reuse the installed BDS binary instead of silently upgrading a world. A requested exact BDS version must still be available upstream; download failures appear in Console. Bedrock does not use Java. WaterdogPE, if used for remote connections, has its own Java requirement.

## First-time connection setup (Java and Palworld)

For Java and Palworld, before creating the first server, BlockOps opens a guided connection setup:

1. On Windows, choose **Install Playit**. BlockOps downloads the signed official portable agent. On macOS, use the official Playit download link, install the app, and return to BlockOps.
2. Choose **Connect Account**, open the one-time **Claim Playit Agent** link, and approve this computer on playit.gg. Account credentials are entered only on Playit's site.
3. Open **Playit Tunnels**, create a **Minecraft Java** tunnel targeting `127.0.0.1:25565` for Minecraft or a **UDP** tunnel targeting `127.0.0.1:8211` for Palworld, and confirm that step in BlockOps.
4. Choose **Create My First Server**, select the game, and choose RAM limits. Minecraft validates the version and downloads the correct Java runtime. Palworld downloads its official Dedicated Server package through SteamCMD directly on Windows or through Docker Desktop on macOS. Forge requires an exact Forge version; Fabric can select the latest stable loader automatically.

The **Setup Guide** reports installation, account, and tunnel status separately and provides recovery guidance when a download, firewall, antivirus, permission, or agent-start problem occurs. Existing installations with servers are never blocked by this newly introduced first-run gate.

## Create a Bedrock instance

1. Open **Create New Server**, choose **Minecraft**, then **Bedrock Edition**. If the first-run Playit guide opens, close it; Bedrock can be created and started locally without a Playit account.
2. Give the instance a unique name. Choose `LATEST` for the current stable BDS on its first start, or an exact four-part BDS version. Choose a memory limit that fits Docker's allocation.
3. For a client on this computer, keep advertised address `127.0.0.1`. For LAN clients, enter this computer's LAN IPv4 address, such as `192.168.1.10`. Choose a TCP signaling port (default **19132**) and UDP gameplay port (default **19140**).
4. Choose **Create & Install**, select that instance in the sidebar, and **Start Server**. The first start pulls the container and downloads/extracts BDS; watch Console for progress and content errors.
5. Under **World Settings**, configure game mode, difficulty, maximum players, allowlist, cheats, resource-pack requirement, RAM, and networking while the instance is stopped. When allowlist is enabled, add player entries to `allowlist.json` using Config Files or the Bedrock console's allowlist commands.

Every instance has its own profile directory, Docker name, `server.properties`, installed packs, `worlds/<level-name>/` LevelDB database, logs, and backups. The whole profile directory is bind-mounted at `/data`, so removing/recreating the container or restarting Docker does not erase it. Do not delete the profile directory. BlockOps retains its existing **one active game instance at a time** model; stop one instance before starting another. Java and Palworld keep their existing loaders and runtimes.

### Connect to Bedrock: current transport

Networking was checked against upstream documentation on **November 7, 2026**. Current BDS uses **NetherNet**, with TCP signaling and separately negotiated UDP gameplay. The implementation publishes a single fixed UDP gameplay port, which current BDS can share between clients. It advertises the configured host IPv4 and UDP port instead of the container's private IP. The old RakNet UDP-only 19132/19133 setup and Java's TCP 25565 tunnel do not cover this configuration. See [Mojang's protocol guide](https://mojang.github.io/bedrock-protocol-docs/guides/nether-net-onboarding-guide/) and the [runtime's networking guidance](https://github.com/itzg/docker-minecraft-bedrock-server#nethernet).

For **local/LAN play**:

1. Stop the instance. In **World Settings**, use **This computer / local proxy only** and advertised `127.0.0.1` for a client on the host. For other computers, choose **LAN / Internet (all host interfaces)** and advertise the host's LAN IPv4. Save, then start.
2. Allow the selected TCP signaling and UDP gameplay ports through the host firewall. LAN clients must be able to reach both. Router forwarding is unnecessary on the same LAN.
3. In a compatible Bedrock client, choose **Play → Servers → Add Server**, enter the host address and the **TCP signaling port**. A client on another device must use the host's LAN IPv4, not `127.0.0.1`.
4. Docker broadcast discovery is not guaranteed; add the server manually. Some console editions do not expose custom **Add Server**. On Windows, same-computer Minecraft clients may require the Minecraft package's loopback exemption; use the package-specific instructions in the BDS guide shipped in the profile, rather than disabling the firewall globally.
5. From another LAN computer, `curl http://HOST_IP:TCP_PORT/v1/join` checks signaling reachability. It does **not** verify UDP negotiation, authentication, or actual gameplay. Likewise, BlockOps **Online** means local BDS readiness only.

For **direct Internet access**, advertise your public IPv4 and forward the selected TCP and UDP ports to the host's LAN IPv4. Keep Xbox authentication enabled, configure firewall access, and test from a different network. This IPv4 configuration does not implement an IPv6-only deployment. Public addressing may need different settings when switching back to LAN clients.

### Connect remotely through Playit (manual)

Follow the current [official Playit NetherNet guide](https://playit.gg/support/minecraft-bedrock-nethernet/), including the tunnel's **Configuration help → WaterdogPE** settings. Upstream currently requires a Playit agent **1.1.0+**, **WaterdogPE**, and **Java 17+**, alongside BDS. Create **Minecraft Bedrock (NetherNet)**, including its experimental acknowledgement; an old **Minecraft Bedrock** UDP tunnel is insufficient.

Keep the proxy files separate from each server profile. Apply the guide's backend settings while stopped, using BlockOps **World Settings** for its TCP port (the guide uses **19142**) and **Config Files** for other properties. BlockOps controls `server-port`, `server-udp-ports`, `transport=nethernet`, and Docker's internal `server-ip=0.0.0.0`. Its **Host network access** must be **This computer / local proxy only** before disabling backend authentication as directed by the proxy guide. Copying a native server's `server-ip=127.0.0.1` literally into a container would break Docker forwarding.

Configure WaterdogPE's backend and private tunnel token from Configuration help. Run WaterdogPE and the current agent separately and verify proxy registration. Players use the assigned tunnel address and the guide's client port (**19132**). BlockOps does not install/manage this proxy, update its existing Java/Palworld Playit agent, create this tunnel, or certify remote connectivity. Never publish proxy tokens in Git or chat.

## Install and manage Bedrock add-ons on a chosen instance

1. **Find a compatible pack.** For downloadable examples, browse [FoxyNoTail's Add-ons](https://foxynotail.com/add-ons) or the [Bedrock Tweaks resource-pack selector](https://bedrocktweaks.net/resource-packs). On Bedrock Tweaks, select the tweaks, enter a pack name and choose Download. On an author's pack page, read its supported versions and requirements before downloading. These are discovery examples, not a guarantee that every listed pack works on every BDS release. Start with an author's official add-on page and check that it explicitly supports **Bedrock Dedicated Server**, your server/client version, and any required experimental features or script APIs. [Minecraft Creator documentation](https://learn.microsoft.com/en-us/minecraft/creator/documents/behaviorpack?view=minecraft-bedrock-stable) explains behavior/resource packs; [Mojang's samples](https://github.com/Mojang/bedrock-samples) provide inspectable source examples, which may need packaging rather than importing the repository ZIP directly.
2. **Download the actual file.** Use the author's release/download link for `.mcpack` or `.mcaddon` and retain the file in Downloads. If the author distributes a wrapper ZIP containing a readme and `.mcpack`/`.mcaddon`, extract that ZIP first and import the enclosed pack file. A `.mcaddon` often bundles a behavior pack and its resource pack, sometimes as nested `.mcpack` files. An HTML download page, installer, `.jar`, `.mcworld`, repository ZIP, or encrypted Marketplace asset is not an importable add-on. Do not rename those formats to bypass validation.
3. **Choose the instance first.** Click its exact name in the BlockOps sidebar. Open **Backups → Create Backup** before changing an existing world, then **Stop Safely**. Wait for **Offline**. Bedrock backups include installed pack assets and world activation files.
4. **Import.** Open **Add-ons**. Its subtitle identifies the selected instance and ID. Choose **Import Add-on** or drag `.mcpack`/`.mcaddon` files into the drop area. The limit is 256 MB per archive and 1 GB expanded across nested bundles. The importer validates paths, links, manifests, UUIDs and versions before committing any pack. Imported packs begin **Inactive**.
5. **Activate.** Review the detected **Behavior** and **Resource** rows, names, UUIDs, versions and minimum engine requirements. Click **Enable** on the behavior pack. BlockOps also enables its installed UUID dependencies, such as its paired resource pack, and writes the world's separate `world_behavior_packs.json` and `world_resource_packs.json`. If the author omitted dependency links, enable each required pack separately. Missing or mismatched UUID dependencies produce an error without changing activation files. Script API and game-version compatibility are ultimately validated by BDS; import success alone does not establish them.
6. **Test.** Start this instance, review Console/content errors, join with a matching Bedrock client, accept the resource download if offered, and test the author's feature. **Require resource packs** in World Settings makes client acceptance mandatory. Client visuals and gameplay must be tested with a real client.
7. **Manage later.** Stop the same instance, then use **Disable**, **Enable**, or **Remove**. Active dependent packs prevent disabling/removing a required pack; disable dependents first. Disabling a behavior pack leaves its separately active resource pack enabled until you disable it too. Changes apply on next start. BlockOps manages UUID-named imported pack folders and leaves Mojang's bundled packs and externally managed references alone.
8. **Update or recover.** Back up first, disable affected dependents, remove the old pack, import the author's compatible new file, and enable it again. Duplicate UUIDs never silently overwrite installed content. Removing a pack can affect blocks/entities/items already saved in a world; restore your pre-change BlockOps backup to recover the world **and** its matching pack files. Do not edit a running LevelDB world or copy Java world data into Bedrock.

Java mods are not Bedrock add-ons. Marketplace purchases may be encrypted, licensed for a particular world, or unavailable as server-compatible files; this feature does not unlock them or promise universal compatibility. Packs requiring experimental world flags need an appropriately prepared Bedrock world; BlockOps does not edit binary `level.dat` flags automatically.

## Terminal tools

The graphical dashboard is recommended, but direct commands are available:

| Task | macOS | Windows |
| --- | --- | --- |
| Open manager | `./server-manager` | `server-manager.bat` |
| Create server | `./server-manager create instance --name Survival --minecraft 1.21.8 --loader fabric` | `server-manager.bat create instance --name Survival --minecraft 1.21.8 --loader fabric` |
| Create Palworld server | `./server-manager create instance --game palworld --name Pals --max-ram 16G` | `server-manager.bat create instance --game palworld --name Pals --max-ram 16G` |
| Create Bedrock server | `./server-manager create instance --game minecraft --edition bedrock --name Bedrock --max-ram 4G --bedrock-address 192.168.1.10` | `server-manager.bat create instance --game minecraft --edition bedrock --name Bedrock --max-ram 4G --bedrock-address 192.168.1.10` |
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

You can also drag a BlockOps `.tar.gz` world backup onto the Backups screen. BlockOps uploads and validates it first, then asks whether to replace the server's current progress. Applying any backup creates a fresh safety snapshot, safely stops a running server, restores with rollback on failure, and restarts it. Java live snapshots use `save-off` and `save-all flush`, then restore automatic saving. Bedrock uses a graceful `stop`, waits for a clean container exit, archives its LevelDB world and both installed pack directories, then restarts if it was running. Scheduled and manual Bedrock backups briefly disconnect players. No Java save commands are sent to Bedrock. Palworld still requires an offline backup.

The Java stop action sends `save-all flush` and `stop`, waiting up to 125 seconds. Bedrock sends `stop` and waits for the container to finish saving; a timeout leaves the container intact and reports an error. Its Stop action can also recover a running container after the runner exits unexpectedly.

## Developer verification

BlockOps uses only the Python standard library. With Python 3.10+ installed:

```sh
python3 -m unittest discover -s tests -t . -v
```

GitHub Actions runs the same tests on Windows and macOS for every push and pull request. Network access is required only for initial setup and first-time downloads of Minecraft, Java, loaders, and Playit.

An optional real Docker integration test creates disposable profiles on unused high ports. It uses `BLOCKOPS_DATA_DIR` to isolate registry, state, Playit credentials and worlds from your normal installation. Do not point that variable at an existing profile directory:

```sh
# macOS / Linux shell
BLOCKOPS_TEST_DOCKER=1 python3 -m unittest tests.test_bedrock.BedrockDockerIntegrationTests -v
```

On Windows PowerShell, set `$env:BLOCKOPS_TEST_DOCKER="1"` first, run the same `python -m unittest ...` command, and remove the environment variable afterward. Docker Desktop must be running. The test downloads official BDS and verifies console commands, a persisted scoreboard across restarts, a loaded behavior-pack function, paired resource activation files, a runner-managed backup/restart, and restoration of world plus packs. It never uses the normal server registry, ports or Playit tunnel. The default suite skips this opt-in test.

Validation on November 7, 2026 used Docker Desktop on Apple Silicon with amd64 emulation and official BDS **1.26.63.1**. This verifies more than container startup, but **does not verify a player login, UDP gameplay from another device, resource-pack rendering/download on a client, or a Playit/WaterdogPE tunnel**. Windows behavior is covered by portable/unit tests; no real Windows Docker or client run was available in this session.

Downloads are obtained from the official Astral, Mojang, Fabric, Forge, Eclipse Adoptium, Azul, Valve SteamCMD, and `playit-cloud/playit-agent` endpoints. The macOS Palworld runtime image is built from the open-source `thijsvanloef/palworld-server-docker` project because Pocketpair does not publish a native macOS server binary.
