#!/usr/bin/env python3
"""Cross-platform game-server profile and playit.gg manager."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import ipaddress
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import bedrock_support as bedrock
import app_session

if os.name == "nt":
    import msvcrt
else:
    import curses
    import fcntl

ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("BLOCKOPS_DATA_DIR", str(ROOT))).expanduser().resolve()
REGISTRY = DATA_ROOT / "server-profiles.json"
EXAMPLE_REGISTRY = ROOT / "server-profiles.example.json"
PROFILES = DATA_ROOT / "profiles"
CACHE = ROOT / "install-cache"
RUNTIMES = ROOT / "runtimes" / "java"
STATE = DATA_ROOT / ".manager-state"
MANAGER_LOCK = STATE / "manager.lock"
SERVER_PID = STATE / "server.pid"
PLAYIT_PID = STATE / "playit.pid"
PLAYIT_LOG = STATE / "playit.log"
PLAYIT_INVALID = STATE / "playit.invalid"
PLAYIT_CONFIG = DATA_ROOT / "playit.toml"
def playit_socket_path(state: Path) -> Path:
    path = state / "playit.sock"
    if os.name != "nt" and len(str(path).encode()) > 90:
        # macOS Unix sockets have a 104-byte pathname limit. Long installation
        # paths and isolated test directories need a short, installation-unique name.
        identity = hashlib.sha256(str(state.resolve()).encode()).hexdigest()[:12]
        return Path(tempfile.gettempdir()) / f"bo-{os.getuid()}-{identity}.sock"
    return path


PLAYIT_SOCKET = playit_socket_path(STATE)
PLAYIT_CLAIM_CODE = STATE / "playit-claim-code"
PLAYIT_PORTABLE_URL = (
    "https://github.com/playit-cloud/playit-agent/releases/download/"
    "v0.17.1/playit-windows-x86_64-signed.exe"
)
BEDROCK_PORT = 19132
BEDROCK_DOCKER_IMAGE = "itzg/minecraft-bedrock-server@sha256:0d8799379a492bd636037587298fa8a77ee409264fd77a3c68dba55104dbcca0"
DEFAULT_PORT = 25565
PALWORLD_PORT = 8211
PALWORLD_QUERY_PORT = 27016
PALWORLD_APP_ID = "2394010"
STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
STEAMCMD_MAC_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_osx.tar.gz"
PALWORLD_DOCKER_IMAGE = (
    "thijsvanloef/palworld-server-docker@"
    "sha256:be3ad49e373045a7b60478fd8b7f7411c1e293713dfa4733e563e798f276d688"
)
IS_WINDOWS = os.name == "nt"
IS_MAC = platform.system() == "Darwin"
# Legacy fallbacks are intentionally unchanged so profiles created before
# backupSettings was persisted keep their existing effective behavior.
BACKUP_INTERVAL_SECONDS = 10 * 60
BACKUP_RETENTION = 12
NEW_PROFILE_BACKUP_INTERVAL_MINUTES = 30
NEW_PROFILE_BACKUP_RETENTION = 10
PLAYER_LIST_PATTERNS = (
    re.compile(r"There are (\d+) of a max of \d+ players online:", re.IGNORECASE),
    re.compile(r"There are (\d+)/\d+ players online:", re.IGNORECASE),
    re.compile(r"There are (\d+) out of maximum \d+ players online\.", re.IGNORECASE),
)


ManagerError = bedrock.BedrockError


def detached_process_options() -> dict:
    """Return Popen flags that detach a long-running child on this platform."""
    if IS_WINDOWS:
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        return {"creationflags": new_group | no_window}
    return {"start_new_session": True}


@contextlib.contextmanager
def manager_lock():
    """Hold the single-operation lock using the native OS locking primitive."""
    STATE.mkdir(parents=True, exist_ok=True)
    with MANAGER_LOCK.open("a+b") as lock:
        try:
            if IS_WINDOWS:
                lock.seek(0)
                if lock.read(1) == b"":
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            raise ManagerError(
                "Server Manager is already open elsewhere. Close the other window or wait for its current action, then retry."
            ) from None
        try:
            yield
        finally:
            if IS_WINDOWS:
                lock.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def open_in_file_manager(path: Path) -> None:
    if IS_WINDOWS:
        os.startfile(path)  # type: ignore[attr-defined]
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def backup_settings(profile: dict) -> dict:
    """Return validated per-profile backup settings with safe defaults."""
    values = profile.get("backupSettings", {})
    if not isinstance(values, dict):
        values = {}
    return {
        "enabled": bool(values.get("enabled", True)),
        "intervalMinutes": min(1440, max(5, int(values.get("intervalMinutes", BACKUP_INTERVAL_SECONDS // 60)))),
        "retention": min(100, max(1, int(values.get("retention", BACKUP_RETENTION)))),
        "compressionLevel": min(9, max(1, int(values.get("compressionLevel", 6)))),
        "backupOnStop": bool(values.get("backupOnStop", False)),
        "onlyWhenEmpty": bool(values.get("onlyWhenEmpty", False)),
    }


def new_profile_backup_settings() -> dict:
    """Defaults persisted only into profiles created from this version onward."""
    return {
        "enabled": True,
        "intervalMinutes": NEW_PROFILE_BACKUP_INTERVAL_MINUTES,
        "retention": NEW_PROFILE_BACKUP_RETENTION,
        "compressionLevel": 6,
        "backupOnStop": False,
        "onlyWhenEmpty": False,
    }


def load_json(path: Path):
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def registry() -> dict:
    if not REGISTRY.exists():
        save_json(REGISTRY, {"schemaVersion": 2, "profiles": []})
    data = load_json(REGISTRY)
    data.setdefault("profiles", [])
    return data


def absolute_profile_path(profile: dict) -> Path:
    path = Path(profile["path"]).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def profile_by_id(profile_id: str | None, *, prompt: bool = False) -> dict:
    profiles = registry()["profiles"]
    if not profiles:
        raise ManagerError("No profiles exist. Create one with: ./server-manager create")
    if profile_id:
        matches = [p for p in profiles if p["id"] == profile_id or p["name"].lower() == profile_id.lower()]
        if len(matches) != 1:
            raise ManagerError(f"Profile not found: {profile_id}")
        return matches[0]
    if len(profiles) == 1:
        return profiles[0]
    if not prompt or not sys.stdin.isatty():
        names = ", ".join(p["id"] for p in profiles)
        raise ManagerError(f"Choose a profile with --profile. Available: {names}")
    for number, item in enumerate(profiles, 1):
        label = "Palworld" if profile_game(item) == "palworld" else f"{item.get('minecraftVersion', 'Bedrock')} {item.get('loader', '')}"
        print(f"[{number}] {item['name']} ({label})")
    try:
        return profiles[int(input("Profile: ").strip()) - 1]
    except (ValueError, IndexError):
        raise ManagerError("Invalid profile selection.") from None


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def profile_game(profile: dict) -> str:
    """Return the supported game type, keeping profiles made before game types Minecraft."""
    game = str(profile.get("game", "minecraft")).lower()
    if game not in {"minecraft", "bedrock", "palworld"}:
        raise ManagerError(f"Unsupported game type in profile: {game}")
    return game


def download(url: str, destination: Path, *, overwrite: bool = False) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite and destination.exists() and destination.stat().st_size:
        return
    print(f"Downloading {destination.name} …")
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "BlockOps/1"})
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=90) as response, temporary.open("wb") as output:
                    shutil.copyfileobj(response, output)
                last_error = None
                break
            except (OSError, urllib.error.URLError) as error:
                last_error = error
                temporary.unlink(missing_ok=True)
                if attempt < 2:
                    time.sleep(attempt + 1)
        if last_error:
            raise ManagerError(f"Download failed after three attempts: {url}\n{last_error}") from last_error
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def api_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "BlockOps/1"})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(attempt + 1)
    raise ManagerError(f"Metadata request failed after three attempts: {url}\n{last_error}") from last_error


def minecraft_details(version: str) -> dict:
    manifest_cache = CACHE / "version_manifest_v2.json"
    refresh = not manifest_cache.exists() or time.time() - manifest_cache.stat().st_mtime > 3600
    if refresh:
        manifest_cache.parent.mkdir(parents=True, exist_ok=True)
        data = api_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
        save_json(manifest_cache, data)
    else:
        data = load_json(manifest_cache)
    item = next((item for item in data["versions"] if item["id"] == version), None)
    if not item:
        raise ManagerError(f"Minecraft version does not exist: {version}")
    details_cache = CACHE / "versions" / version / "metadata.json"
    if not details_cache.exists():
        save_json(details_cache, api_json(item["url"]))
    return load_json(details_cache)


def required_java(version: str) -> int:
    details = minecraft_details(version)
    component = details.get("javaVersion", {}).get("majorVersion")
    if component:
        return int(component)
    match = re.match(r"1\.(\d+)(?:\.(\d+))?", version)
    if not match:
        return 21
    minor, patch = int(match.group(1)), int(match.group(2) or 0)
    if minor <= 16:
        return 8
    if minor == 17:
        return 16
    if minor <= 19 or (minor == 20 and patch <= 4):
        return 17
    return 21


def java_major(java: Path) -> int | None:
    try:
        output = subprocess.run([java, "-version"], text=True, capture_output=True, timeout=10).stderr
        match = re.search(r'version "(?:1\.)?(\d+)', output)
        return int(match.group(1)) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def resolve_java(major: int) -> Path:
    candidates: list[Path] = []
    java_home = Path("/usr/libexec/java_home")
    if java_home.exists():
        result = subprocess.run([java_home, "-v", str(major)], text=True, capture_output=True)
        if result.returncode == 0:
            candidates.append(Path(result.stdout.strip()) / "bin" / "java")
    candidates.extend(RUNTIMES.glob(f"temurin-{major}/**/bin/{'java.exe' if IS_WINDOWS else 'java'}"))
    path_java = shutil.which("java")
    if path_java:
        candidates.append(Path(path_java))
    for candidate in candidates:
        if java_major(candidate) == major:
            return candidate.resolve()

    machine = platform.machine().lower()
    architecture = "aarch64" if machine in {"arm64", "aarch64"} else "x64"
    operating_system = "windows" if IS_WINDOWS else "mac"
    vendor = "temurin"
    url = f"https://api.adoptium.net/v3/binary/latest/{major}/ga/{operating_system}/{architecture}/jre/hotspot/normal/eclipse"
    # Eclipse Adoptium does not publish Java 8 for Apple Silicon. Azul does,
    # so use its current native ARM JDK rather than requiring Rosetta/x86 Java.
    if major == 8 and architecture == "aarch64" and not IS_WINDOWS:
        vendor = "zulu"
        packages = api_json(
            "https://api.azul.com/metadata/v1/zulu/packages/"
            "?java_version=8&os=macos&arch=arm&archive_type=tar.gz"
            "&java_package_type=jdk&latest=true&release_status=ga&availability_types=CA"
        )
        package = next((item for item in packages if "-fx-" not in item.get("name", "")), None)
        if not package:
            raise ManagerError("Azul did not return a native Apple Silicon Java 8 package.")
        url = package["download_url"]
    runtime = RUNTIMES / f"{vendor}-{major}"
    archive = RUNTIMES / f"{vendor}-{major}-{operating_system}-{architecture}{'.zip' if IS_WINDOWS else '.tar.gz'}"
    print(f"Java {major} is missing; installing a private {vendor.title()} runtime …")
    download(url, archive)
    if runtime.exists():
        shutil.rmtree(runtime)
    runtime.mkdir(parents=True)
    if IS_WINDOWS:
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                if not (runtime / member.filename).resolve().is_relative_to(runtime.resolve()):
                    raise ManagerError("The downloaded Java archive contains an unsafe path.")
            package.extractall(runtime)
    else:
        with tarfile.open(archive, "r:gz") as package:
            for member in package.getmembers():
                if not (runtime / member.name).resolve().is_relative_to(runtime.resolve()):
                    raise ManagerError("The downloaded Java archive contains an unsafe path.")
            package.extractall(runtime)
    matches = list(runtime.glob("**/bin/java.exe" if IS_WINDOWS else "**/bin/java"))
    if not matches or java_major(matches[0]) != major:
        raise ManagerError(f"Could not verify the downloaded Java {major} runtime.")
    return matches[0].resolve()


def link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def install_vanilla(folder: Path, version: str) -> tuple[str, str]:
    details = minecraft_details(version)
    server = details.get("downloads", {}).get("server")
    if not server:
        raise ManagerError(f"Mojang does not publish a server JAR for {version}.")
    cached = CACHE / "versions" / version / "vanilla" / "server.jar"
    download(server["url"], cached)
    if server.get("sha1") and hashlib.sha1(cached.read_bytes()).hexdigest() != server["sha1"]:
        cached.unlink(missing_ok=True)
        raise ManagerError("The downloaded Minecraft server JAR failed its SHA-1 check; retry creation.")
    link_or_copy(cached, folder / "server.jar")
    return "server.jar", ""


def install_fabric(folder: Path, version: str, loader_version: str | None) -> tuple[str, str]:
    choices = api_json(f"https://meta.fabricmc.net/v2/versions/loader/{version}")
    if not loader_version:
        choice = next((item for item in choices if item["loader"].get("stable")), None)
        if not choice:
            raise ManagerError(f"No stable Fabric loader supports Minecraft {version}.")
        loader_version = choice["loader"]["version"]
    elif not any(item["loader"]["version"] == loader_version for item in choices):
        raise ManagerError(f"Fabric loader {loader_version} does not support Minecraft {version}.")
    installers = api_json("https://meta.fabricmc.net/v2/versions/installer")
    installer = next((item["version"] for item in installers if item.get("stable")), installers[0]["version"])
    cached = CACHE / "versions" / version / f"fabric-{loader_version}" / "fabric-server-launch.jar"
    download(f"https://meta.fabricmc.net/v2/versions/loader/{version}/{loader_version}/{installer}/server/jar", cached)
    link_or_copy(cached, folder / cached.name)
    return cached.name, loader_version


def copy_tree_links(source: Path, destination: Path) -> None:
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            link_or_copy(item, target)


def install_forge(folder: Path, version: str, loader_version: str, java: Path) -> tuple[str, str]:
    if not loader_version:
        raise ManagerError("Forge requires an exact loader version (for example 14.23.5.2860).")
    template = CACHE / "versions" / version / f"forge-{loader_version}" / "server"
    if not template.exists():
        template.parent.mkdir(parents=True, exist_ok=True)
        installer_name = f"forge-{version}-{loader_version}-installer.jar"
        installer = template.parent / installer_name
        download(f"https://maven.minecraftforge.net/net/minecraftforge/forge/{version}-{loader_version}/{installer_name}", installer)
        staging = Path(tempfile.mkdtemp(prefix="forge-", dir=template.parent))
        try:
            result = subprocess.run([java, "-jar", installer, "--installServer", staging])
            if result.returncode:
                raise ManagerError("Forge installer failed.")
            staging.replace(template)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    copy_tree_links(template, folder)
    legacy = list(folder.glob(f"forge-{version}-{loader_version}*.jar"))
    if legacy:
        return legacy[0].name, loader_version
    unix_args = folder / "libraries" / "net" / "minecraftforge" / "forge" / f"{version}-{loader_version}" / "unix_args.txt"
    platform_args = unix_args.with_name("win_args.txt") if IS_WINDOWS else unix_args
    if platform_args.exists():
        return f"@{platform_args.relative_to(folder)}", loader_version
    raise ManagerError("Forge installed, but its launcher was not found.")


def steamcmd_executable() -> Path:
    """Install SteamCMD privately and return its verified Windows executable."""
    if not IS_WINDOWS:
        raise ManagerError("Palworld dedicated servers are currently supported by BlockOps on Windows only.")
    folder = ROOT / "runtimes" / "steamcmd"
    executable = folder / "steamcmd.exe"
    if executable.is_file():
        return executable
    archive = folder / "steamcmd.zip"
    download(STEAMCMD_URL, archive)
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            if not (folder / member.filename).resolve().is_relative_to(folder.resolve()):
                raise ManagerError("The SteamCMD archive contains an unsafe path.")
        package.extractall(folder)
    if not executable.is_file():
        raise ManagerError("SteamCMD did not provide steamcmd.exe. Retry creating the Palworld server.")
    return executable


def docker_executable_candidates() -> list[Path]:
    """Known macOS Docker locations missing from Finder's minimal PATH."""
    if not IS_MAC:
        return []
    return [
        Path.home() / ".docker" / "bin" / "docker",
        Path("/Applications/Docker.app/Contents/Resources/bin/docker"),
        Path.home() / "Applications" / "Docker.app" / "Contents" / "Resources" / "bin" / "docker",
        Path("/usr/local/bin/docker"),
        Path("/opt/homebrew/bin/docker"),
    ]


