"""Per-instance Bedrock proxy services, launched in the foreground by BlockOps."""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import app_session
import server_manager as manager

PLAYIT_SOURCE_TAG = 'v2.0.0-r1'  # Includes the macOS foreground daemon.
WATERDOG_URL = 'https://github.com/WaterdogPE/WaterdogPE/releases/download/latest/Waterdog.jar'


def check_cancelled():
    if app_session.cancellation_requested(manager.STATE):
        raise manager.ManagerError('BlockOps is closing; service preparation was cancelled.')


def tool_folder():
    return manager.ROOT / 'runtimes' / 'playit-scoped'


def ensure_playit_tools():
    folder = tool_folder()
    suffix = '.exe' if manager.IS_WINDOWS else ''
    cli, daemon = folder / ('playit-cli' + suffix), folder / ('playitd' + suffix)
    if cli.is_file() and daemon.is_file():
        return daemon, cli
    check_cancelled()
    folder.mkdir(parents=True, exist_ok=True)
    if manager.IS_WINDOWS:
        release = manager.api_json('https://api.github.com/repos/playit-cloud/playit-agent/releases/tags/v1.2.0')
        for name, target in [('playit-windows-x86_64-signed.exe', cli), ('playitd-windows-x86_64-signed.exe', daemon)]:
            asset = next((a for a in release.get('assets', []) if a['name'] == name), None)
            if not asset:
                raise manager.ManagerError('The official Playit release has no foreground Windows daemon pair. Choose a supported Playit build in the networking setup.')
            manager.download(asset['browser_download_url'], target)
        return daemon, cli
    cargo = shutil.which('cargo')
    if not cargo:
        candidate = Path.home() / '.cargo' / 'bin' / 'cargo'
        cargo = str(candidate) if candidate.is_file() else None
    if not cargo:
        raise manager.ManagerError('The current Playit macOS release needs a one-time Rust build. Install Rust from https://rustup.rs, then choose Prepare networking tools again. Later launches reuse the binaries.')
    source = manager.CACHE / f'playit-{PLAYIT_SOURCE_TAG}'
    manifest = source / 'Cargo.toml'
    if not manifest.is_file():
        archive = manager.CACHE / f'playit-{PLAYIT_SOURCE_TAG}.zip'
        manager.download(f'https://github.com/playit-cloud/playit-agent/archive/refs/tags/{PLAYIT_SOURCE_TAG}.zip', archive)
        with tempfile.TemporaryDirectory(prefix='playit-source-', dir=manager.CACHE) as temporary:
            staging = Path(temporary)
            with zipfile.ZipFile(archive) as package:
                for member in package.infolist():
                    manager.bedrock.safe_path(staging, member.filename)
                    if (member.external_attr >> 16) & 0o170000 == 0o120000:
                        raise manager.ManagerError('The Playit source archive contains a link.')
                package.extractall(staging)
            roots = [p for p in staging.iterdir() if p.is_dir() and (p / 'Cargo.toml').is_file()]
            if len(roots) != 1:
                raise manager.ManagerError('The Playit source archive has an unexpected layout.')
            roots[0].replace(source)
    check_cancelled()
    env = dict(os.environ)
    env['PATH'] = str(Path(cargo).parent) + os.pathsep + env.get('PATH', '')
    print('Building the scoped Playit daemon once. This can take several minutes…', flush=True)
    build = subprocess.Popen([cargo, 'build', '--release', '--locked', '--bin', 'playit-cli', '--bin', 'playitd',
                              '--target-dir', str(folder / 'build')], cwd=source, env=env,
                             **manager.detached_process_options())
    while build.poll() is None:
        if app_session.cancellation_requested(manager.STATE):
            _stop_child(build)
            check_cancelled()
        time.sleep(.3)
    if build.returncode:
        raise manager.ManagerError('Playit compilation failed. Review the setup output; Rust and the macOS command-line developer tools are required.')
    for target in (cli, daemon):
        temporary = target.with_suffix('.installing')
        shutil.copy2(folder / 'build' / 'release' / target.name, temporary)
        temporary.chmod(0o755)
        temporary.replace(target)
    return daemon, cli


