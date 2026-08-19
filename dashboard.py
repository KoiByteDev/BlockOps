#!/usr/bin/env python3
"""Local, authenticated web dashboard for the Minecraft server manager."""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import http.cookies
import json
import mimetypes
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

import server_manager as manager


HOST = "127.0.0.1"
PORT = 8765
WEB_ROOT = Path(__file__).resolve().parent / "dashboard_web"
TOKEN_FILE = manager.STATE / "dashboard-token"
ONBOARDING_FILE = manager.STATE / "onboarding.json"
MAX_JSON_BODY = 3 * 1024 * 1024
MAX_PLAYIT_UPLOAD = 100 * 1024 * 1024
MAX_MOD_UPLOAD = 1024 * 1024 * 1024
MAX_BACKUP_UPLOAD = 50 * 1024 * 1024 * 1024
MAX_BACKUP_EXPANDED = 250 * 1024 * 1024 * 1024
MAX_BACKUP_MEMBERS = 2_000_000
MAX_EDITABLE_FILE = 2 * 1024 * 1024
ALLOWED_ASSETS = {"/": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}
RAM_PATTERN = re.compile(r"[1-9][0-9]*[MG]", re.IGNORECASE)
EDITABLE_SUFFIXES = {
    ".cfg", ".conf", ".csv", ".ini", ".js", ".json", ".json5", ".jsonc",
    ".lang", ".list", ".md", ".properties", ".rules", ".toml", ".txt", ".xml",
    ".yaml", ".yml", ".zs",
}
HIDDEN_SERVER_DIRECTORIES = {
    ".control", ".fabric", "backups", "cache", "crash-reports", "dumps", "libraries",
    "logs", "mods", "versions",
}
PLAYER_LIST_PATTERNS = (
    re.compile(r"There are (\d+) of a max of (\d+) players online:\s*(.*)", re.IGNORECASE),
    re.compile(r"There are (\d+)/(\d+) players online:\s*(.*)", re.IGNORECASE),
    re.compile(r"There are (\d+) out of maximum (\d+) players online\.\s*(.*)", re.IGNORECASE),
)
LOG_PREFIX_PATTERN = re.compile(r"^(?:\[[^\]\r\n]+\]\s*)+:\s*")
PLAYER_NAME_PATTERN = re.compile(r"[A-Za-z0-9_]{1,16}")

job_lock = threading.Lock()
job: dict = {
    "id": None,
    "kind": None,
    "profileId": None,
    "status": "idle",
    "startedAt": None,
    "finishedAt": None,
    "lines": [],
    "message": "",
    "claimUrl": None,
}
players_lock = threading.Lock()
players_cache: dict[str, dict] = {}
performance_lock = threading.Lock()
performance_cache: dict[str, dict] = {}
performance_history: dict[str, list[dict]] = {}
filesystem_cache: dict[str, dict] = {}

LAG_PATTERN = re.compile(r"Can't keep up.*?Running (\d+)ms behind, skipping (\d+) tick", re.IGNORECASE)
LOG_TIME_PATTERN = re.compile(r"\[(\d{2}:\d{2}:\d{2})\]")
SPARK_URL_PATTERN = re.compile(r"https://spark\.lucko\.me/[A-Za-z0-9/?=&._-]+")
SENSITIVE_PROPERTY_PATTERN = re.compile(r"password|secret|token|key", re.IGNORECASE)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def public_job() -> dict:
    with job_lock:
        result = dict(job)
        result["lines"] = list(job["lines"][-120:])
        return result


def append_job_line(line: str) -> None:
    clean = line.rstrip()
    if not clean:
        return
    with job_lock:
        job["lines"].append(clean)
        job["lines"] = job["lines"][-300:]
        claim_urls = re.findall(r"https://playit\.gg/claim/[A-Za-z0-9]+", clean)
        if claim_urls:
            job["claimUrl"] = claim_urls[-1]
            job["message"] = "Playit is ready to connect. Complete the secure account claim in your browser."
        elif job.get("kind") == "playit setup":
            if "Downloading playit" in clean:
                job["message"] = "Downloading the official Playit agent…"
            elif "Waiting for Playit" in clean:
                job["message"] = "Preparing a secure Playit account claim…"


