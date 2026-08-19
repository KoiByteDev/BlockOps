import io
import os
import tempfile
import tarfile
import unittest
from pathlib import Path
from unittest import mock

import dashboard


class DashboardTests(unittest.TestCase):
    def test_manual_playit_upload_places_valid_windows_executable(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(dashboard.platform, "system", return_value="Windows"), \
             mock.patch.object(dashboard.manager, "ROOT", Path(directory)):
            destination = dashboard.install_playit_executable(io.BytesIO(b"MZportable"), 10, "playit.exe")
            self.assertEqual(destination, Path(directory) / "runtimes" / "playit" / "playit.exe")
            self.assertEqual(destination.read_bytes(), b"MZportable")

    def test_manual_playit_upload_rejects_non_executable(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(dashboard.platform, "system", return_value="Windows"), \
             mock.patch.object(dashboard.manager, "ROOT", Path(directory)):
            with self.assertRaisesRegex(dashboard.manager.ManagerError, "not a valid Windows executable"):
                dashboard.install_playit_executable(io.BytesIO(b"not-an-exe"), 10, "playit.exe")
            self.assertFalse((Path(directory) / "runtimes" / "playit" / "playit.exe").exists())

    def test_setup_status_gates_first_world_behind_playit(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(dashboard, "ONBOARDING_FILE", Path(directory) / "onboarding.json"), \
             mock.patch.object(dashboard.manager, "registry", return_value={"profiles": []}), \
             mock.patch.object(dashboard.manager, "playit_credential_ready", return_value=False), \
             mock.patch.object(dashboard.manager, "read_pid", return_value=None):
            result = dashboard.setup_status()
        self.assertTrue(result["pythonReady"])
        self.assertFalse(result["profileReady"])
        self.assertFalse(result["canCreateServer"])
        self.assertEqual(
            [step["id"] for step in result["steps"]],
            ["runtime", "agent", "account", "tunnel", "world"],
        )

    def test_existing_profiles_are_not_blocked_by_new_onboarding_gate(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(dashboard, "ONBOARDING_FILE", Path(directory) / "onboarding.json"), \
             mock.patch.object(dashboard.manager, "registry", return_value={"profiles": [{"id": "legacy"}]}), \
             mock.patch.object(dashboard.manager, "playit_credential_ready", return_value=False), \
             mock.patch.object(dashboard.manager, "read_pid", return_value=None):
            self.assertTrue(dashboard.setup_status()["canCreateServer"])

    def test_confirming_tunnel_persists_first_run_completion(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(dashboard, "ONBOARDING_FILE", Path(directory) / "onboarding.json"), \
             mock.patch.object(dashboard.manager, "registry", return_value={"profiles": []}), \
             mock.patch.object(dashboard.manager, "playit_credential_ready", return_value=True), \
             mock.patch.object(dashboard.manager, "read_pid", return_value=123):
            result = dashboard.confirm_playit_tunnel()
            saved = dashboard.manager.load_json(dashboard.ONBOARDING_FILE)
        self.assertTrue(result["canCreateServer"])
        self.assertTrue(saved["playitTunnelConfirmed"])

    def test_properties_are_updated_without_losing_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.properties"
            path.write_text("# Keep me\nmotd=Old world\npvp=true\n", encoding="utf-8")
            dashboard.update_properties(path, {"motd": "New world", "hardcore": "true"})
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "# Keep me\nmotd=New world\npvp=true\nhardcore=true\n",
            )

    def test_tail_lines_returns_only_latest_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latest.log"
            path.write_text("\n".join(f"line {value}" for value in range(20)), encoding="utf-8")
            self.assertEqual(dashboard.tail_lines(path, limit=3), ["line 17", "line 18", "line 19"])

    def test_profile_id_rejects_paths(self):
        with self.assertRaisesRegex(dashboard.manager.ManagerError, "Invalid server identifier"):
            dashboard.profile_for_id("../outside")

    def test_cached_versions_uses_release_entries_only(self):
        with tempfile.TemporaryDirectory() as directory:
            original = dashboard.manager.CACHE
            dashboard.manager.CACHE = Path(directory)
            try:
                dashboard.manager.save_json(
                    dashboard.manager.CACHE / "version_manifest_v2.json",
                    {"versions": [{"id": "1.21", "type": "release"}, {"id": "26w01a", "type": "snapshot"}]},
                )
                self.assertEqual(dashboard.cached_minecraft_versions(), ["1.21"])
            finally:
                dashboard.manager.CACHE = original

    def test_player_list_parses_modern_output(self):
        result = dashboard.parse_player_list(
            '[Server thread/INFO]: There are 2 of a max of 20 players online: Alex, Nicodraco'
        )
        self.assertEqual(result, {"online": 2, "maximum": 20, "players": ["Alex", "Nicodraco"]})

    def test_player_list_parses_legacy_forge_output(self):
        result = dashboard.parse_player_list(
            '[Server thread/INFO] [DedicatedServer]: There are 1/8 players online: Nicodraco'
        )
        self.assertEqual(result, {"online": 1, "maximum": 8, "players": ["Nicodraco"]})

    def test_player_list_parses_multiline_legacy_forge_output(self):
        result = dashboard.parse_player_list(
            '[03:39:11] [Server thread/INFO] [net.minecraft.server.dedicated.DedicatedServer]: '
            'There are 3/20 players online:\n'
            '[03:39:11] [Server thread/INFO] [net.minecraft.server.dedicated.DedicatedServer]: '
            'jrd6, Nicodraco, UlraBlazar'
        )
        self.assertEqual(
            result,
            {"online": 3, "maximum": 20, "players": ["jrd6", "Nicodraco", "UlraBlazar"]},
        )

    def test_player_list_waits_for_multiline_names(self):
        result = dashboard.parse_player_list(
            '[Server thread/INFO] [DedicatedServer]: There are 3/20 players online:'
        )
        self.assertIsNone(result)

    def test_player_list_parses_empty_server(self):
        result = dashboard.parse_player_list(
            '[Server thread/INFO]: There are 0 of a max of 20 players online:'
        )
        self.assertEqual(result, {"online": 0, "maximum": 20, "players": []})

    def test_config_path_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(dashboard.manager.ManagerError, "Invalid configuration path"):
                dashboard.safe_config_path(Path(directory), "../secret.txt")

    def test_config_file_round_trip_creates_recovery_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            config = folder / "config"
            config.mkdir()
            path = config / "example.cfg"
            path.write_text("enabled=true\n", encoding="utf-8")
            profile = {"id": "test", "path": str(folder)}
            with mock.patch.object(dashboard.manager, "absolute_profile_path", return_value=folder), \
                 mock.patch.object(dashboard.manager, "world_path", return_value=folder / "world"):
                original = dashboard.read_config_file(profile, "config", "example.cfg")
                saved = dashboard.save_config_file(profile, "config", "example.cfg", "enabled=false\n", original["hash"])
            self.assertEqual(saved["content"], "enabled=false\n")
            backups = list((folder / ".blockops-history").rglob("example.cfg"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "enabled=true\n")

    def test_config_save_detects_external_change(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            config = folder / "config"
            config.mkdir()
            path = config / "example.toml"
            path.write_text("value = 1\n", encoding="utf-8")
            profile = {"id": "test", "path": str(folder)}
            with mock.patch.object(dashboard.manager, "absolute_profile_path", return_value=folder), \
                 mock.patch.object(dashboard.manager, "world_path", return_value=folder / "world"):
                original = dashboard.read_config_file(profile, "config", "example.toml")
                path.write_text("value = 2\n", encoding="utf-8")
                with self.assertRaisesRegex(dashboard.manager.ManagerError, "changed on disk"):
                    dashboard.save_config_file(profile, "config", "example.toml", "value = 3\n", original["hash"])

    def test_apply_backup_archive_replaces_world(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            world = folder / "world"
            world.mkdir()
            (world / "level.dat").write_bytes(b"old")
            (world / "old.txt").write_text("old", encoding="utf-8")
            source_root = folder / "source"
            restored = source_root / "world"
            restored.mkdir(parents=True)
            (restored / "level.dat").write_bytes(b"new")
            (restored / "new.txt").write_text("new", encoding="utf-8")
            archive_path = folder / "backup.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(restored, arcname="world")
            profile = {"id": "test", "path": str(folder)}
            with mock.patch.object(dashboard.manager, "absolute_profile_path", return_value=folder), \
                 mock.patch.object(dashboard.manager, "world_path", return_value=world):
                dashboard.apply_backup_archive(profile, archive_path)
            self.assertEqual((world / "level.dat").read_bytes(), b"new")
            self.assertTrue((world / "new.txt").is_file())
            self.assertFalse((world / "old.txt").exists())

    def test_imported_backup_can_use_a_different_world_folder_name(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            world = folder / "current-world"
            world.mkdir()
            (world / "level.dat").write_bytes(b"old")
            imported = folder / "from-friend" / "their-world"
            imported.mkdir(parents=True)
            (imported / "level.dat").write_bytes(b"new")
            (imported / "playerdata.dat").write_bytes(b"progress")
            archive_path = folder / "imported.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(imported, arcname="their-world")
            profile = {"id": "test", "path": str(folder)}
            with mock.patch.object(dashboard.manager, "absolute_profile_path", return_value=folder), \
                 mock.patch.object(dashboard.manager, "world_path", return_value=world):
                dashboard.apply_backup_archive(profile, archive_path)
            self.assertEqual((world / "level.dat").read_bytes(), b"new")
            self.assertEqual((world / "playerdata.dat").read_bytes(), b"progress")

    def test_backup_validation_rejects_archive_without_one_world_root(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            archive_path = folder / "not-a-world.tar.gz"
            source = folder / "notes.txt"
            source.write_text("not a Minecraft world", encoding="utf-8")
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(source, arcname="notes.txt")
            profile = {"id": "test", "path": str(folder)}
            with self.assertRaisesRegex(dashboard.manager.ManagerError, "exactly one world folder"):
                dashboard.validate_backup_archive(profile, archive_path)

    def test_backup_pruning_deletes_oldest_after_retention_is_exceeded(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            world = folder / "world"
            backups = folder / "backups"
            world.mkdir()
            backups.mkdir()
            profile = {"id": "test", "path": str(folder), "backupSettings": {"retention": 10}}
            created = []
            for index in range(11):
                path = backups / f"world-{index:02d}.tar.gz"
                path.write_bytes(b"backup")
                os.utime(path, (index + 1, index + 1))
                created.append(path)
            with mock.patch.object(dashboard.manager, "absolute_profile_path", return_value=folder), \
                 mock.patch.object(dashboard.manager, "world_path", return_value=world):
                dashboard.prune_profile_backups(profile)
            self.assertFalse(created[0].exists())
            self.assertTrue(all(path.exists() for path in created[1:]))

    def test_apply_backup_archive_rejects_traversal_without_touching_world(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            world = folder / "world"
            world.mkdir()
            (world / "level.dat").write_bytes(b"safe")
            archive_path = folder / "unsafe.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                member = tarfile.TarInfo("../outside.txt")
                member.size = 0
                archive.addfile(member)
            profile = {"id": "test", "path": str(folder)}
            with mock.patch.object(dashboard.manager, "absolute_profile_path", return_value=folder), \
                 mock.patch.object(dashboard.manager, "world_path", return_value=world):
                with self.assertRaisesRegex(dashboard.manager.ManagerError, "unsafe entry"):
                    dashboard.apply_backup_archive(profile, archive_path)
            self.assertEqual((world / "level.dat").read_bytes(), b"safe")

    def test_performance_capabilities_are_discovered_per_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            mods = folder / "mods"
            mods.mkdir()
            (mods / "spark-forge.jar").write_bytes(b"jar")
            profile = {"id": "test", "path": str(folder), "loader": "forge", "javaPath": str(folder / "java")}
            with mock.patch.object(dashboard.manager, "absolute_profile_path", return_value=folder):
                capabilities = dashboard.performance_capabilities(profile)
            self.assertTrue(capabilities["spark"])
            self.assertTrue(capabilities["forgeTps"])
            self.assertFalse(capabilities["fabric"])

    def test_vanilla_performance_does_not_claim_profiler_support(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            folder.mkdir(exist_ok=True)
            profile = {"id": "test", "path": str(folder), "loader": "vanilla", "javaPath": str(folder / "java")}
            with mock.patch.object(dashboard.manager, "absolute_profile_path", return_value=folder):
                capabilities = dashboard.performance_capabilities(profile)
            self.assertTrue(capabilities["processMetrics"])
            self.assertFalse(capabilities["spark"])
            self.assertFalse(capabilities["forgeTps"])

    def test_diagnostic_events_parse_lag_and_backup_without_debug_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            logs = folder / "logs"
            backups = folder / "backups"
            logs.mkdir()
            backups.mkdir()
            line = "[12:00:00] [Server thread/WARN]: Can't keep up! Running 2500ms behind, skipping 50 tick(s)"
            (logs / "latest.log").write_text(line + "\n", encoding="utf-8")
            (backups / "world-20260101.tar.gz").write_bytes(b"backup")
            profile = {"id": "test", "path": str(folder)}
            with mock.patch.object(dashboard.manager, "absolute_profile_path", return_value=folder):
                events = dashboard.diagnostic_events(profile)
            lag = next(event for event in events if event["category"] == "lag")
            self.assertEqual(lag["behindMs"], 2500)
            self.assertEqual(lag["skippedTicks"], 50)
            self.assertTrue(any(event["category"] == "backup" for event in events))

    def test_performance_action_rejects_missing_capability_without_command(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            profile = {"id": "test", "path": str(folder), "loader": "vanilla", "javaPath": str(folder / "java")}
            with mock.patch.object(dashboard.manager, "absolute_profile_path", return_value=folder), \
                 mock.patch.object(dashboard.manager, "send_command") as send_command:
                with self.assertRaisesRegex(dashboard.manager.ManagerError, "not supported"):
                    dashboard.run_performance_action(profile, "spark-tps")
            send_command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