def find_docker_executable() -> str | None:
    """Resolve the CLI without changing PATH or depending on shell startup files."""
    executable = shutil.which("docker")
    if executable:
        return executable
    for candidate in docker_executable_candidates():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def docker_executable() -> Path:
    """Return a working Docker CLI for a Linux dedicated-server runtime."""
    executable = find_docker_executable()
    if not executable:
        raise ManagerError(
            "The Docker command could not be found. Install Docker Desktop, then reopen BlockOps and retry."
        )
    result = subprocess.run(
        [executable, "info", "--format", "{{.ServerVersion}}"],
        text=True, encoding="utf-8", errors="replace", capture_output=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f" ({detail[-1]})" if detail else ""
        raise ManagerError(f"Docker Desktop is installed but is not running or accessible{suffix}.")
    return Path(executable)


def palworld_container_name(profile: dict) -> str:
    """Return a stable Docker-safe name scoped to this BlockOps profile."""
    identifier = slug(str(profile.get("id") or absolute_profile_path(profile).name)) or "server"
    return f"blockops-palworld-{identifier}"[:63]


def palworld_docker_platform() -> str:
    return "linux/arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "linux/amd64"


def mac_steamcmd_executable() -> Path:
    """Install Valve's native macOS SteamCMD privately for Linux depot downloads."""
    runtime = ROOT / "runtimes" / "steamcmd-mac"
    executable = runtime / "steamcmd.sh"
    if executable.is_file():
        return executable
    archive = runtime / "steamcmd_osx.tar.gz"
    download(STEAMCMD_MAC_URL, archive)
    with tarfile.open(archive, "r:gz") as package:
        base = runtime.resolve()
        for member in package.getmembers():
            target = (runtime / member.name).resolve()
            safe_kind = member.isfile() or member.isdir()
            if member.issym() and not Path(member.linkname).is_absolute():
                link_target = (runtime / member.name).parent.joinpath(member.linkname).resolve()
                safe_kind = link_target.is_relative_to(base)
            if not target.is_relative_to(base) or not safe_kind:
                raise ManagerError("The macOS SteamCMD archive contains an unsafe path.")
        if sys.version_info >= (3, 12):
            package.extractall(runtime, filter="fully_trusted")
        else:
            package.extractall(runtime)
    if not executable.is_file():
        raise ManagerError("SteamCMD did not provide its macOS launcher. Retry creating the Palworld server.")
    executable.chmod(executable.stat().st_mode | 0o111)
    return executable


def run_palworld_steamcmd(folder: Path) -> Path:
    """Install the official Linux Palworld depot using native macOS SteamCMD."""
    # Fail before the 5+ GB game download if the required runtime is unavailable.
    docker_executable()
    executable = mac_steamcmd_executable()
    folder.mkdir(parents=True, exist_ok=True)
    def run_steamcmd(arguments: list[str]) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [executable, *arguments], cwd=executable.parent,
            text=True, encoding="utf-8", errors="replace", capture_output=True,
        )
        output = f"{result.stdout}\n{result.stderr}".strip()
        if output:
            print(output)
        return result

    platform_args = ["+@sSteamCmdForcePlatformType", "linux"]
    run_steamcmd([*platform_args, "+login", "anonymous", "+quit"])
    command = [
        *platform_args, "+force_install_dir", str(folder.resolve()),
        "+login", "anonymous", "+app_update", PALWORLD_APP_ID, "validate", "+quit",
    ]
    result = run_steamcmd(command)
    output = f"{result.stdout}\n{result.stderr}".strip()
    server = folder / "PalServer.sh"
    if "Missing configuration" in output and not server.is_file():
        print("SteamCMD is finishing its initial configuration; retrying Palworld download …")
        time.sleep(3)
        result = run_steamcmd(command)
        output = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode or not server.is_file():
        raise ManagerError(
            "SteamCMD could not install the Palworld Linux dedicated server. "
            "Review the creation log and confirm Docker Desktop has enough disk space."
        )
    server.chmod(server.stat().st_mode | 0o111)
    return server


def install_palworld(folder: Path) -> Path:
    """Install Palworld's anonymous SteamCMD dedicated-server package."""
    if IS_MAC:
        return run_palworld_steamcmd(folder)
    executable = steamcmd_executable()
    server = folder / "PalServer.exe"
    def run_steamcmd(arguments: list[str]) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [executable, *arguments], cwd=executable.parent,
            text=True, encoding="utf-8", errors="replace", capture_output=True,
        )
        output = f"{result.stdout}\n{result.stderr}".strip()
        if output:
            print(output)
        return result

    # A newly downloaded SteamCMD updates and restarts itself before its app
    # configuration is ready. Logging in once first avoids a transient
    # "Missing configuration" response for the initial app_update request.
    run_steamcmd(["+login", "anonymous", "+quit"])
    command = ["+force_install_dir", str(folder), "+login", "anonymous", "+app_update", PALWORLD_APP_ID, "validate", "+quit"]
    result = run_steamcmd(command)
    output = f"{result.stdout}\n{result.stderr}"
    if "Missing configuration" in output and not server.is_file():
        print("SteamCMD is finishing its initial configuration; retrying Palworld download …")
        time.sleep(3)
        result = run_steamcmd(command)
        output = f"{result.stdout}\n{result.stderr}"
    if result.returncode or not server.is_file():
        detail = "SteamCMD did not produce PalServer.exe."
        if "Missing configuration" in output:
            detail = "SteamCMD is still initializing its account configuration; try again in a minute."
        raise ManagerError(f"{detail} See the creation log above for SteamCMD details.")
    return server