def run_job(kind: str, profile_id: str | None, command: list[str]) -> None:
    try:
        process = subprocess.Popen(
            command,
            cwd=manager.ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            append_job_line(line)
        return_code = process.wait()
        output = "\n".join(public_job()["lines"])
        claim_urls = re.findall(r"https://playit\.gg/claim/[A-Za-z0-9]+", output)
        with job_lock:
            job["finishedAt"] = now_iso()
            job["claimUrl"] = claim_urls[-1] if claim_urls else None
            if return_code == 0:
                job["status"] = "succeeded"
                job["message"] = f"{kind.title()} completed."
            elif claim_urls:
                job["status"] = "attention"
                job["message"] = "Claim the Playit agent, then start the server again."
            else:
                job["status"] = "failed"
                job["message"] = job["lines"][-1] if job["lines"] else f"{kind.title()} failed."
    except Exception as error:  # Dashboard must report background failures, never disappear.
        with job_lock:
            job["status"] = "failed"
            job["finishedAt"] = now_iso()
            job["message"] = str(error)


def start_job(kind: str, profile_id: str | None, args: list[str]) -> dict:
    with job_lock:
        if job["status"] == "running":
            raise manager.ManagerError(f"{job['kind'].title()} is already in progress.")
        job.update(
            {
                "id": secrets.token_hex(6),
                "kind": kind,
                "profileId": profile_id,
                "status": "running",
                "startedAt": now_iso(),
                "finishedAt": None,
                "lines": [],
                "message": f"{kind.title()} in progress…",
                "claimUrl": None,
            }
        )
    command = [sys.executable, "-u", str(manager.ROOT / "server_manager.py"), *args]
    threading.Thread(target=run_job, args=(kind, profile_id, command), daemon=True).start()
    return public_job()


def prepare_custom_job(kind: str, profile_id: str) -> None:
    with job_lock:
        if job["status"] == "running":
            raise manager.ManagerError(f"{job['kind'].title()} is already in progress.")
        job.update(
            {
                "id": secrets.token_hex(6), "kind": kind, "profileId": profile_id,
                "status": "running", "startedAt": now_iso(), "finishedAt": None,
                "lines": [], "message": f"{kind.title()} in progress…", "claimUrl": None,
            }
        )


def run_manager_step(args: list[str]) -> None:
    process = subprocess.Popen(
        [sys.executable, str(manager.ROOT / "server_manager.py"), *args],
        cwd=manager.ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        append_job_line(line)
    if process.wait():
        raise manager.ManagerError(public_job()["lines"][-1] if public_job()["lines"] else "Server operation failed.")


def profile_for_id(profile_id: str) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", profile_id):
        raise manager.ManagerError("Invalid server identifier.")
    return manager.profile_by_id(profile_id)


def parse_properties(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value
    return values


def update_properties(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            key = line.partition("=")[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)
    output.extend(f"{key}={value}" for key, value in remaining.items())
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    temporary.replace(path)


def tail_lines(path: Path, limit: int = 300, max_bytes: int = 256 * 1024) -> list[str]:
    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            handle.seek(max(0, size - max_bytes))
            data = handle.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        return lines[-limit:]
    except OSError:
        return []


def directory_size(path: Path) -> int:
    """Return a symlink-safe recursive size for dashboard storage diagnostics."""
    total = 0
    if not path.exists():
        return total
    try:
        for root, directories, files in os.walk(path, followlinks=False):
            directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
            for name in files:
                item = Path(root) / name
                with contextlib.suppress(OSError):
                    if not item.is_symlink():
                        total += item.stat().st_size
    except OSError:
        pass
    return total


def cached_storage(profile: dict) -> dict:
    cached = filesystem_cache.get(profile["id"])
    if cached and time.monotonic() - cached["cachedAt"] < 30:
        return {key: value for key, value in cached.items() if key != "cachedAt"}
    folder = manager.absolute_profile_path(profile)
    with contextlib.suppress(manager.ManagerError):
        world = manager.world_path(folder)
    if "world" not in locals():
        world = folder / parse_properties(folder / "server.properties").get("level-name", "world")
    usage = shutil.disk_usage(folder)
    storage = {
        "worldBytes": directory_size(world),
        "backupBytes": directory_size(folder / "backups"),
        "logBytes": directory_size(folder / "logs"),
        "modBytes": directory_size(folder / "mods"),
        "regionFiles": sum(1 for _ in world.rglob("*.mca")) if world.is_dir() else 0,
        "diskTotalBytes": usage.total,
        "diskFreeBytes": usage.free,
        "diskFreePercent": round(usage.free / usage.total * 100, 1) if usage.total else None,
        "cachedAt": time.monotonic(),
    }
    filesystem_cache[profile["id"]] = storage
    return {key: value for key, value in storage.items() if key != "cachedAt"}


def performance_capabilities(profile: dict) -> dict:
    folder = manager.absolute_profile_path(profile)
    jars = []
    for location in (folder / "mods", folder / "plugins"):
        if location.is_dir():
            jars.extend(item.name.lower() for item in location.glob("*.jar"))
    has_spark = any("spark" in name for name in jars)
    loader = str(profile.get("loader", "vanilla")).lower()
    java_path = Path(str(profile.get("javaPath", "")))
    java_bin = java_path.parent
    return {
        "loader": loader,
        "spark": has_spark,
        "forgeTps": loader == "forge",
        "fabric": loader == "fabric",
        "vanilla": loader == "vanilla",
        "jcmd": (java_bin / "jcmd").is_file(),
        "jstack": (java_bin / "jstack").is_file(),
        "playit": (manager.ROOT / "runtimes" / "playit").exists(),
        "processMetrics": True,
        "logDiagnostics": True,
        "storageMetrics": True,
        "networkThroughput": False,
        "networkThroughputReason": "Per-process packet counters are not exposed portably by the host OS.",
    }


def process_metrics(profile: dict) -> dict:
    active = manager.active_profile()
    pid = manager.read_pid(manager.SERVER_PID) if active and active["id"] == profile["id"] else None
    result = {
        "running": bool(pid), "pid": pid, "cpuPercent": None, "rssBytes": None,
        "uptime": None, "heapUsedBytes": None, "heapCommittedBytes": None,
        "heapMaximumBytes": None, "gcPauseMillis": None, "gcCount": None,
    }
    if not pid:
        return result
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "%cpu=", "-o", "rss=", "-o", "etime="],
            capture_output=True, text=True, timeout=2, check=False,
        )
        parts = completed.stdout.strip().split(None, 2)
        if len(parts) == 3:
            result.update({"cpuPercent": float(parts[0]), "rssBytes": int(parts[1]) * 1024, "uptime": parts[2]})
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return result


def local_port_latency(profile: dict) -> dict:
    active = manager.active_profile()
    if not active or active["id"] != profile["id"]:
        return {"reachable": False, "latencyMs": None, "kind": "local TCP", "reason": "Server is offline."}
    started = time.perf_counter()
    try:
        with socket.create_connection((HOST, int(profile.get("port", manager.DEFAULT_PORT))), timeout=0.75):
            elapsed = (time.perf_counter() - started) * 1000
            return {"reachable": True, "latencyMs": round(elapsed, 2), "kind": "local TCP", "reason": None}
    except OSError as error:
        return {"reachable": False, "latencyMs": None, "kind": "local TCP", "reason": str(error)}


def _read_diagnostic_lines(path: Path) -> list[str]:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                return handle.readlines()
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def diagnostic_events(profile: dict, limit: int = 160) -> list[dict]:
    folder = manager.absolute_profile_path(profile)
    log_folder = folder / "logs"
    paths = sorted((item for item in log_folder.glob("*.log.gz") if not item.name.startswith("debug")), key=lambda item: item.stat().st_mtime)[-20:]
    latest = log_folder / "latest.log"
    if latest.is_file():
        paths.append(latest)
    events: list[dict] = []
    patterns = (
        ("lag", "critical", re.compile(r"Can't keep up|server overloaded", re.I)),
        ("memory", "critical", re.compile(r"OutOfMemory|GC overhead limit", re.I)),
        ("watchdog", "critical", re.compile(r"watchdog.*(?:killed|crash|stall)|single server tick took", re.I)),
        ("network", "warning", re.compile(r"timed out|connection reset|broken pipe", re.I)),
        ("crash", "critical", re.compile(r"exception ticking world|crash report", re.I)),
        ("lifecycle", "info", re.compile(r"Done \(|Stopping server|Saving chunks", re.I)),
    )
    for path in paths:
        source_date = path.name[:10] if re.match(r"\d{4}-\d{2}-\d{2}", path.name) else time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime))
        for line in _read_diagnostic_lines(path):
            for category, severity, pattern in patterns:
                if not pattern.search(line):
                    continue
                lag = LAG_PATTERN.search(line)
                clock = LOG_TIME_PATTERN.search(line)
                events.append({
                    "category": category, "severity": severity,
                    "time": f"{source_date}T{clock.group(1) if clock else '00:00:00'}",
                    "message": line.strip()[-700:],
                    "behindMs": int(lag.group(1)) if lag else None,
                    "skippedTicks": int(lag.group(2)) if lag else None,
                    "source": path.name,
                })
                break
    unique: dict[tuple, dict] = {}
    for event in events:
        unique[(event["time"], event["category"], event["message"])] = event
    for archive in sorted((folder / "backups").glob("*.tar.gz"), key=lambda item: item.stat().st_mtime)[-40:]:
        modified = time.localtime(archive.stat().st_mtime)
        event_time = time.strftime("%Y-%m-%dT%H:%M:%S", modified)
        unique[(event_time, "backup", archive.name)] = {
            "category": "backup", "severity": "info", "time": event_time,
            "message": f"Backup completed: {archive.name} ({archive.stat().st_size} bytes)",
            "behindMs": None, "skippedTicks": None, "source": "backups",
        }
    for crash in sorted((folder / "crash-reports").glob("*.txt"), key=lambda item: item.stat().st_mtime)[-20:]:
        event_time = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(crash.stat().st_mtime))
        first_lines = "\n".join(_read_diagnostic_lines(crash)[:20])
        description = re.search(r"Description:\s*([^\r\n]+)", first_lines)
        message = f"Crash report: {crash.name}"
        if description:
            message += f" — {description.group(1).strip()}"
        unique[(event_time, "crash", crash.name)] = {
            "category": "crash", "severity": "critical", "time": event_time,
            "message": message, "behindMs": None, "skippedTicks": None, "source": "crash-reports",
        }
    return sorted(unique.values(), key=lambda item: item["time"], reverse=True)[:limit]


def host_metrics() -> dict:
    total_memory = None
    if platform.system() == "Darwin":
        with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
            output = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2, check=False).stdout.strip()
            total_memory = int(output)
    elif hasattr(os, "sysconf"):
        with contextlib.suppress(OSError, ValueError):
            total_memory = int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    return {
        "system": platform.system(), "release": platform.release(), "machine": platform.machine(),
        "cpuCount": os.cpu_count(), "loadAverage": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        "totalMemoryBytes": total_memory,
    }


