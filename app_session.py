"""Dashboard-tab lifetime and ownership of BlockOps child processes."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path


def process_identity(pid: int) -> str | None:
    """Fingerprint a process start, so an old PID file cannot target a reused PID."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return f'{times[0].dwHighDateTime}:{times[0].dwLowDateTime}'
        finally:
            kernel.CloseHandle(handle)
    try:
        stat = Path(f'/proc/{pid}/stat')
        if stat.is_file():
            # The command field can contain spaces and parentheses.
            fields = stat.read_text().rsplit(')', 1)[1].split()
            return fields[19]  # Linux starttime, field 22.
        result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=', '-o', 'pid='],
                                capture_output=True, text=True, timeout=5)
        return hashlib.sha256(result.stdout.strip().encode()).hexdigest() if result.returncode == 0 and result.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def record_owner(pid_file: Path, pid: int) -> None:
    session = os.environ.get('BLOCKOPS_SESSION_ID')
    if not session:
        return
    identity = process_identity(pid)
    if not identity:
        raise RuntimeError('Could not establish ownership of a BlockOps child process.')
    owner = pid_file.with_suffix(pid_file.suffix + '.owner')
    temporary = owner.with_suffix(owner.suffix + '.tmp')
    temporary.write_text(json.dumps({'session': session, 'pid': pid, 'identity': identity}), encoding='utf-8')
    temporary.replace(owner)


def owned_process(pid_file: Path, session: str) -> bool:
    try:
        owner = json.loads(pid_file.with_suffix(pid_file.suffix + '.owner').read_text())
        pid = int(pid_file.read_text())
        return owner['session'] == session and owner['pid'] == pid and owner['identity'] == process_identity(pid)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def cancellation_requested(state: Path) -> bool:
    session = os.environ.get('BLOCKOPS_SESSION_ID')
    return bool(session and (state / f'close-session-{session}').exists())


class TabLifetime:
    """Connection leases tolerate reloads, background tabs, and multiple windows."""
    def __init__(self, grace=12.0, clock=time.monotonic):
        self.grace, self.clock = grace, clock
        self.lock = threading.RLock()
        self.clients = {}
        self.deadline = None
        self.seen_client = False

    def connect(self, client_id):
        generation = object()
        with self.lock:
            self.clients[client_id] = generation
            self.seen_client = True
            self.deadline = None
        return generation

    def disconnect(self, client_id, generation=None):
        with self.lock:
            if client_id not in self.clients or (generation is not None and self.clients[client_id] is not generation):
                return
            del self.clients[client_id]
            if not self.clients:
                self.deadline = self.clock() + self.grace

    def current(self, client_id, generation):
        with self.lock:
            return self.clients.get(client_id) is generation

    def expired(self):
        with self.lock:
            return self.seen_client and not self.clients and self.deadline is not None and self.clock() >= self.deadline


class AppSession:
    def __init__(self, server, manager, job_status, *, grace=12.0):
        import secrets
        self.server, self.manager, self.job_status = server, manager, job_status
        self.id = secrets.token_hex(16)
        self.tabs = TabLifetime(grace)
        self.closing = threading.Event()
        self.finished = threading.Event()
        self.status_lock = threading.Lock()
        self.state, self.message = 'open', 'Closing the last tab saves and stops the managed server and its services.'
        self.cancel_path = manager.STATE / f'close-session-{self.id}'
        self.job_process = None
        self.job_kind = None
        self.worker = threading.Thread(target=self._watch, name='blockops-session', daemon=True)

    def start(self):
        # Children get a session-specific ownership marker, never an OS-wide service.
        os.environ['BLOCKOPS_SESSION_ID'] = self.id
        self.worker.start()

    def snapshot(self):
        with self.status_lock:
            return {'state': self.state, 'message': self.message, 'closeGraceSeconds': self.tabs.grace}

    def _status(self, state, message):
        with self.status_lock:
            self.state, self.message = state, message

    def request_shutdown(self):
        self.manager.STATE.mkdir(parents=True, exist_ok=True)
        self.cancel_path.touch()
        self.closing.set()
        self._status('stopping', 'Saving the managed server and stopping its Playit/proxy processes…')

    def _watch(self):
        while not self.finished.wait(.5):
            if self.tabs.expired() and not self.closing.is_set():
                self.request_shutdown()
            if not self.closing.is_set():
                continue
            # Account setup can wait for a browser indefinitely. It has no world
            # writes; stop only this session's setup subprocess group.
            process = self.job_process
            if process and self.job_kind == 'playit setup' and process.poll() is None:
                self._terminate_setup(process)
            if self.job_status().get('status') == 'running':
                continue
            try:
                with self.manager.manager_lock():
                    runner = self.manager.STATE / 'runner.pid'
                    if owned_process(runner, self.id):
                        self.manager.stop_profile()
                    # The runner may have crashed with its Docker container alive.
                    # Its ownership record remains; the active-profile marker is
                    # session stamped separately by the start operation.
                    else:
                        marker = self.manager.STATE / 'active-session'
                        active = self.manager.active_profile()
                        if active and marker.exists() and marker.read_text().strip() == self.id:
                            self.manager.stop_profile()
                    for name in ('waterdog.pid', 'playit.pid'):
                        path = self.manager.STATE / name
                        if owned_process(path, self.id):
                            self.manager.stop_process(path)
                            if name == "playit.pid":
                                self.manager.PLAYIT_SOCKET.unlink(missing_ok=True)
                self._status('closed', 'The managed server and its services are stopped. You can close this tab.')
                self.finished.set()
                self.cancel_path.unlink(missing_ok=True)
                self.server.shutdown()
                return
            except Exception as error:
                # Never force-kill a world that is saving, or call it stopped.
                self._status('stop-failed', f'Safe shutdown needs attention: {error}. BlockOps will retry; reopen the app for status.')
                self.finished.wait(10)

    @staticmethod
    def _terminate_setup(process):
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T'], capture_output=True)
        else:
            import signal
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
