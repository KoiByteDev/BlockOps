import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server_manager as manager


class ServerManagerTests(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(manager.slug("My Fabric 1.21!"), "my-fabric-1-21")

    def test_claim_url_uses_latest_prompt(self):
        output = "claim https://playit.gg/claim/old123\nclaim https://playit.gg/claim/new456\n"
        self.assertEqual(manager.claim_url_from_output(output), "https://playit.gg/claim/new456")

    def test_claim_url_absent_for_enabled_agent(self):
        self.assertIsNone(manager.claim_url_from_output("agent registered; tunnel running"))

    def test_playit_setup_surfaces_claim_url_without_creating_a_world(self):
        claim = "https://playit.gg/claim/new456"
        with mock.patch.object(manager, "playit_credential_ready", return_value=False), \
             mock.patch.object(manager, "start_playit", return_value=(123, 0)), \
             mock.patch.object(manager, "is_alive", return_value=True), \
             mock.patch.object(manager, "playit_output_since", return_value=f"claim {claim}"):
            with self.assertRaisesRegex(manager.ManagerError, "Finish the secure Playit account claim"):
                manager.setup_playit()

    def test_playit_setup_reuses_connected_agent(self):
        with mock.patch.object(manager, "playit_credential_ready", return_value=True), \
             mock.patch.object(manager, "start_playit", return_value=(123, 0)) as start:
            manager.setup_playit()
        start.assert_called_once_with()

    def test_required_java_uses_mojang_metadata(self):
        with mock.patch.object(manager, "minecraft_details", return_value={"javaVersion": {"majorVersion": 21}}):
            self.assertEqual(manager.required_java("1.21.8"), 21)

    def test_java_8_apple_silicon_uses_native_zulu(self):
        package = {"name": "zulu8-ca-jdk-macosx_aarch64.tar.gz", "download_url": "https://example.test/zulu8.tar.gz"}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(manager, "RUNTIMES", Path(directory)), \
             mock.patch.object(manager.platform, "machine", return_value="arm64"), \
             mock.patch.object(manager, "api_json", return_value=[package]), \
             mock.patch.object(manager, "download", side_effect=manager.ManagerError("selected")) as download:
            with self.assertRaisesRegex(manager.ManagerError, "selected"):
                manager.resolve_java(8)
            self.assertEqual(download.call_args.args[0], package["download_url"])

    def test_windows_java_uses_adoptium_zip(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(manager, "RUNTIMES", Path(directory)), \
             mock.patch.object(manager, "IS_WINDOWS", True), \
             mock.patch.object(manager.platform, "machine", return_value="AMD64"), \
             mock.patch.object(manager, "download", side_effect=manager.ManagerError("selected")) as download:
            with self.assertRaisesRegex(manager.ManagerError, "selected"):
                manager.resolve_java(21)
            self.assertIn("/windows/x64/", download.call_args.args[0])
            self.assertTrue(str(download.call_args.args[1]).endswith(".zip"))

    def test_link_or_copy_reuses_cached_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "cache.jar", root / "profile" / "server.jar"
            source.write_bytes(b"jar")
            manager.link_or_copy(source, destination)
            self.assertEqual(source.stat().st_ino, destination.stat().st_ino)

    def test_launch_command_for_legacy_jar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server.jar").write_bytes(b"jar")
            profile = {
                "path": str(root), "minecraftVersion": "1.20.4", "javaMajor": 17,
                "javaPath": "/java", "minimumRam": "2G", "maximumRam": "6G",
                "launchJar": "server.jar", "jvmArguments": ["-XX:+UseG1GC"], "serverArguments": ["nogui"],
            }
            with mock.patch.object(manager.Path, "exists", return_value=True), mock.patch.object(manager, "java_major", return_value=17):
                command = manager.launch_command(profile)
            self.assertEqual(command, ["/java", "-XX:+UseG1GC", "-Xms2G", "-Xmx6G", "-jar", "server.jar", "nogui"])

    def test_detached_options_are_platform_specific(self):
        with mock.patch.object(manager, "IS_WINDOWS", False):
            self.assertEqual(manager.detached_process_options(), {"start_new_session": True})
        with mock.patch.object(manager, "IS_WINDOWS", True):
            options = manager.detached_process_options()
            self.assertIn("creationflags", options)
            self.assertNotIn("start_new_session", options)

    def test_backup_settings_defaults_and_bounds(self):
        self.assertEqual(
            manager.backup_settings({}),
            {"enabled": True, "intervalMinutes": 10, "retention": 12, "compressionLevel": 6, "backupOnStop": False, "onlyWhenEmpty": False},
        )
        self.assertEqual(manager.backup_settings({"backupSettings": {"intervalMinutes": 1}})["intervalMinutes"], 5)
        self.assertEqual(manager.backup_settings({"backupSettings": {"retention": 999}})["retention"], 100)

    def test_new_profiles_use_thirty_minutes_and_ten_backups(self):
        self.assertEqual(
            manager.new_profile_backup_settings(),
            {"enabled": True, "intervalMinutes": 30, "retention": 10, "compressionLevel": 6, "backupOnStop": False, "onlyWhenEmpty": False},
        )
        # The fallback for existing legacy profiles remains unchanged.
        self.assertEqual(manager.backup_settings({})["intervalMinutes"], 10)
        self.assertEqual(manager.backup_settings({})["retention"], 12)

    def test_live_player_count_uses_server_list_output(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            logs = folder / "logs"
            logs.mkdir()
            latest = logs / "latest.log"
            latest.write_text("startup\n", encoding="utf-8")

            class Input:
                def write(self, value):
                    latest.write_text(latest.read_text(encoding="utf-8") + "[12:00:00] There are 2 of a max of 20 players online: Alex, Steve\n", encoding="utf-8")
                def flush(self):
                    pass

            process = mock.Mock()
            process.stdin = Input()
            process.poll.return_value = None
            self.assertEqual(manager.live_player_count(folder, process, timeout=0.2), 2)


if __name__ == "__main__":
    unittest.main()