def setup_status() -> dict:
    """Return user-facing onboarding checks without changing the machine."""
    profiles = manager.registry().get("profiles", [])
    bundled_playit = manager.ROOT / "runtimes" / "playit"
    playit_installed = bool(
        shutil.which("playit")
        or shutil.which("playit-cli")
        or (bundled_playit / "playit-cli.exe").is_file()
        or (bundled_playit / "playit-cli").is_file()
        or any(candidate.is_file() for candidate in manager.playit_executable_candidates())
    )
    account_ready = manager.playit_credential_ready()
    onboarding = manager.load_json(ONBOARDING_FILE) if ONBOARDING_FILE.is_file() else {}
    tunnel_confirmed = bool(onboarding.get("playitTunnelConfirmed"))
    existing_installation = bool(profiles)
    can_create_server = bool(
        sys.version_info >= (3, 10)
        and (existing_installation or (account_ready and tunnel_confirmed))
    )
    current_job = public_job()
    setup_error = (
        current_job["message"]
        if current_job.get("kind") == "playit setup" and current_job.get("status") == "failed"
        else None
    )
    return {
        "platform": platform.system(),
        "python": platform.python_version(),
        "pythonReady": sys.version_info >= (3, 10),
        "profileReady": bool(profiles),
        "playitInstalled": playit_installed,
        "playitAccountReady": account_ready,
        "playitRunning": bool(manager.read_pid(manager.PLAYIT_PID)),
        "tunnelConfirmed": tunnel_confirmed,
        "canCreateServer": can_create_server,
        "setupError": setup_error,
        "claimUrl": current_job.get("claimUrl") if current_job.get("kind") == "playit setup" else None,
        "playitManualPath": str(bundled_playit / ("playit.exe" if platform.system() == "Windows" else "playit")),
        "playitPortableUrl": manager.PLAYIT_PORTABLE_URL if platform.system() == "Windows" else None,
        "steps": [
            {"id": "runtime", "title": "BlockOps ready", "done": sys.version_info >= (3, 10), "help": "Run setup.command on macOS or setup.bat on Windows."},
            {"id": "agent", "title": "Install Playit", "done": playit_installed, "help": "BlockOps installs the official portable agent on Windows. macOS requires the official Playit download."},
            {"id": "account", "title": "Connect your account", "done": account_ready, "help": "Claim this computer's agent in your browser. BlockOps never sees your Playit password."},
            {"id": "tunnel", "title": "Create the Minecraft tunnel", "done": tunnel_confirmed, "help": "Create a Minecraft Java tunnel targeting 127.0.0.1:25565, then confirm it here."},
            {"id": "world", "title": "Create your first server", "done": bool(profiles), "help": "BlockOps will install Minecraft and the correct Java runtime."},
        ],
    }


def confirm_playit_tunnel() -> dict:
    if not manager.playit_credential_ready():
        raise manager.ManagerError("Connect the Playit agent to your account before confirming the tunnel.")
    data = manager.load_json(ONBOARDING_FILE) if ONBOARDING_FILE.is_file() else {}
    data["playitTunnelConfirmed"] = True
    data["confirmedAt"] = now_iso()
    manager.save_json(ONBOARDING_FILE, data)
    return setup_status()


def install_playit_executable(source, length: int, filename: str) -> Path:
    """Validate and atomically store a user-selected portable Playit executable."""
    if platform.system() != "Windows":
        raise manager.ManagerError("Manual Playit file setup is only available on Windows.")
    if length <= 0 or length > MAX_PLAYIT_UPLOAD:
        raise manager.ManagerError("Choose a Playit .exe smaller than 100 MB.")
    if not Path(filename).name.lower().endswith(".exe"):
        raise manager.ManagerError("Choose the portable Playit Windows .exe, not the .msi installer.")
    destination = manager.ROOT / "runtimes" / "playit" / "playit.exe"
    temporary = destination.with_suffix(".exe.upload")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("wb") as handle:
            remaining = length
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise manager.ManagerError("The Playit file transfer ended unexpectedly.")
                handle.write(chunk)
                remaining -= len(chunk)
        with temporary.open("rb") as handle:
            if handle.read(2) != b"MZ":
                raise manager.ManagerError("That file is not a valid Windows executable.")
        temporary.replace(destination)
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def parse_spark_observations(profile: dict) -> dict:
    folder = manager.absolute_profile_path(profile)
    text = "\n".join(tail_lines(folder / "logs" / "latest.log", limit=700, max_bytes=768 * 1024))
    clean = re.sub(r"\x1b\[[0-9;]*m|§.", "", text)
    urls = SPARK_URL_PATTERN.findall(clean)
    tps = None
    mspt = None
    tps_matches = re.findall(r"(?:TPS[^:\n]*:\s*|TPS\s*=\s*)(20(?:\.0+)?|1?\d(?:\.\d+)?)", clean, re.I)
    mspt_matches = re.findall(r"(?:MSPT|mean tick time)[^:\n]*:\s*(\d+(?:\.\d+)?)", clean, re.I)
    if tps_matches:
        tps = float(tps_matches[-1])
    if mspt_matches:
        mspt = float(mspt_matches[-1])
    return {"tps": tps, "mspt": mspt, "reportUrls": list(dict.fromkeys(urls))[-8:]}


def performance_alerts(profile: dict, snapshot: dict, events: list[dict]) -> list[dict]:
    alerts: list[dict] = []
    properties = parse_properties(manager.absolute_profile_path(profile) / "server.properties")
    if properties.get("max-tick-time") == "-1":
        alerts.append({"severity": "warning", "title": "Watchdog disabled", "detail": "A permanent tick-thread stall will not be terminated automatically."})
    free = snapshot["storage"].get("diskFreePercent")
    if free is not None and free < 15:
        alerts.append({"severity": "critical", "title": "Low disk space", "detail": f"Only {free}% of this disk is free."})
    recent_lag = [event for event in events if event["category"] == "lag"]
    if recent_lag:
        worst = max(event.get("behindMs") or 0 for event in recent_lag)
        alerts.append({"severity": "warning", "title": f"{len(recent_lag)} retained lag events", "detail": f"Worst recorded stall was {worst / 1000:.1f} seconds behind."})
    cpu = snapshot["process"].get("cpuPercent")
    if cpu is not None and cpu >= 90:
        alerts.append({"severity": "warning", "title": "One-core CPU pressure", "detail": f"The Java process is using {cpu:.0f}% CPU; Minecraft's tick loop is commonly bounded by one hot core."})
    return alerts


def performance_recommendations(profile: dict, snapshot: dict) -> list[dict]:
    properties = parse_properties(manager.absolute_profile_path(profile) / "server.properties")
    recommendations = [
        {"category": "Measure", "risk": "none", "title": "Capture a busy-period baseline", "detail": "Compare idle, ordinary play, and new-chunk exploration before changing settings."},
        {"category": "Storage", "risk": "low", "title": "Correlate backups with slow ticks", "detail": "Use the event timeline to determine whether compression and world saves overlap stalls."},
        {"category": "World", "risk": "medium", "title": "Profile new-chunk generation", "detail": "If exploration dominates tick time, test compatible world pre-generation on a copy."},
        {"category": "Entities", "risk": "medium", "title": "Find expensive entity classes", "detail": "Use a slow-tick profile before changing spawn, AI, or despawn rules."},
        {"category": "JVM", "risk": "medium", "title": "Compare heap and collector baselines", "detail": "Record GC pauses first; then test one reversible JVM configuration at a time."},
        {"category": "Network", "risk": "low", "title": "Separate host and route latency", "detail": "Compare local reachability, tunnel health, and individual player ping."},
    ]
    try:
        view_distance = int(properties.get("view-distance", "10"))
        if view_distance > 6:
            recommendations.append({"category": "World", "risk": "low", "title": f"Benchmark view distance {view_distance} versus {view_distance - 1}", "detail": "A lower distance can reduce ticking, generation, entity population, and chunk traffic."})
    except ValueError:
        pass
    return recommendations


