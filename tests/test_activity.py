"""Activity policy regressions: no host service or filesystem mutations."""
import contextlib
import io
import json
from unittest.mock import Mock, patch
import unittest
from test_add_subvolumes import engine, result
import test_add_subvolumes as base


def unit(name, active="active", **extra):
    return {"Id": name, "LoadState": "loaded", "ActiveState": active,
            "UnitFileState": "enabled", "ControlGroup": "/system.slice/" + name,
            "CanStop": "yes", "CanStart": "yes", "RefuseManualStop": "no",
            "RefuseManualStart": "no", "TriggeredBy": "", "Triggers": "",
            "ConsistsOf": "", "BoundBy": "", "PropagatesStopTo": "", **extra}


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.m = engine.Migration(True, "/unused")
        self.m.record = Mock()
        self.m.exists = Mock(side_effect=lambda p: p == "/run/systemd/system")
        self.m.no_open_users = Mock()

    def test_declining_busy_logs_skips_without_consent(self):
        item = engine.Item("/var/log", "convert")
        self.m.no_open_users.side_effect = engine.Busy(item.path, {11})
        self.m.accept_data_risk = Mock(return_value=False)
        self.assertFalse(self.m.prepare_activity(item))
        self.assertEqual(item.action, "skip")
        self.assertFalse(item.accept_active_risk)

    def test_acceptance_is_exact_path_only_and_stored(self):
        item = engine.Item("/var/log", "convert")
        self.m.no_open_users.side_effect = engine.Busy(item.path, {11})
        self.m.accept_data_risk = Mock(return_value=True)
        self.assertTrue(self.m.prepare_activity(item))
        self.assertTrue(item.accept_active_risk)
        self.m.record.assert_called_once()
        for path in ("/var/spool", "/opt", "/var/log/app"):
            self.assertFalse(self.m.allow_active_data(engine.Item(path, "convert", accept_active_risk=True)))

    def test_idle_logs_need_no_prompt(self):
        self.m.accept_data_risk = Mock()
        self.assertTrue(self.m.prepare_activity(engine.Item("/var/log", "convert")))
        self.m.accept_data_risk.assert_not_called()

    def test_inspection_failure_is_not_treated_as_accepted_activity(self):
        item = engine.Item("/var/log", "convert")
        self.m.no_open_users.side_effect = engine.Refusal("cannot inspect")
        self.m.accept_data_risk = Mock(return_value=True)
        with self.assertRaisesRegex(engine.Refusal, "cannot inspect"):
            self.m.prepare_activity(item)
        self.m.accept_data_risk.assert_not_called()

    def test_missing_tty_defaults_to_skip(self):
        with patch("builtins.open", side_effect=OSError("no tty")), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.m.accept_data_risk("/var/log"))

    def test_terminal_requires_exact_acceptance(self):
        for answer, accepted in (("ACCEPT\n", True), ("\n", False), ("yes\n", False), ("", False)):
            terminal = Mock()
            terminal.__enter__ = Mock(return_value=terminal)
            terminal.__exit__ = Mock(return_value=False)
            terminal.readline.return_value = answer
            with patch("builtins.open", return_value=terminal), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.m.accept_data_risk("/var/log"), accepted)

    def test_discovers_cgroup_owner_and_activator(self):
        self.m.unit_info = Mock(side_effect=lambda name: unit(name,
            **({"TriggeredBy": "cups.socket"} if name == "cups.service" else {"Triggers": "cups.service"})))
        with patch.object(engine.shutil, "which", return_value="/bin/systemctl"), \
             patch.object(engine, "root", return_value=result("0::/system.slice/cups.service\n")):
            self.assertEqual(set(self.m.blocker_units({123})), {"cups.service", "cups.socket"})

    def test_anacron_owner_and_timer_are_discovered(self):
        self.m.unit_info = Mock(side_effect=lambda name: unit(name,
            **({"TriggeredBy": "anacron.timer"} if name == "anacron.service"
               else {"Triggers": "anacron.service"})))
        with patch.object(engine.shutil, "which", return_value="/bin/systemctl"), \
             patch.object(engine, "root", return_value=result("0::/system.slice/anacron.service\n")):
            self.assertEqual(set(self.m.blocker_units({677})),
                             {"anacron.service", "anacron.timer"})

    def test_cgroup_v1_supported(self):
        self.m.unit_info = Mock(return_value=unit("cron.service"))
        with patch.object(engine.shutil, "which", return_value="/bin/systemctl"), \
             patch.object(engine, "root", return_value=result("1:name=systemd:/system.slice/cron.service\n")):
            self.assertEqual(set(self.m.blocker_units({123})), {"cron.service"})

    def test_unknown_session_or_ambiguous_owner_untouched(self):
        for cgroup in ("/user.slice/user-1000.slice/session-2.scope",
                       "/system.slice/cron.service/other.service"):
            with patch.object(engine.shutil, "which", return_value="/bin/systemctl"), \
                 patch.object(engine, "root", return_value=result("0::" + cgroup + "\n")) as commands:
                with self.assertRaises(engine.Refusal):
                    self.m.blocker_units({123})
                self.assertEqual(len(commands.call_args_list), 1)

    def test_dynamic_services_including_previous_handlers(self):
        for name in ("custom-spool.service", "cron.service", "crond.service", "anacron.service",
                     "atd.service", "cups.service", "postfix.service", "postfix@-.service", "exim4.service"):
            with self.subTest(name=name):
                self.m.unit_info = Mock(return_value=unit(name))
                with patch.object(engine.shutil, "which", return_value="/bin/systemctl"), \
                     patch.object(engine, "root", return_value=result("0::/system.slice/" + name + "\n")):
                    self.assertEqual(set(self.m.blocker_units({123})), {name})

    def test_critical_names_aliases_and_lifecycle_actions_rejected(self):
        for name, extra in (("systemd-journald.service", {}), ("sshd@client.service", {}),
                            ("custom.service", {"Names": "custom.service display-manager.service"}),
                            ("custom.service", {"FailureAction": "reboot"}),
                            ("custom.service", {"OnSuccess": "unrelated.service"})):
            with self.subTest(name=name, extra=extra), self.assertRaises(engine.Refusal):
                self.m.check_manageable_unit(name, unit(name, **extra))

    def test_required_dependent_outside_group_rejected(self):
        self.m.unit_info = Mock(side_effect=lambda name: unit(name, RequiredBy="ssh.service") if name == "custom.service" else unit(name))
        with patch.object(engine.shutil, "which", return_value="/bin/systemctl"), \
             patch.object(engine, "root", return_value=result("0::/system.slice/custom.service\n")):
            with self.assertRaisesRegex(engine.Refusal, "ssh.service"):
                self.m.blocker_units({123})

    def test_shared_activation_and_stop_propagation_rejected(self):
        for settings in ({"BoundBy": "network.target"}, {"RefuseManualStop": "yes"},
                         {"TriggeredBy": "shared.socket"}, {"UnitFileState": "masked"}):
            self.m.unit_info = Mock(side_effect=lambda name: unit(name, **settings) if name == "cron.service"
                                    else unit(name, Triggers="cron.service important.service"))
            with patch.object(engine.shutil, "which", return_value="/bin/systemctl"), \
                 patch.object(engine, "root", return_value=result("0::/system.slice/cron.service\n")):
                with self.assertRaises(engine.Refusal):
                    self.m.blocker_units({123})

    def test_cups_dependent_and_its_activator_discovered_recursively(self):
        infos = {
            "cups.service": unit("cups.service", RequiredBy="cups-browsed.service",
                                 ConsistsOf="cups.socket cups.path", TriggeredBy="cups.socket cups.path"),
            "cups.socket": unit("cups.socket", Triggers="cups.service"),
            "cups.path": unit("cups.path", Triggers="cups.service"),
            "cups-browsed.service": unit("cups-browsed.service", TriggeredBy="browse.timer",
                                         RequiredBy="cups.service"),
            "browse.timer": unit("browse.timer", "inactive", Triggers="cups-browsed.service"),
        }
        self.m.unit_info = Mock(side_effect=infos.__getitem__)
        with patch.object(engine.shutil, "which", return_value="/bin/systemctl"), \
             patch.object(engine, "root", return_value=result("0::/system.slice/cups.service\n")):
            found = self.m.blocker_units({123})
        self.assertEqual(set(found), set(infos))
        self.assertEqual(self.m.unit_info.call_count, len(infos))
        self.m.blocker_units = Mock(return_value=found)
        self.m.unit_info = Mock(side_effect=lambda name: unit(name, UnitFileState="masked-runtime"))
        self.m.wait_units = Mock()
        item = engine.Item("/var/spool", "convert")
        self.m.items = [item]
        with patch.object(engine, "root", return_value=result()) as commands, contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.m.prepare_blockers(item, {123}))
            self.m.restore_services()
        active = next(call.args[0] for call in commands.call_args_list if call.args[0][:2] == ["systemctl", "start"])
        self.assertIn("cups-browsed.service", active)
        self.assertNotIn("browse.timer", active)
        self.assertTrue(all(entry["restored"] for entry in self.m.service_changes))

    def test_managed_preparation_precedes_all_migrations(self):
        cache = engine.Item("/var/cache", "convert")
        spool = engine.Item("/var/spool", "convert")
        self.m.items = [cache, spool]
        self.m.prepare_activity = Mock(return_value=True)
        self.m.prepare_managed_activity()
        self.m.prepare_activity.assert_called_once_with(spool)
        self.assertEqual(cache.status, "planned")

    def prepare(self):
        self.item = engine.Item("/var/spool", "convert")
        self.m.items = [self.item]
        self.m.blocker_units = Mock(return_value={"cron.service": unit("cron.service"),
                                               "cron.timer": unit("cron.timer", "inactive")})
        self.m.unit_info = Mock(side_effect=lambda name: unit(name, UnitFileState="masked-runtime"))
        self.m.wait_units = Mock()

    def test_records_before_stop_and_restores_only_previously_active_units(self):
        self.prepare()
        calls = []
        self.m.record = Mock(side_effect=lambda: calls.append("record"))
        def run(args, **kw):
            calls.append(args)
            return result()
        with patch.object(engine, "root", side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.m.prepare_blockers(self.item, {123}))
            self.assertEqual(calls[0], "record")
            self.m.restore_services()
        starts = [a for a in calls if isinstance(a, list) and a[:2] == ["systemctl", "start"]]
        self.assertEqual(starts, [["systemctl", "start", "--no-block", "--", "cron.service"]])
        self.assertTrue(all(e["restored"] for e in self.m.service_changes))

    def test_still_busy_restores_before_skip(self):
        self.prepare()
        self.m.no_open_users.side_effect = engine.Busy("/var/spool", {555})
        with patch.object(engine, "root", return_value=result()) as commands, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.m.prepare_blockers(self.item, {123}))
        self.assertEqual(self.item.action, "skip")
        self.assertTrue(any(c.args[0][1] == "start" for c in commands.call_args_list))

    def test_failed_stop_restores_before_skip(self):
        self.prepare()
        self.m.wait_units.side_effect = [engine.Refusal("stop timed out"), None]
        with patch.object(engine, "root", return_value=result()), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.m.prepare_blockers(self.item, {123}))
        self.assertEqual(self.item.action, "skip")
        self.assertTrue(all(e["restored"] for e in self.m.service_changes))

    def test_partial_spool_never_restarts_services(self):
        self.prepare()
        self.m.service_changes = [{"unit": "cron.service", "path": "/var/spool", "was_active": True, "restored": False}]
        for status in ("starting", "source-renamed", "mounted", "verified", "removing-verified-backup"):
            self.item.status = status
            with patch.object(engine, "root") as commands, self.assertRaisesRegex(engine.Refusal, "needs recovery"):
                self.m.restore_services()
            commands.assert_not_called()

    def test_committed_path_with_retained_backup_restores_services(self):
        self.prepare()
        self.item.status = "complete-backup-retained"
        self.m.service_changes = [{"unit": "cron.service", "path": "/var/spool",
                                   "was_active": True, "restored": False}]
        self.m.unit_info = Mock(return_value=unit("cron.service", UnitFileState="masked-runtime"))
        self.m.wait_units = Mock()
        with patch.object(engine, "root", return_value=result()) as commands:
            self.m.restore_services()
        self.assertTrue(self.m.service_changes[0]["restored"])
        self.assertTrue(any(call.args[0][:2] == ["systemctl", "start"]
                            for call in commands.call_args_list))

    def test_restore_failure_retains_unrestored_ledger(self):
        self.prepare()
        self.m.service_changes = [{"unit": "cron.service", "path": "/var/spool", "was_active": True, "restored": False}]
        with patch.object(engine, "root", side_effect=engine.Refusal("start failed")):
            with self.assertRaises(engine.Refusal):
                self.m.restore_services()
        self.assertFalse(self.m.service_changes[0]["restored"])

    def test_poll_accepts_masked_unit_and_bounds_wait(self):
        with patch.object(engine, "root", return_value=result("Id=cron.service\nLoadState=masked\nActiveState=inactive\n")):
            self.m.wait_units(["cron.service"], active=False)
        self.m.unit_info = Mock(return_value=unit("cron.service", "deactivating"))
        with patch.object(engine.time, "monotonic", side_effect=[0, 16]), self.assertRaises(engine.Refusal):
            self.m.wait_units(["cron.service"], active=False)

    def test_cups_activators_stop_before_service_masking(self):
        self.prepare()
        names = ["cron.service", "cups.service", "cups.socket", "cups.path"]
        self.m.blocker_units.return_value = {name: unit(name) for name in names}
        calls = []
        self.m.wait_units = Mock(side_effect=lambda names, active: calls.append(("wait", list(names), active)))
        def run(args, **kwargs):
            calls.append(args)
            return result()
        with patch.object(engine, "root", side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.m.prepare_blockers(self.item, {123}))
        stop = ["systemctl", "stop", "--no-block", "--", "cups.socket", "cups.path"]
        mask = ["systemctl", "mask", "--runtime", "--no-reload", "--", *names]
        self.assertLess(calls.index(stop), calls.index(mask))
        self.assertLess(calls.index(("wait", ["cups.socket", "cups.path"], False)), calls.index(mask))
        self.assertLess(calls.index(mask), calls.index(["systemctl", "daemon-reload"]))
        self.m.no_open_users.assert_called_once_with("/var/spool")

    def test_failure_reports_only_unsettled_units(self):
        self.m.unit_info = Mock(side_effect=lambda name: unit(name, "failed" if name == "cups.path" else "inactive"))
        with self.assertRaises(engine.Refusal) as error:
            self.m.wait_units(["cron.service", "cups.path"], active=False)
        self.assertIn("cups.path (failed)", str(error.exception))
        self.assertNotIn("cron.service", str(error.exception))


