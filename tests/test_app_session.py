import http.client
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import app_session
import dashboard
import managed_network
import server_manager as manager


CONFIG = '''listener:
  host: 127.0.0.1:19132
  priorities:
    - vanilla
servers:
  vanilla:
    address: 127.0.0.1:19142
    server_type: bedrock
nethernet:
  enabled: true
  signaling_mode: nxs
  udp_port: 19133
  nxs:
    endpoint: https://nethernet.playit.gg
    token: unit-test-only-not-a-real-token
online_mode: true
upstream_encryption: true
use_login_extras: false
enable_edu_features: false
'''


class TabLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.tabs = app_session.TabLifetime(grace=12, clock=lambda: self.now)

    def test_opening_without_a_browser_client_does_not_launch_or_stop_services(self):
        self.now = 1000
        self.assertFalse(self.tabs.expired())

    def test_last_tab_close_expires_only_after_reload_grace(self):
        generation = self.tabs.connect('tab')
        self.tabs.disconnect('tab', generation)
        self.now = 11
        self.assertFalse(self.tabs.expired())
        self.now = 12
        self.assertTrue(self.tabs.expired())

    def test_refresh_reconnect_cancels_shutdown(self):
        generation = self.tabs.connect('old-page')
        self.tabs.disconnect('old-page', generation)
        self.now = 5
        self.tabs.connect('new-page')
        self.now = 1000
        self.assertFalse(self.tabs.expired())

    def test_background_tab_needs_no_javascript_heartbeat(self):
        self.tabs.connect('background')
        self.now = 100000
        self.assertFalse(self.tabs.expired())

    def test_one_of_multiple_tabs_closing_keeps_app_open(self):
        a = self.tabs.connect('a')
        self.tabs.connect('b')
        self.tabs.disconnect('a', a)
        self.now = 20
        self.assertFalse(self.tabs.expired())

    def test_old_sse_disconnect_cannot_remove_its_reconnected_client(self):
        old = self.tabs.connect('same-page')
        current = self.tabs.connect('same-page')
        self.tabs.disconnect('same-page', old)
        self.assertTrue(self.tabs.current('same-page', current))
        self.assertFalse(self.tabs.current('same-page', old))


