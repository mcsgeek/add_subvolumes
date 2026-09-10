"""Preferred mount flags and non-migrating fstab updates."""
import contextlib
import io
import os
import json
from unittest.mock import Mock, patch
import unittest
import test_add_subvolumes as base
from test_add_subvolumes import engine, result


class MergeTests(unittest.TestCase):
    def test_adds_original_preferences(self):
        self.assertEqual(engine.preferred_options("defaults"),
                         "defaults,noatime,autodefrag,discard=async,compress=zstd:1")

    def test_preserves_explicit_atime_and_unrelated_flags(self):
        options = engine.preferred_options("rw,relatime,nodev,nosuid,noexec,space_cache=v2,subvol=/@homefs")
        flags = options.split(",")
        self.assertIn("relatime", flags)
        self.assertNotIn("strictatime", flags)
        for flag in ("nodev", "nosuid", "noexec", "space_cache=v2", "subvol=/@homefs"):
            self.assertIn(flag, flags)
        self.assertEqual(engine.preferred_options(options), options)

    def test_all_explicit_atime_modes_preserved(self):
        for mode in ("atime", "relatime", "strictatime", "noatime", "norelatime"):
            flags = engine.preferred_options("defaults," + mode).split(",")
            self.assertEqual(set(flags) & engine.ATIME_OPTIONS, {mode})

    def test_new_child_uses_fstab_atime_not_implicit_kernel_default(self):
        flags = engine.options_for("rw,relatime,nodev", configured="defaults,subvol=@rootfs").split(",")
        self.assertIn("noatime", flags)
        self.assertNotIn("relatime", flags)
        self.assertIn("nodev", flags)
        flags = engine.options_for("rw,relatime", configured="defaults,strictatime").split(",")
        self.assertIn("strictatime", flags)
        self.assertNotIn("noatime", flags)
        self.assertNotIn("relatime", flags)

    def test_explicit_compression_in_fstab_wins_over_stale_live_mount(self):
        flags = engine.options_for("rw,relatime,compress=zstd:1", configured="defaults,compress=zstd:3").split(",")
        self.assertIn("compress=zstd:3", flags)
        self.assertNotIn("compress=zstd:1", flags)

    def test_explicit_compression_discard_and_defrag_choices_survive(self):
        for compression in ("compress=zstd:3", "compress-force=lzo", "compress=no", "nodatacow", "nodatasum"):
            flags = engine.preferred_options(compression + ",nodiscard,noautodefrag").split(",")
            self.assertIn(compression, flags)
            self.assertNotIn("compress=zstd:1", flags)
            self.assertNotIn("discard=async", flags)
            self.assertNotIn("autodefrag", flags)

    def test_option_patch_changes_only_fourth_field(self):
        m = engine.Migration(False, "/unused")
        m.fstab = ("# comment\nUUID=aaa  /home  btrfs  defaults,subvol=/  0  2 # keep\n"
                   "UUID=bbb /swap btrfs defaults,nodatacow 0 0\nUUID=ccc none swap sw 0 0\n")
        m.option_updates = {"/home": {"before": "defaults,subvol=/", "after": "defaults,subvol=/,noatime"}}
        self.assertEqual(m.updated_fstab(), m.fstab.replace("defaults,subvol=/  ", "defaults,subvol=/,noatime  "))


