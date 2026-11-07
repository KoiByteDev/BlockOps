import io
import http.client
import threading
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

import bedrock_support as packs
import dashboard
import server_manager as manager

BP = 'af0731f7-b7d4-4917-8e5b-dee811edc373'
RP = 'da9c6afe-2226-462e-b0e8-39b70b9faed3'


def manifest(identifier=BP, kind='data', dependencies=None):
    value = {'format_version': 2,
             'header': {'name': 'BlockOps Test ' + kind, 'description': 'Disposable integration fixture',
                        'uuid': identifier, 'version': [1, 0, 0], 'min_engine_version': [1, 21, 0]},
             'modules': [{'type': kind, 'uuid': str(uuid.uuid4()), 'version': [1, 0, 0]}]}
    if dependencies:
        value['dependencies'] = dependencies
    return value


def bundle(nested=True):
    resource = {'manifest.json': json.dumps(manifest(RP, 'resources'))}
    behavior = {'manifest.json': json.dumps(manifest(dependencies=[{'uuid': RP, 'version': [1, 0, 0]}])),
                'functions/blockops_probe.mcfunction': 'scoreboard players set BlockOpsAddon blockops_probe 73\n'}
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w') as archive:
        for name, files in [('behavior', behavior), ('resource', resource)]:
            if nested:
                inner = io.BytesIO()
                with zipfile.ZipFile(inner, 'w') as pack:
                    for filename, content in files.items():
                        pack.writestr(filename, content)
                archive.writestr(name + '.mcpack', inner.getvalue())
            else:
                for filename, content in files.items():
                    archive.writestr(name + '/' + filename, content)
    return result.getvalue()


class BedrockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.folder = self.root / 'instance'
        self.folder.mkdir()
        self.profile = {'id': 'test', 'name': 'Bedrock Test', 'game': 'bedrock', 'path': str(self.folder),
                        'port': 29132, 'bedrockUdpPort': 29140, 'bedrockAddress': '127.0.0.1',
                        'maximumRam': '4G', 'backupSettings': manager.new_profile_backup_settings()}
        manager.save_json(self.folder / 'profile.json', self.profile)
        (self.folder / 'server.properties').write_text('level-name=Test World\ntransport=nethernet\n')
        self.world = self.folder / 'worlds' / 'Test World'
        self.world.mkdir(parents=True)
        self.active_profile_impl = manager.active_profile
        self.container_running_impl = manager.bedrock_container_running
        self.patchers = [mock.patch.object(manager, 'REGISTRY', self.root / 'registry.json'),
                         mock.patch.object(manager, 'STATE', self.root / 'state'),
                         mock.patch.object(manager, 'MANAGER_LOCK', self.root / 'state' / 'manager.lock'),
                         mock.patch.object(manager, 'PROFILES', self.root / 'profiles'),
                         mock.patch.object(manager, 'active_profile', return_value=None),
                         mock.patch.object(manager, 'bedrock_container_running', return_value=False)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        manager.save_json(manager.REGISTRY, {'profiles': [self.profile]})

    def install(self, content=None):
        archive = self.root / 'test.mcaddon'
        archive.write_bytes(content if content is not None else bundle())
        return packs.import_packs(self.folder, self.world, archive)

    def test_nested_bundle_imports_inactive_and_enables_paired_dependencies(self):
        self.assertEqual(len(self.install()), 2)
        self.assertFalse(any(p['enabled'] for p in packs.list_packs(self.folder, self.world)))
        packs.change_pack(self.folder, self.world, BP, 'enable')
        installed = packs.list_packs(self.folder, self.world)
        self.assertTrue(all(p['enabled'] for p in installed))
        self.assertEqual(json.loads((self.world / 'world_behavior_packs.json').read_text()), [{'pack_id': BP, 'version': [1, 0, 0]}])
        self.assertEqual(json.loads((self.world / 'world_resource_packs.json').read_text()), [{'pack_id': RP, 'version': [1, 0, 0]}])

    def test_flat_bundle_and_single_mcpack(self):
        self.assertEqual(len(self.install(bundle(False))), 2)
        with zipfile.ZipFile(self.root / 'single.mcpack', 'w') as archive:
            archive.writestr('manifest.json', json.dumps(manifest(str(uuid.uuid4()))))
        self.assertEqual(len(packs.import_packs(self.folder, self.world, self.root / 'single.mcpack')), 1)

    def test_prevents_disabling_or_removing_an_active_dependency(self):
        self.install()
        packs.change_pack(self.folder, self.world, BP, 'enable')
        before = (self.world / 'world_resource_packs.json').read_bytes()
        for action in ('disable', 'remove'):
            with self.assertRaisesRegex(manager.ManagerError, 'requires pack'):
                packs.change_pack(self.folder, self.world, RP, action)
        self.assertEqual((self.world / 'world_resource_packs.json').read_bytes(), before)
        packs.change_pack(self.folder, self.world, BP, 'disable')
        packs.change_pack(self.folder, self.world, RP, 'remove')
        self.assertFalse((self.folder / 'resource_packs' / RP).exists())

    def test_missing_or_wrong_version_dependency_does_not_activate(self):
        self.install()
        path = self.folder / 'behavior_packs' / BP / 'manifest.json'
        data = json.loads(path.read_text())
        data['dependencies'][0]['version'] = [2, 0, 0]
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(manager.ManagerError, 'requires pack'):
            packs.change_pack(self.folder, self.world, BP, 'enable')
        self.assertFalse((self.world / 'world_behavior_packs.json').exists())

    def test_duplicate_uuid_does_not_overwrite_installed_data(self):
        self.install()
        path = self.folder / 'behavior_packs' / BP / 'manifest.json'
        before = path.read_bytes()
        with self.assertRaisesRegex(manager.ManagerError, 'already installed'):
            self.install()
        self.assertEqual(path.read_bytes(), before)

    def test_rejects_cross_platform_paths_links_duplicates_and_encryption(self):
        for path in ('../escape', '/absolute', 'C:/escape', 'a\\b', 'CON.txt', 'a/../b', 'a./b', 'a /b', 'a//b'):
            with self.subTest(path=path):
                out = io.BytesIO()
                with zipfile.ZipFile(out, 'w') as archive:
                    archive.writestr(path, 'bad')
                with self.assertRaises(manager.ManagerError):
                    self.install(out.getvalue())
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            link = zipfile.ZipInfo('link')
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, '../outside')
        with self.assertRaisesRegex(manager.ManagerError, 'link or special'):
            self.install(out.getvalue())
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            archive.writestr('Folder/file', 'first')
            archive.writestr('folder/FILE', 'second')
        with self.assertRaisesRegex(manager.ManagerError, 'Duplicate'):
            self.install(out.getvalue())
        self.assertFalse((self.root / 'escape').exists())

    def test_rejects_invalid_manifest_without_partial_install(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            archive.writestr('valid/manifest.json', json.dumps(manifest()))
            archive.writestr('invalid/manifest.json', '{}')
        with self.assertRaises(manager.ManagerError):
            self.install(out.getvalue())
        self.assertFalse((self.folder / 'behavior_packs' / BP).exists())

    def test_rejects_extraction_bomb_and_corrupt_archive(self):
        with mock.patch.object(packs, 'MAX_EXPANDED_BYTES', 10):
            with self.assertRaisesRegex(manager.ManagerError, 'safety limit'):
                self.install()
        with self.assertRaisesRegex(manager.ManagerError, 'Cannot extract'):
            self.install(b'not a ZIP archive')

    def test_rejects_symlink_destinations(self):
        if os.name == 'nt':
            self.skipTest('Creating symlinks can require Windows developer mode')
        outside = self.root / 'outside'
        outside.mkdir()
        (self.folder / 'behavior_packs').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(manager.ManagerError):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])

    def test_activation_transaction_rolls_back_on_second_write_failure(self):
        self.install()
        old = []
        packs.write_json(self.world / 'world_behavior_packs.json', old)
        original = Path.replace
        failed = False
        def replace(source, target):
            nonlocal failed
            if not failed and source.name == 'world_resource_packs.json' and '.blockops-activation-' in str(source):
                failed = True
                raise OSError('disk full')
            return original(source, target)
        with mock.patch.object(Path, 'replace', replace):
            with self.assertRaisesRegex(OSError, 'disk full'):
                packs.change_pack(self.folder, self.world, BP, 'enable')
        self.assertEqual(json.loads((self.world / 'world_behavior_packs.json').read_text()), old)
        self.assertFalse((self.world / 'world_resource_packs.json').exists())

    def test_world_path_uses_bedrock_worlds_and_rejects_traversal(self):
        self.assertEqual(manager.world_path(self.folder), self.world)
        for name in ('../outside', '/outside', '..', '.', 'world\\outside'):
            (self.folder / 'server.properties').write_text('level-name=' + name)
            with self.assertRaises(manager.ManagerError):
                manager.world_path(self.folder)

    def test_docker_command_persists_files_and_uses_amd64_on_both_platforms(self):
        for windows in (False, True):
            with mock.patch.object(manager, 'IS_WINDOWS', windows), mock.patch.object(manager, 'docker_executable', return_value=Path('docker')):
                command = manager.launch_command(self.profile)
            self.assertIn('linux/amd64', command)
            self.assertIn(f'{self.folder}:/data', command)
            self.assertIn('0.0.0.0:29132:29132/tcp', command)
            self.assertIn('0.0.0.0:29140:29140/udp', command)
            self.assertIn('SERVER_UDP_PORTS=127.0.0.1:29140:29140', command)
            self.assertIn('TRANSPORT=nethernet', command)
            self.assertNotIn('--privileged', command)
        (self.folder / 'bedrock_server-1.26.63.1').touch()
        with mock.patch.object(manager, 'docker_executable', return_value=Path('docker')):
            self.assertIn('VERSION=EXISTING', manager.launch_command(self.profile))

    def test_create_edition_dispatch_and_separate_instances(self):
        with mock.patch.object(manager, 'docker_executable', return_value=Path('docker')), \
             mock.patch.object(manager, 'resolve_java') as java:
            one = manager.make_profile(manager.parser().parse_args(['create', '--game', 'minecraft', '--edition', 'bedrock', '--name', 'One']))
            two = manager.make_profile(manager.parser().parse_args(['create', '--game', 'bedrock', '--name', 'Two']))
        java.assert_not_called()
        self.assertNotEqual(one['path'], two['path'])
        self.assertNotEqual(manager.bedrock_container_name(one), manager.bedrock_container_name(two))
        self.assertEqual(one['backupSettings']['retention'], 10)

    def test_mutations_reject_running_instance_and_jobs(self):
        with mock.patch.object(manager, 'active_profile', return_value=self.profile):
            with self.assertRaisesRegex(manager.ManagerError, 'Stop this Bedrock'):
                dashboard.ensure_bedrock_mutable(self.profile)
        with mock.patch.object(manager, 'bedrock_container_running', return_value=True):
            with self.assertRaisesRegex(manager.ManagerError, 'stop first'):
                manager.bedrock_offline_backup(self.profile)
        with mock.patch.object(dashboard, 'public_job', return_value={'status': 'running'}):
            with self.assertRaisesRegex(manager.ManagerError, 'current server operation'):
                dashboard.ensure_bedrock_mutable(self.profile)

    def test_docker_inspect_distinguishes_missing_container_from_daemon_failure(self):
        with mock.patch.object(manager.shutil, 'which', return_value='docker'):
            with mock.patch.object(manager.subprocess, 'run', return_value=mock.Mock(returncode=1, stderr='error: no such object', stdout='')):
                self.assertFalse(self.container_running_impl(self.profile))
            with mock.patch.object(manager.subprocess, 'run', return_value=mock.Mock(returncode=1, stderr='Cannot connect to daemon', stdout='')):
                with self.assertRaisesRegex(manager.ManagerError, 'Cannot inspect'):
                    self.container_running_impl(self.profile)
            with mock.patch.object(manager.subprocess, 'run', side_effect=subprocess.TimeoutExpired('docker', 15)):
                with self.assertRaisesRegex(manager.ManagerError, 'Cannot inspect'):
                    self.container_running_impl(self.profile)

    def test_orphaned_container_keeps_stop_available_after_runner_exits(self):
        manager.STATE.mkdir()
        (manager.STATE / 'active-profile').write_text('test')
        with mock.patch.object(manager, 'read_pid', return_value=None), \
             mock.patch.object(manager, 'bedrock_container_running', return_value=True):
            self.assertEqual(self.active_profile_impl()['id'], 'test')

    def test_stop_timeout_never_terminates_saving_bedrock(self):
        with mock.patch.object(manager, 'active_profile', return_value=self.profile), \
             mock.patch.object(manager, 'bedrock_container_running', return_value=True), \
             mock.patch.object(manager, 'read_pid', return_value=999), \
             mock.patch.object(manager.time, 'time', side_effect=[0, 126]), \
             mock.patch.object(manager, 'stop_process') as terminate:
            with self.assertRaisesRegex(manager.ManagerError, 'did not stop cleanly'):
                manager.stop_profile()
        terminate.assert_not_called()
        self.assertTrue((self.folder / '.control' / 'stop.request').exists())

    def test_start_timeout_requests_safe_stop_without_killing_container_or_playit(self):
        process = mock.Mock(pid=999)
        process.poll.return_value = None
        with mock.patch.object(manager, 'game_port_in_use', return_value=False), \
             mock.patch.object(manager.subprocess, 'Popen', return_value=process), \
             mock.patch.object(manager.time, 'time', side_effect=[0, 0, 1000]), \
             mock.patch.object(manager, 'start_playit') as playit, \
             mock.patch.object(manager, 'stop_process') as terminate:
            with self.assertRaisesRegex(manager.ManagerError, 'safe stop was requested'):
                manager.start_profile(self.profile, 1)
        playit.assert_not_called()
        terminate.assert_not_called()
        process.terminate.assert_not_called()
        self.assertTrue((self.folder / '.control' / 'stop.request').exists())

    def test_bundled_mojang_packs_are_not_managed_or_removed(self):
        builtin = self.folder / 'behavior_packs' / 'vanilla'
        builtin.mkdir(parents=True)
        (builtin / 'manifest.json').write_text('{"modules": [{"type":"editorExtension"}]}')
        self.install()
        self.assertEqual(len(packs.list_packs(self.folder, self.world)), 2)
        self.assertTrue((builtin / 'manifest.json').exists())

    def test_script_only_behavior_pack_is_supported(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            archive.writestr('manifest.json', json.dumps(manifest(kind='script')))
        installed = self.install(out.getvalue())
        self.assertEqual(installed[0]['kind'], 'behavior_packs')
        self.assertTrue(installed[0]['scripted'])

    def test_upload_limits_and_truncation(self):
        for length, name, contents in [(0, 'pack.mcpack', b''), (257 * 1024**2, 'pack.mcpack', b''), (2, 'pack.jar', b'xx'), (10, 'pack.mcpack', b'x')]:
            with self.subTest(name=name, length=length), self.assertRaises(manager.ManagerError):
                dashboard.receive_addon(self.profile, io.BytesIO(contents), length, name)
        data = bundle()
        self.assertEqual(len(dashboard.receive_addon(self.profile, io.BytesIO(data), len(data), 'test.mcaddon')), 2)
        self.assertFalse(list(self.folder.glob('.blockops-upload-*')))

    def test_bedrock_backup_restore_recovers_world_and_pack_files(self):
        self.install()
        packs.change_pack(self.folder, self.world, BP, 'enable')
        (self.world / 'level.dat').write_bytes(b'bedrock-data')
        (self.world / 'db').mkdir()
        (self.world / 'db' / 'CURRENT').write_bytes(b'MANIFEST-000001')
        name = dashboard.create_offline_backup(self.profile)
        path = self.folder / 'backups' / name
        self.assertEqual(dashboard.validate_backup_archive(self.profile, path), 'Test World')
        (self.world / 'level.dat').write_bytes(b'changed')
        shutil.rmtree(self.folder / 'behavior_packs' / BP)
        dashboard.apply_backup_archive(self.profile, path)
        self.assertEqual((self.world / 'level.dat').read_bytes(), b'bedrock-data')
        self.assertTrue((self.folder / 'behavior_packs' / BP / 'functions' / 'blockops_probe.mcfunction').exists())
        self.assertTrue(all(p['enabled'] for p in packs.list_packs(self.folder, self.world)))
        self.assertFalse((self.world / packs.SNAPSHOT_PACKS).exists())

    def test_java_world_backup_is_not_accepted_for_bedrock(self):
        path = self.root / 'java.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            entry = tarfile.TarInfo('world/level.dat')
            entry.size = 4
            archive.addfile(entry, io.BytesIO(b'java'))
        with self.assertRaisesRegex(manager.ManagerError, 'LevelDB'):
            dashboard.validate_backup_archive(self.profile, path)

    def test_settings_use_bedrock_properties_and_keep_network_in_profile(self):
        dashboard.save_profile_settings('test', {'name': 'New name', 'maximumRam': '5G', 'bedrockAddress': '192.168.1.20', 'port': 19142, 'bedrockUdpPort': 19145,
                                                 'properties': {'whiteList': True, 'allowCheats': True, 'texturepackRequired': True, 'gamemode': 'creative', 'maxPlayers': 8}})
        props = dashboard.parse_properties(self.folder / 'server.properties')
        self.assertEqual(props['allow-list'], 'true')
        self.assertEqual(props['allow-cheats'], 'true')
        self.assertEqual(props['texturepack-required'], 'true')
        self.assertNotIn('white-list', props)
        self.assertNotIn('hardcore', props)
        self.assertNotIn('jvmArguments', manager.load_json(self.folder / 'profile.json'))
        self.assertEqual(manager.profile_by_id('test')['port'], 19142)

    def test_invalid_network_does_not_change_settings(self):
        for address in ('bad-hostname', '0.0.0.0', '224.0.0.1', '::1'):
            with self.subTest(address=address), self.assertRaises(manager.ManagerError):
                dashboard.save_profile_settings('test', {'bedrockAddress': address})
        self.assertEqual(manager.profile_by_id('test')['name'], 'Bedrock Test')

    def test_authenticated_http_import_activation_and_java_rejection(self):
        class QuietHandler(dashboard.DashboardHandler):
            def log_message(self, *args):
                pass
        server = dashboard.DashboardServer(('127.0.0.1', 0), QuietHandler, 'isolated-test-token')
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        port = server.server_address[1]
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        headers = {'Cookie': 'mc_dashboard=isolated-test-token', 'Origin': f'http://127.0.0.1:{port}'}
        try:
            with mock.patch.object(dashboard, 'PORT', port):
                connection.request('PUT', '/api/profiles/test/addons/test.mcaddon', bundle())
                response = connection.getresponse()
                self.assertEqual(response.status, 401)
                response.read()
                connection.request('PUT', '/api/profiles/test/addons/test.mcaddon', bundle(), headers)
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200, payload)
                self.assertEqual(len(payload['packs']), 2)
                connection.request('POST', '/api/profiles/test/addons/change', json.dumps({'id': BP, 'action': 'enable'}), headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 200, response.read())
                connection.request('GET', '/api/profiles/test/addons', headers=headers)
                response = connection.getresponse()
                self.assertTrue(all(p['enabled'] for p in json.loads(response.read())['addons']))
                connection.request('PUT', '/api/profiles/test/mods/bad.jar', b'not-bedrock', headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                self.assertIn('incompatible', json.loads(response.read())['error'])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            worker.join()

    def test_creation_api_bypasses_java_playit_gate_for_bedrock(self):
        handler = object.__new__(dashboard.DashboardHandler)
        handler.send_json = mock.Mock()
        with mock.patch.object(manager, 'registry', return_value={'profiles': []}), \
             mock.patch.object(dashboard, 'setup_status', return_value={'canCreateServer': False}), \
             mock.patch.object(dashboard, 'start_job', return_value={}) as start:
            handler.handle_api_post('/api/jobs/create', {'name': 'Bedrock', 'game': 'minecraft', 'edition': 'bedrock'})
            args = start.call_args.args[2]
            self.assertEqual(args[args.index('--game') + 1], 'bedrock')
            self.assertIn('--bedrock-address', args)
            with self.assertRaisesRegex(manager.ManagerError, 'Finish Playit'):
                handler.handle_api_post('/api/jobs/create', {'name': 'Java', 'minecraftVersion': '1.21.8'})


@unittest.skipUnless(os.environ.get('BLOCKOPS_TEST_DOCKER') == '1', 'Opt-in real Docker test; downloads official BDS')
class BedrockDockerIntegrationTests(unittest.TestCase):
    def test_real_lifecycle_persistence_addons_and_restore(self):
        root = Path(tempfile.mkdtemp(prefix='blockops-bedrock-integration-')).resolve()
        env = {**os.environ, 'BLOCKOPS_DATA_DIR': str(root)}
        executable = Path(manager.__file__).resolve()
        def cli(*arguments):
            result = subprocess.run([sys.executable, '-u', str(executable), *arguments], env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout
        profile = None
        try:
            cli('create', '--game', 'bedrock', '--name', 'Integration', '--port', '29152', '--udp-port', '29160')
            profile = json.loads((root / 'server-profiles.json').read_text())['profiles'][0]
            folder = manager.absolute_profile_path(profile)
            world = manager.world_path(folder)
            def wait_log(text, offset=0):
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    log = (folder / 'profile-runner.log').read_text(errors='replace')
                    if text in log[offset:]:
                        return
                    time.sleep(.25)
                self.fail(f'Expected log {text!r}; log tail: {log[-5000:]}')
            cli('start', '--profile', 'integration', '--timeout', '240')
            self.assertTrue(manager.bedrock_ready(profile))
            manager.bedrock_command(profile, 'scoreboard objectives add blockops_probe dummy')
            manager.bedrock_command(profile, 'scoreboard players set BlockOpsProbe blockops_probe 37')
            wait_log('37')
            cli('stop')
            self.assertFalse(manager.bedrock_container_running(profile))
            self.assertTrue((world / 'db' / 'CURRENT').is_file())
            archive = root / 'fixture.mcaddon'
            archive.write_bytes(bundle())
            packs.import_packs(folder, world, archive)
            packs.change_pack(folder, world, BP, 'enable')
            # Functions require cheats; this changes only the disposable world.
            dashboard.update_properties(folder / 'server.properties', {'allow-cheats': 'true'})
            backup = manager.bedrock_offline_backup(profile)
            offset = len((folder / 'profile-runner.log').read_text())
            cli('start', '--profile', 'integration', '--timeout', '120')
            manager.bedrock_command(profile, 'scoreboard players list BlockOpsProbe')
            wait_log('37', offset)
            manager.bedrock_command(profile, 'function blockops_probe')
            manager.bedrock_command(profile, 'scoreboard players list BlockOpsAddon')
            wait_log('blockops_probe: 73', offset)
            wait_log('Pack Stack', offset)
            # Manual backup exercises runner stop/snapshot/restart, not a raw tar copy.
            (folder / '.control' / 'backup.request').write_text('integration\n')
            wait_log('Bedrock backup created:', offset)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and not manager.bedrock_ready(profile):
                time.sleep(.5)
            self.assertTrue(manager.bedrock_ready(profile))
            cli('stop')
            packs.change_pack(folder, world, BP, 'remove')
            self.assertFalse((folder / 'behavior_packs' / BP).exists())
            # Restore also recovers installed pack assets and activations.
            with mock.patch.object(manager, "STATE", root / ".manager-state"), \
                 mock.patch.object(manager, "MANAGER_LOCK", root / ".manager-state" / "manager.lock"), \
                 mock.patch.object(manager, "REGISTRY", root / "server-profiles.json"):
                dashboard.apply_backup_archive(profile, backup)
            self.assertTrue((folder / 'behavior_packs' / BP).exists())
            offset = len((folder / 'profile-runner.log').read_text())
            cli('start', '--profile', 'integration', '--timeout', '120')
            manager.bedrock_command(profile, 'function blockops_probe')
            manager.bedrock_command(profile, 'scoreboard players list BlockOpsAddon')
            wait_log('blockops_probe: 73', offset)
            manager.bedrock_command(profile, 'scoreboard players list BlockOpsProbe')
            wait_log('37', offset)
            cli('stop')
            self.assertNotIn('save-all', (folder / 'profile-runner.log').read_text())
            self.assertNotIn('save-off', (folder / 'profile-runner.log').read_text())
            print('REAL DOCKER VERIFIED: official BDS start/stop, console, NetherNet HTTP readiness, LevelDB scoreboard persistence, behavior function activation, paired resource activation files, backup restart and restore.')
        finally:
            if profile:
                if manager.bedrock_container_running(profile):
                    subprocess.run([sys.executable, str(executable), 'stop'], env=env, capture_output=True, timeout=140)
                if manager.bedrock_container_running(profile):
                    print(f'Bedrock test container still saving; retained temporary data: {root}')
                else:
                    shutil.rmtree(root)
            else:
                shutil.rmtree(root)