def performance_snapshot(profile: dict, *, force: bool = False) -> dict:
    cached = performance_cache.get(profile["id"])
    if cached and not force and time.monotonic() - cached["cachedAt"] < 1.5:
        return {key: value for key, value in cached.items() if key != "cachedAt"}
    with performance_lock:
        process = process_metrics(profile)
        observations = parse_spark_observations(profile)
        events = diagnostic_events(profile)
        snapshot = {
            "profileId": profile["id"], "generatedAt": now_iso(),
            "process": process, "tick": {"tps": observations["tps"], "mspt": observations["mspt"], "source": "spark/log" if observations["tps"] is not None else None},
            "network": {"local": local_port_latency(profile), "playitRunning": bool(manager.read_pid(manager.PLAYIT_PID)), "bytesInPerSecond": None, "bytesOutPerSecond": None},
            "storage": cached_storage(profile), "capabilities": performance_capabilities(profile),
            "host": host_metrics(),
            "events": events, "reports": observations["reportUrls"],
        }
        point = {"time": time.time(), "cpu": process["cpuPercent"], "rss": process["rssBytes"], "tps": observations["tps"], "mspt": observations["mspt"], "latency": snapshot["network"]["local"]["latencyMs"]}
        history = performance_history.setdefault(profile["id"], [])
        if not history or point["time"] - history[-1]["time"] >= 1:
            history.append(point)
            del history[:-300]
        snapshot["history"] = list(history)
        snapshot["alerts"] = performance_alerts(profile, snapshot, events)
        snapshot["recommendations"] = performance_recommendations(profile, snapshot)
        snapshot["cachedAt"] = time.monotonic()
        performance_cache[profile["id"]] = snapshot
        return {key: value for key, value in snapshot.items() if key != "cachedAt"}


def performance_report(profile: dict) -> dict:
    folder = manager.absolute_profile_path(profile)
    properties = {
        key: ("[redacted]" if SENSITIVE_PROPERTY_PATTERN.search(key) else value)
        for key, value in parse_properties(folder / "server.properties").items()
    }
    mods = []
    mod_folder = folder / "mods"
    if mod_folder.is_dir():
        for item in sorted(mod_folder.glob("*.jar"), key=lambda value: value.name.lower()):
            mods.append({"name": item.name, "bytes": item.stat().st_size})
    return {
        "format": "BlockOps performance report v1", "generatedAt": now_iso(),
        "server": {key: profile.get(key) for key in ("id", "name", "minecraftVersion", "loader", "loaderVersion", "javaMajor", "minimumRam", "maximumRam", "jvmArguments", "port")},
        "host": host_metrics(),
        "properties": properties, "mods": mods, "diagnostics": performance_snapshot(profile, force=True),
    }


PERFORMANCE_COMMANDS = {
    "spark-tps": ("spark", "spark tps", "Requested a Spark TPS and CPU reading."),
    "spark-health": ("spark", "spark health --memory --network", "Requested a Spark health report."),
    "spark-profile-30": ("spark", "spark profiler start --timeout 30", "Started a 30-second Spark profile."),
    "spark-profile-60": ("spark", "spark profiler start --timeout 60", "Started a 60-second Spark profile."),
    "spark-profile-slow": ("spark", "spark profiler start --timeout 60 --only-ticks-over 100", "Started a slow-tick-only Spark profile."),
    "spark-profile-stop": ("spark", "spark profiler stop", "Stopped the Spark profile and requested its report."),
    "spark-profile-cancel": ("spark", "spark profiler cancel", "Cancelled the active Spark profile."),
    "spark-gc": ("spark", "spark gc", "Requested Spark garbage-collection history."),
    "spark-heapsummary": ("spark", "spark heapsummary", "Requested a Spark heap summary."),
    "spark-ping": ("spark", "spark ping", "Requested player ping statistics."),
    "forge-tps": ("forgeTps", "forge tps", "Requested Forge per-dimension TPS."),
}


def run_performance_action(profile: dict, action: str) -> dict:
    entry = PERFORMANCE_COMMANDS.get(action)
    if not entry:
        raise manager.ManagerError("Unknown diagnostic action.")
    capability, command, message = entry
    capabilities = performance_capabilities(profile)
    if not capabilities.get(capability):
        raise manager.ManagerError("This diagnostic is not supported by the selected server.")
    active = manager.active_profile()
    if not active or active["id"] != profile["id"] or not manager.port_open(int(profile.get("port", manager.DEFAULT_PORT))):
        raise manager.ManagerError("Start this server before running live diagnostics.")
    manager.send_command(command)
    performance_cache.pop(profile["id"], None)
    return {"ok": True, "message": message, "command": command}


def parse_player_list(output: str) -> dict | None:
    """Parse Vanilla/Fabric/Forge `list` output across Minecraft generations."""
    lines = output.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        plain = re.sub(r"\x1b\[[0-9;]*m", "", line)
        for pattern in PLAYER_LIST_PATTERNS:
            match = pattern.search(plain)
            if not match:
                continue
            online = int(match.group(1))
            maximum = int(match.group(2))
            if online == 0:
                return {"online": online, "maximum": maximum, "players": []}

            # Some Forge 1.12 servers log the count and player names as two
            # separate messages. Do not cache an empty result after seeing only
            # the first line; wait for the names line to arrive instead.
            candidates = [match.group(3)]
            candidates.extend(lines[index + 1:index + 4])
            for candidate in candidates:
                candidate = re.sub(r"\x1b\[[0-9;]*m", "", candidate)
                candidate = LOG_PREFIX_PATTERN.sub("", candidate).strip()
                names = [name.strip() for name in candidate.split(",") if name.strip()]
                if len(names) == online and all(PLAYER_NAME_PATTERN.fullmatch(name) for name in names):
                    return {"online": online, "maximum": maximum, "players": names}
            return None
    return None


def read_log_since(path: Path, offset: int) -> str:
    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            handle.seek(offset if size >= offset else 0)
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def player_status(profile: dict, *, force: bool = False) -> dict:
    """Return an exact live player list using Minecraft's read-only list command."""
    folder = manager.absolute_profile_path(profile)
    properties = parse_properties(folder / "server.properties")
    maximum = int(properties.get("max-players", "20") or 20)
    active = manager.active_profile()
    if not active or active["id"] != profile["id"] or not manager.port_open(int(profile.get("port", manager.DEFAULT_PORT))):
        return {"online": 0, "maximum": maximum, "players": [], "status": "offline", "updatedAt": now_iso()}
    cached = players_cache.get(profile["id"])
    if cached and not force and time.monotonic() - cached["cachedAt"] < 8:
        return {key: value for key, value in cached.items() if key != "cachedAt"}
    with players_lock:
        cached = players_cache.get(profile["id"])
        if cached and not force and time.monotonic() - cached["cachedAt"] < 8:
            return {key: value for key, value in cached.items() if key != "cachedAt"}
        log_path = folder / "logs" / "latest.log"
        offset = log_path.stat().st_size if log_path.exists() else 0
        try:
            manager.send_command("list")
        except manager.ManagerError:
            if cached:
                result = {key: value for key, value in cached.items() if key != "cachedAt"}
                result["status"] = "stale"
                return result
            return {"online": 0, "maximum": maximum, "players": [], "status": "waiting", "updatedAt": now_iso()}
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            parsed = parse_player_list(read_log_since(log_path, offset))
            if parsed:
                parsed.update({"status": "online", "updatedAt": now_iso(), "cachedAt": time.monotonic()})
                players_cache[profile["id"]] = parsed
                return {key: value for key, value in parsed.items() if key != "cachedAt"}
            time.sleep(0.1)
        if cached:
            result = {key: value for key, value in cached.items() if key != "cachedAt"}
            result["status"] = "stale"
            return result
        return {"online": 0, "maximum": maximum, "players": [], "status": "waiting", "updatedAt": now_iso()}


def profile_summary(profile: dict, active_id: str | None) -> dict:
    folder = manager.absolute_profile_path(profile)
    properties = parse_properties(folder / "server.properties")
    mods = list((folder / "mods").rglob("*.jar")) if (folder / "mods").is_dir() else []
    disabled = list((folder / "mods").rglob("*.jar.disabled")) if (folder / "mods").is_dir() else []
    backups = list((folder / "backups").glob("*.tar.gz")) if (folder / "backups").is_dir() else []
    running = profile["id"] == active_id
    return {
        "id": profile["id"],
        "name": profile["name"],
        "minecraftVersion": profile["minecraftVersion"],
        "loader": profile["loader"],
        "loaderVersion": profile.get("loaderVersion", ""),
        "minimumRam": profile.get("minimumRam", "2G"),
        "maximumRam": profile.get("maximumRam", "6G"),
        "port": int(profile.get("port", manager.DEFAULT_PORT)),
        "running": running,
        "status": "online" if running and manager.port_open(int(profile.get("port", manager.DEFAULT_PORT))) else ("starting" if running else "offline"),
        "folderExists": folder.is_dir(),
        "path": str(folder),
        "modsCount": len(mods),
        "disabledModsCount": len(disabled),
        "backupsCount": len(backups),
        "properties": {
            "motd": properties.get("motd", profile["name"]),
            "gamemode": properties.get("gamemode", "survival"),
            "difficulty": properties.get("difficulty", "normal"),
            "maxPlayers": properties.get("max-players", "20"),
            "whiteList": properties.get("white-list", "false") == "true",
            "hardcore": properties.get("hardcore", "false") == "true",
            "onlineMode": properties.get("online-mode", "true") == "true",
            "pvp": properties.get("pvp", "true") == "true",
        },
        "jvmArguments": profile.get("jvmArguments", []),
        "backupSettings": manager.backup_settings(profile),
    }