class OptionIntegrationTests(unittest.TestCase):
    setUp = base.EndToEndSimulationTests.setUp

    def setup_existing(self):
        self.backend.subvolumes.add(("abcd-1234", "/@rootfs/opt"))
        self.backend.mounts.append(engine.Mount("/opt", "/dev/fake", "btrfs", "/@rootfs/opt", "abcd-1234", "rw,relatime"))
        self.backend.mounts.append(engine.Mount("/home", "/dev/home", "btrfs", "/", "1234-abcd", "rw,relatime"))
        self.backend.storage("1234-abcd", "/").mkdir(parents=True)
        self.backend.subvolumes.add(("1234-abcd", "/"))
        self.backend.write("/etc/fstab",
            "# installer layout\nUUID=abcd-1234 / btrfs defaults,subvol=@rootfs 0 0\n"
            "UUID=1234-abcd /home btrfs defaults 0 0\n"
            "UUID=abcd-1234 /opt btrfs subvol=/@rootfs/opt,relatime,nodev 0 0\n"
            "UUID=9999 /boot/efi vfat umask=0077 0 1\nUUID=8888 none swap sw 0 0\n")
        self.migration.refresh_mounts()

    def run_options(self, execute):
        m = self.migration
        m.execute = execute
        m.items = []
        actual_command = engine.command
        def dispatch(args, **kw):
            return result() if args[0] == "sudo" else actual_command(args, **kw)
        with patch.object(engine.os, "geteuid", return_value=1000), \
             patch.object(engine.os, "chdir"), patch.dict(os.environ), \
             patch.object(engine, "load_config", side_effect=[["/opt"], ["/home/alice/.cache"]]), \
             patch.object(engine.shutil, "which", return_value="/stub/tool"), \
             patch.object(engine, "command", side_effect=dispatch), \
             patch.object(m, "acquire_lock"), \
             patch.object(m, "plan_item", side_effect=lambda path: engine.Item(path, "preserve")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            m.run()
        return output.getvalue()

    def test_dryrun_reports_three_option_updates_without_mutation(self):
        self.setup_existing()
        before = self.backend.resolve("/etc/fstab").read_text()
        output = self.run_options(False)
        self.assertEqual(set(self.migration.option_updates), {"/", "/home", "/opt"})
        self.assertIn("Fstab options: 3 mounts", output)
        self.assertIn("/, /home: add", output)
        self.assertEqual(self.backend.resolve("/etc/fstab").read_text(), before)
        self.assertFalse(self.backend.resolve(self.migration.STATE + "/pending.json").exists())

    def test_execute_updates_only_options_and_second_run_does_nothing(self):
        self.setup_existing()
        before_subvols = set(self.backend.subvolumes)
        original_data = self.backend.resolve("/opt/data").read_text()
        output = self.run_options(True)
        self.assertIn("Fstab options updated for 3 mounts", output)
        self.assertIn("REBOOT", output)
        self.assertEqual(self.backend.subvolumes, before_subvols)
        self.assertEqual(self.backend.resolve("/opt/data").read_text(), original_data)
        self.assertFalse(any(c[0] in ("rsync", "mount", "umount") for c in self.backend.calls))
        fstab = self.backend.resolve("/etc/fstab").read_text()
        self.assertIn("subvol=@rootfs", fstab)
        home = next(line for line in fstab.splitlines() if " /home " in line)
        self.assertNotIn("subvol=", home)
        self.assertIn("compress=zstd:1", home)
        self.assertIn("UUID=9999 /boot/efi vfat umask=0077 0 1", fstab)
        self.assertIn("UUID=8888 none swap sw 0 0", fstab)
        self.assertIn("relatime", next(line for line in fstab.splitlines() if " /opt " in line))
        self.assertFalse(self.migration.pending)
        manifest = json.loads(self.backend.resolve(self.migration.state_dir + "/complete.json").read_text())
        self.assertEqual(set(manifest["fstab_option_updates"]), {"/", "/home", "/opt"})
        self.backend.calls.clear()
        output = self.run_options(True)
        self.assertIn("Nothing to change", output)
        self.assertEqual(self.migration.option_updates, {})
        self.assertEqual(self.backend.resolve("/etc/fstab").read_text(), fstab)
        self.assertFalse(any(c[0] in ("tee", "mv", "mount", "rsync") for c in self.backend.calls))

    def test_new_subvolume_options_are_already_canonical_on_rerun(self):
        self.migration.read_fstab()
        with contextlib.redirect_stdout(io.StringIO()): self.migration.build_plan(["/opt"], [])
        base.EndToEndSimulationTests.execute(self)
        self.migration.items = []
        self.migration.refresh_mounts()
        self.migration.read_fstab()
        with contextlib.redirect_stdout(io.StringIO()): self.migration.build_plan(["/opt"], [])
        self.assertEqual(self.migration.changes(), [])
        self.assertEqual(self.migration.option_updates, {})

    def test_option_only_commit_failure_retains_fstab_backup(self):
        self.setup_existing()
        before = self.backend.resolve("/etc/fstab").read_text()
        actual = self.backend.dispatch
        def dispatch(args, **kw):
            if args[0] == "findmnt" and "/etc/.fstab" in str(args[-1]):
                raise engine.Refusal("staged verification failed")
            return actual(args, **kw)
        with patch.object(engine, "root", side_effect=dispatch), self.assertRaises(engine.Refusal):
            self.run_options(True)
        self.assertEqual(self.backend.resolve("/etc/fstab").read_text(), before)
        self.assertEqual(self.backend.resolve(self.migration.state_dir + "/fstab.before").read_text(), before)
        self.assertTrue(self.migration.pending)


if __name__ == "__main__": unittest.main()
