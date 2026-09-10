"""Configurable policies, partial completion and cache conversion regressions."""
import contextlib
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
import unittest
import test_add_subvolumes as base
from test_add_subvolumes import engine, result


class PolicyTests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "ACTIVITY_POLICIES.conf"
            if data is not None:
                p.write_text(data if isinstance(data, str) else json.dumps(data))
            return engine.load_policies(p, "alice")

    def test_shipped_defaults_and_expansion(self):
        expected = {"/var/log": "ask", "/home/alice/.cache": "ask", "/var/spool": "manage"}
        defaults = dict(expected, **{"/var/crash": "manage", "/home/alice/.ssh": "runtime"})
        self.assertEqual(self.load(None), defaults)
        self.assertEqual(self.load({"accept_risk": ["var/log", "home/${REAL_USER}/.cache"],
                                    "manage_blockers": ["var/spool"]}), expected)

    def test_empty_lists_make_everything_strict(self):
        self.assertEqual(self.load({"accept_risk": [], "manage_blockers": []}), {})

    def test_exact_paths_not_descendant_or_other_user_matches(self):
        m = engine.Migration(False, "/unused")
        m.policies = self.load(None)
        self.assertEqual(m.policy_for("/home/alice/.cache"), "ask")
        for path in ("/home/bob/.cache", "/home/alice/.cache/app", "/var/log/app", "/var/spool/app"):
            self.assertEqual(m.policy_for(path), "strict")

    def test_conflicts_duplicates_and_unsafe_paths_fail(self):
        for data in (
            {"accept_risk": ["var/log"], "manage_blockers": ["var/log"]},
            {"accept_risk": ["var/log", "var/log"], "manage_blockers": []},
            {"accept_risk": ["home/${REAL_USER}/.cache", "home/alice/.cache"], "manage_blockers": []},
            {"accept_risk": ["var/*"], "manage_blockers": []},
            {"accept_risk": ["home/../etc"], "manage_blockers": []},
            {"accept_risk": ["boot"], "manage_blockers": []},
            {"accept_risk": ["home"], "manage_blockers": []},
            {"accept_risk": ["home/alice/.snapshots"], "manage_blockers": []},
            {"accept_risk": "var/log", "manage_blockers": []},
            {"accept_risk": [True], "manage_blockers": []},
            {"accept_risk": [], "other": []},
            '{"accept_risk": [], "accept_risk": [], "manage_blockers": []}',
        ):
            with self.subTest(data=data), self.assertRaises(engine.Refusal):
                self.load(data)

    def test_new_configured_path_gets_prompt_not_advance_consent(self):
        m = engine.Migration(True, "/unused")
        m.policies = self.load({"accept_risk": ["opt/cache"], "manage_blockers": []})
        m.no_open_users = Mock(side_effect=engine.Busy("/opt/cache", {123}))
        m.accept_data_risk = Mock(return_value=False)
        item = engine.Item("/opt/cache", "convert")
        self.assertFalse(m.prepare_activity(item))
        m.accept_data_risk.assert_called_once_with("/opt/cache")
        self.assertFalse(item.accept_active_risk)

    def test_strict_busy_at_execution_skips_with_reboot_guidance(self):
        m = engine.Migration(True, "/unused")
        m.no_open_users = Mock(side_effect=engine.Busy("/opt", {123}))
        item = engine.Item("/opt", "convert")
        self.assertFalse(m.prepare_activity(item))
        self.assertEqual(item.action, "skip")
        self.assertIn("Reboot and retry", item.reason)

    def test_no_reboot_guidance_for_unknown_inspection_error(self):
        m = engine.Migration(True, "/unused")
        m.no_open_users = Mock(side_effect=engine.Refusal("lsof inspection failed"))
        for path in ("/opt", "/var/log", "/var/spool"):
            with self.assertRaisesRegex(engine.Refusal, "inspection failed"):
                m.prepare_activity(engine.Item(path, "convert"))

    def test_service_restore_deferred_across_managed_paths(self):
        m = engine.Migration(True, "/unused")
        m.policies = {"/srv": "manage", "/var/spool": "manage"}
        m.items = [engine.Item("/srv", "convert", status="mounted"), engine.Item("/var/spool", "skip")]
        m.service_changes = [{"unit": "cron.service", "path": "/var/spool", "restored": False, "was_active": True}]
        with patch.object(engine, "root") as commands:
            m.restore_services(only_path="/var/spool")
            with self.assertRaisesRegex(engine.Refusal, "needs recovery"):
                m.restore_services()
        commands.assert_not_called()

    def test_partial_summary_is_explicit_and_cache_risk_named(self):
        m = engine.Migration(True, "/unused")
        m.policies = engine.default_policies("alice")
        m.items = [engine.Item("/home/alice/.cache", "convert", status="complete", accept_active_risk=True),
                   engine.Item("/opt", "skip", reason="busy")]
        output = io.StringIO()
        with contextlib.redirect_stdout(output): m.report_success()
        self.assertIn("COMPLETED WITH SKIPPED PATHS", output.getvalue())
        self.assertIn("SKIPPED /opt", output.getvalue())
        self.assertIn("accepted active-data risk: /home/alice/.cache", output.getvalue())