def dashboard_state() -> dict:
    profiles = manager.registry()["profiles"]
    active = manager.active_profile()
    active_id = active["id"] if active else None
    playit_pid = manager.read_pid(manager.PLAYIT_PID)
    return {
        "profiles": [profile_summary(profile, active_id) for profile in profiles],
        "activeProfileId": active_id,
        "minecraftRunning": bool(active_id),
        "playitRunning": bool(playit_pid),
        "job": public_job(),
        "serverTime": now_iso(),
        "setup": setup_status(),
    }


def list_backups(profile: dict) -> list[dict]:
    folder = manager.absolute_profile_path(profile) / "backups"
    result = []
    if folder.is_dir():
        for item in sorted(folder.glob("*.tar.gz"), key=lambda value: value.stat().st_mtime, reverse=True):
            stat = item.stat()
            result.append({"name": item.name, "bytes": stat.st_size, "modified": stat.st_mtime})
    return result


def list_mods(profile: dict) -> list[dict]:
    folder = manager.absolute_profile_path(profile) / "mods"
    result = []
    if folder.is_dir():
        for item in sorted(folder.rglob("*"), key=lambda value: value.relative_to(folder).as_posix().lower()):
            if item.is_file() and (item.name.endswith(".jar") or item.name.endswith(".jar.disabled")):
                relative = item.relative_to(folder).as_posix()
                result.append(
                    {
                        "name": relative,
                        "enabled": item.name.endswith(".jar"),
                        "bytes": item.stat().st_size,
                        "modified": item.stat().st_mtime,
                    }
                )
    return result


def cached_minecraft_versions() -> list[str]:
    path = manager.CACHE / "version_manifest_v2.json"
    if not path.exists():
        return []
    try:
        data = manager.load_json(path)
        return [item["id"] for item in data.get("versions", []) if item.get("type") == "release"][:150]
    except (OSError, ValueError, KeyError):
        return []


def safe_config_path(root: Path, relative: str) -> Path:
    """Resolve an explorer path while rejecting traversal and symlink escapes."""
    normalized = relative.replace("\\", "/").strip("/")
    candidate_relative = Path(normalized) if normalized else Path()
    if candidate_relative.is_absolute() or ".." in candidate_relative.parts or "\x00" in relative:
        raise manager.ManagerError("Invalid configuration path.")
    candidate = (root / candidate_relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise manager.ManagerError("Configuration paths must stay inside this server.")
    return candidate


def config_roots(profile: dict) -> list[dict]:
    folder = manager.absolute_profile_path(profile)
    roots = [{"id": "server", "name": "Server files", "description": "Top-level server and modpack folders"}]
    known = (
        ("config", "Mod configs", folder / "config", "Forge, Fabric, and mod configuration"),
        ("defaultconfigs", "Default configs", folder / "defaultconfigs", "Defaults copied into new worlds"),
        ("scripts", "Scripts", folder / "scripts", "CraftTweaker and pack scripts"),
        ("kubejs", "KubeJS", folder / "kubejs", "KubeJS startup, server, and client scripts"),
        ("resources", "Resources", folder / "resources", "Pack-provided data and text resources"),
    )
    for root_id, name, path, description in known:
        if path.is_dir():
            roots.append({"id": root_id, "name": name, "description": description})
    with contextlib.suppress(manager.ManagerError):
        world_configs = manager.world_path(folder) / "serverconfig"
        if world_configs.is_dir():
            roots.append({"id": "world-serverconfig", "name": "World configs", "description": "Settings for this specific world"})
    return roots


def config_root_path(profile: dict, root_id: str) -> Path:
    folder = manager.absolute_profile_path(profile)
    mapping = {
        "server": folder,
        "config": folder / "config",
        "defaultconfigs": folder / "defaultconfigs",
        "scripts": folder / "scripts",
        "kubejs": folder / "kubejs",
        "resources": folder / "resources",
    }
    with contextlib.suppress(manager.ManagerError):
        mapping["world-serverconfig"] = manager.world_path(folder) / "serverconfig"
    root = mapping.get(root_id)
    available = {item["id"] for item in config_roots(profile)}
    if not root or root_id not in available or not root.is_dir():
        raise manager.ManagerError("That configuration location is unavailable for this server.")
    return root.resolve()


def editable_text_file(path: Path) -> bool:
    if path.suffix.lower() not in EDITABLE_SUFFIXES or not path.is_file():
        return False
    try:
        if path.stat().st_size > MAX_EDITABLE_FILE:
            return False
        return b"\x00" not in path.read_bytes()[:8192]
    except OSError:
        return False


def list_config_directory(profile: dict, root_id: str, relative: str) -> dict:
    root = config_root_path(profile, root_id)
    directory = safe_config_path(root, relative)
    if not directory.is_dir():
        raise manager.ManagerError("Configuration folder was not found.")
    entries = []
    hidden_root_directories = set(HIDDEN_SERVER_DIRECTORIES)
    with contextlib.suppress(manager.ManagerError):
        hidden_root_directories.add(manager.world_path(manager.absolute_profile_path(profile)).name)
    for item in sorted(directory.iterdir(), key=lambda value: (not value.is_dir(), value.name.lower())):
        if item.is_symlink() or item.name == ".DS_Store" or item.name.startswith(".blockops"):
            continue
        if root_id == "server" and directory == root and item.is_dir() and item.name in hidden_root_directories:
            continue
        relative_path = item.relative_to(root).as_posix()
        entries.append(
            {
                "name": item.name,
                "path": relative_path,
                "directory": item.is_dir(),
                "editable": editable_text_file(item) if item.is_file() else False,
                "bytes": item.stat().st_size if item.is_file() else None,
                "modified": item.stat().st_mtime,
            }
        )
    return {"root": root_id, "path": Path(relative).as_posix() if relative else "", "entries": entries}


def read_config_file(profile: dict, root_id: str, relative: str) -> dict:
    root = config_root_path(profile, root_id)
    path = safe_config_path(root, relative)
    if not editable_text_file(path):
        raise manager.ManagerError("This file is binary, unsupported, or larger than 2 MB.")
    content = path.read_text(encoding="utf-8", errors="strict")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "root": root_id,
        "path": path.relative_to(root).as_posix(),
        "name": path.name,
        "content": content,
        "hash": digest,
        "bytes": path.stat().st_size,
        "modified": path.stat().st_mtime,
    }


def save_config_file(profile: dict, root_id: str, relative: str, content: str, expected_hash: str) -> dict:
    if len(content.encode("utf-8")) > MAX_EDITABLE_FILE:
        raise manager.ManagerError("Configuration files must remain smaller than 2 MB.")
    root = config_root_path(profile, root_id)
    path = safe_config_path(root, relative)
    if not editable_text_file(path):
        raise manager.ManagerError("This configuration file cannot be edited here.")
    current = path.read_text(encoding="utf-8", errors="strict")
    current_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
    if expected_hash and not secrets.compare_digest(expected_hash, current_hash):
        raise manager.ManagerError("This file changed on disk. Reload it before saving your edits.")
    folder = manager.absolute_profile_path(profile)
    timestamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
    backup = folder / ".blockops-history" / "config-files" / timestamp / root_id / path.relative_to(root)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    temporary = path.with_suffix(path.suffix + ".blockops-save")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return read_config_file(profile, root_id, relative)


