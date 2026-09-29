import datetime as dt
import hashlib
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

TEST_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_ROOT / 'tools'))
sys.path.insert(0, str(TEST_ROOT / 'tests'))
sys.path.insert(0, str(TEST_ROOT))

import dayblock_shadow as shadow
import dayblock_scheduled_runner as runner
from test_dayblock_shadow import authority, make_source_db

TZ = ZoneInfo('Asia/Shanghai')


class ScheduledDayBlockRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dayblock-r2-')
        self.root = Path(self.temp.name)
        self.source_db = self.root / 'source.db'
        self.preview_db = self.root / 'preview.db'
        make_source_db(self.source_db)
        self.started_at = dt.datetime(2026, 9, 27, 3, 0, 5, tzinfo=TZ)
        self.auth_calls = 0
        self.persona_calls = 0

    def tearDown(self):
        self.temp.cleanup()

    def capture_authority(self):
        self.auth_calls += 1
        return authority(model='explicit:claude-opus-5-5')

    def capture_persona(self):
        self.persona_calls += 1
        text = 'Persona used only for deterministic fixture generation.'
        return {
            'revision': 'runtime_persona_sha256:' + hashlib.sha256(text.encode()).hexdigest(),
            'token_estimate': shadow._token_estimate(text), 'source_mtime_ns': 1, 'text': text,
        }

    @staticmethod
    def success_result(request, frozen_authority):
        return types.SimpleNamespace(
            text='今天我完成了两项工作，并记录了下一步。',
            provider=frozen_authority.provider,
            model_identity=frozen_authority.model_identity,
            actual_executor='fake_claude_code_background_oneshot',
            usage={'input_tokens': 42, 'output_tokens': 15},
        )

    def run_once(self, generate=None):
        return runner.run_scheduled_dayblock(
            source_db=self.source_db, preview_db=self.preview_db,
            started_at=self.started_at, authority_capture=self.capture_authority,
            persona_capture=self.capture_persona,
            generate_fn=generate or self.success_result,
        )

    def receipt(self):
        conn = sqlite3.connect(self.preview_db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute('SELECT * FROM dayblock_generation_receipts').fetchall()
        finally:
            conn.close()

    def job_rows(self, source_day='2026-09-26'):
        conn = sqlite3.connect(self.preview_db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute('SELECT * FROM dayblock_shadow_jobs WHERE source_day=?', (source_day,)).fetchall()
        finally:
            conn.close()

    def test_utc_19_maps_to_shanghai_03_and_prior_natural_day(self):
        utc = dt.datetime(2026, 9, 26, 19, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(utc.astimezone(TZ).strftime('%H:%M'), '03:00')
        self.assertEqual(runner.SHANGHAI.key, 'Asia/Shanghai')
        self.assertEqual((utc.astimezone(TZ).date() - dt.timedelta(days=1)).isoformat(), '2026-09-26')
        self.assertEqual(self.auth_calls, 0)
        self.assertEqual(self.persona_calls, 0)

    def test_success_calls_fake_once_and_persists_one_immutable_preview_result(self):
        calls = []
        result = self.run_once(generate=lambda req, auth: calls.append(auth) or self.success_result(req, auth))
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['model_call_count'], 1)
        self.assertEqual(result['source_start'], '2026-09-26T00:00:00+08:00')
        self.assertEqual(result['source_end'], '2026-09-27T00:00:00+08:00')
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0].provider, calls[0].model_identity),
                         ('claude_code', 'explicit:claude-opus-5-5'))
        self.assertEqual(self.auth_calls, 1)
        self.assertEqual(self.persona_calls, 1)
        rows = self.receipt()
        self.assertEqual(len(rows), 1)
        receipt = rows[0]
        self.assertEqual(receipt['status'], 'ready')
        self.assertEqual(receipt['provider'], 'claude_code')
        self.assertEqual(receipt['model_identity'], 'explicit:claude-opus-5-5')
        self.assertEqual(receipt['actual_executor'], 'fake_claude_code_background_oneshot')
        self.assertEqual(receipt['summary_body'], result['summary_body'])
        self.assertEqual(hashlib.sha256(receipt['summary_body'].encode()).hexdigest(), receipt['body_hash'])
        self.assertEqual(receipt['source_coverage'], 1.0)
        self.assertEqual(self.job_rows()[0]['status'], 'ready')
        conn = sqlite3.connect(self.preview_db)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE dayblock_generation_receipts SET summary_body='rewritten' WHERE status='ready'")
        conn.rollback()
        conn.close()

    def test_real_generation_request_contains_frozen_source_day_and_prompt_contract(self):
        # Move only this temporary fixture's evidence to the requested prior day.
        conn = sqlite3.connect(self.source_db)
        conn.execute("UPDATE chat_messages SET created_at=replace(created_at, '2026-09-25', '2026-09-27')")
        conn.execute("UPDATE wake_log SET woke_at=replace(woke_at, '2026-09-25', '2026-09-27')")
        conn.commit()
        conn.close()
        requests = []

        def capture(request, frozen_authority):
            requests.append((request, frozen_authority))
            return self.success_result(request, frozen_authority)

        result = runner.run_scheduled_dayblock(
            source_db=self.source_db, preview_db=self.preview_db,
            started_at=dt.datetime(2026, 9, 28, 3, 0, 5, tzinfo=TZ),
            authority_capture=self.capture_authority, persona_capture=self.capture_persona,
            generate_fn=capture,
        )
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['source_day'], '2026-09-27')
        self.assertEqual(len(requests), 1)
        system_text = requests[0][0]['system_text']
        self.assertIn('dayblock_prompt_contract_r3', system_text)
        self.assertIn('记录日期：2026-09-27。以下内容只回顾这一个完整自然日。', system_text)
        self.assertNotIn('记录日期：2026-09-28', system_text)
        self.assertIn('用第一人称写一篇简短日记，800字左右。', system_text)
        self.assertIn('First-person recap grounded only in exact natural-day source evidence.', system_text)
        self.assertIn('open loops', system_text)
        self.assertIn('Treat assistant descriptions of attachments as dialogue evidence', system_text)
        self.assertIn('Represent an unmatched user message as an incomplete turn', system_text)

    def test_ready_replay_and_processing_duplicate_make_zero_second_calls(self):
        calls = []
        first = self.run_once(generate=lambda req, auth: calls.append(1) or self.success_result(req, auth))
        replay = self.run_once(generate=lambda *_: self.fail('ready replay must not generate'))
        self.assertEqual(first['status'], 'ready')
        self.assertEqual(replay['status'], 'ready')
        self.assertTrue(replay['idempotent_replay'])
        self.assertEqual(replay['model_call_count'], 0)
        self.assertEqual(len(calls), 1)

        nested = []
        fresh = self.root / 'processing-same.db'
        def recurse_same_store(req, auth):
            nested.append(runner.run_scheduled_dayblock(
                source_db=self.source_db, preview_db=fresh, started_at=self.started_at,
                authority_capture=self.capture_authority, persona_capture=self.capture_persona,
                generate_fn=lambda *_: self.fail('processing duplicate must not generate'),
            ))
            return self.success_result(req, auth)
        result = runner.run_scheduled_dayblock(
            source_db=self.source_db, preview_db=fresh, started_at=self.started_at,
            authority_capture=self.capture_authority, persona_capture=self.capture_persona,
            generate_fn=recurse_same_store,
        )
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(nested[0]['status'], 'processing')
        self.assertEqual(nested[0]['model_call_count'], 0)

    def test_provider_failure_is_failed_and_never_retried(self):
        calls = []
        def fail(*_):
            calls.append(1)
            raise RuntimeError('provider_timeout')
        result = self.run_once(generate=fail)
        replay = self.run_once(generate=lambda *_: self.fail('failed job must not retry'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['model_call_count'], 1)
        self.assertEqual(replay['model_call_count'], 0)
        self.assertEqual(replay['status'], 'failed')
        self.assertEqual(len(calls), 1)
        self.assertIsNone(self.receipt()[0]['summary_body'])
        self.assertEqual(self.job_rows()[0]['status'], 'failed')

    def test_wrong_provider_result_fails_without_cross_provider_retry(self):
        calls = []
        def wrong_provider(req, auth):
            calls.append(auth)
            return types.SimpleNamespace(
                text='总结正文。', provider='api_relay', model_identity=auth.model_identity,
                actual_executor='fake_relay', usage={},
            )
        result = self.run_once(generate=wrong_provider)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['model_call_count'], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['error_code'], 'provider_provenance_mismatch')
        self.assertIsNone(self.receipt()[0]['summary_body'])

    def test_empty_output_fails_without_ready_result_and_preserves_observability(self):
        calls = []

        def empty_output(req, auth):
            calls.append(1)
            return types.SimpleNamespace(
                text='', provider=auth.provider, model_identity=auth.model_identity,
                actual_executor='fake_empty_output_executor',
                usage={'input_tokens': 45187, 'output_tokens': 0, 'trace': 'returned-before-validation'},
            )

        result = self.run_once(generate=empty_output)
        replay = self.run_once(generate=lambda *_: self.fail('empty failed job must not retry'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['model_call_count'], 1)
        self.assertEqual(result['actual_executor'], 'fake_empty_output_executor')
        self.assertEqual(result['provider_usage']['output_tokens'], 0)
        self.assertEqual(replay['status'], 'failed')
        self.assertEqual(replay['model_call_count'], 0)
        self.assertEqual(len(calls), 1)
        receipt = self.receipt()[0]
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['actual_executor'], 'fake_empty_output_executor')
        self.assertEqual(json.loads(receipt['usage_json'])['trace'], 'returned-before-validation')
        self.assertIsNone(receipt['summary_body'])
        self.assertIsNone(receipt['body_hash'])

    def test_source_coverage_or_budget_failure_makes_zero_calls(self):
        calls = []
        original = shadow.build_generation_input_plan
        def incomplete(*args, **kwargs):
            plan = original(*args, **kwargs)
            plan['source_coverage'] = 0.5
            plan['uncovered_source_count'] = 1
            plan['blocking_reasons'] = ['dayblock_source_coverage_incomplete']
            return plan
        with patch.object(shadow, 'build_generation_input_plan', side_effect=incomplete):
            result = self.run_once(generate=lambda *args: calls.append(args))
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['model_call_count'], 0)
        self.assertEqual(calls, [])
        self.assertEqual(self.job_rows()[0]['status'], 'blocked')

        preview2 = self.root / 'budget.db'
        with patch.object(shadow, 'frozen_model_context_budget', return_value=None):
            result2 = runner.run_scheduled_dayblock(
                source_db=self.source_db, preview_db=preview2, started_at=self.started_at,
                authority_capture=self.capture_authority, persona_capture=self.capture_persona,
                generate_fn=lambda *args: calls.append(args),
            )
        self.assertEqual(result2['model_call_count'], 0)
        self.assertEqual(calls, [])

    def test_source_change_during_fake_generation_marks_stale_without_body(self):
        def mutate_fixture(req, auth):
            conn = sqlite3.connect(self.source_db)
            conn.execute("UPDATE chat_messages SET content='changed during generation' WHERE id=5")
            conn.commit()
            conn.close()
            return self.success_result(req, auth)
        result = self.run_once(generate=mutate_fixture)
        self.assertEqual(result['status'], 'stale')
        receipt = self.receipt()[0]
        self.assertEqual(receipt['status'], 'stale')
        self.assertIsNone(receipt['summary_body'])
        self.assertEqual(self.job_rows()[0]['status'], 'stale')

    def test_old_blocked_sep25_job_is_not_repaired_by_sep26_run(self):
        old = shadow.build_shadow_plan(
            self.source_db, self.preview_db, '2026-09-25',
            now=dt.datetime(2026, 9, 26, 12, 0, tzinfo=TZ),
            frozen_authority=authority(model='explicit:claude-opus-5-5'),
            frozen_authority_captured_at='2026-09-26T12:00:00+08:00',
            persona_snapshot=self.capture_persona(), persona_captured_at='2026-09-26T12:00:00+08:00',
            pre_captured=True,
        )
        self.assertEqual(old['status'], 'blocked')
        conn = sqlite3.connect(self.preview_db)
        before = tuple(conn.execute(
            "SELECT status,blocking_reasons_json,captured_at FROM dayblock_shadow_jobs WHERE source_day='2026-09-25'"
        ).fetchone())
        conn.close()
        result = self.run_once()
        self.assertEqual(result['source_day'], '2026-09-26')
        conn = sqlite3.connect(self.preview_db)
        after = tuple(conn.execute(
            "SELECT status,blocking_reasons_json,captured_at FROM dayblock_shadow_jobs WHERE source_day='2026-09-25'"
        ).fetchone())
        conn.close()
        self.assertEqual(before, after)

    def test_fixture_source_database_is_not_written(self):
        before = self.source_db.read_bytes()
        self.run_once()
        self.assertEqual(self.source_db.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