class OwnershipTests(unittest.TestCase):
    def test_owner_requires_session_pid_and_start_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'process.pid'
            path.write_text('123')
            with mock.patch.dict(os.environ, {'BLOCKOPS_SESSION_ID': 'owner'}), mock.patch.object(app_session, 'process_identity', return_value='start-a'):
                app_session.record_owner(path, 123)
                self.assertTrue(app_session.owned_process(path, 'owner'))
                self.assertFalse(app_session.owned_process(path, 'different-session'))
            with mock.patch.object(app_session, 'process_identity', return_value='start-b'):
                self.assertFalse(app_session.owned_process(path, 'owner'))
            path.write_text('456')
            with mock.patch.object(app_session, 'process_identity', return_value='start-a'):
                self.assertFalse(app_session.owned_process(path, 'owner'))

    def test_live_process_identity_and_nonexistent_process(self):
        self.assertIsNotNone(app_session.process_identity(os.getpid()))
        self.assertIsNone(app_session.process_identity(2147483647))

    def test_shutdown_only_stops_owned_processes_and_preserves_external_server(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            fake = mock.Mock()
            fake.STATE = state
            fake.manager_lock = manager.manager_lock
            fake.active_profile.return_value = {'id': 'external'}
            server = mock.Mock()
            session = app_session.AppSession(server, fake, lambda: {'status': 'idle'})
            session.id = 'this-session'
            for name in ('playit.pid', 'waterdog.pid'):
                (state / name).write_text('123')
            with mock.patch.object(manager, 'STATE', state), mock.patch.object(manager, 'MANAGER_LOCK', state / 'lock'), \
                 mock.patch.object(app_session, 'owned_process', side_effect=lambda path, owner: path.name == 'waterdog.pid'):
                session.request_shutdown()
                session._watch()
            fake.stop_profile.assert_not_called()
            fake.stop_process.assert_called_once_with(state / 'waterdog.pid')
            server.shutdown.assert_called_once()

    def test_save_failure_is_retried_instead_of_reporting_closed_or_killing_world(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            fake = mock.Mock(); fake.STATE = state; fake.manager_lock = manager.manager_lock
            fake.stop_profile.side_effect = [manager.ManagerError('still saving'), None]
            session = app_session.AppSession(mock.Mock(), fake, lambda: {'status': 'idle'})
            states = []
            original = session._status
            def record(state, message):
                states.append(state); original(state, message)
            with mock.patch.object(manager, 'STATE', state), mock.patch.object(manager, 'MANAGER_LOCK', state / 'lock'), \
                 mock.patch.object(app_session, 'owned_process', return_value=True), \
                 mock.patch.object(session.finished, 'wait', return_value=False), mock.patch.object(session, '_status', side_effect=record):
                session.request_shutdown(); session._watch()
            self.assertEqual(fake.stop_profile.call_count, 2)
            self.assertIn('stop-failed', states)
            self.assertEqual(states[-1], 'closed')


class ManagedNetworkTests(unittest.TestCase):
    def test_valid_config_and_authentication_guard(self):
        managed_network.validate_proxy_config(CONFIG, 19142)
        for invalid in (CONFIG.replace('online_mode: true', 'online_mode: false'),
                        CONFIG.replace('127.0.0.1:19142', '192.168.0.8:19142'),
                        CONFIG.replace('signaling_mode: nxs', 'signaling_mode: builtin'),
                        CONFIG.replace('https://nethernet.playit.gg', 'https://elsewhere.example'),
                        CONFIG.replace('unit-test-only-not-a-real-token', ''),
                        CONFIG.replace('server_type: bedrock', 'server_type: raknet')):
            with self.subTest(invalid=invalid), self.assertRaises(manager.ManagerError):
                managed_network.validate_proxy_config(invalid, 19142)

    def test_local_bedrock_prepares_docker_without_playit_or_proxy(self):
        with mock.patch.object(managed_network, 'ensure_docker') as docker, \
             mock.patch.object(managed_network, 'ensure_playit_tools') as playit, \
             mock.patch.object(managed_network, 'ensure_waterdog') as proxy, \
             mock.patch.object(managed_network, 'check_cancelled'):
            managed_network.prepare({'game': 'bedrock'})
            managed_network.start({'game': 'bedrock'})
        docker.assert_called_once()
        playit.assert_not_called()
        proxy.assert_not_called()

    def test_managed_config_makes_backend_private_before_auth_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            profile = {'id': 'test', 'name': 'Test', 'game': 'bedrock', 'path': str(root), 'port': 19132, 'maximumRam': '4G'}
            manager.save_json(root / 'profile.json', profile)
            manager.save_json(root / 'registry.json', {'profiles': [profile]})
            (root / 'server.properties').write_text('online-mode=true\n')
            with mock.patch.object(manager, 'REGISTRY', root / 'registry.json'):
                dashboard.save_bedrock_settings(profile, {'bedrockRemote': {'enabled': True, 'publicAddress': 'test.tun.ply.gg'}, 'waterdogConfig': CONFIG})
            self.assertEqual(profile['bedrockBindAddress'], '127.0.0.1')
            self.assertEqual(profile['bedrockAddress'], '127.0.0.1')
            self.assertEqual(profile['port'], 19142)
            self.assertEqual(profile['bedrockUdpPort'], 19142)
            props = dashboard.parse_properties(root / 'server.properties')
            self.assertEqual(props['online-mode'], 'false')
            self.assertEqual(props['enable-lan-visibility'], 'false')
            config = managed_network.proxy_folder(profile) / 'config.yml'
            self.assertEqual(config.read_text(), CONFIG)
            summary = dashboard.profile_summary(profile, None)
            self.assertTrue(summary['bedrockRemote']['configured'])
            self.assertNotIn('unit-test-only-not-a-real-token', json.dumps(summary))

    def test_invalid_proxy_config_does_not_modify_existing_properties(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = {'id': 'test', 'name': 'Test', 'game': 'bedrock', 'path': str(root), 'port': 19132, 'maximumRam': '4G'}
            original = 'online-mode=true\n'
            (root / 'server.properties').write_text(original)
            with self.assertRaises(manager.ManagerError):
                dashboard.save_bedrock_settings(profile, {'bedrockRemote': {'enabled': True}, 'waterdogConfig': CONFIG.replace('online_mode: true', 'online_mode: false')})
            self.assertEqual((root / 'server.properties').read_text(), original)
            self.assertNotIn('bedrockRemote', profile)

    def test_missing_remote_config_fails_before_starting_any_service(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(managed_network, 'ensure_docker') as docker:
            with self.assertRaisesRegex(manager.ManagerError, 'Finish Playit setup'):
                managed_network.prepare({'path': directory, 'bedrockRemote': {'enabled': True}})
        docker.assert_not_called()

    def test_disabling_proxy_restores_backend_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = {'id': 'test', 'name': 'Test', 'game': 'bedrock', 'path': str(root), 'port': 19142, 'maximumRam': '4G', 'bedrockRemote': {'enabled': True}}
            (root / 'server.properties').write_text('online-mode=false\n')
            manager.save_json(root / 'registry.json', {'profiles': [profile]})
            with mock.patch.object(manager, 'REGISTRY', root / 'registry.json'):
                dashboard.save_bedrock_settings(profile, {'bedrockRemote': {'enabled': False}, 'properties': {'onlineMode': False}})
            self.assertEqual(dashboard.parse_properties(root / 'server.properties')['online-mode'], 'true')

    def test_duplicate_yaml_or_unrelated_enabled_value_cannot_bypass_guards(self):
        bad = [CONFIG + 'online_mode: false\n', CONFIG.replace('  enabled: true', '  enabled: false') + 'other:\n  enabled: true\n']
        for content in bad:
            with self.assertRaises(manager.ManagerError):
                managed_network.validate_proxy_config(content, 19142)

    def test_first_install_discovers_only_the_new_private_cli_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'runtimes' / 'playit-scoped'; folder.mkdir(parents=True)
            cli = folder / ('playit-cli.exe' if manager.IS_WINDOWS else 'playit-cli')
            daemon = folder / ('playitd.exe' if manager.IS_WINDOWS else 'playitd')
            cli.touch(); daemon.touch(); cli.chmod(0o755); daemon.chmod(0o755)
            with mock.patch.object(manager, 'ROOT', root):
                self.assertEqual(manager.playit_executable(), cli.resolve())
                self.assertEqual(manager.modern_playit_binaries(), (daemon.resolve(), cli.resolve()))

    def test_windows_uses_real_signed_release_asset_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ['playit-windows-x86_64-signed.exe', 'playitd-windows-x86_64-signed.exe']
            assets = {'assets': [{'name': name, 'browser_download_url': 'https://github.com/' + name} for name in names]}
            def download(url, path):
                path.write_bytes(b'MZtest-fixture')
            with mock.patch.object(manager, 'ROOT', root), mock.patch.object(manager, 'IS_WINDOWS', True), \
                 mock.patch.object(manager, 'api_json', return_value=assets), mock.patch.object(manager, 'download', side_effect=download):
                daemon, cli = managed_network.ensure_playit_tools()
            self.assertEqual(daemon.name, 'playitd.exe')
            self.assertEqual(cli.name, 'playit-cli.exe')
            self.assertTrue(cli.is_file())

    def test_socket_fallback_is_short_and_unique_for_long_install_paths(self):
        if os.name == 'nt':
            self.skipTest('Unix socket path length is not a Windows constraint')
        a = manager.playit_socket_path(Path('/long-installation' * 10) / 'a')
        b = manager.playit_socket_path(Path('/long-installation' * 10) / 'b')
        self.assertLess(len(str(a).encode()), 104)
        self.assertNotEqual(a, b)

    def test_private_playit_daemon_does_not_use_os_service_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            process = mock.Mock(pid=123)
            process.poll.return_value = None
            with mock.patch.object(manager, 'STATE', state), \
                 mock.patch.object(manager, 'PLAYIT_PID', state / 'playit.pid'), \
                 mock.patch.object(manager, 'PLAYIT_LOG', state / 'playit.log'), \
                 mock.patch.object(manager, 'PLAYIT_CONFIG', state / 'playit.toml'), \
                 mock.patch.object(manager, 'PLAYIT_SOCKET', state / 'playit.sock'), \
                 mock.patch.object(manager, 'read_pid', return_value=None), \
                 mock.patch.object(manager, 'modern_playit_binaries', return_value=(Path('/private/playitd'), Path('/private/playit-cli'))), \
                 mock.patch.object(manager.subprocess, 'Popen', return_value=process) as launch, \
                 mock.patch.object(manager.time, 'sleep'), \
                 mock.patch.object(app_session, 'record_owner') as owner:
                manager.start_playit()
            command = launch.call_args.args[0]
            self.assertEqual(command[0], Path('/private/playitd'))
            self.assertIn(state / 'playit.sock', command)
            self.assertIn(state / 'playit.toml', command)
            self.assertNotIn('--windows-service', command)
            owner.assert_called_once_with(state / 'playit.pid', 123)

    def test_java_scoped_stop_waits_for_save_without_forcing_termination(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            manager.save_json(folder / 'profile.json', {'id': 'java', 'name': 'Java', 'game': 'minecraft', 'path': str(folder)})
            control = folder / '.control'; control.mkdir()
            (control / 'stop.request').write_text('closing during startup')
            process = mock.Mock(pid=123)
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired('java', 120), None]
            with mock.patch.dict(os.environ, {'BLOCKOPS_SESSION_ID': 'test-scope'}), \
                 mock.patch.object(manager, 'SERVER_PID', folder / 'server.pid'), \
                 mock.patch.object(manager, 'launch_command', return_value=['java']), \
                 mock.patch.object(manager.subprocess, 'Popen', return_value=process):
                manager.runner_mode(str(folder))
            process.terminate.assert_not_called()
            self.assertEqual(process.wait.call_count, 2)
            self.assertEqual(process.wait.call_args.kwargs, {})
            commands = b''.join(call.args[0] for call in process.stdin.write.call_args_list)
            self.assertIn(b'save-all flush', commands)
            self.assertIn(b'stop', commands)


class ScopedHTTPTests(unittest.TestCase):
    def test_actual_stream_reload_multitab_and_quit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = '''import os,sys
sys.path.insert(0, sys.argv[1])
import dashboard as d
s=d.DashboardServer(('127.0.0.1',0),d.DashboardHandler,'test-token',scoped=True)
d.PORT=s.server_address[1]
d.app_lifecycle=s.app_session
s.app_session.tabs.grace=1
s.app_session.start()
print(d.PORT,flush=True)
s.serve_forever(poll_interval=.05)
s.server_close()
'''
            process = subprocess.Popen([sys.executable, '-u', '-c', script, str(manager.ROOT)],
                                       env={**os.environ, 'BLOCKOPS_DATA_DIR': str(root)}, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True)
            self.addCleanup(lambda: process.poll() is None and process.terminate())
            self.addCleanup(process.stdout.close)
            port = int(process.stdout.readline().strip())
            headers = {'Cookie': 'mc_dashboard=test-token', 'Origin': f'http://127.0.0.1:{port}'}
            def stream(identifier):
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', '/api/session/events?id=' + identifier, headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(response.readline().startswith(b'data: '))
                return connection, response
            def leave(identifier):
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('POST', '/api/session/leave', json.dumps({'id': identifier}), headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                connection.close()
            a, ar = stream('a' * 32)
            b, br = stream('b' * 32)
            leave('a' * 32)
            ar.close(); a.close()
            time.sleep(1.4)
            self.assertIsNone(process.poll(), 'Closing one tab must preserve another tab')
            leave('b' * 32)
            br.close(); b.close()
            c, cr = stream('c' * 32)  # Refresh/reopen within grace.
            time.sleep(1.4)
            self.assertIsNone(process.poll())
            self.assertFalse(list(root.rglob('playit.pid')), 'Opening the app must not start Playit')
            raw = socket.create_connection(('127.0.0.1', port), timeout=5)
            raw.sendall(b'GET /api/session/events?id=ffffffffffffffffffffffffffffffff HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: mc_dashboard=test-token\r\n\r\n')
            received = b''
            while b'data:' not in received:
                received += raw.recv(4096)
            cr.close(); c.close()
            raw.shutdown(socket.SHUT_RDWR); raw.close()  # Abrupt close: no beacon.
            process.wait(timeout=12)
            self.assertEqual(process.returncode, 0)


@unittest.skipUnless(os.environ.get('BLOCKOPS_TEST_DOCKER') == '1', 'Opt-in real Docker session test')
class SessionDockerTests(unittest.TestCase):
    def test_tab_close_saves_bedrock_without_starting_unneeded_services(self):
        root = Path(tempfile.mkdtemp(prefix='blockops-scoped-docker-')).resolve()
        env = {**os.environ, 'BLOCKOPS_DATA_DIR': str(root)}
        profile = None
        process = None
        try:
            create = subprocess.run([sys.executable, str(manager.ROOT / 'server_manager.py'), 'create', '--game', 'bedrock', '--name', 'Scoped', '--port', '29232', '--udp-port', '29240'],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(create.returncode, 0, create.stdout + create.stderr)
            profile = json.loads((root / 'server-profiles.json').read_text())['profiles'][0]
            # Existing official server tests cover worlds/packs. This test verifies
            # actual tab lifetime + Docker shutdown + a foreground Playit daemon.
            script = '''import sys
sys.path.insert(0,sys.argv[1])
import dashboard as d
s=d.DashboardServer(('127.0.0.1',0),d.DashboardHandler,'test-token',scoped=True)
d.PORT=s.server_address[1]; d.app_lifecycle=s.app_session
s.app_session.tabs.grace=1; s.app_session.start()
print(d.PORT,flush=True)
s.serve_forever(poll_interval=.05)
s.server_close()
'''
            log = (root / 'dashboard.log').open('w')
            process = subprocess.Popen([sys.executable, '-u', '-c', script, str(manager.ROOT)], env=env,
                                       stdout=subprocess.PIPE, stderr=log, text=True)
            port = int(process.stdout.readline().strip())
            headers = {'Cookie': 'mc_dashboard=test-token', 'Origin': f'http://127.0.0.1:{port}'}
            stream = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            stream.request('GET', '/api/session/events?id=' + 'd' * 32, headers=headers)
            response = stream.getresponse()
            response.readline()
            request = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            request.request('POST', '/api/jobs/start', json.dumps({'profileId': 'scoped'}), headers)
            self.assertEqual(request.getresponse().status, 202)
            request.close()
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline and not manager.bedrock_ready(profile):
                self.assertIsNone(process.poll())
                time.sleep(1)
            self.assertTrue(manager.bedrock_ready(profile))
            self.assertFalse((root / ".manager-state" / "playit.pid").exists())
            self.assertFalse((root / ".manager-state" / "waterdog.pid").exists())
            # Disconnect through the same beacon route a closing browser tab uses.
            request = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            request.request('POST', '/api/session/leave', json.dumps({'id': 'd' * 32}), headers)
            reply = request.getresponse(); reply.read(); request.close()
            response.close(); stream.close()
            process.wait(timeout=150)
            process.stdout.close()
            self.assertEqual(process.returncode, 0)
            self.assertFalse(manager.bedrock_container_running(profile))
            folder = manager.absolute_profile_path(profile)
            self.assertTrue((manager.world_path(folder) / 'db' / 'CURRENT').is_file())
            text = (folder / 'profile-runner.log').read_text()
            self.assertIn('Quit correctly', text)
            print('REAL SESSION VERIFIED: last tab close safely stopped the official Bedrock container and preserved LevelDB; opening the dashboard started no Playit service.')
            log.close()
        finally:
            if profile and manager.bedrock_container_running(profile):
                subprocess.run([sys.executable, str(manager.ROOT / 'server_manager.py'), 'stop'], env=env, capture_output=True, timeout=150)
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            if process and process.stdout:
                process.stdout.close()
            if profile and manager.bedrock_container_running(profile):
                print(f'Test data retained: {root}')
            else:
                shutil.rmtree(root)


@unittest.skipUnless(os.environ.get('BLOCKOPS_TEST_SERVICES') == '1', 'Opt-in real Playit and Waterdog process test')
class RealServiceSessionTests(unittest.TestCase):
    def test_tab_close_stops_private_daemon_and_proxy_without_os_service(self):
        root = Path(tempfile.mkdtemp(prefix='blockops-scoped-services-')).resolve()
        process = None
        try:
            script = '''import json,sys,subprocess
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import dashboard as d,server_manager as m,app_session as own
from tests.test_app_session import CONFIG
class Handler(d.DashboardHandler):
 def handle_api_post(self,path,payload):
  if path!='/api/test/start-services': return super().handle_api_post(path,payload)
  pid,_=m.start_playit()
  folder=m.DATA_ROOT/'proxy'; folder.mkdir()
  # Builtin loopback-only proxy: no tunnel/account/token is used in this test.
  config=CONFIG.replace('127.0.0.1:19132','127.0.0.1:29332').replace('signaling_mode: nxs','signaling_mode: builtin').replace('udp_port: 19133','udp_port: 29333')
  (folder/'config.yml').write_text(config)
  java=m.resolve_java(25)
  with (folder/'proxy.log').open('ab',buffering=0) as log:
   proxy=subprocess.Popen([str(java),'-Xms256M','-Xmx1G','-Dio.netty.tryReflectionSetAccessible=true','--add-opens','java.base/jdk.internal.misc=ALL-UNNAMED','-jar',str(m.ROOT/'runtimes'/'waterdog'/'Waterdog.jar')],cwd=folder,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,**m.detached_process_options())
  path=m.STATE/'waterdog.pid'; path.write_text(str(proxy.pid)); own.record_owner(path,proxy.pid)
  self.send_json({'playit':pid,'proxy':proxy.pid,'socket':str(m.PLAYIT_SOCKET)})
s=d.DashboardServer(('127.0.0.1',0),Handler,'test-token',scoped=True)
d.PORT=s.server_address[1]; d.app_lifecycle=s.app_session
s.app_session.tabs.grace=1; s.app_session.start()
print(d.PORT,flush=True)
s.serve_forever(poll_interval=.05);s.server_close()
'''
            stderr = (root / 'server.log').open('w')
            process = subprocess.Popen([sys.executable, '-u', '-c', script, str(manager.ROOT)],
                                       env={**os.environ, 'BLOCKOPS_DATA_DIR': str(root)},
                                       stdout=subprocess.PIPE, stderr=stderr, text=True)
            port = int(process.stdout.readline().strip())
            headers = {'Cookie': 'mc_dashboard=test-token', 'Origin': f'http://127.0.0.1:{port}'}
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            connection.request('GET', '/api/session/events?id=' + 'e' * 32, headers=headers)
            stream = connection.getresponse(); stream.readline()
            request = http.client.HTTPConnection('127.0.0.1', port, timeout=20)
            request.request('POST', '/api/test/start-services', '{}', headers)
            response = request.getresponse(); body = response.read()
            if response.status != 200:
                print((root / '.manager-state' / 'playit.log').read_text()[-4000:])
            self.assertEqual(response.status, 200, body)
            pids = json.loads(body); request.close()
            # A healthy HTTP probe shows the actual native NetherNet listener,
            # rather than proving only that a Java process exists.
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                try:
                    with __import__('urllib.request', fromlist=['urlopen']).urlopen('http://127.0.0.1:29332/v1/join', timeout=1) as probe:
                        if probe.status == 200:
                            break
                except OSError:
                    time.sleep(.5)
            else:
                self.fail((root / 'proxy' / 'proxy.log').read_text()[-6000:])
            status = subprocess.run([str(manager.ROOT / 'runtimes' / 'playit-scoped' / 'playit-cli'), '--socket-path', pids['socket'], 'status'],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn('waiting for secret', status.stdout.lower())
            request = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            request.request('POST', '/api/session/leave', json.dumps({'id': 'e' * 32}), headers)
            response = request.getresponse(); response.read(); request.close()
            stream.close(); connection.close()
            process.wait(timeout=45)
            self.assertEqual(process.returncode, 0)
            self.assertFalse(manager.is_alive(pids['playit']))
            self.assertFalse(manager.is_alive(pids['proxy']))
            self.assertFalse((root / '.manager-state' / 'playit.pid').exists())
            self.assertFalse((root / '.manager-state' / 'waterdog.pid').exists())
            print('REAL SERVICES VERIFIED: private Playit 2.0 daemon IPC, Waterdog NetherNet HTTP listener, and last-tab cleanup of both processes. No account claim, public tunnel or global service was used.')
            stderr.close()
        finally:
            if process and process.poll() is None:
                # The real controller must close services before test cleanup.
                process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=15)
            if process and process.stdout:
                process.stdout.close()
            remaining = []
            for name in ('playit.pid', 'waterdog.pid'):
                path = root / '.manager-state' / name
                pid = manager.read_pid(path)
                if pid:
                    remaining.append(pid)
            if remaining:
                print(f'Test processes retained for safe inspection: {remaining}; data: {root}')
            else:
                shutil.rmtree(root)
