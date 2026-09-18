import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import oci_retry_notify as app


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in {
            'ROOT': root, 'STATE_FILE': root / 'retry-state.json',
            'SUCCESS_MARKER': root / 'success.marker', 'STOP_MARKER': root / 'stop.marker',
        }.items():
            self.enterContext(patch.object(app, name, value))
        self.enterContext(patch.dict(os.environ, {
            'OCI_STACK_ID': 'stack', 'SMTP_HOST': 'smtp.example.com', 'SMTP_USER': 'user',
            'SMTP_PASSWORD': 'dummy', 'MAIL_FROM': 'a@example.com', 'MAIL_TO': 'b@example.com',
        }, clear=True))
        self.active = self.enterContext(patch.object(app, 'latest_active_job', return_value=None))
        self.create = self.enterContext(patch.object(app, 'create_apply_job', return_value='job'))
        self.wait = self.enterContext(patch.object(app, 'wait_for_job', return_value='SUCCEEDED'))
        self.mail = self.enterContext(patch.object(app, 'send_success_mail'))
        self.logs = self.enterContext(patch.object(app, 'get_job_log_text', return_value='Out of host capacity'))
        self.cli = self.enterContext(patch.object(app, 'run_oci', side_effect=AssertionError('unexpected CLI')))

    def test_success_is_not_reapplied(self):
        self.assertEqual(app.main(), 0)
        self.assertEqual(app.main(), 0)
        self.create.assert_called_once()
        self.mail.assert_called_once()

    def test_mail_failure_only_retries_mail(self):
        self.mail.side_effect = RuntimeError('SMTP unavailable')
        self.assertEqual(app.main(), 1)
        self.assertTrue(app.SUCCESS_MARKER.exists())
        self.mail.side_effect = None
        self.assertEqual(app.main(), 0)
        self.create.assert_called_once()
        self.assertEqual(self.mail.call_count, 2)

    def test_timeout_resumes_same_job(self):
        self.wait.side_effect = TimeoutError('pending')
        self.assertEqual(app.main(), 1)
        self.wait.side_effect = None
        self.assertEqual(app.main(), 0)
        self.create.assert_called_once()

    def test_capacity_failure_allows_next_scheduled_attempt(self):
        self.wait.return_value = 'FAILED'
        self.assertEqual(app.main(), 2)
        self.assertEqual(app.main(), 2)
        self.assertEqual(self.create.call_count, 2)
        self.mail.assert_not_called()

    def test_other_failure_stops(self):
        self.wait.return_value = 'FAILED'
        self.logs.return_value = 'NotAuthorized'
        self.assertEqual(app.main(), 3)
        self.assertEqual(app.main(), 0)
        self.create.assert_called_once()

    def test_cancel_is_not_capacity_retry(self):
        self.wait.return_value = 'CANCELED'
        self.assertEqual(app.main(), 3)

    def test_log_failure_keeps_job(self):
        self.wait.return_value = 'FAILED'
        self.logs.side_effect = RuntimeError('network')
        self.assertEqual(app.main(), 1)
        self.logs.side_effect = None
        self.assertEqual(app.main(), 2)
        self.create.assert_called_once()

    def test_uncertain_creation_never_blindly_retries(self):
        self.create.side_effect = TimeoutError('request timeout')
        self.assertEqual(app.main(), 1)
        self.cli.side_effect = None
        self.cli.return_value = json.dumps({'data': []})
        self.assertEqual(app.main(), 1)
        self.create.assert_called_once()

    def test_uncertain_creation_recovers_by_unique_name(self):
        app.save_state({'stack_id': 'stack', 'creating': True, 'display_name': 'unique'})
        self.cli.side_effect = None
        self.cli.return_value = json.dumps({'data': [
            {'id': 'recovered', 'display-name': 'unique', 'operation': 'APPLY'}]})
        self.assertEqual(app.main(), 0)
        self.wait.assert_called_once_with('recovered')
        self.create.assert_not_called()

    def test_other_active_job_skips(self):
        self.active.return_value = ('other-job', 'IN_PROGRESS')
        self.assertEqual(app.main(), 0)
        self.create.assert_not_called()

    def test_env_poll_settings_loaded_before_use(self):
        (app.ROOT / '.env').write_text('\ufeffOCI_POLL_SECONDS=7\nOCI_POLL_TIMEOUT_SECONDS=90\n', encoding='utf-8')
        self.assertEqual(app.main(), 0)
        self.assertEqual(app.POLL_SECONDS, 7)
        self.assertEqual(app.POLL_TIMEOUT_SECONDS, 90)

    def test_invalid_interval_does_not_create(self):
        os.environ['OCI_POLL_SECONDS'] = '0'
        self.assertEqual(app.main(), 1)
        self.create.assert_not_called()

    def test_lock_prevents_overlap_and_releases(self):
        with app.single_instance() as acquired:
            self.assertTrue(acquired)
            self.assertEqual(app.main(), 0)
            self.create.assert_not_called()
        self.assertEqual(app.main(), 0)
        self.create.assert_called_once()

    def test_stack_change_and_corrupt_state_fail_closed(self):
        app.save_state({'stack_id': 'different'})
        self.assertEqual(app.main(), 1)
        app.STATE_FILE.write_text('broken', encoding='utf-8')
        self.assertEqual(app.main(), 1)
        self.create.assert_not_called()

    def test_legacy_success_marker(self):
        app.SUCCESS_MARKER.write_text('job_id=old')
        self.assertEqual(app.main(), 0)
        self.create.assert_not_called()
        self.mail.assert_not_called()


if __name__ == '__main__':
    unittest.main()
