"""set-casda-password: hidden input, mode-600 file, no password echo, optional login check."""
import contextlib
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from askap_auto import casda_client, cli


class SetCasdaPasswordTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.secret = self.root / 'private' / 'casda-password'
        self.config = self.root / 'config.py'
        self.write_config(CASDA_USERNAME='someone@example.org')

    def write_config(self, **values):
        values.setdefault('CASDA_PASSWORD_FILE', str(self.secret))
        values.setdefault('CASDA_PASSWORD_ENV', 'CASDA_PASSWORD')
        values.setdefault('CASDA_USERNAME', None)
        self.config.write_text(''.join(f'{k} = {v!r}\n' for k, v in values.items()))

    def invoke(self, *answers, extra=(), single=False, environment=None, login=None):
        out = io.StringIO()
        prompts = iter(answers)
        login = login or (lambda settings: object())
        with patch.dict(os.environ, environment or {}, clear=True), \
                patch('getpass.getpass', side_effect=lambda text='': next(prompts)), \
                patch.object(casda_client, 'build_casda_client', side_effect=login), \
                contextlib.redirect_stdout(out):
            cli.main(['set-casda-password', '--config', str(self.config), *extra], single=single)
        return out.getvalue()

    def test_saves_mode_600_file_readable_by_the_downloader(self):
        output = self.invoke('pa ss:word', 'pa ss:word')
        self.assertEqual(stat.S_IMODE(self.secret.stat().st_mode), 0o600)
        self.assertEqual(self.secret.read_text(), 'pa ss:word\n')
        self.assertEqual(casda_client._read_password(self.secret, 'CASDA_PASSWORD_UNUSED'), 'pa ss:word')
        self.assertIn('CASDA login OK', output)
        self.assertNotIn('pa ss:word', output)

    def test_both_entry_points_offer_the_command(self):
        self.invoke('secret', 'secret', single=True)
        self.assertTrue(self.secret.is_file())

    def test_mismatch_empty_and_whitespace_save_nothing(self):
        for answers, message in ((('one', 'two'), 'differ'), (('',), 'No password'), ((' padded', ' padded'), 'whitespace')):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self.invoke(*answers)
                self.assertFalse(self.secret.exists())

    def test_cancel_is_reported_without_traceback_or_file(self):
        def cancel(text=''):
            raise KeyboardInterrupt()
        with patch('getpass.getpass', side_effect=cancel), self.assertRaisesRegex(ValueError, 'Cancelled'):
            cli.main(['set-casda-password', '--config', str(self.config)])
        self.assertFalse(self.secret.exists())

    def test_replaces_existing_file_and_refuses_symbolic_link(self):
        self.invoke('first', 'first')
        self.invoke('second', 'second')
        self.assertEqual(self.secret.read_text(), 'second\n')
        self.assertEqual(stat.S_IMODE(self.secret.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.secret.parent.iterdir()), ['casda-password'])
        link = self.root / 'link'
        link.symlink_to(self.root / 'elsewhere')
        self.write_config(CASDA_USERNAME='someone@example.org', CASDA_PASSWORD_FILE=str(link))
        with self.assertRaisesRegex(RuntimeError, 'symbolic link'):
            self.invoke('third', 'third')
        self.assertFalse((self.root / 'elsewhere').exists())

    def test_failed_login_keeps_the_file_and_reports_it(self):
        def reject(settings):
            raise casda_client.CasdaCredentialError('CASDA authentication failed')
        output = self.invoke('secret', 'secret', login=reject)
        self.assertTrue(self.secret.is_file())
        self.assertIn('login test failed', output)

    def test_only_the_casda_settings_are_required(self):
        self.config.write_text(f'CASDA_PASSWORD_FILE = {str(self.secret)!r}\n')
        with self.assertRaisesRegex(ValueError, 'Add CASDA_USERNAME, CASDA_PASSWORD_ENV to'):
            self.invoke('secret', 'secret')
        self.assertFalse(self.secret.exists())

    def test_login_check_is_skipped_when_it_cannot_test_the_file(self):
        calls = []
        def record(settings):
            calls.append(settings)
        self.assertIn('not tested', self.invoke('secret', 'secret', environment={'CASDA_PASSWORD': 'x'}, login=record))
        self.invoke('secret', 'secret', extra=['--no-check'], login=record)
        self.write_config()
        self.assertIn('CASDA_USERNAME', self.invoke('secret', 'secret', login=record))
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