def save_profile_settings(profile_id: str, payload: dict) -> None:
    profile = profile_for_id(profile_id)
    minimum = str(payload.get("minimumRam", profile.get("minimumRam", "2G"))).upper()
    maximum = str(payload.get("maximumRam", profile.get("maximumRam", "6G"))).upper()
    if not RAM_PATTERN.fullmatch(minimum) or not RAM_PATTERN.fullmatch(maximum):
        raise manager.ManagerError("RAM values must look like 2G or 4096M.")
    name = str(payload.get("name", profile["name"])).strip()
    if not name or len(name) > 80:
        raise manager.ManagerError("Server name must be between 1 and 80 characters.")
    raw_jvm = payload.get("jvmArguments", [])
    if not isinstance(raw_jvm, list) or not all(isinstance(value, str) and len(value) <= 200 for value in raw_jvm):
        raise manager.ManagerError("JVM arguments must be a list of short values.")
    profile["name"] = name
    profile["minimumRam"] = minimum
    profile["maximumRam"] = maximum
    profile["jvmArguments"] = [value.strip() for value in raw_jvm if value.strip()]
    data = manager.registry()
    for index, current in enumerate(data["profiles"]):
        if current["id"] == profile_id:
            data["profiles"][index] = profile
            break
    manager.save_json(manager.REGISTRY, data)
    folder = manager.absolute_profile_path(profile)
    manager.save_json(folder / "profile.json", profile)
    properties = payload.get("properties", {})
    allowed = {
        "motd": "motd",
        "gamemode": "gamemode",
        "difficulty": "difficulty",
        "maxPlayers": "max-players",
        "whiteList": "white-list",
        "hardcore": "hardcore",
        "onlineMode": "online-mode",
        "pvp": "pvp",
    }
    updates: dict[str, str] = {}
    if isinstance(properties, dict):
        for source, destination in allowed.items():
            if source not in properties:
                continue
            value = properties[source]
            if isinstance(value, bool):
                updates[destination] = "true" if value else "false"
            else:
                clean = str(value).replace("\r", " ").replace("\n", " ")[:250]
                updates[destination] = clean
    update_properties(folder / "server.properties", updates)


def save_backup_settings(profile_id: str, payload: dict) -> dict:
    profile = profile_for_id(profile_id)
    try:
        interval = int(payload.get("intervalMinutes", 10))
        retention = int(payload.get("retention", 12))
        compression = int(payload.get("compressionLevel", 6))
    except (TypeError, ValueError) as error:
        raise manager.ManagerError("Backup schedule values must be whole numbers.") from error
    if not 5 <= interval <= 1440:
        raise manager.ManagerError("Backup frequency must be between 5 and 1,440 minutes.")
    if not 1 <= retention <= 100:
        raise manager.ManagerError("Keep between 1 and 100 backups.")
    if not 1 <= compression <= 9:
        raise manager.ManagerError("Compression level must be between 1 and 9.")
    settings = {
        "enabled": bool(payload.get("enabled", True)),
        "intervalMinutes": interval,
        "retention": retention,
        "compressionLevel": compression,
        "backupOnStop": bool(payload.get("backupOnStop", False)),
        "onlyWhenEmpty": bool(payload.get("onlyWhenEmpty", False)),
    }
    profile["backupSettings"] = settings
    data = manager.registry()
    for index, current in enumerate(data["profiles"]):
        if current["id"] == profile_id:
            data["profiles"][index] = profile
            break
    manager.save_json(manager.REGISTRY, data)
    manager.save_json(manager.absolute_profile_path(profile) / "profile.json", profile)
    return settings


def create_offline_backup(profile: dict, *, label: str = "", prune: bool = True) -> str:
    folder = manager.absolute_profile_path(profile)
    world = manager.world_path(folder)
    if not world.is_dir():
        raise manager.ManagerError("This server does not have a world to back up yet.")
    backups = folder / "backups"
    backups.mkdir(exist_ok=True)
    suffix = f"-{label}" if label else ""
    destination = backups / f"{world.name}{suffix}-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}.tar.gz"
    temporary = destination.with_suffix(destination.suffix + ".part")
    settings = manager.backup_settings(profile)
    try:
        with tarfile.open(temporary, "w:gz", compresslevel=settings["compressionLevel"]) as archive:
            archive.add(world, arcname=world.name, recursive=True)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    if prune:
        prune_profile_backups(profile)
    return destination.name


def prune_profile_backups(profile: dict) -> None:
    """Keep the configured number of automatic, manual, and imported snapshots."""
    folder = manager.absolute_profile_path(profile)
    world = manager.world_path(folder)
    backups = folder / "backups"
    completed = sorted(backups.glob(f"{world.name}-*.tar.gz"), key=lambda item: item.stat().st_mtime)
    for expired in completed[:-manager.backup_settings(profile)["retention"]]:
        expired.unlink(missing_ok=True)


def backup_file(profile: dict, name: str) -> Path:
    if Path(name).name != name or not name.endswith(".tar.gz"):
        raise manager.ManagerError("Invalid backup name.")
    path = manager.absolute_profile_path(profile) / "backups" / name
    if not path.is_file():
        raise manager.ManagerError("Backup file was not found.")
    return path


def validate_backup_archive(profile: dict, archive_path: Path) -> str:
    """Validate an archive and return the single top-level Minecraft world folder."""
    roots: set[str] = set()
    expanded_bytes = 0
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            member_count = 0
            for member in archive:
                member_count += 1
                if member_count > MAX_BACKUP_MEMBERS:
                    raise manager.ManagerError("The selected backup contains too many files.")
                if "\\" in member.name:
                    raise manager.ManagerError("The selected backup contains an unsafe entry.")
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or ".." in relative.parts or member.issym() or member.islnk() or member.isdev():
                    raise manager.ManagerError("The selected backup contains an unsafe entry.")
                expanded_bytes += max(0, member.size)
                if expanded_bytes > MAX_BACKUP_EXPANDED:
                    raise manager.ManagerError("The selected backup expands beyond the 250 GB safety limit.")
                clean_parts = tuple(part for part in relative.parts if part not in {"", "."})
                if len(clean_parts) == 2 and clean_parts[1] == "level.dat" and member.isfile():
                    roots.add(clean_parts[0])
            if member_count == 0:
                raise manager.ManagerError("The selected backup is empty.")
    except (tarfile.TarError, EOFError) as error:
        raise manager.ManagerError("The selected file is not a readable .tar.gz world backup.") from error
    if len(roots) != 1:
        raise manager.ManagerError("The backup must contain exactly one world folder with level.dat at its top level.")
    return next(iter(roots))


def apply_backup_archive(profile: dict, archive_path: Path) -> None:
    """Replace a stopped world's data with a validated archive, rolling back on failure."""
    folder = manager.absolute_profile_path(profile)
    world = manager.world_path(folder)
    staging = Path(tempfile.mkdtemp(prefix=".blockops-restore-", dir=folder))
    rollback = folder / f".blockops-restore-rollback-{time.time_ns()}"
    moved_original = False
    installed_restore = False
    try:
        archive_world_root = validate_backup_archive(profile, archive_path)
        with tarfile.open(archive_path, "r:gz") as archive:
            if sys.version_info >= (3, 12):
                archive.extractall(staging, filter="data")
            else:
                # Members were explicitly path- and link-validated above.
                archive.extractall(staging)
        restored = staging / archive_world_root
        if not restored.is_dir() or not (restored / "level.dat").is_file():
            raise manager.ManagerError("The extracted backup does not contain a valid Minecraft world.")
        if world.exists():
            world.replace(rollback)
            moved_original = True
        restored.replace(world)
        installed_restore = True
    except Exception:
        if installed_restore and world.exists():
            shutil.rmtree(world, ignore_errors=True)
        if moved_original and rollback.exists():
            rollback.replace(world)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if rollback.exists():
        shutil.rmtree(rollback, ignore_errors=True)


