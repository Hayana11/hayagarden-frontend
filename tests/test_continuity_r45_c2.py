"""R4.5-C2 production CLI contract tests.

Future owner-approved scheduler contract, documentation only and not installed:
7,22,37,52 * * * *
/usr/bin/flock -n /run/lock/hayagarden-continuity-producer.lock /usr/bin/python3.11 /opt/frontend/tools/continuity_producer.py >> /var/log/continuity_producer.log 2>&1

The existing legacy rolling summary remains */15 * * * * and is outside C2.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import continuity_producer as runner


REQUIRED_JSON_FIELDS = {
    'status',
    'error_code',
    'context_id',
    'context_epoch',
    'source_member_count',
    'unclaimed_source_count',
    'sealed_candidate_count',
    'queued_generation_job_count',
    'generated_generation_job_id',
    'generated_chunk_id',
    'model_call_count',
}


def _result(
    status: str,
    error_code: str | None,
) -> runner.ContinuityProducerResult:
    return runner.ContinuityProducerResult(
        status=status,
        error_code=error_code,
        window_identity={
            'chat_id': 'default',
            'context_id': 7,
            'context_epoch': 3,
        },
        source_member_count=2,
        unclaimed_source_count=1,
        sealed_candidate_count=1,
        queued_generation_job_count=1,
        generated_generation_job_id='generation-job-1',
        generated_chunk_id='chunk-1',
        model_call_count=0,
    )


class ContinuityProducerCliTests(unittest.TestCase):
    def _run_main(self, result):
        output = io.StringIO()
        with patch.object(
            runner,
            'run_continuity_producer',
            return_value=result,
        ) as producer, contextlib.redirect_stdout(output):
            exit_code = runner.main()
        return exit_code, json.loads(output.getvalue()), producer

    def test_runner_delegates_to_existing_producer_once(self):
        exit_code, payload, producer = self._run_main(_result('idle', None))
        self.assertEqual(exit_code, 0)
        producer.assert_called_once_with(
            source_db_path=runner.PRODUCTION_DB,
            continuity_store_path=runner.PRODUCTION_DB,
        )
        self.assertEqual(payload['status'], 'idle')

    def test_json_contract_and_exit_codes(self):
        cases = (
            ('idle', None, 0),
            ('ready', None, 0),
            ('blocked', 'window_identity_unavailable', 1),
            ('failed', 'producer_error', 1),
        )
        for status, error_code, expected_exit in cases:
            with self.subTest(status=status):
                exit_code, payload, _producer = self._run_main(
                    _result(status, error_code)
                )
                self.assertEqual(exit_code, expected_exit)
                self.assertEqual(payload['status'], status)
                self.assertEqual(payload['error_code'], error_code)
                self.assertTrue(REQUIRED_JSON_FIELDS.issubset(payload))

    def test_db_path_is_root_based_and_cwd_independent(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as other_cwd:
            os.chdir(other_cwd)
            try:
                _exit_code, _payload, producer = self._run_main(
                    _result('idle', None)
                )
            finally:
                os.chdir(original_cwd)
        self.assertEqual(
            runner.REPOSITORY_ROOT,
            Path(runner.__file__).resolve().parents[1],
        )
        self.assertEqual(
            runner.PRODUCTION_DB,
            runner.REPOSITORY_ROOT / 'memories.db',
        )
        producer.assert_called_once_with(
            source_db_path=runner.REPOSITORY_ROOT / 'memories.db',
            continuity_store_path=runner.REPOSITORY_ROOT / 'memories.db',
        )

    def test_import_does_not_execute_producer(self):
        try:
            with patch('continuity.producer.run_continuity_producer') as producer:
                importlib.reload(runner)
                producer.assert_not_called()
        finally:
            importlib.reload(runner)

    def test_runner_has_no_scheduler_or_generation_logic(self):
        source = Path(runner.__file__).read_text(encoding='utf-8').lower()
        for forbidden in (
            'sqlite3',
            'seal_snapshot',
            'generate_continuity_chunk',
            'crontab',
            'systemctl',
            'subprocess',
            'flock',
            'wake',
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    unittest.main()
