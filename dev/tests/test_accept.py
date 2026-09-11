import contextlib
import io
import subprocess
import unittest
from unittest.mock import Mock, patch
from test_add_subvolumes import engine, SCRIPT

class AcceptTests(unittest.TestCase):
    def test_parser_valid_modes_and_option_order(self):
        launcher = SCRIPT.read_text().split('command -v python3', 1)[0]
        for args, expected in ((['--execute', '--accept'], '--execute 1'),
                               (['--accept', '--dryrun'], '--dryrun 1'),
                               (['--execute'], '--execute 0')):
            result = subprocess.run(['bash', '-c', launcher+'\nprintf "%s %s" "$RUN_MODE" "$AUTO_ACCEPT"', 'script', *args], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, expected)
        for args in (['--accept'], ['--execute', '--accept', '--accept'], ['--dryrun', '--execute']):
            self.assertNotEqual(subprocess.run(['bash', '-c', launcher, 'script', *args], capture_output=True).returncode, 0)

    def test_auto_accept_records_item_consent_without_terminal(self):
        m = engine.Migration(True, '/unused', auto_accept=True)
        m.policies = {'/var/log': 'ask'}
        m.no_open_users = Mock(side_effect=engine.Busy('/var/log', {123}))
        m.record = Mock()
        item = engine.Item('/var/log', 'convert')
        output = io.StringIO()
        with patch('builtins.open') as terminal, contextlib.redirect_stdout(output):
            self.assertTrue(m.prepare_activity(item))
        terminal.assert_not_called()
        self.assertTrue(item.accept_active_risk)
        self.assertIn('Log history', output.getvalue())
        self.assertIn('--accept', output.getvalue())
        m.record.assert_called_once()

    def test_strict_busy_and_inspection_errors_not_overridden(self):
        m = engine.Migration(True, '/unused', auto_accept=True)
        m.no_open_users = Mock(side_effect=engine.Busy('/opt', {123}))
        self.assertFalse(m.prepare_activity(engine.Item('/opt', 'convert')))
        m.no_open_users.side_effect = engine.Refusal('inspection failed')
        with self.assertRaisesRegex(engine.Refusal, 'inspection failed'):
            m.prepare_activity(engine.Item('/var/log', 'convert'))