def run_restore_job(profile_id: str, backup_name: str) -> None:
    profile = profile_for_id(profile_id)
    active = manager.active_profile()
    was_running = bool(active and active["id"] == profile_id)
    stopped = False
    restored = False
    try:
        if not active and manager.port_open(int(profile.get("port", manager.DEFAULT_PORT))):
            raise manager.ManagerError("The Minecraft port is in use by an unmanaged server; restore was cancelled.")
        archive_path = backup_file(profile, backup_name)
        append_job_line(f"Selected backup: {backup_name}")
        if was_running:
            append_job_line("Saving and stopping the running server safely…")
            run_manager_step(["stop", "instance"])
            stopped = True
        if manager.world_path(manager.absolute_profile_path(profile)).is_dir():
            append_job_line("Creating a pre-restore safety snapshot…")
            safety_name = create_offline_backup(profile, label="before-restore", prune=False)
            append_job_line(f"Safety snapshot created: {safety_name}")
        else:
            append_job_line("No existing world progress was found; no safety snapshot is needed.")
        append_job_line("Validating and applying the selected world backup…")
        apply_backup_archive(profile, archive_path)
        restored = True
        append_job_line("World backup applied and verified.")
        prune_profile_backups(profile)
        if was_running:
            append_job_line("Restarting the server…")
            run_manager_step(["start", "instance", "--profile", profile_id])
        with job_lock:
            job["status"] = "succeeded"
            job["finishedAt"] = now_iso()
            job["message"] = "Backup restored safely" + (" and the server is online." if was_running else ".")
    except Exception as error:
        append_job_line(f"Restore failed: {error}")
        if was_running and stopped and not restored:
            append_job_line("The original world was preserved. Restarting it…")
            with contextlib.suppress(Exception):
                run_manager_step(["start", "instance", "--profile", profile_id])
        with job_lock:
            job["status"] = "failed"
            job["finishedAt"] = now_iso()
            job["message"] = str(error)