def make_palworld_profile(args) -> dict:
    name = args.name or input("Profile name: ").strip()
    profile_id = args.id or slug(name)
    if not profile_id:
        raise ManagerError("The profile name must contain letters or digits.")
    if not re.fullmatch(r"[1-9][0-9]*[MG]", args.min_ram, re.IGNORECASE) or not re.fullmatch(r"[1-9][0-9]*[MG]", args.max_ram, re.IGNORECASE):
        raise ManagerError("RAM values must look like 2G or 4096M.")
    data = registry()
    if any(item["id"] == profile_id for item in data["profiles"]):
        raise ManagerError(f"A profile named {profile_id} already exists.")
    folder = Path(args.path).expanduser().resolve() if args.path else PROFILES / profile_id
    if folder.exists() and any(folder.iterdir()):
        raise ManagerError(f"Profile folder is not empty: {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    try:
        install_palworld(folder)
        default_settings = folder / "DefaultPalWorldSettings.ini"
        settings_platform = "WindowsServer" if IS_WINDOWS else "LinuxServer"
        settings_path = folder / "Pal" / "Saved" / "Config" / settings_platform / "PalWorldSettings.ini"
        if not default_settings.is_file():
            raise ManagerError("Palworld installed but did not include its default server settings file.")
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(default_settings, settings_path)
        (folder / "Pal" / "Saved" / "SaveGames" / "0").mkdir(parents=True, exist_ok=True)
        profile = {
            "id": profile_id, "name": name, "game": "palworld",
            "path": str(folder.relative_to(ROOT) if folder.is_relative_to(ROOT) else folder),
            "minimumRam": args.min_ram, "maximumRam": args.max_ram,
            "launchExecutable": "PalServer.exe" if IS_WINDOWS else "PalServer.sh",
            "palworldRuntime": "windows" if IS_WINDOWS else "docker-linux",
            "containerAdminPassword": secrets.token_urlsafe(24) if not IS_WINDOWS else "",
            "port": PALWORLD_PORT, "queryPort": PALWORLD_QUERY_PORT,
            "backupSettings": new_profile_backup_settings(),
        }
        save_json(folder / "profile.json", profile)
        data["schemaVersion"] = 3
        data["profiles"].append(profile)
        save_json(REGISTRY, data)
        print(f"Created {name}: Palworld Dedicated Server")
        print(f"Files: {folder}")
        return profile
    except Exception:
        if folder.is_relative_to(PROFILES):
            shutil.rmtree(folder, ignore_errors=True)
        raise


def bedrock_container_name(profile: dict) -> str:
    identity = hashlib.sha256(str(absolute_profile_path(profile)).encode()).hexdigest()[:12]
    return f"blockops-bedrock-{slug(profile['id'])[:30]}-{identity}"


def validate_bedrock_network(address: str, port: int, udp_port: int) -> str:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        raise ManagerError("Bedrock advertised address must be an IPv4 address reachable by your players.") from None
    if parsed.version != 4 or parsed.is_unspecified or parsed.is_multicast:
        raise ManagerError("Use a specific IPv4 host address for Bedrock networking.")
    if not 1024 <= port <= 65535 or not 1024 <= udp_port <= 65535:
        raise ManagerError("Bedrock ports must be between 1024 and 65535.")
    return str(parsed)


def make_bedrock_profile(args) -> dict:
    docker_executable()
    name = args.name or input("Profile name: ").strip()
    profile_id = args.id or slug(name)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", profile_id or ""):
        raise ManagerError("Use letters, digits and hyphens for the instance ID (at most 80 characters).")
    maximum = args.max_ram or "4G"
    if not re.fullmatch(r"[1-9][0-9]*[MG]", maximum, re.IGNORECASE):
        raise ManagerError("RAM values must look like 2G or 4096M.")
    address = validate_bedrock_network(args.bedrock_address, args.port, args.udp_port)
    requested_version = args.bedrock_version
    if requested_version != "LATEST" and not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", requested_version):
        raise ManagerError("Use LATEST or an exact Bedrock Dedicated Server version, such as 1.26.52.3.")
    data = registry()
    if any(p["id"] == profile_id for p in data["profiles"]):
        raise ManagerError(f"A profile named {profile_id} already exists.")
    folder = Path(args.path).expanduser().resolve() if args.path else PROFILES / profile_id
    if folder.exists() and any(folder.iterdir()):
        raise ManagerError(f"Profile folder is not empty: {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    profile = {
        "id": profile_id, "name": name, "game": "bedrock", "edition": "bedrock",
        "path": str(folder.relative_to(ROOT) if folder.is_relative_to(ROOT) else folder),
        "minecraftVersion": requested_version, "loader": "bedrock", "bedrockVersion": requested_version,
        "minimumRam": "1G", "maximumRam": maximum.upper(),
        "port": args.port, "bedrockUdpPort": args.udp_port, "bedrockAddress": address,
        "bedrockBindAddress": args.bedrock_bind or ("127.0.0.1" if address == "127.0.0.1" else "0.0.0.0"),
        "backupSettings": new_profile_backup_settings(),
    }
    # BDS itself is downloaded from Mojang at the first Docker start. Later starts
    # use the existing binary, preventing an unattended world format upgrade.
    (folder / "server.properties").write_text(
        "server-name=" + name.replace("\r", " ").replace("\n", " ") +
        "\nlevel-name=Bedrock level\ngamemode=survival\ndifficulty=normal\nmax-players=10\n"
        "online-mode=true\nallow-list=false\nallow-cheats=false\ntexturepack-required=false\n"
        "transport=nethernet\ncontent-log-file-enabled=true\ncontent-log-console-output-enabled=true\n",
        encoding="utf-8",
    )
    for child in (*bedrock.PACK_KINDS, "worlds", "backups"):
        (folder / child).mkdir(exist_ok=True)
    save_json(folder / "profile.json", profile)
    data["schemaVersion"] = 4
    data["profiles"].append(profile)
    save_json(REGISTRY, data)
    print(f"Created {name}: Minecraft Bedrock, Docker linux/amd64. The first start downloads the official server (EULA accepted).")
    print("Apple Silicon uses amd64 emulation; enable Rosetta in Docker Desktop's Apple Virtualization Framework settings.")
    print(f"Advertised address: {address}; TCP {args.port}, UDP {args.udp_port}. Configure managed Playit once in World Settings; see README.")
    return profile


def bedrock_launch_command(profile: dict) -> list[str]:
    folder = absolute_profile_path(profile)
    port, udp = int(profile.get("port", BEDROCK_PORT)), int(profile.get("bedrockUdpPort", 19140))
    address = validate_bedrock_network(profile.get("bedrockAddress", "127.0.0.1"), port, udp)
    binding = profile.get("bedrockBindAddress", "0.0.0.0")
    if binding not in {"0.0.0.0", "127.0.0.1"}:
        raise ManagerError("Choose a loopback or all-interfaces Docker binding.")
    # Preserve installed BDS across restarts. No Java/Box64 server substitution.
    existing = (folder / "bedrock_server").is_file() or any(folder.glob("bedrock_server-[0-9]*"))
    version = "EXISTING" if existing else profile.get("bedrockVersion", "LATEST")
    world = world_path(folder)  # Reject unsafe level-name before mounting writable data.
    return [str(docker_executable()), "run", "--rm", "--interactive", "--platform", "linux/amd64",
            "--name", bedrock_container_name(profile), "--label", "app=blockops-bedrock",
            "--memory", str(profile.get("maximumRam", "4G")).lower(),
            "--publish", f"{binding}:{port}:{port}/tcp", "--publish", f"{binding}:{udp}:{udp}/udp",
            "--volume", f"{folder.resolve()}:/data", "--env", "EULA=TRUE",
            "--env", f"VERSION={version}", "--env", f"SERVER_PORT={port}",
            "--env", f"SERVER_UDP_PORTS={address}:{udp}:{udp}", "--env", "TRANSPORT=nethernet",
            "--env", f"LEVEL_NAME={world.name}", "--env", "SERVER_IP=0.0.0.0",
            BEDROCK_DOCKER_IMAGE]


def bedrock_container_running(profile: dict) -> bool:
    executable = find_docker_executable()
    if not executable:
        raise ManagerError("Docker is unavailable. Cannot establish whether this Bedrock instance is safely stopped.")
    try:
        result = subprocess.run([executable, "inspect", "--format", "{{.State.Running}}", bedrock_container_name(profile)],
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        raise ManagerError(f"Cannot inspect the Bedrock container: {error}") from error
    if result.returncode:
        if "no such" in result.stderr.lower():
            return False
        raise ManagerError("Cannot inspect the Bedrock container. Start Docker Desktop before modifying this instance.")
    return result.stdout.strip() == "true"


def bedrock_ready(profile: dict) -> bool:
    # A listening Docker proxy alone is not evidence of BDS readiness.
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(profile['port'])}/v1/join", timeout=1) as response:
            if response.status != 200:
                return False
            body = response.read()
        # With online-mode=false, the private BDS backend acknowledges the
        # NetherNet probe with HTTP 200 but omits its public advertisement.
        # Docker's port proxy cannot generate this HTTP acknowledgement.
        if not body.strip():
            return (profile.get('bedrockRemote', {}).get('enabled') is True
                    and profile.get('bedrockBindAddress') == '127.0.0.1'
                    and profile.get('bedrockAddress') == '127.0.0.1')
        info = json.loads(body)
        return isinstance(info, dict) and bool(info.get("version"))
    except (OSError, ValueError):
        return False


def bedrock_command(profile: dict, command: str) -> None:
    if not command.strip() or any(c in command for c in "\r\n\x00"):
        raise ManagerError("Enter one Bedrock console command at a time.")
    # send-command from the upstream image searches /proc/*/exe, which points
    # at Rosetta rather than BDS under Apple Silicon emulation. Match the kernel
    # process name instead, passing console text as an argument (never shell code).
    script = """for process in /proc/[0-9]*; do
      name=$(cat "$process/comm" 2>/dev/null) || continue
      case "$name" in bedrock_server*)
        [ "$(readlink "$process/cwd")" = /data ] || continue
        printf '%s\\n' "$1" > "$process/fd/0" && exit 0
      esac
    done
    echo 'Bedrock console is not ready' >&2
    exit 2"""
    try:
        result = subprocess.run([str(docker_executable()), "exec", bedrock_container_name(profile),
                                 "/bin/sh", "-c", script, "blockops-console", command.strip().lstrip("/")],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as error:
        raise ManagerError(f"Bedrock console is unavailable: {error}") from error
    if result.returncode:
        raise ManagerError(f"Bedrock console command failed: {(result.stderr or result.stdout).strip()}")



def assert_bedrock_stopped(profile: dict) -> None:
    active = active_profile()
    if (active and active['id'] == profile['id']) or bedrock_container_running(profile):
        raise ManagerError("Stop this Bedrock instance before importing, activating, removing packs or editing its files.")


def bedrock_offline_backup(profile: dict, *, label="", prune=True) -> Path:
    folder = absolute_profile_path(profile)
    if bedrock_container_running(profile):
        raise ManagerError("A Bedrock backup requires the container to finish saving and stop first.")
    world = world_path(folder)
    if not (world / "level.dat").is_file() or not (world / "db").is_dir():
        raise ManagerError("No Bedrock LevelDB world exists yet. Start and stop this instance first.")
    settings = backup_settings(profile)
    backups = folder / "backups"
    backups.mkdir(exist_ok=True)
    destination = backups / f"{world.name}-{label + '-' if label else ''}{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}.tar.gz"
    temporary = destination.with_suffix(".part")
    try:
        with tarfile.open(temporary, "w:gz", compresslevel=settings['compressionLevel']) as archive:
            def no_links(member):
                if member.issym() or member.islnk() or member.isdev():
                    raise ManagerError("Bedrock backups cannot contain symbolic links or special files.")
                return member
            archive.add(world, arcname=world.name, filter=no_links)
            for kind in bedrock.PACK_KINDS:
                root = bedrock.safe_path(folder, kind)
                if root.exists():
                    archive.add(root, arcname=f"{world.name}/{bedrock.SNAPSHOT_PACKS}/{kind}", filter=no_links)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    if prune:
        completed = sorted(backups.glob(f"{world.name}-*.tar.gz"), key=lambda p: p.stat().st_mtime)
        for expired in completed[:-settings['retention']]:
            expired.unlink(missing_ok=True)
    return destination


def bedrock_runner(profile: dict, folder: Path) -> None:
    """Attached Docker lifecycle; backups restart only after an acknowledged clean exit."""
    control = folder / ".control"
    settings = backup_settings(profile)
    log = folder / "profile-runner.log"
    deadline = time.monotonic() + settings['intervalMinutes'] * 60
    process = None
    try:
        while True:
            # A timeout/stop during a slow Docker pull must survive until BDS starts.
            if (control / "stop.request").exists():
                return
            with log.open("ab", buffering=0) as output:
                process = subprocess.Popen(bedrock_launch_command(profile), cwd=folder,
                                           stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT)
                SERVER_PID.write_text(str(process.pid))
                restart = False
                while process.poll() is None:
                    stop = (control / "stop.request").exists()
                    manual = (control / "backup.request").exists()
                    scheduled = settings['enabled'] and time.monotonic() >= deadline
                    if scheduled and settings['onlyWhenEmpty'] and not stop and not manual:
                        # BDS console logs are the acknowledgement channel for list.
                        offset = log.stat().st_size
                        bedrock_command(profile, "list")
                        time.sleep(1)
                        with log.open('rb') as reader:
                            reader.seek(offset)
                            text = reader.read().decode('utf-8', errors='replace')
                        counts = [m for pattern in PLAYER_LIST_PATTERNS for m in pattern.finditer(text)]
                        if not counts or int(counts[-1].group(1)) > 0:
                            deadline = time.monotonic() + 60
                            scheduled = False
                    if stop or manual or scheduled:
                        # send-command stop saves BDS. Never docker stop/kill on a save timeout.
                        try:
                            bedrock_command(profile, "stop")
                        except ManagerError:
                            # Docker may still be downloading BDS. Keep the stop
                            # request pending until the console becomes available.
                            time.sleep(1)
                            continue
                        try:
                            process.wait(timeout=120)
                        except subprocess.TimeoutExpired:
                            raise ManagerError("Bedrock has not finished saving. Container left untouched; inspect its logs.")
                        if process.returncode != 0 or bedrock_container_running(profile):
                            raise ManagerError("Bedrock did not exit cleanly; snapshot and restart cancelled.")
                        if manual or scheduled or (stop and settings['backupOnStop']):
                            try:
                                backup = bedrock_offline_backup(profile)
                                output.write(f"Bedrock backup created: {backup.name}\n".encode())
                            except (ManagerError, OSError, tarfile.TarError) as error:
                                output.write(f"Bedrock backup failed: {error}\n".encode())
                        (control / "backup.request").unlink(missing_ok=True)
                        if stop:
                            (control / "stop.request").unlink(missing_ok=True)
                            return
                        deadline = time.monotonic() + settings['intervalMinutes'] * 60
                        restart = True
                        break
                    request = control / "command.request"
                    if request.exists():
                        commands = request.read_text(encoding='utf-8').splitlines()
                        request.unlink()
                        for command in commands:
                            try:
                                bedrock_command(profile, command)
                            except (ManagerError, OSError, subprocess.SubprocessError) as error:
                                output.write(f"Console error: {error}\n".encode())
                    time.sleep(0.25)
                if not restart:
                    output.write(b"Bedrock exited; leaving the instance stopped.\n")
                    return
    finally:
        if process is None or process.poll() is not None:
            SERVER_PID.unlink(missing_ok=True)


def make_profile(args) -> dict:
    if getattr(args, "game", "minecraft") == "bedrock" or (getattr(args, "game", "minecraft") == "minecraft" and getattr(args, "edition", "java") == "bedrock"):
        return make_bedrock_profile(args)
    if getattr(args, "game", "minecraft") == "palworld":
        args.min_ram = args.min_ram or "4G"
        args.max_ram = args.max_ram or "16G"
        return make_palworld_profile(args)
    args.min_ram = args.min_ram or "2G"
    args.max_ram = args.max_ram or "6G"
    name = args.name or input("Profile name: ").strip()
    version = args.minecraft or input("Minecraft version: ").strip()
    loader = (args.loader or input("Loader (vanilla/fabric/forge): ").strip()).lower()
    loader_version = args.loader_version
    if loader not in {"vanilla", "fabric", "forge"}:
        raise ManagerError("Loader must be vanilla, fabric, or forge.")
    if loader in {"fabric", "forge"} and loader_version is None and sys.stdin.isatty():
        loader_version = input(f"{loader.title()} loader version (blank = latest stable where supported): ").strip() or None
    profile_id = args.id or slug(name)
    if not profile_id:
        raise ManagerError("The profile name must contain letters or digits.")
    if not re.fullmatch(r"[1-9][0-9]*[MG]", args.min_ram, re.IGNORECASE) or not re.fullmatch(r"[1-9][0-9]*[MG]", args.max_ram, re.IGNORECASE):
        raise ManagerError("RAM values must look like 2G or 4096M.")
    data = registry()
    if any(item["id"] == profile_id for item in data["profiles"]):
        raise ManagerError(f"A profile named {profile_id} already exists.")
    minecraft_details(version)
    major = required_java(version)
    java = resolve_java(major)
    folder = Path(args.path).expanduser().resolve() if args.path else PROFILES / profile_id
    if folder.exists() and any(folder.iterdir()):
        raise ManagerError(f"Profile folder is not empty: {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    for child in ("mods", "config", "backups"):
        (folder / child).mkdir()
    try:
        if loader == "vanilla":
            launch, loader_version = install_vanilla(folder, version)
        elif loader == "fabric":
            launch, loader_version = install_fabric(folder, version, loader_version)
        else:
            launch, loader_version = install_forge(folder, version, loader_version or "", java)
        (folder / "eula.txt").write_text("eula=true\n", encoding="utf-8")
        properties = ["server-port=25565", "server-ip=", "online-mode=true", "gamemode=survival", "difficulty=normal", "hardcore=false", "white-list=false", f"motd={name}"]
        (folder / "server.properties").write_text("\n".join(properties) + "\n", encoding="utf-8")
        profile = {
            "id": profile_id, "name": name, "path": str(folder.relative_to(ROOT) if folder.is_relative_to(ROOT) else folder),
            "game": "minecraft",
            "minecraftVersion": version, "loader": loader, "loaderVersion": loader_version or "",
            "javaMajor": major, "javaPath": str(java), "minimumRam": args.min_ram, "maximumRam": args.max_ram,
            "launchJar": launch, "jvmArguments": [], "serverArguments": ["nogui"], "port": DEFAULT_PORT,
            "backupSettings": new_profile_backup_settings(),
        }
        save_json(folder / "profile.json", profile)
        data["schemaVersion"] = 2
        data["profiles"].append(profile)
        save_json(REGISTRY, data)
        print(f"Created {name}: Minecraft {version}, {loader} {loader_version or ''}".rstrip())
        print(f"Files: {folder}")
        return profile
    except Exception:
        if folder.is_relative_to(PROFILES):
            shutil.rmtree(folder, ignore_errors=True)
        raise


def is_alive(pid: int) -> bool:
    if not IS_WINDOWS:
        try:
            reaped, _ = os.waitpid(pid, os.WNOHANG)
            if reaped == pid:
                return False
        except ChildProcessError:
            pass
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def read_pid(path: Path) -> int | None:
    try:
        pid = int(path.read_text().strip())
        return pid if is_alive(pid) else None
    except (OSError, ValueError):
        return None


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def game_port_in_use(profile: dict) -> bool:
    """Check whether the ports needed by a profile are bound."""
    port = int(profile.get("port", DEFAULT_PORT))
    if profile_game(profile) == "minecraft":
        return port_open(port)
    if profile_game(profile) == "bedrock":
        if port_open(port):
            return True
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                sock.bind(("127.0.0.1", int(profile.get("bedrockUdpPort", 19140))))
            except OSError:
                return True
        return False
    query_port = int(profile.get("queryPort", PALWORLD_QUERY_PORT))

    def udp_port_bound(candidate: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
                return False
            except OSError:
                return True

    return udp_port_bound(port) or udp_port_bound(query_port)


def palworld_game_port_bound(profile: dict) -> bool:
    """Return whether Palworld is listening on its player-facing UDP port."""
    port = int(profile.get("port", PALWORLD_PORT))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def palworld_ready(profile: dict) -> bool:
    """Verify Palworld itself, rather than only Docker's host port proxy, is ready."""
    if IS_MAC and profile.get("palworldRuntime", "").startswith("docker-linux"):
        docker = find_docker_executable()
        if not docker:
            return False
        ports = (
            f"{int(profile.get('port', PALWORLD_PORT)):04X}",
            f"{int(profile.get('queryPort', PALWORLD_QUERY_PORT)):04X}",
        )
        script = " && ".join(
            f"grep -qi ':{port} ' /proc/net/udp /proc/net/udp6 2>/dev/null" for port in ports
        )
        result = subprocess.run(
            [docker, "exec", palworld_container_name(profile), "/bin/sh", "-c", script],
            capture_output=True,
        )
        return result.returncode == 0
    return palworld_game_port_bound(profile)


def stop_palworld_runtime(profile: dict, timeout: int = 120) -> None:
    """Ask the macOS Palworld container to stop and flush before its runner exits."""
    if not (IS_MAC and profile.get("palworldRuntime", "").startswith("docker-linux")):
        return
    docker = find_docker_executable()
    if not docker:
        return
    subprocess.run(
        [docker, "stop", "--timeout", str(timeout), palworld_container_name(profile)],
        text=True, encoding="utf-8", errors="replace", capture_output=True,
        timeout=timeout + 10,
    )


def playit_executable_candidates() -> list[Path]:
    """Return supported Playit locations, including the official Windows install folders."""
    override = os.environ.get("PLAYIT_EXECUTABLE")
    def windows_install_path(variable: str, *parts: str) -> Path | None:
        base = os.environ.get(variable)
        return Path(base, *parts) if IS_WINDOWS and base else None

    path_candidates: list[Path | None] = [
        Path(override).expanduser() if override else None,
        ROOT / "runtimes" / "playit-scoped" / ("playit-cli.exe" if IS_WINDOWS else "playit-cli"),
        ROOT / "runtimes" / "playit" / ("playit.exe" if IS_WINDOWS else "playit"),
        Path("/Applications/playit.app/Contents/MacOS/playit") if not IS_WINDOWS else None,
        Path("/Applications/Playit.app/Contents/MacOS/playit") if not IS_WINDOWS else None,
        windows_install_path("LOCALAPPDATA", "playit_gg", "bin", "playit.exe"),
        windows_install_path("LOCALAPPDATA", "Programs", "playit", "playit.exe"),
        windows_install_path("LOCALAPPDATA", "Programs", "playit_gg", "bin", "playit.exe"),
        windows_install_path("ProgramFiles", "playit", "playit.exe"),
        windows_install_path("ProgramFiles", "playit_gg", "bin", "playit.exe"),
        windows_install_path("ProgramFiles(x86)", "playit_gg", "bin", "playit.exe"),
        ROOT.parent / "playit-agent" / "target" / "release" / "agent",
        Path.home() / ".local" / "bin" / "playit",
    ]
    # Prefer the managed portable agent over an unrelated `playit` command on
    # PATH. A current CLI may be installed globally while the bundled agent
    # remains the compatible single-process executable BlockOps can launch.
    found = shutil.which("playit") or shutil.which("playit-cli")
    if found:
        path_candidates.append(Path(found))
    return list(dict.fromkeys(candidate for candidate in path_candidates if candidate))


def playit_executable() -> Path:
    for candidate in playit_executable_candidates():
        if candidate.is_file() and (IS_WINDOWS or os.access(candidate, os.X_OK)):
            return candidate.resolve()

    if not IS_WINDOWS:
        raise ManagerError(
            "Playit is not installed. Download and run the current macOS agent from "
            "https://playit.gg/download/macos, then start the server again. "
            "Advanced users can set PLAYIT_EXECUTABLE=/absolute/path/to/playit."
        )

    print("playit.gg is missing; installing the official portable Windows agent …")
    releases = api_json("https://api.github.com/repos/playit-cloud/playit-agent/releases?per_page=100")
    # The signed portable v0.17 agent retains the single-process CLI used by
    # BlockOps. Newer 1.x Windows builds are installed as a service/MSI.
    asset = next(
        (
            item
            for release in releases
            if release.get("tag_name") == "v0.17.1"
            for item in release.get("assets", [])
            if item["name"].lower() == "playit-windows-x86_64-signed.exe"
        ),
        None,
    )
    if not asset:
        raise ManagerError(
            f"Playit's official releases have no compatible standalone {platform.system()} agent. "
            "Install it from https://playit.gg/download or set PLAYIT_EXECUTABLE=/path/to/playit."
        )
    destination = ROOT / "runtimes" / "playit" / ("playit.exe" if IS_WINDOWS else "playit")
    download(asset["browser_download_url"], destination, overwrite=True)
    if not IS_WINDOWS:
        destination.chmod(0o755)
    return destination


def supports_playit_commands(candidate: Path) -> bool:
    """Return whether a standalone agent exposes the current command interface."""
    if not IS_WINDOWS or candidate.suffix.lower() != ".exe":
        return False
    try:
        result = subprocess.run(
            [candidate, "--help"], text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return False
    output = f"{result.stdout}\n{result.stderr}".lower()
    return "claim" in output and "start" in output and "secret_path" in output


def modern_playit_binaries() -> tuple[Path, Path] | None:
    """Find a modern playitd + CLI pair in BlockOps or an official Windows installation."""
    folders = [ROOT / "runtimes" / "playit-scoped", ROOT / "runtimes" / "playit"]
    if IS_MAC:
        folders.extend([Path("/Applications/playit.app/Contents/MacOS"), Path("/Applications/Playit.app/Contents/MacOS"), Path.home() / "Applications" / "playit.app" / "Contents" / "MacOS"])
    if IS_WINDOWS:
        folders.extend(
            candidate.parent
            for candidate in playit_executable_candidates()
            if candidate.name.lower() == "playit.exe"
        )
    for folder in dict.fromkeys(folders):
        daemon = folder / ("playitd.exe" if IS_WINDOWS else "playitd")
        cli_names = ("playit-cli.exe", "playit.exe") if IS_WINDOWS else ("playit-cli", "playit")
        cli = next((folder / name for name in cli_names if (folder / name).is_file()), None)
        if daemon.is_file() and cli:
            return daemon.resolve(), cli.resolve()
    return None


def playit_output_since(offset: int) -> str:
    if not PLAYIT_LOG.exists():
        return ""
    with PLAYIT_LOG.open("rb") as handle:
        handle.seek(min(offset, PLAYIT_LOG.stat().st_size))
        return handle.read().decode("utf-8", errors="replace")


def claim_url_from_output(output: str) -> str | None:
    matches = re.findall(r"https://playit\.gg/claim/[A-Za-z0-9]+", output)
    return matches[-1] if matches else None


def new_playit_claim_code() -> str:
    """Generate the same five-byte hexadecimal claim code as Playit CLI."""
    return secrets.token_hex(5)


def pending_playit_claim_code() -> str | None:
    """Return the unexchanged Playit claim code, if this computer has one."""
    try:
        code = PLAYIT_CLAIM_CODE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return code if re.fullmatch(r"[A-Fa-f0-9]{10}", code) else None


def playit_claim_code() -> str:
    """Reuse a pending claim so a browser approval can be resumed after a delay."""
    existing = pending_playit_claim_code()
    if existing:
        return existing
    code = new_playit_claim_code()
    STATE.mkdir(parents=True, exist_ok=True)
    PLAYIT_CLAIM_CODE.write_text(code, encoding="utf-8")
    return code


def save_playit_claim_code(code: str) -> None:
    """Persist a valid code displayed by an earlier dashboard claim attempt."""
    if not re.fullmatch(r"[A-Fa-f0-9]{10}", code):
        raise ManagerError("Invalid Playit claim code.")
    STATE.mkdir(parents=True, exist_ok=True)
    PLAYIT_CLAIM_CODE.write_text(code, encoding="utf-8")


def clear_playit_claim_code() -> None:
    """Discard a claim code once its credential has been safely stored."""
    PLAYIT_CLAIM_CODE.unlink(missing_ok=True)


def playit_secret_from_output(output: str) -> str | None:
    """Extract Playit's 256-bit secret returned by ``claim exchange``."""
    clean = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", output)
    matches = re.findall(r"(?<![A-Fa-f0-9])[A-Fa-f0-9]{64}(?![A-Fa-f0-9])", clean)
    return matches[-1] if matches else None


def playit_credential_ready() -> bool:
    """Return whether Playit has persisted a non-empty account credential."""
    try:
        return PLAYIT_CONFIG.is_file() and PLAYIT_CONFIG.stat().st_size > 0 and not PLAYIT_INVALID.exists()
    except OSError:
        return False


def setup_playit(claim_code: str | None = None) -> None:
    """Install/start Playit before the first world and surface its secure claim URL."""
    # Legacy agents have no status CLI, so their persisted credential remains
    # the best available signal. Modern playitd credentials must be checked by
    # the daemon; a non-empty file can contain an expired or revoked key.
    if playit_credential_ready() and not modern_playit_binaries():
        pid, _ = start_playit()
        print(f"Playit is connected and ready (PID {pid}).")
        return

    executable = playit_executable()
    if supports_playit_commands(executable) and not modern_playit_binaries():
        # Playit's official claim generator is five random bytes encoded as
        # ten hexadecimal characters. Generate it here instead of scraping
        # the CLI's terminal UI, which is not stable when stdout is captured.
        # Keep the code until the exchange succeeds. Claim approval can take
        # longer than the CLI's wait window (for example, while the account is
        # at its agent limit and the user removes an old agent). Retrying setup
        # must exchange that same, already-approved claim rather than create a
        # second agent and strand the first one.
        if claim_code:
            save_playit_claim_code(claim_code)
        code = claim_code or playit_claim_code()
        claim_url = f"https://playit.gg/claim/{code}"
        print(f"Claim this computer's Playit agent: {claim_url}")
        exchanged = subprocess.run(
            [executable, "--secret_path", PLAYIT_CONFIG, "claim", "exchange", code, "--wait", "45"],
            text=True, encoding="utf-8", errors="replace", capture_output=True,
        )
        secret = playit_secret_from_output(f"{exchanged.stdout}\n{exchanged.stderr}")
        if exchanged.returncode or not secret:
            raise ManagerError(
                f"Finish the secure Playit account claim at {claim_url}, then retry connecting your account."
            )
        PLAYIT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        PLAYIT_CONFIG.write_text(f'secret_key = "{secret}"\n', encoding="utf-8")
        clear_playit_claim_code()
        pid, _ = start_playit()
        print(f"Playit account connected successfully (PID {pid}).")
        return

    pid, log_offset = start_playit()
    modern = modern_playit_binaries()
    if modern:
        _, cli = modern
        print("Waiting for the Playit service to become ready …")
        deadline = time.time() + 15
        while time.time() < deadline and is_alive(pid):
            status = subprocess.run(
                [cli, "--socket-path", PLAYIT_SOCKET, "status"],
                text=True, encoding="utf-8", errors="replace", capture_output=True,
            )
            if "Phase: waiting for secret" in status.stdout:
                break
            if "Phase: running" in status.stdout and PLAYIT_CONFIG.is_file() and PLAYIT_CONFIG.stat().st_size:
                PLAYIT_INVALID.unlink(missing_ok=True)
                print("Playit account is already connected.")
                return
            time.sleep(0.5)
        else:
            raise ManagerError(f"Playit started but its local service did not become ready. Review {PLAYIT_LOG}")
        print("Playit is ready. Open the secure claim URL below and approve this computer.")
        result = subprocess.run([cli, "--socket-path", PLAYIT_SOCKET, "setup"])
        if result.returncode:
            raise ManagerError("The Playit account claim was not completed. Retry and approve the agent in your browser.")
        PLAYIT_INVALID.unlink(missing_ok=True)
        if not playit_credential_ready():
            raise ManagerError("The Playit account claim was not completed. Retry and approve the agent in your browser.")
        print("Playit account connected successfully.")
        return

    print("Waiting for Playit to prepare a secure account claim …")
    deadline = time.time() + 45
    while time.time() < deadline and is_alive(pid):
        if playit_credential_ready():
            print("Playit account connected successfully.")
            return
        claim_url = claim_url_from_output(playit_output_since(log_offset))
        if claim_url:
            print(f"Claim this computer's Playit agent: {claim_url}")
            raise ManagerError(
                f"Finish the secure Playit account claim at {claim_url}, then return to BlockOps and check the connection."
            )
        time.sleep(0.5)
    if not is_alive(pid):
        raise ManagerError(f"Playit exited during setup. Review {PLAYIT_LOG}")
    raise ManagerError(
        "Playit started but did not provide an account claim. Check the internet connection, then retry setup."
    )


def start_playit() -> tuple[int, int]:
    log_offset = PLAYIT_LOG.stat().st_size if PLAYIT_LOG.exists() else 0
    current = read_pid(PLAYIT_PID)
    if current:
        session = os.environ.get("BLOCKOPS_SESSION_ID")
        if session and not app_session.owned_process(PLAYIT_PID, session):
            raise ManagerError("A Playit daemon from another BlockOps session is running. Close that session before connecting this one.")
        return current, log_offset
    modern = modern_playit_binaries()
    STATE.mkdir(parents=True, exist_ok=True)
    with PLAYIT_LOG.open("ab", buffering=0) as log:
        if modern:
            modern_daemon, _ = modern
            PLAYIT_SOCKET.unlink(missing_ok=True)
            process = subprocess.Popen(
                [modern_daemon, "--secret-path", PLAYIT_CONFIG, "--socket-path", PLAYIT_SOCKET, "--log-path", PLAYIT_LOG],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **detached_process_options(),
            )
        else:
            executable = playit_executable()
            if supports_playit_commands(executable):
                command = [executable, "--secret_path", PLAYIT_CONFIG, "--stdout", "start"]
            else:
                command = [executable, "--config-file", PLAYIT_CONFIG, "--stdout-logs"]
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **detached_process_options())
    PLAYIT_PID.write_text(str(process.pid))
    app_session.record_owner(PLAYIT_PID, process.pid)
    time.sleep(1)
    if process.poll() is not None:
        output = playit_output_since(log_offset)
        if "unexpected argument '--config-file'" in output:
            raise ManagerError(
                "A newer Playit CLI was found, but it is not compatible with BlockOps' legacy launcher. "
                "Open Setup Guide and use the official portable Playit .exe, or install the official Windows MSI so playitd.exe is installed too."
            )
        raise ManagerError(f"playit.gg exited immediately. Review {PLAYIT_LOG}")
    print(f"playit.gg started (PID {process.pid}).")
    return process.pid, log_offset


def ensure_playit_enabled(pid: int, log_offset: int, timeout: int = 30, profile: dict | None = None) -> None:
    """Require account claim/tunnel setup before a managed game is launched."""
    game = profile_game(profile or {})
    tunnel_help = ("Minecraft Bedrock (NetherNet) tunnel with the WaterdogPE configuration in World Settings" if game == "bedrock" else ("Minecraft Java tunnel targeting 127.0.0.1:25565" if game == "minecraft" else "UDP tunnel targeting 127.0.0.1:8211 for Palworld"))
    modern = modern_playit_binaries()
    if modern:
        _, modern_cli = modern
        deadline = time.time() + timeout
        status = None
        while time.time() < deadline and is_alive(pid):
            status = subprocess.run(
                [modern_cli, "--socket-path", PLAYIT_SOCKET, "status"],
                text=True, encoding="utf-8", errors="replace", capture_output=True,
            )
            if status.returncode == 0 and any(
                phase in status.stdout
                for phase in ("Phase: running", "Phase: waiting for secret", "Phase: invalid secret")
            ):
                break
            time.sleep(0.5)
        if not status or status.returncode != 0 or "Phase: starting" in status.stdout:
            raise ManagerError(f"playit.gg daemon did not become ready. Review {PLAYIT_LOG}")
        if "Phase: waiting for secret" in status.stdout:
            if PLAYIT_CONFIG.is_file() and PLAYIT_CONFIG.stat().st_size:
                PLAYIT_INVALID.touch()
            if not sys.stdin.isatty():
                raise ManagerError("Playit's saved account credential is invalid. Open Setup Guide and reconnect your Playit account before starting the server.")
            print("\nplayit.gg is not enabled yet. Starting its secure account-claim setup …")
            setup = subprocess.run([modern_cli, "--socket-path", PLAYIT_SOCKET, "setup"])
            if setup.returncode:
                raise ManagerError("Playit account claim was not completed.")
            status = subprocess.run(
                [modern_cli, "--socket-path", PLAYIT_SOCKET, "status"],
                text=True, encoding="utf-8", errors="replace", capture_output=True,
            )
        elif "Phase: invalid secret" in status.stdout:
            PLAYIT_INVALID.touch()
            raise ManagerError("Playit's saved account credential is invalid. Remove playit.toml and start again to reclaim it.")
        if status.returncode or "Phase: running" not in status.stdout:
            raise ManagerError("Playit is not authenticated and running; the server was not started.")
        PLAYIT_INVALID.unlink(missing_ok=True)
        print("playit.gg agent is authenticated and online.")
        print(f"Ensure your Playit dashboard has a {tunnel_help}.")
        return

    # Compatibility path for an already installed legacy standalone agent.
    deadline = time.time() + timeout
    claim_url = None
    while time.time() < deadline and is_alive(pid):
        claim_url = claim_url_from_output(playit_output_since(log_offset))
        if claim_url:
            break
        time.sleep(0.5)
    if not is_alive(pid):
        raise ManagerError(f"playit.gg exited during startup. Review {PLAYIT_LOG}")
    if not claim_url:
        # A claimed agent does not emit a claim URL. Give it a short window to
        # authenticate before allowing Minecraft to start.
        print("playit.gg agent is claimed and enabled.")
        return
    if not sys.stdin.isatty():
        raise ManagerError(
            "playit.gg must be enabled before this server can start. "
            f"Claim it at {claim_url}, create a {tunnel_help}, then retry."
        )
    print("\nplayit.gg is not enabled yet.")
    print(f"Claim this computer's agent: {claim_url}")
    input(
        "Copy the URL above into a working browser. In Playit, claim the agent "
        f"and enable a {tunnel_help}. "
        "Then press Enter here: "
    )
    # The agent exchanges the approved claim in the background. Do not launch
    # a local-only Minecraft server until claim prompts stop.
    quiet_since = None
    deadline = time.time() + 120
    last_size = PLAYIT_LOG.stat().st_size if PLAYIT_LOG.exists() else 0
    while time.time() < deadline and is_alive(pid):
        size = PLAYIT_LOG.stat().st_size if PLAYIT_LOG.exists() else 0
        if size != last_size:
            new_output = playit_output_since(last_size)
            last_size = size
            quiet_since = None if claim_url_from_output(new_output) else (quiet_since or time.time())
        elif quiet_since is None:
            quiet_since = time.time()
        if quiet_since and time.time() - quiet_since >= 3:
            print("playit.gg claim accepted; public tunneling is enabled.")
            return
        time.sleep(0.5)
    raise ManagerError("Playit did not confirm the claim. Complete setup in the browser and try Start instance again.")


def stop_process(pid_path: Path, timeout: int = 15) -> None:
    pid = read_pid(pid_path)
    if not pid:
        pid_path.unlink(missing_ok=True)
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except PermissionError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + timeout
    while is_alive(pid) and time.time() < deadline:
        time.sleep(0.2)
    if is_alive(pid):
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except PermissionError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    pid_path.unlink(missing_ok=True)


def launch_command(profile: dict) -> list[str]:
    folder = absolute_profile_path(profile)
    if profile_game(profile) == "bedrock":
        return bedrock_launch_command(profile)
    if profile_game(profile) == "palworld":
        if IS_MAC and profile.get("palworldRuntime", "").startswith("docker-linux"):
            executable = folder / profile.get("launchExecutable", "PalServer.sh")
            if not executable.is_file():
                raise ManagerError(f"Palworld server executable is missing: {executable}")
            docker = docker_executable()
            port = int(profile.get("port", PALWORLD_PORT))
            query_port = int(profile.get("queryPort", PALWORLD_QUERY_PORT))
            command = [
                str(docker), "run", "--rm", "--platform", palworld_docker_platform(),
                "--name", palworld_container_name(profile),
                "--stop-timeout", "120", "--memory", str(profile.get("maximumRam", "6G")).lower(),
                "--publish", f"127.0.0.1:{port}:{port}/udp",
                "--publish", f"127.0.0.1:{query_port}:{query_port}/udp",
                "--volume", f"{folder.resolve()}:/palworld",
                "--env", "PUID=1000", "--env", "PGID=1000",
                "--env", f"PORT={port}", "--env", f"QUERY_PORT={query_port}",
                "--env", "PLAYERS=32", "--env", f"SERVER_NAME={profile['name']}",
                "--env", "UPDATE_ON_BOOT=false", "--env", "AUTO_UPDATE_ENABLED=false",
                "--env", "BACKUP_ENABLED=false", "--env", "REST_API_ENABLED=false",
                "--env", "RCON_ENABLED=true",
                "--env", f"ADMIN_PASSWORD={profile.get('containerAdminPassword', 'blockops-internal')}",
                "--env", "DISABLE_GENERATE_SETTINGS=true",
                "--env", "PALWORLD_ALLOW_NEGATIVE_DELTA_TIME=true",
            ]
            if palworld_docker_platform() == "linux/arm64":
                command.extend(["--env", "ARM64_DEVICE=m1"])
            command.append(PALWORLD_DOCKER_IMAGE)
            return command
        executable = folder / profile.get("launchExecutable", "PalServer.exe")
        if not executable.is_file():
            raise ManagerError(f"Palworld server executable is missing: {executable}")
        return [str(executable), f"-port={int(profile.get('port', PALWORLD_PORT))}", f"-queryport={int(profile.get('queryPort', PALWORLD_QUERY_PORT))}", "-players=32", "-useperfthreads", "-NoAsyncLoadingThread", "-UseMultithreadForDS"]
    java = Path(profile.get("javaPath", ""))
    required = int(profile.get("javaMajor") or required_java(profile["minecraftVersion"]))
    if not java.exists() or java_major(java) != required:
        java = resolve_java(required)
        profile["javaPath"] = str(java)
    command = [str(java), *profile.get("jvmArguments", []), f"-Xms{profile['minimumRam']}", f"-Xmx{profile['maximumRam']}"]
    launcher = profile["launchJar"]
    if launcher.startswith("@"):
        command.append(launcher)
    else:
        if not (folder / launcher).exists():
            raise ManagerError(f"Server launcher is missing: {folder / launcher}")
        command.extend(["-jar", launcher])
    command.extend(profile.get("serverArguments", ["nogui"]))
    return command


def world_path(folder: Path) -> Path:
    """Return the configured world directory without trusting paths outside the profile."""
    with contextlib.suppress(OSError, ValueError):
        profile = load_json(folder / "profile.json")
        if profile_game(profile) == "palworld":
            world = (folder / "Pal" / "Saved" / "SaveGames").resolve()
            if world.is_relative_to(folder.resolve()):
                return world
    is_bedrock = False
    with contextlib.suppress(OSError, ValueError):
        is_bedrock = profile_game(load_json(folder / "profile.json")) == "bedrock"
    level_name = "Bedrock level" if is_bedrock else "world"
    properties = folder / "server.properties"
    if properties.exists():
        for line in properties.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("level-name="):
                level_name = line.partition("=")[2].strip() or "world"
                break
    if is_bedrock and (level_name in {".", ".."} or any(c in level_name for c in "/\\:")):
        raise ManagerError("Bedrock level-name must be a single world folder name.")
    if is_bedrock:
        bedrock.safe_path(folder, f"worlds/{level_name}")
    world = ((folder / "worlds" / level_name) if is_bedrock else (folder / level_name)).resolve()
    if not world.is_relative_to(folder.resolve()):
        raise ManagerError(f"Unsafe level-name in {properties}: {level_name}")
    return world


def write_server_command(process: subprocess.Popen, command: str) -> None:
    if not process.stdin:
        raise ManagerError("This server does not accept console commands through BlockOps.")
    process.stdin.write((command.strip().lstrip("/") + "\n").encode())
    process.stdin.flush()


def live_player_count(folder: Path, process: subprocess.Popen, timeout: float = 3.0) -> int | None:
    """Ask the running server for its exact player count without enabling RCON."""
    latest = folder / "logs" / "latest.log"
    try:
        offset = latest.stat().st_size if latest.exists() else 0
        write_server_command(process, "list")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and process.poll() is None:
            if latest.exists():
                with latest.open("rb") as handle:
                    size = latest.stat().st_size
                    handle.seek(offset if size >= offset else 0)
                    output = handle.read().decode("utf-8", errors="replace")
                for line in reversed(output.splitlines()):
                    for pattern in PLAYER_LIST_PATTERNS:
                        match = pattern.search(line)
                        if match:
                            return int(match.group(1))
            time.sleep(0.1)
    except (OSError, ValueError):
        return None
    return None


def create_world_backup(folder: Path, process: subprocess.Popen, settings: dict | None = None) -> Path | None:
    """Flush and briefly suspend world writes while making a restorable snapshot."""
    world = world_path(folder)
    if not world.is_dir():
        return None
    backups = folder / "backups"
    backups.mkdir(exist_ok=True)
    destination = backups / f"{world.name}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    temporary = destination.with_suffix(destination.suffix + ".part")
    settings = settings or backup_settings(load_json(folder / "profile.json"))
    try:
        write_server_command(process, "save-off")
        write_server_command(process, "save-all flush")
        # Minecraft 1.12 does not expose a command acknowledgement channel to
        # this runner, so allow the synchronous flush to finish before reading.
        time.sleep(5)
        with tarfile.open(temporary, "w:gz", compresslevel=settings["compressionLevel"]) as archive:
            archive.add(world, arcname=world.name, recursive=True)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
        if process.poll() is None:
            with contextlib.suppress(BrokenPipeError, OSError):
                write_server_command(process, "save-on")
    completed = sorted(backups.glob(f"{world.name}-*.tar.gz"), key=lambda item: item.stat().st_mtime)
    for expired in completed[:-settings["retention"]]:
        expired.unlink(missing_ok=True)
    return destination


def runner_mode(profile_path: str) -> None:
    profile = load_json(Path(profile_path) / "profile.json")
    folder = Path(profile_path)
    control = folder / ".control"
    control.mkdir(exist_ok=True)
    if profile_game(profile) != "bedrock" and not os.environ.get("BLOCKOPS_SESSION_ID"):
        (control / "stop.request").unlink(missing_ok=True)
    log_path = folder / "profile-runner.log"
    settings = backup_settings(profile)
    game = profile_game(profile)
    if game == "bedrock":
        bedrock_runner(profile, folder)
        return
    while True:
        with log_path.open("a", encoding="utf-8") as runner_log:
            runner_log.write(f"{time.strftime('%F %T')} starting {profile['name']}\n")
            runner_log.flush()
            process = subprocess.Popen(launch_command(profile), cwd=folder, stdin=subprocess.PIPE)
            SERVER_PID.write_text(str(process.pid))
            requested_stop = False
            next_backup = time.monotonic() + settings["intervalMinutes"] * 60 if settings["enabled"] else None
            while process.poll() is None:
                command_file = control / "command.request"
                if command_file.exists():
                    commands = command_file.read_text(encoding="utf-8").splitlines()
                    command_file.unlink(missing_ok=True)
                    if game == "minecraft":
                        for command in commands:
                            if command.strip():
                                write_server_command(process, command)
                backup_request = control / "backup.request"
                if backup_request.exists():
                    backup_request.unlink(missing_ok=True)
                    try:
                        backup = create_world_backup(folder, process, settings) if game == "minecraft" else None
                        if backup:
                            runner_log.write(f"{time.strftime('%F %T')} manual backup created: {backup.name}\n")
                            runner_log.flush()
                    except (ManagerError, OSError, tarfile.TarError) as error:
                        runner_log.write(f"{time.strftime('%F %T')} manual backup failed: {error}\n")
                        runner_log.flush()
                if (control / "stop.request").exists():
                    (control / "stop.request").unlink(missing_ok=True)
                    requested_stop = True
                    if game == "minecraft" and process.stdin:
                        if settings["backupOnStop"]:
                            try:
                                backup = create_world_backup(folder, process, settings)
                                if backup:
                                    runner_log.write(f"{time.strftime('%F %T')} stop backup created: {backup.name}\n")
                                    runner_log.flush()
                            except (ManagerError, OSError, tarfile.TarError) as error:
                                runner_log.write(f"{time.strftime('%F %T')} stop backup failed: {error}\n")
                                runner_log.flush()
                        write_server_command(process, "save-all flush")
                        write_server_command(process, "stop")
                    elif game == "palworld" and IS_MAC:
                        stop_palworld_runtime(profile)
                    else:
                        process.terminate()
                    # `docker stop` is authoritative for the macOS Palworld
                    # runtime.  The attached `docker run` client can linger
                    # after its container has already flushed and exited, so
                    # do not leave the dashboard saying "stopping" for two
                    # additional minutes while waiting on that wrapper.
                    wait_timeout = 5 if game == "palworld" and IS_MAC else 120
                    try:
                        process.wait(timeout=wait_timeout)
                    except subprocess.TimeoutExpired:
                        if os.environ.get("BLOCKOPS_SESSION_ID") and game == "minecraft":
                            runner_log.write("Java is still saving; keeping the process alive until its safe stop finishes.\n")
                            runner_log.flush()
                            process.wait()
                        else:
                            process.terminate()
                            with contextlib.suppress(subprocess.TimeoutExpired):
                                process.wait(timeout=3)
                    break
                if game == "minecraft" and next_backup is not None and time.monotonic() >= next_backup:
                    if settings.get("onlyWhenEmpty"):
                        players = live_player_count(folder, process)
                        if players is None:
                            runner_log.write(f"{time.strftime('%F %T')} scheduled backup deferred: player count unavailable\n")
                            runner_log.flush()
                            next_backup = time.monotonic() + 60
                            continue
                        if players > 0:
                            runner_log.write(f"{time.strftime('%F %T')} scheduled backup deferred: {players} player(s) online\n")
                            runner_log.flush()
                            next_backup = time.monotonic() + 60
                            continue
                    try:
                        backup = create_world_backup(folder, process, settings)
                        if backup:
                            runner_log.write(f"{time.strftime('%F %T')} backup created: {backup.name}\n")
                            runner_log.flush()
                    except (ManagerError, OSError, tarfile.TarError) as error:
                        runner_log.write(f"{time.strftime('%F %T')} backup failed: {error}\n")
                        runner_log.flush()
                    next_backup = time.monotonic() + settings["intervalMinutes"] * 60
                time.sleep(0.25)
            SERVER_PID.unlink(missing_ok=True)
            if requested_stop:
                return
            runner_log.write(f"{time.strftime('%F %T')} server exited; leaving it stopped\n")
            runner_log.flush()
            return


def start_profile(profile: dict, timeout: int) -> None:
    if app_session.cancellation_requested(STATE):
        raise ManagerError("BlockOps is closing; the server was not started.")
    if os.environ.get("BLOCKOPS_SESSION_ID") and profile_game(profile) == "bedrock":
        import managed_network
        managed_network.prepare(profile)
    port = int(profile.get("port", DEFAULT_PORT))
    if game_port_in_use(profile):
        raise ManagerError(f"Port {port} is already in use. Stop the active server first.")
    folder = absolute_profile_path(profile)
    if not folder.exists():
        raise ManagerError(f"Profile folder is missing: {folder}")
    if profile_game(profile) == "bedrock" and bedrock_container_running(profile):
        raise ManagerError("This Bedrock container is already running. Stop it safely before starting a second runner.")
    STATE.mkdir(parents=True, exist_ok=True)
    if active_profile():
        raise ManagerError("Stop the active instance before starting another server.")
    if profile_game(profile) != "bedrock":
        try:
            playit_pid, playit_log_offset = start_playit()
            ensure_playit_enabled(playit_pid, playit_log_offset, profile=profile)
        except Exception:
            stop_process(PLAYIT_PID)
            raise
    if profile_game(profile) == "bedrock" or os.environ.get("BLOCKOPS_SESSION_ID"):
        control = folder / ".control"
        control.mkdir(exist_ok=True)
        for stale in ("stop.request", "backup.request", "command.request"):
            (control / stale).unlink(missing_ok=True)
    if app_session.cancellation_requested(STATE):
        raise ManagerError("BlockOps is closing; the server was not started.")
    # Only launch the server after Playit has been claimed and enabled. This
    # prevents Start instance from silently creating a localhost-only server.
    with (folder / "profile-runner.log").open("ab", buffering=0) as runner_log:
        process = subprocess.Popen([sys.executable, __file__, "_run", str(folder)], cwd=folder, stdin=subprocess.DEVNULL, stdout=runner_log, stderr=subprocess.STDOUT, **detached_process_options())
    (STATE / "runner.pid").write_text(str(process.pid))
    app_session.record_owner(STATE / "runner.pid", process.pid)
    if os.environ.get("BLOCKOPS_SESSION_ID"):
        (STATE / "active-session").write_text(os.environ["BLOCKOPS_SESSION_ID"])
    latest = folder / "logs" / "latest.log"
    deadline = time.time() + timeout
    palworld_ready_after = time.time() + min(10, timeout)
    print(f"Starting {profile['name']} …")
    try:
        while time.time() < deadline:
            if app_session.cancellation_requested(STATE):
                raise ManagerError("BlockOps closed during startup; a safe stop was requested.")
            if process.poll() is not None:
                raise ManagerError(f"Server runner exited. Review {folder / 'profile-runner.log'}")
            if profile_game(profile) == "bedrock" and bedrock_ready(profile):
                if os.environ.get("BLOCKOPS_SESSION_ID"):
                    import managed_network
                    managed_network.start(profile)
                print(f"{profile['name']} is ready locally. Player connections and remote networking need separate verification.")
                return
            if profile_game(profile) == "minecraft" and port_open(port):
                print(f"{profile['name']} is online through the configured playit.gg public address.")
                return
            if profile_game(profile) == "palworld" and time.time() >= palworld_ready_after and palworld_ready(profile):
                print(f"{profile['name']} is running through the configured playit.gg public address.")
                return
            if latest.exists():
                try:
                    tail = latest.read_text(encoding="utf-8", errors="replace").splitlines()[-1:]
                    if tail:
                        print(f"  {tail[0][-160:]}", end="\r", flush=True)
                except OSError:
                    pass
            time.sleep(1)
        raise ManagerError(f"Server did not open port {port} within {timeout} seconds. Review {latest}")
    except Exception:
        if app_session.cancellation_requested(STATE):
            control = folder / ".control"
            control.mkdir(exist_ok=True)
            (control / "stop.request").write_text("BlockOps closed during startup\n")
            raise ManagerError("BlockOps closed during startup. A safe stop was requested; the app will wait for saving to finish.")
        # Never leave a detached runner alive when startup fails. An orphaned
        # runner would let a later Start action launch a second game process.
        if profile_game(profile) == "bedrock":
            if os.environ.get("BLOCKOPS_SESSION_ID"):
                stop_process(STATE / "waterdog.pid")
                stop_process(PLAYIT_PID)
            # Never kill a potentially saving BDS container on startup timeout.
            control = folder / ".control"
            control.mkdir(exist_ok=True)
            (control / "stop.request").write_text("startup failed\n")
            raise ManagerError(f"Bedrock startup failed or timed out. A safe stop was requested; inspect {folder / 'profile-runner.log'} before retrying.")
        if profile_game(profile) == "palworld":
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                stop_palworld_runtime(profile, timeout=30)
        if process.poll() is None:
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
        with contextlib.suppress(OSError):
            if (STATE / "runner.pid").read_text(encoding="utf-8", errors="ignore").strip() == str(process.pid):
                (STATE / "runner.pid").unlink(missing_ok=True)
        SERVER_PID.unlink(missing_ok=True)
        stop_process(PLAYIT_PID)
        raise


def active_profile() -> dict | None:
    active = STATE / "active-profile"
    if active.exists():
        with contextlib.suppress(ManagerError, OSError, subprocess.SubprocessError):
            profile = profile_by_id(active.read_text().strip())
            if read_pid(STATE / "runner.pid"):
                return profile
            # Keep Stop available after an attached Docker client/runner exits
            # unexpectedly while the persistent BDS container is still alive.
            if profile_game(profile) == "bedrock" and bedrock_container_running(profile):
                return profile
    return None


def stop_profile() -> None:
    profile = active_profile()
    if profile:
        folder = absolute_profile_path(profile)
        control = folder / ".control"
        control.mkdir(exist_ok=True)
        (control / "stop.request").write_text("server-manager\n")
        if profile_game(profile) == "bedrock" and not read_pid(STATE / "runner.pid"):
            bedrock_command(profile, "stop")
        print(f"Saving and stopping {profile['name']} …")
        deadline = time.time() + 125
        port = int(profile.get("port", DEFAULT_PORT))
        # The runner can linger briefly (or remain as a zombie until its
        # terminal reaps it) after Minecraft has saved and exited. The port is
        # the authoritative server state; do not make the UI hang on a stale
        # wrapper PID after the server is safely offline.
        while ((bedrock_container_running(profile) or read_pid(STATE / "runner.pid")) if profile_game(profile) == "bedrock" else game_port_in_use(profile)) and time.time() < deadline:
            time.sleep(1)
        if ((bedrock_container_running(profile) or read_pid(STATE / "runner.pid")) if profile_game(profile) == "bedrock" else game_port_in_use(profile)):
            raise ManagerError("The server did not stop cleanly within 125 seconds.")
        stop_process(STATE / "runner.pid", timeout=3)
        SERVER_PID.unlink(missing_ok=True)
    stop_process(STATE / "waterdog.pid")
    stop_process(PLAYIT_PID)
    PLAYIT_SOCKET.unlink(missing_ok=True)
    (STATE / "active-session").unlink(missing_ok=True)
    (STATE / "active-profile").unlink(missing_ok=True)
    print("The managed server and playit.gg are stopped.")


def send_command(command: str) -> None:
    profile = active_profile()
    if not profile:
        raise ManagerError("No managed server is running.")
    if profile_game(profile) not in {"minecraft", "bedrock"}:
        raise ManagerError("Palworld console commands are not supported by BlockOps yet. Use its configuration files instead.")
    if profile_game(profile) == "bedrock" and (not command.strip() or any(c in command for c in "\r\n\x00")):
        raise ManagerError("Enter one Bedrock console command at a time.")
    request = absolute_profile_path(profile) / ".control" / "command.request"
    if request.exists():
        raise ManagerError("The previous server command is still pending.")
    request.parent.mkdir(exist_ok=True)
    temporary = request.with_suffix(".pending")
    temporary.write_text(command.strip().lstrip("/") + "\n", encoding="utf-8")
    temporary.replace(request)
    print(f"Command queued for {profile['name']}.")


def console_ui(profile: dict) -> None:
    """Detachable curses console backed by the server log/control files."""
    folder = absolute_profile_path(profile)
    log_path = folder / "logs" / "latest.log"
    request = folder / ".control" / "command.request"
    request.parent.mkdir(exist_ok=True)

    if IS_WINDOWS:
        print(f"{profile['name']} — detachable server console")
        print("Enter Minecraft commands. Type :quit to detach; the server keeps running.")
        while active_profile() and port_open(int(profile.get("port", DEFAULT_PORT))):
            try:
                submitted = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if submitted.lower() in {":quit", ":exit", ":detach"}:
                return
            if submitted:
                send_command(submitted)
        print("Server is offline.")
        return

    def run(screen) -> None:
        curses.curs_set(1)
        screen.keypad(True)
        screen.timeout(250)
        command = ""
        message = "Type a server command. Type :quit or press Ctrl+C to detach."
        lines: list[str] = []
        last_size = -1
        while True:
            if not active_profile() or not port_open(int(profile.get("port", DEFAULT_PORT))):
                message = "Server is offline. Press any key to close this console."
            try:
                size = log_path.stat().st_size
                if size != last_size:
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    last_size = size
            except OSError:
                lines = ["Waiting for logs/latest.log …"]

            height, width = screen.getmaxyx()
            screen.erase()
            title = f" {profile['name']} — detachable server console "
            screen.addnstr(0, 0, title, max(0, width - 1), curses.A_REVERSE)
            available = max(1, height - 4)
            for row, line in enumerate(lines[-available:], 1):
                screen.addnstr(row, 0, line, max(0, width - 1))
            screen.addnstr(height - 3, 0, message, max(0, width - 1), curses.A_DIM)
            prompt = "> " + command
            screen.addnstr(height - 2, 0, prompt, max(0, width - 1))
            screen.move(height - 2, min(len(prompt), max(0, width - 2)))
            screen.refresh()

            key = screen.getch()
            if key == -1:
                continue
            if key in (3, 4):  # Ctrl+C / Ctrl+D detach only.
                return
            if key in (curses.KEY_ENTER, 10, 13):
                submitted = command.strip()
                command = ""
                if not submitted:
                    continue
                if submitted.lower() in {":quit", ":exit", ":detach"}:
                    return
                if request.exists():
                    message = "Previous command is still pending; try again in a moment."
                    continue
                temporary = request.with_suffix(".pending")
                temporary.write_text(submitted.lstrip("/") + "\n", encoding="utf-8")
                temporary.replace(request)
                message = f"Submitted: {submitted}"
                continue
            if key in (curses.KEY_BACKSPACE, 127, 8):
                command = command[:-1]
            elif key == curses.KEY_RESIZE:
                continue
            elif 32 <= key <= 126:
                command += chr(key)

    try:
        curses.wrapper(run)
    except curses.error as error:
        raise ManagerError(f"The terminal window is too small for the server console: {error}") from error


def show_status() -> None:
    data = registry()["profiles"]
    if not data:
        print("No profiles exist.")
    for profile in data:
        if profile_game(profile) == "bedrock":
            print(f"{profile['id']}: Minecraft Bedrock ({profile['maximumRam']}, Docker linux/amd64)")
        elif profile_game(profile) == "palworld":
            print(f"{profile['id']}: Palworld ({profile['minimumRam']}–{profile['maximumRam']})")
        else:
            print(f"{profile['id']}: {profile['minecraftVersion']} {profile['loader']} {profile.get('loaderVersion', '')} ({profile['minimumRam']}–{profile['maximumRam']})")
    active = active_profile()
    print(f"Server: {'running — ' + active['name'] if active else 'stopped'}")
    playit_state = "stopped"
    modern_cli = ROOT / "runtimes" / "playit" / ("playit-cli.exe" if IS_WINDOWS else "playit-cli")
    if read_pid(PLAYIT_PID) and modern_cli.is_file():
        result = subprocess.run(
            [modern_cli, "--socket-path", PLAYIT_SOCKET, "status"],
            text=True, encoding="utf-8", errors="replace", capture_output=True,
        )
        playit_state = "authenticated and online" if result.returncode == 0 and "Phase: running" in result.stdout else "not ready"
    elif read_pid(PLAYIT_PID):
        playit_state = "running (legacy agent)"
    print(f"playit.gg: {playit_state}")


def interactive() -> None:
    while True:
        print("\nMinecraft Server Manager\n[1] Start instance\n[2] Stop instance\n[3] Open live console\n[4] Send one command\n[5] Create instance\n[6] Status\n[7] Exit")
        choice = input("Choice: ").strip()
        try:
            if choice == "1":
                profile = profile_by_id(None, prompt=True)
                (STATE / "active-profile").parent.mkdir(parents=True, exist_ok=True)
                (STATE / "active-profile").write_text(profile["id"])
                if os.environ.get("BLOCKOPS_SESSION_ID"):
                    (STATE / "active-session").write_text(os.environ["BLOCKOPS_SESSION_ID"])
                start_profile(profile, 900)
            elif choice == "2": stop_profile()
            elif choice == "3":
                profile = active_profile()
                if not profile: raise ManagerError("No managed server is running.")
                console_ui(profile)
            elif choice == "4": send_command(input("Server command: "))
            elif choice == "5": make_profile(argparse.Namespace(game="minecraft", name=None, minecraft=None, loader=None, loader_version=None, id=None, path=None, min_ram="2G", max_ram="6G"))
            elif choice == "6": show_status()
            elif choice == "7": return
            else: print("Invalid choice.")
        except ManagerError as error:
            print(f"Error: {error}", file=sys.stderr)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Cross-platform game-server and playit.gg manager")
    commands = result.add_subparsers(dest="action")
    start = commands.add_parser("start", help="start an instance and playit.gg")
    start.add_argument("noun", nargs="?", choices=["instance"])
    start.add_argument("--profile", "-p")
    start.add_argument("--timeout", type=int, default=900)
    commands.add_parser("stop", help="save and stop the instance and playit.gg").add_argument("noun", nargs="?", choices=["instance"])
    create = commands.add_parser("create", help="create and install an instance")
    create.add_argument("noun", nargs="?", choices=["instance"])
    create.add_argument("--name")
    create.add_argument("--id")
    create.add_argument("--game", choices=["minecraft", "bedrock", "palworld"], default="minecraft")
    create.add_argument("--edition", choices=["java", "bedrock"], default="java")
    create.add_argument("--bedrock-version", default="LATEST")
    create.add_argument("--bedrock-address", default="127.0.0.1")
    create.add_argument("--bedrock-bind", choices=["127.0.0.1", "0.0.0.0"])
    create.add_argument("--port", type=int, default=BEDROCK_PORT)
    create.add_argument("--udp-port", type=int, default=19140)
    create.add_argument("--minecraft", "--minecraft-version")
    create.add_argument("--loader", choices=["vanilla", "fabric", "forge"])
    create.add_argument("--loader-version")
    create.add_argument("--path")
    create.add_argument("--min-ram")
    create.add_argument("--max-ram")
    command = commands.add_parser("command", help="send a Minecraft console command")
    command.add_argument("text", nargs="+")
    commands.add_parser("console", help="open a detachable live server console")
    commands.add_parser("status", help="show profiles and process status")
    setup = commands.add_parser("setup", help="prepare an external service before creating a server")
    setup.add_argument("noun", choices=["playit", "bedrock-network"])
    setup.add_argument("--claim-code", help=argparse.SUPPRESS)
    run = commands.add_parser("_run", help=argparse.SUPPRESS)
    run.add_argument("profile_path")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.action == "_run":
            runner_mode(args.profile_path)
            return 0
        # The detachable console is deliberately read/control-channel only and
        # must be openable while the interactive manager owns its operation
        # lock. Closing this process never signals the server runner.
        if args.action == "console":
            profile = active_profile()
            if not profile:
                raise ManagerError("No managed server is running.")
            console_ui(profile)
            return 0
        with manager_lock():
            if args.action is None:
                interactive()
            elif args.action == "create":
                make_profile(args)
            elif args.action == "start":
                profile = profile_by_id(args.profile, prompt=True)
                if active_profile():
                    raise ManagerError("Stop the active instance before starting another server.")
                (STATE / "active-profile").write_text(profile["id"])
                if not os.environ.get("BLOCKOPS_SESSION_ID"):
                    (STATE / "active-session").unlink(missing_ok=True)
                try:
                    start_profile(profile, args.timeout)
                except Exception:
                    cancelled_runner = app_session.cancellation_requested(STATE) and read_pid(STATE / "runner.pid")
                    if not cancelled_runner and (profile_game(profile) != "bedrock" or not (read_pid(STATE / "runner.pid") or bedrock_container_running(profile))):
                        (STATE / "active-profile").unlink(missing_ok=True)
                    raise
            elif args.action == "stop":
                stop_profile()
            elif args.action == "command":
                send_command(" ".join(args.text))
            elif args.action == "status":
                show_status()
            elif args.action == "setup" and args.noun == "playit":
                if os.environ.get("BLOCKOPS_SESSION_ID"):
                    import managed_network
                    managed_network.ensure_playit_tools()
                setup_playit(args.claim_code)
            elif args.action == "setup" and args.noun == "bedrock-network":
                import managed_network
                managed_network.ensure_playit_tools()
                managed_network.ensure_waterdog()
                print("Bedrock networking tools are ready. Connect your Playit account, then paste the tunnel configuration in World Settings.")
        return 0
    except (ManagerError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
