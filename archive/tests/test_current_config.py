"""Editable configuration against real SQLite state, with offline external services."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from askap_auto import cli, controller, query
from askap_auto.common import read_json, write_json
import test_auto as fixtures
from template_settings import write_settings


class CurrentConfigTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Tests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.state = self.fixture.state
        self.values = self.fixture.s
        self.catalogue = self.root / 'targets.csv'
        self.catalogue.write_text('Name,RA,DEC\nStar A,180,-28\nOther,180.01,-28\n')
        self.values['catalogue_path'] = str(self.catalogue)
        self.config = self.root / 'config.py'
        self.save_config()
        self.fetches = []

    def save_config(self, **changes):
        self.values.update(changes)
        write_settings(self.config, self.values)

    def invoke(self, *arguments, single=False, work=None, answer=None):
        """Run a command; answer is the reply to the replacement prompt (None: no prompt expected)."""
        original_one = query.query_one
        original_catalogue = query.query_catalogue
        def fetch(source, settings):
            self.fetches.append(source['name'])
            return fixtures.rows()
        def one(source, settings, directory):
            return original_one(source, settings, directory, fetch=fetch, release_check=lambda _:range(3))
        def catalogue(*args, **kwargs):
            return original_catalogue(*args, **kwargs, fetch=fetch, release_check=lambda _:range(3))
        argv = [arguments[0], '--config', str(self.config), '--work-dir', str(work or self.values['work_root']), *arguments[1:]]
        reply = AssertionError('unexpected replacement prompt') if answer is None else answer
        self.output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch('askap_auto.cli.query_one', side_effect=one), \
                patch('askap_auto.cli.query_catalogue', side_effect=catalogue), \
                patch('builtins.input', side_effect=reply if isinstance(reply, BaseException) else lambda prompt: reply) as self.prompt, \
                contextlib.redirect_stdout(self.output):
            return cli.main(argv, single=single)

    def test_preview_can_be_replaced_and_history_is_preserved(self):
        self.invoke('run', '--begin', '1', '--end', '1')
        old = self.state.active_run()
        self.invoke('run', '--begin', '2', '--end', '2', answer='y')
        self.assertIn('rows (BEGIN-END): 1-1 -> 2-2', self.output.getvalue())
        new = self.state.active_run()
        self.assertNotEqual(old['id'], new['id'])
        self.assertEqual(new['selection']['begin'], 2)
        with self.state.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM runs WHERE id=?', (old['id'],)).fetchone()[0], 'superseded')
        self.assertEqual(read_json(self.state.root / 'runs' / old['id'] / 'execution-plan.json'), old)
        self.assertEqual(len(self.fetches), 2)

    def test_corrected_runtime_and_model_settings_apply_without_new_workspace(self):
        self.invoke('run')
        old = self.state.active_run()
        self.values['model']['robust'] = -0.5
        self.save_config(account='corrected', partition='corrected', python='/shared/fixed/python', casda_username='fixed@example.invalid')
        self.invoke('run', answer='y')
        for line in ("ACCOUNT: None -> 'corrected'", "MODEL['robust']: 0.5 -> -0.5"):
            self.assertIn(line, self.output.getvalue())
        new = self.state.active_run()
        self.assertNotEqual(new['id'], old['id'])
        for key in ('account','partition','python','casda_username','model'):
            self.assertEqual(new['frozen']['settings'][key], self.values[key])
        self.assertEqual(len(self.fetches), 2, 'Changing processing settings must not repeat CASDA queries')

    def test_unchanged_config_resumes_same_attempts(self):
        self.invoke('run')
        old = self.state.active_run()
        self.invoke('run')
        self.assertEqual(self.state.active_run()['id'], old['id'])
        with self.state.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 1)

    def test_batch_filter_change_refreshes_query_and_uses_current_config(self):
        self.invoke('query')
        self.save_config(exposure_min=700)
        self.invoke('run')
        with self.state.connect() as db:
            newest = json.loads(db.execute('SELECT data FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        self.assertEqual(newest['frozen']['settings']['exposure_min'], 700)
        self.assertEqual(newest['tasks'], [])
        self.assertEqual(len(self.fetches), 4)

    def test_single_source_reads_same_config_and_applies_edits(self):
        work = self.root / 'single'
        self.invoke('query', '--name', 'Star A', '--ra', '180', '--dec=-28', single=True, work=work)
        self.invoke('run', single=True, work=work)
        self.save_config(casda_username='fixed@example.invalid', account='new-account', begin=20, end=30)
        self.invoke('run', single=True, work=work, answer='y')
        state = fixtures.State(work)
        new = state.active_run()
        self.assertEqual(new['frozen']['settings']['account'], 'new-account')
        self.assertEqual(new['selection']['begin'], 1)
        self.assertIsNone(new['selection']['end'])
        self.assertEqual(new['summary']['target_attempts'], 2)

    def test_single_filter_change_does_not_restore_old_query_conditions(self):
        work = self.root / 'single'
        self.invoke('query', '--ra', '180', '--dec=-28', single=True, work=work)
        self.save_config(exposure_min=700)
        self.invoke('run', single=True, work=work)
        self.assertEqual(read_json(work / 'query/query-snapshot.json')['conditions']['exposure_min'], 700)
        self.assertEqual(len(self.fetches), 2)

    def test_query_can_replace_an_unsubmitted_preview(self):
        self.invoke('run')
        self.save_config(exposure_min=550)
        self.invoke('query', answer='y')
        self.assertIn('query replaces it', self.output.getvalue())
        self.assertIsNone(self.state.active_run())
        self.assertEqual(len(self.fetches), 4)

    def test_busy_submitted_job_is_not_replaced_or_duplicated(self):
        self.invoke('run')
        old = self.state.active_run()
        job = controller.pack_job(self.state, old)
        _, manifest = controller.write_sbatch(self.state, job)
        job.update(status='submitted', manifest_path=str(manifest), scheduler_id='12345')
        self.state.store_job(job)
        self.save_config(account='corrected')
        with patch('askap_auto.controller.reconcile_job', return_value='waiting'), patch('askap_auto.controller.submit_job') as submit:
            with self.assertRaisesRegex(RuntimeError, '12345.*still queued/running'):
                self.invoke('run', '--submit')
        submit.assert_not_called()
        self.assertEqual(self.state.active_run()['id'], old['id'])

    def test_cancelled_job_can_be_replaced_without_consuming_processing_retries(self):
        self.invoke('run')
        old = self.state.active_run()
        job = controller.pack_job(self.state, old)
        _, manifest = controller.write_sbatch(self.state, job)
        job.update(status='submitted', manifest_path=str(manifest), scheduler_id='12345')
        write_json(manifest.with_suffix('.submission.json'), {'job_id':'12345'})
        self.state.store_job(job)
        self.save_config(account='corrected')
        result = subprocess.CompletedProcess([], 0, stdout='12345|CANCELLED\n', stderr='')
        with patch('askap_auto.controller.subprocess.run', return_value=result):
            # replace_run accepts an explicit runner so its default binding is
            # not confused with a real scheduler during this offline test.
            with patch('askap_auto.cli.replace_run', side_effect=lambda state, run, **kw:controller.replace_run(state, run, runner=lambda *a, **k:result, **kw)):
                self.invoke('run', answer='y')
        new = self.state.active_run()
        self.assertNotEqual(new['id'], old['id'])
        self.assertTrue(all(t['retry_number'] == 0 for task in new['tasks'] for t in task['targets']))

    def finish(self, status, **values):
        run = self.state.active_run()
        for task in run['tasks']:
            for target in task['targets']:
                self.state.update(target['attempt_id'], status=status, **values)
        self.state.store_run(run, 'complete')
        return run

    def test_environment_failures_are_retried_by_the_same_command(self):
        self.invoke('run')
        old = self.finish('env_failed', error_step='archive_download', error='CASDA unavailable')
        self.invoke('run')
        new = self.state.active_run()
        self.assertEqual(new['summary']['target_attempts'], old['summary']['target_attempts'])
        self.assertTrue(all(t['retry_number'] == 0 for task in new['tasks'] for t in task['targets']))

    def test_processing_failures_need_retry_failed_even_after_config_changes(self):
        self.invoke('run')
        old = self.finish('failed', error_step='extract_ds', error='DStools failed')
        self.save_config(account='corrected')
        self.invoke('run')
        self.assertIsNone(self.state.active_run(), 'An ordinary run must not retry processing failures')
        self.invoke('run', '--retry-failed')
        retry = self.state.active_run()
        self.assertEqual(retry['summary']['target_attempts'], old['summary']['target_attempts'])
        self.assertTrue(all(t['retry_number'] == 1 for task in retry['tasks'] for t in task['targets']))

    def test_replacing_an_unsubmitted_retry_does_not_consume_another_retry(self):
        self.invoke('run')
        initial = self.state.active_run()
        for task in initial['tasks']:
            for target in task['targets']:
                self.state.update(target['attempt_id'], status='failed')
        self.state.store_run(initial, 'complete')
        self.invoke('run', '--retry-failed')
        old_retry = self.state.active_run()
        self.save_config(account='corrected')
        self.invoke('run', '--retry-failed', answer='y')
        new = self.state.active_run()
        self.assertNotEqual(new['id'], old_retry['id'])
        self.assertTrue(new['tasks'])
        self.assertTrue(all(t['retry_number'] == 1 for task in new['tasks'] for t in task['targets']))

    def test_completed_products_survive_new_configuration_without_recomputation(self):
        self.invoke('run')
        old = self.state.active_run()
        self.fixture.execute(old)
        finished = {t['attempt_id']:self.state.attempt(t['attempt_id']) for task in old['tasks'] for t in task['targets']}
        contents = {path:Path(path).read_bytes() for attempt in finished.values() for path in attempt['products'].values()}
        self.save_config(account='corrected')
        self.invoke('run')
        self.assertIsNone(self.state.active_run())
        for attempt in finished.values():
            self.assertEqual(self.state.target(attempt['source_id'], attempt['observation_key'])['complete_attempt'], attempt['id'])
        for path, content in contents.items():
            self.assertEqual(Path(path).read_bytes(), content)

    def test_sbatch_rejection_exposes_reason_and_leaves_reconfigurable_job(self):
        self.invoke('run')
        job = controller.pack_job(self.state, self.state.active_run())
        def reject(*args, **kwargs):
            raise subprocess.CalledProcessError(1, args[0], stderr='Invalid account')
        with self.assertRaisesRegex(RuntimeError, 'Slurm rejected.*Invalid account'):
            controller.submit_job(self.state, job, runner=reject)
        self.assertEqual(self.state.jobs(job['run_id'])[-1]['status'], 'prepared')
        self.save_config(account='corrected')
        self.invoke('run', answer='y')
        self.assertEqual(self.state.active_run()['frozen']['settings']['account'], 'corrected')

    def test_command_logs_include_settings_without_reading_password_contents(self):
        password = self.root / 'password'
        password.write_text('never-save-this-secret')
        self.save_config(casda_password_file=str(password))
        self.invoke('run', '--begin', '1', '--end', '1')
        path = next((Path(self.values['work_root']) / 'logs/commands').glob('*.json'))
        log = read_json(path)
        self.assertIn('--begin 1 --end 1', log['command'])
        self.assertEqual(log['settings']['casda_password_file'], str(password))
        self.assertEqual(log['status'], 'finished')
        self.assertEqual(log['run_id'], self.state.active_run()['id'])
        self.assertEqual(log['dstools_config'], str((self.root / 'dstools-config.py').resolve()))
        self.assertEqual(len(log['dstools_config_sha256']), 64)
        self.assertNotIn('never-save-this-secret', path.read_text())

    def test_submission_communication_error_keeps_unknown_job_protected(self):
        self.invoke('run')
        job = controller.pack_job(self.state, self.state.active_run())
        def failed_connection(*args, **kwargs):
            raise subprocess.CalledProcessError(1, args[0], stderr='Socket timed out')
        with self.assertRaisesRegex(RuntimeError, 'could not be confirmed.*resolve-submission'):
            controller.submit_job(self.state, job, runner=failed_connection)
        self.assertEqual(self.state.jobs(job['run_id'])[-1]['status'], 'submitting')

    def test_catalogue_coordinate_correction_preserves_input_history(self):
        self.invoke('run')
        old = self.state.active_run()
        self.catalogue.write_text('Name,RA,DEC\nStar A,180.1,-28\nOther,180.01,-28\n')
        self.invoke('run', answer='y')
        self.assertIn('observations are queried again', self.output.getvalue())
        new = self.state.active_run()
        self.assertNotEqual(old['frozen']['version'], new['frozen']['version'])
        self.assertTrue(list((Path(self.values['work_root']) / 'logs/catalogues').glob('*/input_catalogue.json')))

    def test_retry_options_are_exclusive_and_listed_as_a_mode_change(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke('run', '--retry-failed', '--retry-exhausted')
        self.invoke('run')
        self.finish('exhausted', error_step='extract_ds', error='DStools failed')
        self.invoke('run', '--retry-failed')
        self.assertIsNone(self.state.active_run(), '--retry-failed leaves exhausted targets alone')
        self.invoke('run', '--retry-exhausted')
        retry = self.state.active_run()
        self.assertEqual(retry['mode'], 'retry_exhausted')
        self.invoke('run', '--retry-failed', answer='y')
        self.assertIn('mode: --retry-exhausted -> --retry-failed', self.output.getvalue())

    def test_declining_or_no_answer_keeps_the_unfinished_plan(self):
        self.invoke('run', '--begin', '1', '--end', '1')
        old = self.state.active_run()
        for answer in ('n', '', EOFError()):
            with self.subTest(answer=answer), self.assertRaisesRegex(ValueError, 'Cancelled; the unfinished plan is unchanged'):
                self.invoke('run', '--begin', '2', '--end', '2', answer=answer)
            self.assertEqual(self.state.active_run()['id'], old['id'])
        with self.state.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 1)

    def test_no_prompt_once_the_previous_plan_completed(self):
        self.invoke('run', '--begin', '1', '--end', '1')
        self.fixture.execute(self.state.active_run())
        self.invoke('run', '--begin', '2', '--end', '2')  # answer=None: a prompt would fail the test
        with self.state.connect() as db:
            newest = json.loads(db.execute('SELECT data FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        self.assertEqual(newest['selection']['begin'], 2)
        self.assertNotIn('unfinished plan', self.output.getvalue())

    def test_resume_repeats_the_recorded_options(self):
        self.invoke('run', '--begin', '2', '--end', '2', '--source', 'Other')
        old = self.state.active_run()
        self.assertIn('--source', old['argv'])
        self.assertTrue(all(token.split('=', 1)[1].startswith('/') for token in old['argv'] if token.startswith(('--config=', '--work-dir='))))
        self.invoke('run', '--resume')
        self.assertEqual(self.state.active_run()['id'], old['id'])
        self.save_config(account='corrected')
        self.invoke('run', '--resume', answer='y')
        self.assertIn("ACCOUNT: None -> 'corrected'", self.output.getvalue())
        new = self.state.active_run()
        self.assertNotEqual(new['id'], old['id'])
        self.assertEqual((new['selection']['begin'], new['selection']['names']), (2, ['Other']))
        self.assertEqual(new['argv'], old['argv'])
        with self.assertRaisesRegex(ValueError, 'remove --begin'):
            self.invoke('run', '--resume', '--begin', '1')

    def test_resume_without_an_unfinished_plan_is_explained(self):
        with self.assertRaisesRegex(ValueError, 'No unfinished plan to resume'):
            self.invoke('run', '--resume')
        self.invoke('run')
        self.fixture.execute(self.state.active_run())
        with self.assertRaisesRegex(ValueError, 'No unfinished plan to resume'):
            self.invoke('run', '--resume')

    def test_environment_failures_in_an_unfinished_plan_get_one_new_attempt_per_command(self):
        self.invoke('run')
        run = self.state.active_run()
        first = [t for task in run['tasks'] for t in task['targets']]
        for target in first:
            self.state.update(target['attempt_id'], status='env_failed', error_step='worker', error='NODE_FAIL')
        self.state.update(first[0]['attempt_id'], status='preparing')  # Keeps the plan unfinished.
        self.invoke('run')
        continued = self.state.active_run()
        self.assertEqual(continued['id'], run['id'])
        added = [t for task in continued['tasks'][len(run['tasks']):] for t in task['targets']]
        self.assertEqual(len(added), len(first) - 1)
        self.assertTrue(all(self.state.attempt(t['attempt_id'])['status'] == 'preparing' for t in added))
        self.assertTrue(all(t['retry_number'] == 0 for t in added))
        self.invoke('run')
        self.assertEqual(len(self.state.active_run()['tasks']), len(continued['tasks']), 'No second retry while the first is pending')


if __name__ == '__main__':
    unittest.main()
