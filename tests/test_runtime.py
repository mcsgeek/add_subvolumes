import unittest
from unittest.mock import Mock, patch
from test_add_subvolumes import engine, result

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.m = engine.Migration(False, '/unused')
        self.m.policies = {'/home/alice/.ssh': 'runtime'}
        self.m.exists = Mock(return_value=True)
        self.m.mounts = []

    def check(self, output):
        with patch.object(engine, 'root', return_value=result(output)):
            self.m.no_open_users('/home/alice/.ssh', ignore_runtime=True)

    def test_socket_only_allowed(self):
        self.check('p940\nf3\ntunix\n')

    def test_regular_file_same_process_still_blocks(self):
        with self.assertRaises(engine.Busy):
            self.check('p940\nf3\ntunix\nf4\ntREG\n')

    def test_unknown_descriptor_blocks(self):
        with self.assertRaises(engine.Busy):
            self.check('p940\nf3\n')

    def test_backup_uses_original_policy(self):
        self.m.no_open_users = Mock()
        item = engine.Item('/home/alice/.ssh', 'convert', status='mounted')
        self.m.check_item_users(item, '/home/alice/.ssh.old')
        self.m.no_open_users.assert_called_once_with('/home/alice/.ssh.old', ignore_runtime=True)

    def test_regular_file_after_start_requires_recovery(self):
        self.m.no_open_users = Mock(side_effect=engine.Busy('/backup', {940}))
        with self.assertRaisesRegex(engine.Refusal, 'Recovery is required'):
            self.m.check_item_users(engine.Item('/home/alice/.ssh', 'convert', status='mounted'), '/backup')

    def test_copy_verification_retains_checksums_and_omits_sockets(self):
        item = engine.Item('/home/alice/.ssh', 'convert', existed=True, backup='/backup')
        with patch.object(engine, 'root', return_value=result()) as command:
            self.m.compare_copy(item)
        args=command.call_args.args[0]
        self.assertIn('--checksum', args)
        self.assertIn('--no-specials', args)

    def test_fifo_not_silently_omitted(self):
        with patch.object(engine, 'root', return_value=result('/backup/pipe\n')):
            with self.assertRaisesRegex(engine.Refusal, 'FIFO'):
                self.m.runtime_copy_options(engine.Item('/home/alice/.ssh', 'convert', backup='/backup'))

    def test_real_rsync_socket_omission_and_file_difference(self):
        import tempfile, socket, subprocess
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"source"
            dest = Path(directory)/"dest"
            source.mkdir(); dest.mkdir()
            (source/"key").write_text("original")
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(source/"agent"))
                options = ["--no-specials", "--info=NONREG0"]
                subprocess.run(["rsync", "-aHAX", *options, str(source)+"/", str(dest)+"/"], check=True, capture_output=True)
                args = ["rsync", "-aHAX", "--checksum", "--dry-run", "--itemize-changes", "--delete", *options, str(source)+"/", str(dest)+"/"]
                verified = subprocess.run(args, check=True, capture_output=True, text=True)
                self.assertEqual(verified.stdout, "")
                self.assertFalse((dest/"agent").exists())
                (dest/"key").write_text("modified")
                changed = subprocess.run(args, check=True, capture_output=True, text=True)
                self.assertIn("key", changed.stdout)
