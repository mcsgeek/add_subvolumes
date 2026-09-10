import subprocess
import unittest
from unittest.mock import patch
from test_add_subvolumes import engine, result

class DiscoveryTests(unittest.TestCase):
    def discover(self, backend, output, missing=('lsof',), code=0, stderr=''):
        with patch.object(engine.shutil, 'which', side_effect=lambda name: '/bin/'+name if name == backend else None), patch.object(engine.subprocess, 'run', return_value=result(output, code, stderr)) as run:
            found = engine.discover_tool_packages(missing)
            args = run.call_args.args[0]
            self.assertNotIn('sudo', args)
            self.assertNotIn('-y', args)
            self.assertNotIn('update', args)
            self.assertEqual(run.call_args.kwargs['timeout'], 5)
            return found

    def test_debian_exact_executables_only(self):
        self.assertEqual(self.discover('apt-file', 'lsof: /usr/bin/lsof\nlsof-doc: /usr/share/doc/lsof\n'), {'lsof':'lsof'})

    def test_arch_machine_records_deduplicate_repositories(self):
        record = '\0'.join(['extra', 'lsof', '1.0', 'usr/bin/lsof']) + '\n'
        self.assertEqual(self.discover('pacman', record + record), {'lsof':'lsof'})

    def test_multiple_providers_are_not_guessed(self):
        self.assertEqual(self.discover('apt-file', 'lsof: /usr/bin/lsof\nalternative: /usr/bin/lsof\n'), {})

    def test_unavailable_index_and_malformed_output_fall_back(self):
        for output, code, error in (('', 1, 'No cache'), ('bad output', 0, ''), ('lsof: /usr/bin/lsof\n', 0, 'warning')):
            self.assertEqual(self.discover('apt-file', output, code=code, stderr=error), {})

    def test_timeout_and_missing_backend_fall_back(self):
        with patch.object(engine.shutil, 'which', return_value=None):
            self.assertEqual(engine.discover_tool_packages(['lsof']), {})
        with patch.object(engine.shutil, 'which', return_value='/bin/lookup'), patch.object(engine.subprocess, 'run', side_effect=subprocess.TimeoutExpired('lookup',5)):
            self.assertEqual(engine.discover_tool_packages(['lsof']), {})

    def test_message_deduplicates_packages_and_names_unresolved_tools(self):
        with patch.object(engine, 'discover_tool_packages', return_value={'blkid':'util-linux','findmnt':'util-linux'}):
            message = engine.missing_tools_message(['blkid','findmnt','lsof'])
        self.assertIn('Packages to install: util-linux\n', message)
        self.assertIn('Package lookup unresolved for: lsof', message)
        self.assertIn('packages providing these tools', message)

    def test_complete_discovery_instruction(self):
        with patch.object(engine, 'discover_tool_packages', return_value={'lsof':'lsof'}):
            message = engine.missing_tools_message(['lsof'])
        self.assertIn('Packages to install: lsof', message)
        self.assertIn('Please install these packages', message)