class ActivityIntegrationTests(unittest.TestCase):
    setUp = base.EndToEndSimulationTests.setUp
    execute = base.EndToEndSimulationTests.execute

    def plan_special(self):
        self.backend.write("/var/log/Xorg.0.log", "old log")
        self.backend.write("/var/spool/job", "queued work")
        self.migration.read_fstab()
        def busy(path):
            if path in ("/var/log", "/var/spool"):
                raise engine.Busy(path, {123})
        self.migration.no_open_users = Mock(side_effect=busy)
        self.migration.blocker_units = Mock(side_effect=engine.Refusal("unknown service"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.migration.build_plan(["/opt", "/var/log", "/var/spool"], [])

    def test_dry_planning_never_prompts_or_stops(self):
        self.migration.accept_data_risk = Mock()
        self.plan_special()
        self.migration.accept_data_risk.assert_not_called()
        self.assertEqual(len(self.migration.changes()), 3)
        self.assertFalse(any(c[0] == "systemctl" for c in self.backend.calls))
        self.assertTrue(all(i.activity for i in self.migration.items if i.path != "/opt"))

    def test_accept_log_skip_spool_and_finish_other_paths(self):
        self.plan_special()
        self.migration.accept_data_risk = Mock(return_value=True)
        self.execute()
        self.assertEqual(self.backend.resolve("/var/log/Xorg.0.log").read_text(), "old log")
        self.assertEqual(self.backend.resolve("/var/spool/job").read_text(), "queued work")
        fstab = self.backend.resolve("/etc/fstab").read_text()
        self.assertIn("/var/log\t", fstab)
        self.assertNotIn("/var/spool\t", fstab)
        self.assertFalse(self.migration.pending)
        manifest = json.loads(self.backend.resolve(self.migration.state_dir + "/complete.json").read_text())
        self.assertTrue(next(i["accept_active_risk"] for i in manifest["items"] if i["path"] == "/var/log"))
        self.assertEqual(next(i["action"] for i in manifest["items"] if i["path"] == "/var/spool"), "skip")

    def test_decline_log_skips_both_and_keeps_data(self):
        self.plan_special()
        self.migration.accept_data_risk = Mock(return_value=False)
        self.execute()
        self.assertEqual([i.path for i in self.migration.changes()], ["/opt"])
        self.assertEqual(self.backend.resolve("/var/log/Xorg.0.log").read_text(), "old log")
        self.assertEqual(self.backend.resolve("/var/spool/job").read_text(), "queued work")

    def setup_managed_spool(self):
        self.plan_special()
        self.migration.accept_data_risk = Mock(return_value=True)
        self.migration.blocker_units = Mock(return_value={"cron.service": unit("cron.service")})
        self.migration.unit_info = Mock(return_value=unit("cron.service", UnitFileState="masked-runtime"))
        self.migration.wait_units = Mock()
        def busy(path):
            if path == "/var/log" or (path == "/var/spool" and not self.migration.service_changes):
                raise engine.Busy(path, {123})
        self.migration.no_open_users = Mock(side_effect=busy)

    def test_managed_spool_completes_and_restores_services(self):
        self.setup_managed_spool()
        self.execute()
        self.assertEqual(self.backend.resolve("/var/spool/job").read_text(), "queued work")
        self.assertTrue(all(e["restored"] for e in self.migration.service_changes))
        calls = [a[1] for a in self.backend.calls if a[0] == "systemctl"]
        self.assertIn("mask", calls)
        self.assertIn("stop", calls)
        self.assertIn("unmask", calls)
        self.assertIn("start", calls)
        self.assertFalse(self.migration.pending)

    def test_failed_spool_copy_keeps_services_stopped_and_recovery_record(self):
        self.setup_managed_spool()
        self.backend.fail_copy = "/var/spool"
        with self.assertRaisesRegex(engine.Refusal, "interrupted copy"):
            self.execute()
        with self.assertRaisesRegex(engine.Refusal, "needs recovery"):
            self.migration.restore_services()
        manifest = json.loads(self.backend.resolve(self.migration.STATE + "/pending.json").read_text())
        self.assertEqual(manifest["service_changes"][0]["unit"], "cron.service")
        self.assertFalse(manifest["service_changes"][0]["restored"])
        calls = [a[1] for a in self.backend.calls if a[0] == "systemctl"]
        self.assertNotIn("start", calls)
        item = next(i for i in self.migration.items if i.path == "/var/spool")
        self.assertEqual(self.backend.resolve(item.backup + "/job").read_text(), "queued work")

    def test_log_io_error_stops_and_retains_backup(self):
        self.plan_special()
        self.migration.accept_data_risk = Mock(return_value=True)
        actual = self.backend.dispatch
        def dispatch(args, **kwargs):
            if args[0] == "rsync" and args[-1] == "/var/log/" and "--dry-run" not in args:
                return result(code=23, stderr="disk full")
            return actual(args, **kwargs)
        with patch.object(engine, "root", side_effect=dispatch), self.assertRaisesRegex(engine.Refusal, "copy failed"):
            self.execute()
        self.assertTrue(self.migration.pending)
        item = next(i for i in self.migration.items if i.path == "/var/log")
        self.assertEqual(self.backend.resolve(item.backup + "/Xorg.0.log").read_text(), "old log")
        self.assertNotIn("/var/log\t", self.backend.resolve("/etc/fstab").read_text())

    def test_vanished_live_log_is_accepted_but_other_files_copied(self):
        self.plan_special()
        self.migration.accept_data_risk = Mock(return_value=True)
        actual = self.backend.dispatch
        def dispatch(args, **kwargs):
            reply = actual(args, **kwargs)
            if args[0] == "rsync" and args[-1] == "/var/log/" and "--dry-run" not in args:
                return result(code=24, stderr="vanished log")
            return reply
        with patch.object(engine, "root", side_effect=dispatch):
            self.execute()
        self.assertFalse(self.migration.pending)


if __name__ == "__main__":
    unittest.main()