class PolicyIntegrationTests(unittest.TestCase):
    setUp = base.EndToEndSimulationTests.setUp
    execute = base.EndToEndSimulationTests.execute
    separate_home = base.EndToEndSimulationTests.separate_home

    def test_busy_strict_path_skipped_other_root_converts(self):
        self.backend.write("/srv/data", "busy data")
        self.migration.no_open_users = Mock(side_effect=lambda p: (_ for _ in ()).throw(engine.Busy(p, {123})) if p == "/srv" else None)
        self.migration.read_fstab()
        with contextlib.redirect_stdout(io.StringIO()): self.migration.build_plan(["/opt", "/srv"], [])
        self.assertEqual(self.migration.items[-1].action, "skip")
        self.execute()
        self.assertEqual(self.backend.resolve("/srv/data").read_text(), "busy data")
        fstab = self.backend.resolve("/etc/fstab").read_text()
        self.assertIn("/opt\t", fstab)
        self.assertNotIn("/srv\t", fstab)

    def cache_plan(self):
        self.separate_home()
        self.migration.policies = engine.default_policies("alice")
        def busy(p):
            if p.startswith("/home/alice/.cache"):
                raise engine.Busy(p, {123})
        self.migration.no_open_users = Mock(side_effect=busy)
        self.migration.read_fstab()
        with contextlib.redirect_stdout(io.StringIO()):
            self.migration.build_plan(["/opt"], ["/home/alice/.cache"])

    def test_accepted_busy_cache_converts_and_records_consent(self):
        self.cache_plan()
        self.migration.accept_data_risk = Mock(return_value=True)
        self.execute()
        self.assertEqual(self.backend.resolve("/home/alice/.cache/data").read_text(), "cache data")
        self.assertFalse(self.migration.pending)
        manifest = json.loads(self.backend.resolve(self.migration.state_dir + "/complete.json").read_text())
        self.assertTrue(next(i["accept_active_risk"] for i in manifest["items"] if i["path"].endswith("/.cache")))
        self.assertEqual(manifest["activity_policies"]["/home/alice/.cache"], "ask")

    def test_declined_cache_keeps_original_and_converts_root(self):
        self.cache_plan()
        self.migration.accept_data_risk = Mock(return_value=False)
        self.execute()
        self.assertEqual(self.backend.resolve("/home/alice/.cache/data").read_text(), "cache data")
        self.assertNotIn(("abcd-1234", "/@home/alice/.cache"), self.backend.subvolumes)
        self.assertFalse(self.migration.pending)

    def test_busy_after_rename_stops_instead_of_skipping(self):
        self.migration.read_fstab()
        with contextlib.redirect_stdout(io.StringIO()): self.migration.build_plan(["/opt"], [])
        def busy(p):
            if ".add-subvolumes-" in p:
                raise engine.Busy(p, {123})
        self.migration.no_open_users = Mock(side_effect=busy)
        with self.assertRaisesRegex(engine.Refusal, "Recovery is required; do not reboot"): self.execute()
        self.assertTrue(self.migration.pending)
        self.assertEqual(self.migration.items[0].action, "convert")
        self.assertEqual(self.backend.resolve(self.migration.items[0].backup + "/data").read_text(), "application data")

    def test_unsafe_storage_still_rejects_even_if_busy(self):
        self.migration.read_fstab()
        self.migration.no_nested_storage = Mock(side_effect=engine.Refusal("nested mount"))
        self.migration.no_open_users = Mock(side_effect=engine.Busy("/opt", {123}))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(engine.Refusal, "Preflight rejected"):
            self.migration.build_plan(["/opt"], [])
        self.migration.no_open_users.assert_not_called()


if __name__ == "__main__": unittest.main()
