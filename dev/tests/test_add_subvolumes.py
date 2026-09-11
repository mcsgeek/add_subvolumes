"""Unprivileged regression tests; no live mounts or system files are changed.

Run: python3 -m unittest discover -s dev/tests -v
"""
import contextlib
import io
import os
import shlex
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import uuid
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[2] / "add_subvolumes.sh"
SOURCE = SCRIPT.read_text().split("<<'PYTHON_ENGINE'\n", 1)[1].rsplit("\nPYTHON_ENGINE", 1)[0]
engine = types.ModuleType("add_subvolumes_engine")
sys.modules[engine.__name__] = engine
exec(compile(SOURCE, str(SCRIPT), "exec"), engine.__dict__)


def result(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class MissingToolsTests(unittest.TestCase):
    @patch.object(engine, "discover_tool_packages", return_value={})
    def test_lsof_message_explains_remedy_and_no_changes(self, discovery):
        self.assertEqual(engine.missing_tools_message(["lsof"]),
                         "Cannot continue: required tools are unavailable.\n\n"
                         "Missing tools: lsof\n\n"
                         "Please install the packages providing these tools and run this script again.\n"
                         "No migration changes have been made.")

    def test_missing_tools_stop_before_sudo_or_migration_in_both_modes(self):
        for execute in (False, True):
            with self.subTest(execute=execute):
                migration = engine.Migration(execute, "/unused")
                with patch.object(engine.os, "geteuid", return_value=1000), \
                     patch.dict(os.environ), \
                     patch.object(engine, "load_config", side_effect=[["/opt"], []]), \
                     patch.object(engine.shutil, "which", side_effect=lambda name: None if name in ("blkid", "lsof") else "/stub/" + name), \
                     patch.object(engine, "command") as commands, \
                     patch.object(migration, "start_transaction") as start:
                    with self.assertRaises(engine.MissingTools) as caught:
                        migration.run()
                    self.assertIn("Missing tools: blkid, lsof", str(caught.exception))
                    commands.assert_not_called()
                    start.assert_not_called()

    def test_main_prints_missing_tools_message_without_generic_error_prefix(self):
        message = engine.missing_tools_message(["lsof"])
        output = io.StringIO()
        with patch.object(sys, "argv", ["script", "--dryrun", "/unused"]), \
             patch.object(engine.signal, "signal"), \
             patch.object(engine.Migration, "run", side_effect=engine.MissingTools(message)), \
             patch.object(engine.Migration, "cleanup", return_value=True), \
             contextlib.redirect_stderr(output):
            self.assertEqual(engine.main(), 1)
        self.assertEqual(output.getvalue(), message + "\n")


class CommandSearchPathTests(unittest.TestCase):
    def test_normal_user_path_includes_system_administration_directories(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            search = engine.command_search_path().split(os.pathsep)
            self.assertIn("/usr/sbin", search)
            self.assertIn("/sbin", search)
            self.assertEqual(search[:2], ["/usr/bin", "/bin"])
            self.assertEqual(os.environ["PATH"], "/usr/bin:/bin")

    def test_installed_blkid_is_found_outside_user_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            userbin = base / "bin"
            sbin = base / "sbin"
            userbin.mkdir()
            sbin.mkdir()
            blkid = sbin / "blkid"
            blkid.write_text("#!/bin/sh\nexit 0\n")
            blkid.chmod(0o755)
            with patch.dict(os.environ, {"PATH": str(userbin)}), \
                 patch.object(engine, "SYSTEM_COMMAND_DIRS", (str(sbin),)):
                self.assertIsNone(shutil.which("blkid"))
                self.assertEqual(shutil.which("blkid", path=engine.command_search_path()), str(blkid))
                self.assertIsNone(shutil.which("lsof", path=engine.command_search_path()))

    def test_existing_paths_keep_precedence_without_duplicate_entries(self):
        with patch.dict(os.environ, {"PATH": "/custom/bin:/usr/sbin:/usr/bin:/usr/sbin"}):
            search = engine.command_search_path().split(os.pathsep)
            self.assertEqual(search[0], "/custom/bin")
            self.assertEqual(search.count("/usr/sbin"), 1)


class LauncherTests(unittest.TestCase):
    def launch(self, mode, version=None, broken=False):
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory)
            if version is not None or broken:
                interpreter = tools / "python3"
                if broken:
                    body = 'echo "interpreter failed" >&2\nexit 42\n'
                else:
                    probe = "import sys; sys.version_info = {!r}; exec(sys.argv[1])".format(version)
                    body = ('if [[ "$1" == "-c" ]]; then\n'
                            '  exec ' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(probe) + ' "$2"\n'
                            'fi\nprintf "ENGINE_REACHED:%s\\n" "$2"\n')
                interpreter.write_text("#!/bin/bash\n" + body)
                interpreter.chmod(0o755)
                (tools / "dirname").symlink_to(shutil.which("dirname"))
            return subprocess.run(["/bin/bash", str(SCRIPT), mode], text=True, capture_output=True,
                                  env={**os.environ, "PATH": directory})

    def test_missing_python_stops_before_engine_in_both_modes(self):
        for mode in ("--dryrun", "--execute"):
            with self.subTest(mode=mode):
                completed = self.launch(mode)
                self.assertEqual(completed.returncode, 1)
                self.assertIn("Python 3.9 or newer is required; python3 was not found", completed.stderr)
                self.assertNotIn("ENGINE_REACHED", completed.stdout)

    def test_old_python_reports_detected_version_before_engine(self):
        for version in ((3, 6, 9), (3, 7, 3), (3, 8, 10)):
            for mode in ("--dryrun", "--execute"):
                with self.subTest(version=version, mode=mode):
                    completed = self.launch(mode, version)
                    self.assertEqual(completed.returncode, 1)
                    self.assertIn("found {}.{}.{}".format(*version), completed.stderr)
                    self.assertNotIn("ENGINE_REACHED", completed.stdout)

    def test_python_39_and_newer_reach_engine(self):
        for version in ((3, 9, 0), (3, 10, 12), (3, 13, 5)):
            for mode in ("--dryrun", "--execute"):
                with self.subTest(version=version, mode=mode):
                    completed = self.launch(mode, version)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertIn("ENGINE_REACHED:" + mode, completed.stdout)

    def test_failed_interpreter_stops_before_engine(self):
        completed = self.launch("--execute", broken=True)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("interpreter failed", completed.stderr)
        self.assertNotIn("ENGINE_REACHED", completed.stdout)


class ConfigurationTests(unittest.TestCase):
    def config(self, text, family="ROOT", optional=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "volumes.conf"
            if text is not None:
                path.write_text(text)
            return engine.load_config(path, family, "alice", optional=optional)

    def test_arrays_comments_and_user_expansion(self):
        self.assertEqual(self.config('''
          # Comment
          CORE_HOMEVOLUMES=( "home/${REAL_USER}/.cache" )
          OPTIONAL_HOMEVOLUMES=(
             'home/alice/.ssh' # Comment after an entry
          )
        ''', "HOME"), ["/home/alice/.cache", "/home/alice/.ssh"])

    def test_empty_arrays_and_missing_optional_home(self):
        self.assertEqual(self.config("CORE_ROOTVOLUMES=()\nOPTIONAL_ROOTVOLUMES=()"), [])
        self.assertEqual(self.config(None, "HOME", optional=True), [])
        with self.assertRaises(engine.Refusal):
            self.config(None)

    def test_missing_duplicate_and_unknown_arrays(self):
        for value in ("CORE_ROOTVOLUMES=()", "UNKNOWN=()",
                      "CORE_ROOTVOLUMES=() CORE_ROOTVOLUMES=() OPTIONAL_ROOTVOLUMES=()"):
            with self.subTest(value=value), self.assertRaises(engine.Refusal):
                self.config(value)

    def test_configuration_cannot_execute_shell(self):
        for value in ('$(touch /tmp/never-run)', '`touch /tmp/never-run`',
                      'opt;touch /tmp/never-run', '${HOME}/cache'):
            with self.subTest(value=value), self.assertRaises(engine.Refusal):
                self.config(f'CORE_ROOTVOLUMES=( "{value}" ) OPTIONAL_ROOTVOLUMES=()')

    def test_reject_alternate_path_spellings(self):
        for value in ("", "/opt", "opt/", "opt//cache", "opt/../boot", "./opt", "opt/.", "opt space"):
            with self.subTest(value=value), self.assertRaises(engine.Refusal):
                engine.safe_relative(value)

    def test_home_is_never_a_root_entry(self):
        for value in ("/home", "/home/alice", "/home/alice/.cache"):
            with self.subTest(value=value), self.assertRaises(engine.Refusal):
                engine.validate_config([value], [])

    def test_protected_paths_and_snapshots(self):
        for value in ("/boot", "/boot/grub", "/boot/efi", "/dev/shm", "/proc", "/sys",
                      "/run", "/etc", "/usr/local", "/var", "/var/lib", "/.snapshots/1/snapshot",
                      "/opt/.snapshots/foo"):
            with self.subTest(value=value), self.assertRaises(engine.Refusal):
                engine.validate_config([value], [])
        engine.validate_config(["/opt", "/root", "/var/log", "/var/lib/docker"], ["/home/alice/.cache"])

    def test_duplicate_and_overlap_rejected(self):
        for roots in (["/opt", "/opt"], ["/opt", "/opt/data"], ["/opt/data", "/opt"]):
            with self.subTest(roots=roots), self.assertRaises(engine.Refusal):
                engine.validate_config(roots, [])
        with self.assertRaises(engine.Refusal):
            engine.validate_config([], ["/home"])

    def test_command_line_requires_one_explicit_mode(self):
        for args in ([], ["--help"], ["--unknown"], ["--dryrun", "--execute"], ["--execute", "extra"]):
            completed = subprocess.run(["bash", str(SCRIPT), *args], text=True, capture_output=True)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Usage:", completed.stderr)


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.migration = engine.Migration(False, "/not-used")
        self.root = engine.Mount("/", "/dev/test", "btrfs", "/@rootfs", "abcd-1234", "rw,noatime,subvolid=256,subvol=/@rootfs")
        self.migration.mounts = [self.root]

    def test_snapshot_and_normal_root_selection(self):
        self.assertEqual(engine.snapshot_base("/@rootfs"), "/@rootfs")
        self.assertEqual(engine.snapshot_base("/@rootfs/.snapshots/22/snapshot"), "/@rootfs")
        self.assertEqual(engine.snapshot_base("/.snapshots/22/snapshot"), "/")
        with self.assertRaises(engine.Refusal):
            engine.snapshot_base("/@rootfs/.snapshots/unknown")

    def test_mount_options_preserve_security_without_source_selector(self):
        self.assertEqual(engine.options_for("rw,nosuid,nodev,noexec,compress=zstd:3,subvolid=256,subvol=/@rootfs,seclabel"),
                         "defaults,nosuid,nodev,noexec,compress=zstd:3,noatime,autodefrag,discard=async")
        with self.assertRaises(engine.Refusal):
            engine.options_for("ro,noatime")

    def test_enclosing_mount_and_stacked_mount_detection(self):
        var = engine.Mount("/var", "/dev/other", "btrfs", "/data", "1234-abcd", "rw,nodev")
        self.migration.mounts.append(var)
        self.assertIs(self.migration.covering("/var/cache"), var)
        self.assertIs(self.migration.covering("/variant"), self.root)
        self.migration.mounts.append(var)
        with self.assertRaises(engine.Refusal):
            self.migration.covering("/var/cache")

    def test_non_btrfs_target_is_rejected(self):
        with self.assertRaises(engine.Refusal):
            self.migration.validate_mount(engine.Mount("/home", "/dev/ext", "ext4", "/", "1234", "rw"))

    def plan_existing(self, *, fsroot="/@rootfs", separate_destination=False):
        migration = self.migration
        self.root.fsroot = fsroot
        migration.check_ancestors = Mock()
        migration.nearest = Mock(side_effect=lambda p: p)
        migration.exists = Mock(side_effect=lambda p: str(p) in {"/opt", "/top/@rootfs/opt"})
        migration.top = Mock(return_value="/top")
        migration.is_subvolume = Mock(side_effect=lambda p: str(p) == "/top/@rootfs")
        migration.no_nested_storage = Mock()
        migration.no_open_users = Mock()
        migration.no_external_hardlinks = Mock()
        migration.metadata = Mock(side_effect=lambda p: (
            "different" if separate_destination and str(p).startswith("/top") else "dev:inode", 0, 0, 0o755))
        with patch.object(engine, "root", return_value=result("4096 /opt\n")):
            return migration.plan_item("/opt")

    def test_non_at_root_destination(self):
        item = self.plan_existing()
        self.assertEqual(item.subvol, "/@rootfs/opt")
        self.assertEqual(item.action, "convert")

    def test_snapshot_does_not_merge_retired_root_content(self):
        item = self.plan_existing(fsroot="/@rootfs/.snapshots/22/snapshot", separate_destination=True)
        self.assertEqual(item.action, "skip")
        self.assertIn("original root; left unchanged", item.reason)
        self.migration.no_open_users.assert_not_called()

    def test_separate_destination_outside_snapshot_still_rejected(self):
        with self.assertRaisesRegex(engine.Refusal, "already exists separately"):
            self.plan_existing(separate_destination=True)

    def test_snapshot_can_plan_unoccupied_stable_destination(self):
        migration = self.migration
        self.root.fsroot = "/@rootfs/.snapshots/22/snapshot"
        migration.check_ancestors = Mock()
        migration.nearest = Mock(side_effect=lambda p: p)
        migration.exists = Mock(side_effect=lambda p: str(p) == "/opt")
        migration.top = Mock(return_value="/top")
        migration.is_subvolume = Mock(side_effect=lambda p: str(p) == "/top/@rootfs")
        migration.no_nested_storage = Mock()
        migration.no_open_users = Mock()
        migration.no_external_hardlinks = Mock()
        migration.metadata = Mock(return_value=("dev:inode", 0, 0, 0o755))
        with patch.object(engine, "root", return_value=result("4096 /opt\n")):
            self.assertEqual(migration.plan_item("/opt").subvol, "/@rootfs/opt")

    def test_legacy_backup_prevents_even_preserved_target(self):
        self.migration.check_ancestors = Mock()
        self.migration.exists = Mock(return_value=True)
        with self.assertRaisesRegex(engine.Refusal, "previous migration"):
            self.migration.plan_item("/opt")

    def test_nested_mount_is_rejected(self):
        self.migration.mounts.append(engine.Mount("/opt/data", "tmpfs", "tmpfs", "/", "", "rw"))
        with self.assertRaisesRegex(engine.Refusal, "contains mounted path"):
            self.migration.no_nested_storage("/opt")

    def test_nested_subvolume_inventory_filters_by_full_path(self):
        self.migration.exists = Mock(return_value=True)
        self.migration.top = Mock(return_value="/top")
        listing = "ID 260 gen 1 top level 256 path @rootfs/srv/data\n"
        with patch.object(engine, "root", return_value=result(listing)):
            self.migration.no_nested_storage("/opt")
            with self.assertRaisesRegex(engine.Refusal, "contains nested subvolume"):
                self.migration.no_nested_storage("/srv")

    def test_open_processes_block_conversion(self):
        self.migration.exists = Mock(return_value=True)
        with patch.object(engine, "root", return_value=result("p9999999\n")):
            with self.assertRaisesRegex(engine.Refusal, "Reboot and retry"):
                self.migration.no_open_users("/opt")

    def test_unrelated_document_portal_is_exempted_from_lsof_stat_calls(self):
        self.migration.exists = Mock(return_value=True)
        portal = engine.Mount("/run/user/1000/doc", "portal", "fuse.portal", "/", "", "rw")
        self.migration.mounts.append(portal)
        with patch.object(engine, "root", return_value=result("", code=1)) as commands:
            self.migration.no_open_users("/home")
        self.assertEqual(commands.call_args.args[0],
                         ["lsof", "-nP", "-F", "p", "-e", "/run/user/1000/doc", "+D", "/home"])

    def test_related_and_prefix_overlapping_fuse_mounts_are_not_exempted(self):
        self.migration.exists = Mock(return_value=True)
        for path, target in (("/home", "/home/alice/remote"), ("/home/alice", "/home"),
                             ("/home", "/home"), ("/home/cache-copy", "/home/cache"),
                             ("/home/cache", "/home/cache-copy")):
            with self.subTest(path=path, target=target):
                self.migration.mounts = [self.root, engine.Mount(target, "remote", "fuse.sshfs", "/", "", "rw")]
                with patch.object(engine, "root", return_value=result("", code=1)) as commands:
                    self.migration.no_open_users(path)
                self.assertNotIn("-e", commands.call_args.args[0])

    def test_unrelated_fuse_exemption_does_not_hide_busy_target(self):
        self.migration.exists = Mock(return_value=True)
        self.migration.mounts.append(engine.Mount("/run/user/1000/doc", "portal", "fuse.portal", "/", "", "rw"))
        with patch.object(engine, "root", return_value=result("p9999999\n")):
            with self.assertRaisesRegex(engine.Refusal, "used by process IDs"):
                self.migration.no_open_users("/opt")

    def test_other_lsof_warnings_still_stop_migration(self):
        self.migration.exists = Mock(return_value=True)
        self.migration.mounts.append(engine.Mount("/run/user/1000/doc", "portal", "fuse.portal", "/", "", "rw"))
        with patch.object(engine, "root", return_value=result("", code=1, stderr="lsof: cannot inspect /opt/private")):
            with self.assertRaisesRegex(engine.Refusal, "Unable to establish"):
                self.migration.no_open_users("/opt")
        with patch.object(engine, "root", return_value=result("", code=1, stderr="permission denied")):
            with self.assertRaisesRegex(engine.Refusal, "Unable to establish"):
                self.migration.no_open_users("/opt")

    def test_ineligible_home_layouts_skip_without_inspecting_children(self):
        cases = [
            (None, False),  # ordinary /home, including a missing directory
            (None, True),   # an inline subvolume without a separate mount
            (engine.Mount("/home", "/dev/other", "ext4", "/", "1234", "rw"), False),
            (engine.Mount("/home", "/dev/test", "btrfs", "/@rootfs/home", "abcd-1234", "rw"), False),
            (engine.Mount("/home", "/dev/test", "btrfs", "/@rootfs", "abcd-1234", "rw"), True),
        ]
        for mount, subvolume in cases:
            with self.subTest(mount=mount, subvolume=subvolume):
                migration = engine.Migration(False, "/unused")
                migration.mounts = [self.root] + ([mount] if mount else [])
                migration.check_ancestors = Mock()
                migration.is_subvolume = Mock(return_value=subvolume)
                migration.plan_item = Mock(return_value=engine.Item("/opt", "preserve"))
                with contextlib.redirect_stdout(io.StringIO()):
                    migration.build_plan(["/opt"], ["/home/alice/.cache"])
                migration.plan_item.assert_called_once_with("/opt")
                self.assertEqual(migration.items[-1].action, "skip")

    def test_eligible_busy_home_still_rejects_entire_plan(self):
        migration = self.migration
        migration.mounts.append(engine.Mount("/home", "/dev/other", "btrfs", "/users", "1234-abcd", "rw"))
        migration.check_ancestors = Mock()
        migration.is_subvolume = Mock(return_value=True)
        migration.plan_item = Mock(side_effect=engine.Refusal("busy home child"))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(engine.Refusal, "Preflight rejected"):
            migration.build_plan([], ["/home/alice/.cache"])
        self.assertEqual(migration.items[-1].reason, "busy home child")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.migration = engine.Migration(True, "/not-used")
        self.item = engine.Item("/opt", "convert", uuid="abcd", subvol="/@rootfs/opt",
                                backup="/opt.unique.old", existed=True,
                                source_identity="dev:inode", status="verified")
        self.migration.items = [self.item]

    def test_pending_record_blocks_new_run(self):
        self.migration.exists = Mock(return_value=True)
        with patch.object(engine, "root", return_value=result('{"items": []}')):
            with self.assertRaisesRegex(engine.Refusal, "interrupted migration"):
                self.migration.check_pending()

    def prepare_cleanup(self):
        migration = self.migration
        migration.verify_mount = Mock()
        migration.check_ancestors = Mock()
        migration.metadata = Mock(return_value=("dev:inode", 0, 0, 0o755))
        migration.no_nested_storage = Mock()
        migration.no_open_users = Mock()
        migration.compare_copy = Mock()
        migration.record = Mock()
        return migration

    def test_unverified_migration_cannot_delete_backup(self):
        migration = self.prepare_cleanup()
        self.item.status = "mounted"
        with patch.object(engine, "root") as commands:
            with self.assertRaisesRegex(engine.Refusal, "unverified"):
                migration.delete_verified_backups()
            commands.assert_not_called()

    def test_differences_preserve_backup_and_pending_record(self):
        migration = self.prepare_cleanup()
        migration.pending = True
        migration.compare_copy.side_effect = engine.Refusal("copy differs")
        with patch.object(engine, "root") as commands:
            with self.assertRaises(engine.Refusal):
                migration.delete_verified_backups()
            commands.assert_not_called()
        self.assertTrue(migration.pending)

    def test_precommit_activity_on_new_path_still_requires_recovery(self):
        migration = self.prepare_cleanup()
        migration.pending = True
        migration.check_item_users = Mock(side_effect=[None, engine.Refusal("new path is busy")])
        with patch.object(engine, "root") as commands:
            with self.assertRaisesRegex(engine.Refusal, "new path is busy"):
                migration.delete_verified_backups()
        self.assertEqual(migration.check_item_users.call_args_list[-1].args[1], self.item.path)
        self.assertFalse(any(call.args[0][0] == "rm" for call in commands.call_args_list))
        self.assertTrue(migration.pending)

    def test_changed_backup_identity_prevents_removal(self):
        migration = self.prepare_cleanup()
        migration.metadata.return_value = ("other:inode", 0, 0, 0o755)
        with patch.object(engine, "root") as commands:
            with self.assertRaisesRegex(engine.Refusal, "identity changed"):
                migration.delete_verified_backups()
            commands.assert_not_called()

    def test_preserved_items_are_never_cleaned_up(self):
        migration = self.prepare_cleanup()
        migration.items.append(engine.Item("/srv", "preserve", backup="/srv-old", existed=True))
        with patch.object(engine, "root", return_value=result()) as commands:
            migration.delete_verified_backups()
        removed = [call.args[0] for call in commands.call_args_list if call.args[0][0] == "rm"]
        self.assertEqual(removed, [["rm", "-rf", "--one-file-system", "--", "/opt.unique.old"]])

    def prepare_committed_cleanup(self):
        migration = self.prepare_cleanup()
        migration.committed = True
        migration.fstab_commit_state = "committed"
        migration.pending = True
        migration.state_dir = migration.STATE + "/run-test"
        migration.restore_services = Mock()
        return migration

    def test_postcommit_busy_old_backup_is_retained_while_other_cleanup_finishes(self):
        migration = self.prepare_committed_cleanup()
        second = engine.Item("/srv", "convert", backup="/srv.unique.old", existed=True,
                             source_identity="dev:inode", status="verified")
        migration.items.append(second)

        def users(item, path):
            self.assertNotEqual(path, item.path, "post-commit activity on the authoritative path must be ignored")
            if path == second.backup:
                raise engine.Refusal("old backup is busy")

        migration.check_item_users = Mock(side_effect=users)
        with patch.object(engine, "root", return_value=result()) as commands, \
             contextlib.redirect_stderr(io.StringIO()):
            migration.delete_verified_backups()

        removed = [call.args[0][-1] for call in commands.call_args_list if call.args[0][0] == "rm"]
        self.assertEqual(removed, [self.item.backup])
        self.assertEqual(second.status, "complete-backup-retained")
        self.assertEqual(self.item.status, "complete")
        self.assertEqual([entry["backup"] for entry in migration.retained_backups], [second.backup])
        migration.restore_services.assert_called_once_with()
        self.assertFalse(migration.pending)
        migration.verify_mount.assert_not_called()
        migration.compare_copy.assert_not_called()
        self.assertFalse(any(call.args[0] == ["sync", "-f", item.path]
                             for call in commands.call_args_list
                             for item in migration.changes()))

    def test_postcommit_delete_failure_retains_only_that_backup_and_continues(self):
        migration = self.prepare_committed_cleanup()
        second = engine.Item("/srv", "convert", backup="/srv.unique.old", existed=True,
                             source_identity="dev:inode", status="verified")
        migration.items.append(second)

        def dispatch(args, **kwargs):
            if args[0] == "rm" and args[-1] == second.backup:
                raise engine.Refusal("injected deletion failure")
            return result()

        with patch.object(engine, "root", side_effect=dispatch) as commands, \
             contextlib.redirect_stderr(io.StringIO()):
            migration.delete_verified_backups()

        removals = [call.args[0][-1] for call in commands.call_args_list if call.args[0][0] == "rm"]
        self.assertEqual(removals, [second.backup, self.item.backup])
        self.assertEqual(second.status, "complete-backup-retained")
        self.assertEqual(self.item.status, "complete")
        self.assertFalse(migration.pending)

    def test_success_report_names_retained_backups_and_completed_migration(self):
        migration = self.prepare_committed_cleanup()
        migration.retained_backups = [{"path": "/opt", "backup": self.item.backup,
                                       "reason": "old backup is busy"}]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            migration.report_success()
        self.assertIn("SUCCESS: Migration completed", output.getvalue())
        self.assertIn(self.item.backup, output.getvalue())
        self.assertIn("retained for manual review", output.getvalue())

    def test_concurrent_fstab_edit_stops_commit(self):
        self.migration.fstab = "original\n"
        with patch.object(engine, "root", return_value=result("changed\n")) as commands:
            with self.assertRaisesRegex(engine.Refusal, "changed during"):
                self.migration.commit_fstab()
        self.assertEqual(len(commands.call_args_list), 1)

    def test_failed_copy_leaves_backup_and_record(self):
        migration = self.migration
        migration.tops = {"abcd": "/top"}
        migration.refresh_mounts = Mock()
        migration.check_ancestors = Mock()
        migration.nearest = Mock(return_value="/opt")
        migration.covering = Mock(return_value=engine.Mount("/", "", "btrfs", "/@rootfs", "abcd", "rw"))
        migration.no_nested_storage = Mock()
        migration.no_open_users = Mock()
        migration.no_external_hardlinks = Mock()
        migration.metadata = Mock(return_value=("dev:inode", 0, 0, 0o755))
        migration.exists = Mock(return_value=False)
        migration.ensure_parents = Mock()
        migration.verify_mount = Mock()
        migration.record = Mock()
        def dispatch(args, **kwargs):
            if args[0] == "rsync":
                raise engine.Refusal("injected copy failure")
            return result()
        with patch.object(engine, "root", side_effect=dispatch) as commands, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(engine.Refusal, "injected copy failure"):
                migration.migrate(self.item)
        self.assertEqual(self.item.status, "mounted")
        self.assertGreaterEqual(migration.record.call_count, 3)
        self.assertFalse(any(call.args[0][0] == "rm" for call in commands.call_args_list))

    def test_dryrun_never_starts_transaction(self):
        migration = self.migration
        migration.execute = False
        migration.acquire_lock = Mock()
        migration.check_pending = Mock()
        migration.read_fstab = Mock()
        migration.refresh_mounts = Mock()
        migration.covering = Mock(return_value=engine.Mount("/", "", "btrfs", "/@rootfs", "abcd", "rw"))
        migration.build_plan = Mock()
        migration.start_transaction = Mock()
        migration.migrate = Mock()
        migration.commit_fstab = Mock()
        with patch.object(engine.os, "geteuid", return_value=1000), \
             patch.object(engine.os, "chdir"), \
             patch.object(engine, "load_config", side_effect=[["/opt"], []]), \
             patch.object(engine.shutil, "which", return_value="/mock/tool"), \
             patch.object(engine, "command", return_value=result()), \
             contextlib.redirect_stdout(io.StringIO()):
            migration.run()
        migration.start_transaction.assert_not_called()
        migration.migrate.assert_not_called()
        migration.commit_fstab.assert_not_called()


class RealCopyTests(unittest.TestCase):
    """Exercise real rsync/find on disposable, unprivileged temporary data."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "before"
        self.target = Path(self.temp.name) / "after"
        self.source.mkdir()
        self.target.mkdir()
        self.migration = engine.Migration(False, "/not-used")
        self.item = engine.Item(str(self.target), "convert", backup=str(self.source), existed=True)
        self.root_patch = patch.object(engine, "root", side_effect=lambda args, **kw: engine.command(args, **kw))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def copy(self):
        engine.command(["rsync", "-aHAXx", "--numeric-ids", str(self.source) + "/", str(self.target) + "/"])

    def test_copy_and_checksum_detect_same_size_same_time_corruption(self):
        (self.source / "data").write_text("original")
        self.copy()
        self.migration.compare_copy(self.item)
        original = (self.target / "data").stat()
        (self.target / "data").write_text("modified")
        os.utime(self.target / "data", ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaisesRegex(engine.Refusal, "verification found differences"):
            self.migration.compare_copy(self.item)

    def test_hardlinks_inside_tree_preserved_and_external_links_rejected(self):
        (self.source / "one").write_text("data")
        os.link(self.source / "one", self.source / "two")
        self.migration.no_external_hardlinks(str(self.source))
        self.copy()
        self.assertEqual((self.target / "one").stat().st_ino, (self.target / "two").stat().st_ino)
        self.migration.compare_copy(self.item)
        os.link(self.source / "one", Path(self.temp.name) / "external")
        with self.assertRaisesRegex(engine.Refusal, "hard links cross"):
            self.migration.no_external_hardlinks(str(self.source))

    def test_symlink_ancestor_is_rejected(self):
        (self.source / "link").symlink_to(self.target, target_is_directory=True)
        with self.assertRaisesRegex(engine.Refusal, "symbolic links"):
            self.migration.check_ancestors(str(self.source / "link" / "child"))


class VirtualBtrfs:
    """Only mount/subvolume operations are simulated. File copies, checksums,
    metadata, backups, fstab staging and journal files use real temporary data.
    Every absolute command path is confined beneath TemporaryDirectory.
    """
    def __init__(self, directory, fsroot="/@rootfs"):
        self.directory = Path(directory)
        self.mounts = [engine.Mount("/", "/dev/fake", "btrfs", fsroot, "abcd-1234", "rw,noatime")]
        self.subvolumes = {("abcd-1234", fsroot)}
        self.calls = []
        self.fail_copy = None
        self.fail_remove = None
        self.storage("abcd-1234", fsroot).mkdir(parents=True)

    def storage(self, uuid_value, path):
        return self.directory / uuid_value / path.lstrip("/")

    def resolve(self, path):
        path = str(path)
        if path.startswith("/top/"):
            parts = path.split("/", 3)
            return self.storage(parts[2], parts[3] if len(parts) == 4 else "/")
        mounts = [m for m in self.mounts if path == m.target or engine.beneath(path, m.target)]
        mount = max(mounts, key=lambda m: len(m.target))
        relative = path[len(mount.target):].lstrip("/")
        return self.storage(mount.uuid, engine.join_subvol(mount.fsroot, relative))

    def write(self, path, content):
        dest = self.resolve(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)

    def dispatch(self, args, **kwargs):
        args = [str(a) for a in args]
        self.calls.append(args)
        if args[0] == "btrfs":
            if args[1:3] == ["subvolume", "list"]:
                uuid_value = args[-1].split("/")[2]
                return result("".join(f"ID {i+256} gen 1 top level 5 path {p.lstrip('/')}\n"
                                      for i, (u, p) in enumerate(sorted(self.subvolumes))
                                      if u == uuid_value))
            if args[1:3] == ["subvolume", "create"]:
                parts = args[-1].split("/", 3)
                self.resolve(args[-1]).mkdir()
                self.subvolumes.add((parts[2], "/" + parts[3]))
                return result()
            raise AssertionError(args)
        if args[0] == "mount":
            target = args[-1]
            if target.startswith("/top/"):
                return result()
            opts = args[args.index("-o") + 1]
            subvol = next(p.split("=", 1)[1] for p in opts.split(",") if p.startswith("subvol="))
            uuid_value = args[-2].removeprefix("UUID=")
            self.mounts.append(engine.Mount(target, "/dev/fake", "btrfs", subvol, uuid_value, "rw," + opts))
            return result()
        if args[0] in ("umount", "sync", "systemctl", "lsof"):
            return result()
        if args[0] == "findmnt":
            return result()
        if args[0] == "mktemp":
            virtual = args[-1].replace("XXXXXXXX", uuid.uuid4().hex[:8])
            dest = self.resolve(virtual)
            if "-d" in args:
                dest.mkdir()
            else:
                dest.touch()
            return result(virtual + "\n")
        if args[0] == "df":
            return result("Avail\n100000000000\n")
        if args[0] == "chown":
            # The test runs unprivileged. Verify that requested ownership is
            # the invoking test account, never an accidental recursive reset.
            assert "-R" not in args
            assert args[1] == f"{os.getuid()}:{os.getgid()}"
            return result()
        if args[0] == "rsync" and "--dry-run" not in args and self.fail_copy:
            if args[-1].rstrip("/") == self.fail_copy:
                raise engine.Refusal("injected interrupted copy")
        if args[0] == "rm" and args[-1] == self.fail_remove:
            raise engine.Refusal("injected backup deletion failure")
        confined = [str(self.resolve(a)) + ("/" if a.endswith("/") else "")
                    if a.startswith("/") else a for a in args]
        # Do not accidentally run real privilege or mount commands in a test.
        assert args[0] in {"test", "stat", "cat", "tee", "chmod", "install", "mkdir", "rmdir",
                           "cp", "mv", "rm", "find", "du", "rsync"}, args
        return engine.command(confined, **kwargs)


class EndToEndSimulationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = VirtualBtrfs(self.temp.name)
        self.migration = engine.Migration(True, "/not-used")
        self.backend.write("/etc/fstab", "UUID=abcd-1234 / btrfs subvol=/@rootfs,noatime 0 0\n")
        self.backend.write("/opt/data", "application data")
        self.backend.write("/home/alice/keep", "user data")
        self.backend.write("/home/alice/.cache/data", "cache data")
        self.migration.refresh_mounts = lambda: setattr(self.migration, "mounts", list(self.backend.mounts))
        self.migration.refresh_mounts()
        def top(uuid_value):
            path = "/top/" + uuid_value
            self.migration.tops[uuid_value] = path
            return path
        self.migration.top = top
        self.migration.is_subvolume = lambda p: any(
            self.backend.resolve(p) == self.backend.storage(u, s) for u, s in self.backend.subvolumes)
        self.migration.no_open_users = Mock()
        self.root_patch = patch.object(engine, "root", side_effect=self.backend.dispatch)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def plan(self):
        self.migration.read_fstab()
        with contextlib.redirect_stdout(io.StringIO()):
            self.migration.build_plan(["/opt"], ["/home/alice/.cache", "/home/alice/.config/app"])

    def execute(self):
        migration = self.migration
        with contextlib.redirect_stdout(io.StringIO()):
            migration.start_transaction()
            for item in migration.changes():
                migration.migrate(item)
            for item in migration.changes():
                migration.verify_mount(item)
                migration.compare_copy(item)
                engine.root(["sync", "-f", item.path])
            migration.commit_fstab()
            migration.delete_verified_backups()

    def separate_home(self, name="/@home"):
        original = self.backend.resolve("/home")
        destination = self.backend.storage("abcd-1234", name)
        original.rename(destination)
        self.backend.mounts.append(engine.Mount("/home", "/dev/fake", "btrfs", name, "abcd-1234", "rw,noatime"))
        self.backend.subvolumes.add(("abcd-1234", name))
        with self.backend.resolve("/etc/fstab").open("a") as f:
            f.write(f"UUID=abcd-1234 /home btrfs subvol={name},noatime 0 0\n")
        self.migration.refresh_mounts()

    def test_inline_home_is_skipped_while_root_executes(self):
        self.plan()
        self.assertEqual([i.path for i in self.migration.changes()], ["/opt"])
        self.assertEqual(next(i.action for i in self.migration.items if i.path == "/home"), "skip")
        self.migration.no_open_users.assert_called_once_with("/opt")
        self.execute()
        self.assertEqual(self.backend.resolve("/home/alice/keep").read_text(), "user data")
        self.assertEqual(self.backend.resolve("/home/alice/.cache/data").read_text(), "cache data")
        self.assertNotIn(("abcd-1234", "/@home"), self.backend.subvolumes)
        self.assertNotIn("/home", self.backend.resolve("/etc/fstab").read_text())

    def test_home_snapshot_storage_does_not_block_sibling_conversion(self):
        self.separate_home()
        snapshots = "/@home/.snapshots"
        self.backend.storage("abcd-1234", snapshots + "/1/snapshot").mkdir(parents=True)
        self.backend.subvolumes.update({("abcd-1234", snapshots),
                                       ("abcd-1234", snapshots + "/1/snapshot")})
        self.plan()
        self.execute()
        self.assertTrue(self.backend.resolve("/home/.snapshots/1/snapshot").is_dir())
        self.assertIn(("abcd-1234", "/@home/alice/.cache"), self.backend.subvolumes)

    def test_later_home_mount_is_reevaluated(self):
        self.plan()
        self.execute()
        self.separate_home("/@users")
        self.migration.items = []
        self.plan()
        self.assertEqual([i.path for i in self.migration.changes()],
                         ["/home/alice/.cache", "/home/alice/.config/app"])
        self.assertEqual(self.migration.changes()[0].subvol, "/@users/alice/.cache")

    def test_home_only_skip_does_not_start_transaction_in_either_mode(self):
        for execute in (False, True):
            with self.subTest(execute=execute):
                migration = self.migration
                migration.items = []
                migration.execute = execute
                output = io.StringIO()
                actual_command = engine.command
                def dispatch_command(args, **kwargs):
                    return result() if args[0] == "sudo" else actual_command(args, **kwargs)
                with patch.object(engine.os, "geteuid", return_value=1000), \
                     patch.object(engine.os, "chdir"), \
                     patch.dict(os.environ), \
                     patch.object(engine, "load_config", side_effect=[[], ["/home/alice/.cache"]]), \
                     patch.object(engine.shutil, "which", return_value="/stub/tool"), \
                     patch.object(engine, "command", side_effect=dispatch_command), \
                     patch.object(migration, "acquire_lock"), \
                     patch.object(migration, "start_transaction") as start, \
                     contextlib.redirect_stdout(output):
                    migration.run()
                start.assert_not_called()
                self.assertIn("Home processing: skipped", output.getvalue())
                self.assertIn("Rerun after establishing a separate /home", output.getvalue())
                self.assertEqual(migration.changes(), [])

    def test_full_root_home_and_missing_child_migration(self):
        self.separate_home()
        self.plan()
        self.execute()
        self.assertEqual(self.backend.resolve("/opt/data").read_text(), "application data")
        self.assertEqual(self.backend.resolve("/home/alice/keep").read_text(), "user data")
        self.assertEqual(self.backend.resolve("/home/alice/.cache/data").read_text(), "cache data")
        self.assertTrue(self.backend.resolve("/home/alice/.config/app").is_dir())
        self.assertIn(("abcd-1234", "/@home"), self.backend.subvolumes)
        self.assertIn(("abcd-1234", "/@home/alice/.cache"), self.backend.subvolumes)
        self.assertFalse(self.backend.resolve(self.migration.STATE + "/pending.json").exists())
        self.assertTrue(self.backend.resolve(self.migration.state_dir + "/complete.json").exists())
        for item in self.migration.changes():
            self.assertFalse(self.backend.resolve(item.backup).exists())
        fstab = self.backend.resolve("/etc/fstab").read_text()
        self.assertIn("subvol=/@rootfs/opt", fstab)
        self.assertIn("/home btrfs defaults,subvol=/@home,noatime,autodefrag,discard=async,compress=zstd:1 0 0\n", fstab)
        self.assertEqual(len(fstab.splitlines()), 5)

    def test_committed_cleanup_failure_keeps_one_backup_and_clears_pending_state(self):
        self.separate_home()
        self.plan()
        retained = next(item for item in self.migration.changes()
                        if item.path == "/home/alice/.cache")
        removed = next(item for item in self.migration.changes() if item.path == "/opt")
        self.backend.fail_remove = retained.backup
        with contextlib.redirect_stderr(io.StringIO()):
            self.execute()

        self.assertTrue(self.migration.committed)
        self.assertFalse(self.migration.pending)
        self.assertFalse(self.backend.resolve(self.migration.STATE + "/pending.json").exists())
        self.assertTrue(self.backend.resolve(self.migration.state_dir + "/complete.json").exists())
        self.assertTrue(self.backend.resolve(retained.backup).exists())
        self.assertFalse(self.backend.resolve(removed.backup).exists())
        self.assertEqual([entry["backup"] for entry in self.migration.retained_backups],
                         [retained.backup])
        commit = next(index for index, call in enumerate(self.backend.calls)
                      if call[:3] == ["mv", "-T", "--"] and call[-1] == "/etc/fstab")
        for item in self.migration.changes():
            self.assertLess(self.backend.calls.index(["sync", "-f", item.path]), commit)

    def test_interrupted_home_child_retains_all_backups_and_original_fstab(self):
        self.separate_home()
        self.plan()
        original = self.backend.resolve("/etc/fstab").read_text()
        self.backend.fail_copy = "/home/alice/.cache"
        with self.assertRaisesRegex(engine.Refusal, "injected interrupted copy"):
            self.execute()
        self.assertEqual(self.backend.resolve("/etc/fstab").read_text(), original)
        self.assertTrue(self.backend.resolve(self.migration.STATE + "/pending.json").is_file())
        parent = next(i for i in self.migration.items if i.path == "/home")
        self.assertEqual(parent.action, "preserve")
        self.assertEqual(self.backend.resolve("/home/alice/keep").read_text(), "user data")
        child = next(i for i in self.migration.items if i.path == "/home/alice/.cache")
        self.assertEqual(self.backend.resolve(child.backup + "/data").read_text(), "cache data")
        with self.assertRaisesRegex(engine.Refusal, "interrupted migration"):
            self.migration.check_pending()

    def test_completed_rerun_preserves_everything(self):
        self.separate_home()
        self.plan()
        self.execute()
        self.migration.items = []
        self.migration.refresh_mounts()
        self.plan()
        self.assertEqual(self.migration.changes(), [])

    def test_fstab_symlink_is_rejected_before_any_write(self):
        path = self.backend.resolve("/etc/fstab")
        path.rename(path.with_name("real-fstab"))
        path.symlink_to("real-fstab")
        with self.assertRaisesRegex(engine.Refusal, "symlink"):
            self.migration.read_fstab()

    def test_fstab_duplicates_use_fields_and_escaped_targets(self):
        self.backend.write("/etc/fstab", "UUID=abcd /var/log btrfs defaults 0 0\nUUID=abcd /var//\\154og/ btrfs defaults 0 0\n")
        with self.assertRaisesRegex(engine.Refusal, "duplicate"):
            self.migration.read_fstab()

    def test_snapshot_execution_uses_stable_root(self):
        # Simulate a snapshot with /opt content while its stable parent root
        # has no /opt collision. The fstab root remains the active snapshot.
        self.backend = VirtualBtrfs(self.temp.name + "/snapshot", "/@rootfs/.snapshots/4/snapshot")
        self.root_patch.stop()
        self.root_patch = patch.object(engine, "root", side_effect=self.backend.dispatch)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.backend.subvolumes.add(("abcd-1234", "/@rootfs"))
        self.backend.write("/etc/fstab", "UUID=abcd-1234 / btrfs subvol=/@rootfs/.snapshots/4/snapshot 0 0\n")
        self.backend.write("/opt/data", "snapshot data")
        self.migration.refresh_mounts()
        self.migration.read_fstab()
        with contextlib.redirect_stdout(io.StringIO()):
            self.migration.build_plan(["/opt"], [])
        self.execute()
        self.assertEqual(self.backend.resolve("/opt/data").read_text(), "snapshot data")
        self.assertEqual(self.migration.items[0].subvol, "/@rootfs/opt")
        self.assertEqual(self.backend.mounts[0].fsroot, "/@rootfs/.snapshots/4/snapshot")

    def test_separate_home_on_another_device_executes_without_parent_conversion(self):
        self.backend.storage("1234-abcd", "/users/alice/.cache").mkdir(parents=True)
        self.backend.mounts.append(engine.Mount("/home", "/dev/other", "btrfs", "/users", "1234-abcd", "rw,noexec"))
        self.backend.subvolumes.add(("1234-abcd", "/users"))
        self.backend.write("/home/alice/.cache/data", "separate home")
        self.migration.refresh_mounts()
        self.migration.read_fstab()
        with contextlib.redirect_stdout(io.StringIO()):
            self.migration.build_plan([], ["/home/alice/.cache"])
        self.execute()
        self.assertEqual(self.backend.resolve("/home/alice/.cache/data").read_text(), "separate home")
        self.assertEqual(len(self.migration.changes()), 1)
        self.assertEqual(self.migration.changes()[0].uuid, "1234-abcd")
        self.assertEqual(self.migration.changes()[0].subvol, "/users/alice/.cache")


if __name__ == "__main__":
    unittest.main()