def start_restore_job(profile: dict, backup_name: str) -> dict:
    backup_file(profile, backup_name)
    prepare_custom_job("restore backup", profile["id"])
    threading.Thread(target=run_restore_job, args=(profile["id"], backup_name), daemon=True).start()
    return public_job()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "BlockOps/1"

    def log_message(self, format_string: str, *args) -> None:
        if self.path != "/api/state":
            sys.stderr.write(f"{self.address_string()} - {format_string % args}\n")

    def security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        )

    def send_json(self, value, status: int = 200) -> None:
        data = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(data)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json({"error": message}, status)

    def token_cookie(self) -> str | None:
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("mc_dashboard")
        return morsel.value if morsel else None

    def authenticated(self) -> bool:
        return secrets.compare_digest(self.token_cookie() or "", self.server.dashboard_token)  # type: ignore[attr-defined]

    def require_auth(self) -> bool:
        if self.authenticated():
            return True
        self.send_error_json(HTTPStatus.UNAUTHORIZED, "Open the dashboard from the BlockOps launcher.")
        return False

    def valid_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return not origin or origin in {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_JSON_BODY:
            raise manager.ManagerError("Request is too large.")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as error:
            raise manager.ManagerError("Invalid request data.") from error
        if not isinstance(value, dict):
            raise manager.ManagerError("Request data must be an object.")
        return value

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/launch/"):
            supplied = path.removeprefix("/launch/")
            if secrets.compare_digest(supplied, self.server.dashboard_token):  # type: ignore[attr-defined]
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", f"mc_dashboard={supplied}; HttpOnly; SameSite=Strict; Path=/")
                self.security_headers()
                self.end_headers()
            else:
                self.send_error_json(HTTPStatus.FORBIDDEN, "Invalid dashboard link.")
            return
        if path == "/api/health":
            self.send_json({"ok": True})
            return
        if path.startswith("/api/"):
            if not self.require_auth():
                return
            try:
                self.handle_api_get(path, urllib.parse.parse_qs(parsed.query))
            except (manager.ManagerError, OSError, ValueError) as error:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))
            return
        if not self.authenticated():
            self.send_error_json(HTTPStatus.UNAUTHORIZED, "Launch BlockOps from its app or platform launcher.")
            return
        asset = ALLOWED_ASSETS.get(path)
        if not asset:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return
        file_path = WEB_ROOT / asset
        try:
            data = file_path.read_bytes()
        except OSError:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Dashboard asset is missing.")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(data)

    def handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/state":
            self.send_json(dashboard_state())
        elif path == "/api/versions":
            self.send_json({"versions": cached_minecraft_versions()})
        elif path == "/api/setup":
            self.send_json(setup_status())
        elif path == "/api/log":
            profile = profile_for_id(query.get("profile", [""])[0])
            folder = manager.absolute_profile_path(profile)
            log_path = folder / "logs" / "latest.log"
            lines = tail_lines(log_path)
            if not lines:
                lines = tail_lines(folder / "profile-runner.log")
            self.send_json({"lines": lines, "source": log_path.name})
        elif path == "/api/players":
            profile = profile_for_id(query.get("profile", [""])[0])
            self.send_json(player_status(profile, force=query.get("refresh", ["0"])[0] == "1"))
        elif path == "/api/performance":
            profile = profile_for_id(query.get("profile", [""])[0])
            self.send_json(performance_snapshot(profile, force=query.get("refresh", ["0"])[0] == "1"))
        elif path == "/api/performance/report":
            profile = profile_for_id(query.get("profile", [""])[0])
            self.send_json(performance_report(profile))
        elif path == "/api/config/roots":
            profile = profile_for_id(query.get("profile", [""])[0])
            self.send_json({"roots": config_roots(profile)})
        elif path == "/api/config/list":
            profile = profile_for_id(query.get("profile", [""])[0])
            self.send_json(list_config_directory(profile, query.get("root", ["config"])[0], query.get("path", [""])[0]))
        elif path == "/api/config/file":
            profile = profile_for_id(query.get("profile", [""])[0])
            self.send_json(read_config_file(profile, query.get("root", ["config"])[0], query.get("path", [""])[0]))
        else:
            match = re.fullmatch(r"/api/profiles/([^/]+)/(backups|mods)", path)
            if not match:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Unknown API route.")
                return
            profile = profile_for_id(match.group(1))
            values = list_backups(profile) if match.group(2) == "backups" else list_mods(profile)
            self.send_json({match.group(2): values})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/") or not self.require_auth():
            return
        if not self.valid_origin():
            self.send_error_json(HTTPStatus.FORBIDDEN, "Invalid request origin.")
            return
        try:
            payload = self.read_json()
            self.handle_api_post(parsed.path, payload)
        except (manager.ManagerError, OSError, ValueError) as error:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))

    def handle_api_post(self, path: str, payload: dict) -> None:
        if path == "/api/jobs/setup-playit":
            self.send_json(start_job("playit setup", None, ["setup", "playit"]), HTTPStatus.ACCEPTED)
        elif path == "/api/setup/tunnel-confirmed":
            self.send_json(confirm_playit_tunnel())
        elif path == "/api/jobs/start":
            profile = profile_for_id(str(payload.get("profileId", "")))
            self.send_json(start_job("start", profile["id"], ["start", "instance", "--profile", profile["id"]]), HTTPStatus.ACCEPTED)
        elif path == "/api/jobs/stop":
            self.send_json(start_job("stop", manager.active_profile()["id"] if manager.active_profile() else None, ["stop", "instance"]), HTTPStatus.ACCEPTED)
        elif path == "/api/jobs/create":
            if not manager.registry().get("profiles") and not setup_status()["canCreateServer"]:
                raise manager.ManagerError(
                    "Finish Playit setup before creating your first server. Open Setup Guide to continue."
                )
            name = str(payload.get("name", "")).strip()
            version = str(payload.get("minecraftVersion", "")).strip()
            loader = str(payload.get("loader", "vanilla")).lower()
            loader_version = str(payload.get("loaderVersion", "")).strip()
            minimum = str(payload.get("minimumRam", "2G")).upper()
            maximum = str(payload.get("maximumRam", "6G")).upper()
            if not name or not version or loader not in {"vanilla", "fabric", "forge"}:
                raise manager.ManagerError("Name, Minecraft version, and a valid loader are required.")
            if not RAM_PATTERN.fullmatch(minimum) or not RAM_PATTERN.fullmatch(maximum):
                raise manager.ManagerError("RAM values must look like 2G or 4096M.")
            args = ["create", "instance", "--name", name, "--minecraft", version, "--loader", loader, "--min-ram", minimum, "--max-ram", maximum]
            if loader_version:
                args.extend(["--loader-version", loader_version])
            self.send_json(start_job("create", None, args), HTTPStatus.ACCEPTED)
        elif path == "/api/jobs/restore-backup":
            profile = profile_for_id(str(payload.get("profileId", "")))
            backup_name = str(payload.get("backupName", ""))
            self.send_json(start_restore_job(profile, backup_name), HTTPStatus.ACCEPTED)
        elif path == "/api/command":
            command = str(payload.get("command", "")).strip()
            if not command or len(command) > 500:
                raise manager.ManagerError("Enter a Minecraft command under 500 characters.")
            manager.send_command(command)
            self.send_json({"ok": True, "message": "Command sent."})
        elif path == "/api/performance/action":
            profile = profile_for_id(str(payload.get("profileId", "")))
            self.send_json(run_performance_action(profile, str(payload.get("action", ""))))
        elif path == "/api/config/save":
            profile = profile_for_id(str(payload.get("profileId", "")))
            content = payload.get("content")
            if not isinstance(content, str):
                raise manager.ManagerError("Configuration content must be text.")
            saved = save_config_file(
                profile,
                str(payload.get("root", "config")),
                str(payload.get("path", "")),
                content,
                str(payload.get("expectedHash", "")),
            )
            self.send_json({"ok": True, "message": "Configuration saved. Restart the server if the mod requires it.", "file": saved})
        elif path == "/api/shutdown":
            self.send_json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            match = re.fullmatch(r"/api/profiles/([^/]+)/(settings|backup-settings|open|backup|mods/toggle)", path)
            if not match:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Unknown API route.")
                return
            profile = profile_for_id(match.group(1))
            action = match.group(2)
            if action == "settings":
                save_profile_settings(profile["id"], payload)
                self.send_json({"ok": True, "message": "Server settings saved. Restart to apply them."})
            elif action == "backup-settings":
                settings = save_backup_settings(profile["id"], payload)
                self.send_json({"ok": True, "message": "Backup settings saved. Restart a running server to apply the new schedule.", "settings": settings})
            elif action == "open":
                manager.open_in_file_manager(manager.absolute_profile_path(profile))
                self.send_json({"ok": True})
            elif action == "backup":
                if manager.active_profile() and manager.active_profile()["id"] == profile["id"]:
                    control = manager.absolute_profile_path(profile) / ".control"
                    control.mkdir(exist_ok=True)
                    request = control / "backup.request"
                    request.write_text("dashboard\n", encoding="utf-8")
                    self.send_json({"ok": True, "message": "Backup queued. The server will flush the world first."})
                else:
                    name = create_offline_backup(profile)
                    self.send_json({"ok": True, "message": f"Created {name}."})
            elif action == "mods/toggle":
                mods = manager.absolute_profile_path(profile) / "mods"
                name = str(payload.get("name", ""))
                source = safe_config_path(mods, name)
                if not source.is_file() or not (source.name.endswith(".jar") or source.name.endswith(".jar.disabled")):
                    raise manager.ManagerError("Mod file was not found.")
                destination = source.with_name(source.name.removesuffix(".disabled") if source.name.endswith(".disabled") else source.name + ".disabled")
                if destination.exists():
                    raise manager.ManagerError("A mod with the target name already exists.")
                source.replace(destination)
                self.send_json({"ok": True, "message": "Mod state changed. Restart to apply it."})

    def do_PUT(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not self.require_auth() or not self.valid_origin():
            return
        if parsed.path == "/api/setup/playit-executable":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                filename = urllib.parse.unquote(self.headers.get("X-File-Name", ""))
                destination = install_playit_executable(self.rfile, length, filename)
                self.send_json({
                    "ok": True,
                    "message": "Playit was placed where BlockOps can find it. Continue to connect your account.",
                    "path": str(destination),
                })
            except (manager.ManagerError, OSError, ValueError) as error:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))
            return
        mod_match = re.fullmatch(r"/api/profiles/([^/]+)/mods/([^/]+)", parsed.path)
        backup_match = re.fullmatch(r"/api/profiles/([^/]+)/backups/([^/]+)", parsed.path)
        match = mod_match or backup_match
        if match is None:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Unknown API route.")
            return
        try:
            profile = profile_for_id(match.group(1))
            requested_name = urllib.parse.unquote(match.group(2))
            name = Path(requested_name).name
            if name != requested_name or "/" in name or "\\" in name:
                raise manager.ManagerError("Invalid upload filename.")
            length = int(self.headers.get("Content-Length", "0"))
            if mod_match:
                if not name.lower().endswith(".jar"):
                    raise manager.ManagerError("Only .jar mod files are accepted.")
                if length <= 0 or length > MAX_MOD_UPLOAD:
                    raise manager.ManagerError("The mod file is empty or larger than 1 GB.")
                upload_folder = manager.absolute_profile_path(profile) / "mods"
                upload_folder.mkdir(exist_ok=True)
                destination = upload_folder / name
            else:
                if not name.lower().endswith(".tar.gz"):
                    raise manager.ManagerError("Only .tar.gz BlockOps world backups are accepted.")
                if length <= 0 or length > MAX_BACKUP_UPLOAD:
                    raise manager.ManagerError("The backup is empty or larger than 50 GB.")
                folder = manager.absolute_profile_path(profile)
                world = manager.world_path(folder)
                upload_folder = folder / "backups"
                upload_folder.mkdir(exist_ok=True)
                label = re.sub(r"[^A-Za-z0-9._-]+", "-", name[:-7]).strip("-.")[:60] or "imported"
                unique = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
                destination = upload_folder / f"{world.name}-uploaded-{unique}-{label}.tar.gz"
            temporary = destination.with_suffix(destination.suffix + ".upload")
            with temporary.open("wb") as handle:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise manager.ManagerError("The upload ended unexpectedly.")
                    handle.write(chunk)
                    remaining -= len(chunk)
            if backup_match:
                validate_backup_archive(profile, temporary)
            temporary.replace(destination)
            if backup_match:
                prune_profile_backups(profile)
                self.send_json({
                    "ok": True,
                    "message": f"Uploaded and validated {name}.",
                    "backup": {"name": destination.name, "bytes": destination.stat().st_size, "modified": destination.stat().st_mtime},
                })
            else:
                self.send_json({"ok": True, "message": f"Uploaded {name}. Restart to load it."})
        except (manager.ManagerError, OSError, ValueError) as error:
            with contextlib.suppress(UnboundLocalError, OSError):
                temporary.unlink(missing_ok=True)
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, token: str):
        super().__init__(address, handler)
        self.dashboard_token = token


def existing_dashboard() -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/health", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def persist_token(token: str) -> None:
    manager.STATE.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    if os.name != "nt":
        TOKEN_FILE.chmod(0o600)


def launch_url(token: str) -> str:
    return f"http://{HOST}:{PORT}/launch/{token}"


def main() -> int:
    arguments = argparse.ArgumentParser(description="BlockOps local Minecraft dashboard")
    arguments.add_argument("--no-browser", action="store_true")
    args = arguments.parse_args()
    if existing_dashboard():
        try:
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if token and not args.no_browser:
            webbrowser.open(launch_url(token))
        return 0
    token = secrets.token_urlsafe(32)
    persist_token(token)
    server = DashboardServer((HOST, PORT), DashboardHandler, token)
    url = launch_url(token)
    print(f"BlockOps is ready at http://{HOST}:{PORT}")
    if not args.no_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