def ensure_waterdog():
    jar = manager.ROOT / 'runtimes' / 'waterdog' / 'Waterdog.jar'
    if not jar.is_file():
        check_cancelled()
        manager.download(WATERDOG_URL, jar)
        try:
            with zipfile.ZipFile(jar) as package:
                if 'META-INF/MANIFEST.MF' not in package.namelist():
                    raise manager.ManagerError('The Waterdog download is not an executable JAR.')
        except (zipfile.BadZipFile, manager.ManagerError):
            jar.unlink(missing_ok=True)
            raise manager.ManagerError('The Waterdog download is not a valid executable JAR.') from None
    return jar


def proxy_folder(profile):
    return manager.absolute_profile_path(profile) / '.blockops-network' / 'waterdog'


def validate_proxy_config(content: str, port: int):
    if not isinstance(content, str) or len(content.encode()) > 256 * 1024 or '\x00' in content or '\t' in content:
        raise manager.ManagerError('Use a plain-text Waterdog YAML configuration smaller than 256 KB, with spaces for indentation.')
    # Inspect the relevant mapping paths, not loose text matches: a true value
    # in an unrelated section must not conceal disabled proxy authentication.
    # Waterdog itself validates the complete YAML syntax/schema at startup.
    values, stack = {}, [(-1, ())]
    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = re.fullmatch(r'([ ]*)([A-Za-z0-9_-]+):(?:[ ]+(.*))?', line)
        if not match:
            continue  # Lists and unrelated complex YAML are handled by Waterdog.
        indent, key, raw = len(match[1]), match[2], match[3] or ''
        while indent <= stack[-1][0]:
            stack.pop()
        path = stack[-1][1] + (key,)
        if path in values:
            raise manager.ManagerError('The proxy configuration contains duplicate YAML settings. Merge each section only once.')
        value = raw.split(' #', 1)[0].strip().strip('"\'')
        values[path] = value
        stack.append((indent, path))
    if values.get(('online_mode',)) != 'true':
        raise manager.ManagerError('Waterdog online_mode must be true so the public proxy authenticates players.')
    # Current Waterdog selects transport on the listener and nests NXS settings
    # under signaling. The older enabled/signaling_mode layout is ignored by it.
    if values.get(('listener', 'transport')) != 'nethernet' or values.get(('nethernet', 'signaling', 'mode')) != 'nxs':
        raise manager.ManagerError('Copy the current WaterdogPE configuration from Playit: listener.transport must be nethernet and nethernet.signaling.mode must be nxs.')
    if values.get(('nethernet', 'signaling', 'nxs', 'endpoint'), '').rstrip('/') != 'https://nethernet.playit.gg':
        raise manager.ManagerError('The Waterdog NXS endpoint must be https://nethernet.playit.gg.')
    token = values.get(('nethernet', 'signaling', 'nxs', 'token'), '')
    if not token or token.startswith(('YOUR', '<', '|', '>', '*', 'file:')) or set(token) == {'•'}:
        raise manager.ManagerError('The tunnel configuration needs its private NXS token. Copy it locally from Playit Configuration help.')
    backends = [path[1] for path, value in values.items() if len(path) == 3 and path[0] == 'servers' and path[2] == 'address' and value == f'127.0.0.1:{port}']
    if not backends or any(values.get(('servers', backend, 'server_type')) not in {'bedrock', 'nethernet'} for backend in backends):
        raise manager.ManagerError(f'The Waterdog backend must use address 127.0.0.1:{port} and server_type bedrock or nethernet.')


def save_config(profile, content: str):
    validate_proxy_config(content, int(profile.get('port', 19142)))
    folder = proxy_folder(profile)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'config.yml'
    if path.exists():
        shutil.copy2(path, folder / 'config.previous.yml')
    temporary = folder / 'config.yml.tmp'
    temporary.write_text(content, encoding='utf-8')
    if os.name != 'nt':
        temporary.chmod(0o600)
    temporary.replace(path)


def ensure_docker():
    try:
        return manager.docker_executable()
    except manager.ManagerError:
        if manager.IS_MAC and Path('/Applications/Docker.app').is_dir():
            subprocess.run(['/usr/bin/open', '-g', '-a', 'Docker'], check=True)
        elif manager.IS_WINDOWS:
            desktop = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Docker' / 'Docker' / 'Docker Desktop.exe'
            if not desktop.is_file():
                raise
            subprocess.Popen([str(desktop)])
        else:
            raise
    print('Waiting for Docker Desktop to become ready…', flush=True)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        check_cancelled()
        try:
            return manager.docker_executable()
        except manager.ManagerError:
            time.sleep(2)
    raise manager.ManagerError('Docker Desktop did not become ready. Open Docker Desktop and check its startup status.')


def prepare(profile):
    check_cancelled()
    if not profile.get('bedrockRemote', {}).get('enabled'):
        ensure_docker()
        return
    config = proxy_folder(profile) / 'config.yml'
    if not config.is_file():
        raise manager.ManagerError('Finish Playit setup for this instance in World Settings: prepare tools, connect the account, and paste its Waterdog configuration.')
    validate_proxy_config(config.read_text(encoding='utf-8'), int(profile['port']))
    if profile.get('bedrockBindAddress') != '127.0.0.1' or profile.get('bedrockAddress') != '127.0.0.1':
        raise manager.ManagerError('Managed Playit requires a private loopback Bedrock backend. Save the Playit settings in World Settings first.')
    ensure_playit_tools()
    ensure_waterdog()
    ensure_docker()


def start(profile):
    if not profile.get('bedrockRemote', {}).get('enabled'):
        return
    check_cancelled()
    before = manager.read_pid(manager.PLAYIT_PID)
    session = os.environ.get('BLOCKOPS_SESSION_ID')
    if before and session and not app_session.owned_process(manager.PLAYIT_PID, session):
        raise manager.ManagerError('A Playit process from another BlockOps session is still running. Close that session before starting this instance.')
    pid, offset = manager.start_playit()
    manager.ensure_playit_enabled(pid, offset, profile=profile)
    folder = proxy_folder(profile)
    log = folder / 'waterdog.log'
    offset = log.stat().st_size if log.exists() else 0
    java = manager.resolve_java(25)
    command = [str(java), '-Xms256M', '-Xmx1G', '-Dio.netty.tryReflectionSetAccessible=true',
               '--add-opens', 'java.base/jdk.internal.misc=ALL-UNNAMED', '-jar', str(ensure_waterdog())]
    with log.open('ab', buffering=0) as output:
        process = subprocess.Popen(command, cwd=folder, stdin=subprocess.PIPE, stdout=output,
                                   stderr=subprocess.STDOUT, **manager.detached_process_options())
    pid_file = manager.STATE / 'waterdog.pid'
    pid_file.write_text(str(process.pid))
    app_session.record_owner(pid_file, process.pid)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        check_cancelled()
        if process.poll() is not None:
            raise manager.ManagerError('WaterdogPE exited during startup. Open the proxy log in World Settings to review configuration or native-library errors.')
        with log.open('rb') as reader:
            reader.seek(offset)
            output = reader.read().decode('utf-8', errors='replace')
        if 'Registered with the external signaling service' in output:
            print('WaterdogPE registered with Playit. Test joining with a Bedrock client to verify the complete route.', flush=True)
            return
        time.sleep(.5)
    raise manager.ManagerError('WaterdogPE did not confirm Playit registration. Check the proxy log and the tunnel configuration.')


def _stop_child(process):
    if process.poll() is not None:
        return
    if manager.IS_WINDOWS:
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
    else:
        import signal
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=10)
