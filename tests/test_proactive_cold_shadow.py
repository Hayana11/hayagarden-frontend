import datetime
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chat import context_window as cw
from chat import daily_context as dc
from chat import proactive_cold_shadow as shadow

MORNING = datetime.datetime(2026, 10, 23, 8, 50)


class ProactiveColdShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'memories.db')
        self.log = str(Path(self.tmp.name) / 'shadow.jsonl')
        dc.ensure_schema(self.db)
        cw.ensure_schema(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO daily_contexts (chat_id, local_day, timezone, boundary_hour, "
            "context_epoch, status, resident_generation, window_mode, is_backfill, "
            "created_at, updated_at) "
            "VALUES ('default', '2026-10-22', 'Asia/Shanghai', 4, 7, ?, 3, 'manual', 0, "
            "'2026-10-22 10:00:00', '2026-10-22 10:00:00')",
            (dc.STATUS_FINALIZED,),
        )
        conn.commit()
        conn.close()
        self._env = mock.patch.dict(os.environ, {'PROACTIVE_COLD_SHADOW_PATH': self.log})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def _record(self, mode='morning', reason='resident_stale:idle', now=MORNING, run='w1'):
        return shadow.record_skip(
            mode=mode, hot_reason=reason, wake_run_id=run, db_path=self.db, now=now,
        )

    def _rows(self):
        with open(self.log, encoding='utf-8') as fh:
            return [json.loads(line) for line in fh]

    def test_cold_morning_would_cold_start_into_the_open_window(self):
        record = self._record()
        self.assertTrue(record['would_cold_start'])
        self.assertEqual(record['blockers'], [])
        self.assertEqual((record['context_epoch'], record['resident_generation']), (7, 3))
        self.assertEqual(record['window_mode'], 'manual')
        self.assertEqual(record['chat_day'], '2026-10-23')
        self.assertTrue(record['shadow_only'])
        self.assertEqual(len(self._rows()), 1)

    def test_daily_cap_counts_per_chat_day(self):
        self.assertTrue(self._record(run='a')['would_cold_start'])
        second = self._record(run='b', now=MORNING + datetime.timedelta(hours=1))
        self.assertFalse(second['would_cold_start'])
        self.assertIn('daily_cap_reached', second['blockers'])
        # 03:00 still belongs to the same chat day; 04:00 starts a new one.
        self.assertIn('daily_cap_reached', self._record(
            run='c', now=datetime.datetime(2026, 10, 24, 3, 0))['blockers'])
        self.assertTrue(self._record(
            run='d', now=datetime.datetime(2026, 10, 24, 4, 0))['would_cold_start'])

    def test_self_trigger_retries_are_consumed_once_and_cap_counts_unique_tasks(self):
        repeated = [
            self._record(mode='self_trigger', run='self-trigger-7')
            for _ in range(3)
        ]
        self.assertEqual(
            [record['would_cold_start'] for record in repeated],
            [True, False, False],
        )
        self.assertFalse(repeated[0]['counterfactual_consumed'])
        self.assertTrue(repeated[1]['counterfactual_consumed'])
        self.assertEqual(repeated[1]['blockers'], ['already_would_cold_started'])
        self.assertEqual(repeated[2]['blockers'], ['already_would_cold_started'])
        self.assertEqual(repeated[2]['cold_starts_today_before'], 1)

        second_task = self._record(
            mode='self_trigger', run='self-trigger-8',
        )
        self.assertTrue(second_task['would_cold_start'])
        self.assertFalse(second_task['counterfactual_consumed'])

        cap_blocked = self._record(
            mode='self_trigger', run='self-trigger-9',
        )
        self.assertFalse(cap_blocked['would_cold_start'])
        self.assertFalse(cap_blocked['counterfactual_consumed'])
        self.assertEqual(cap_blocked['blockers'], ['daily_cap_reached'])
        self.assertEqual(cap_blocked['cold_starts_today_before'], 2)

    def test_chat_day_delegates_to_canonical_helper(self):
        with mock.patch.object(
            __import__('chat.day_handoff', fromlist=['chat_day_for_timestamp']),
            'chat_day_for_timestamp',
            return_value='canonical-day',
        ) as helper:
            self.assertEqual(shadow.chat_day(MORNING), 'canonical-day')
        helper.assert_called_once_with(MORNING)

    def test_structural_reasons_never_cold_start(self):
        record = self._record(reason='context_closed')
        self.assertFalse(record['would_cold_start'])
        self.assertEqual(record['blockers'], ['reason_not_cold_startable'])

    def test_switch_in_progress_blocks(self):
        status = sorted(cw.ACTIVE_INTENT_STATUSES)[0]
        conn = sqlite3.connect(self.db)
        values = {'request_id': 'r1', 'chat_id': 'default', 'status': status}
        for _, name, kind, notnull, default, _ in conn.execute(
            'PRAGMA table_info(context_switch_intents)'
        ):
            if notnull and default is None and name not in values:
                values[name] = 1 if 'INT' in kind.upper() else 'x'
        conn.execute(
            'INSERT INTO context_switch_intents (%s) VALUES (%s)'
            % (', '.join(values), ', '.join('?' for _ in values)),
            tuple(values.values()),
        )
        conn.commit()
        conn.close()
        record = self._record()
        self.assertFalse(record['would_cold_start'])
        self.assertIn('switch_in_progress', record['blockers'])

    def test_hot_only_modes_are_not_observed(self):
        for mode in ('normal', 'nightwatch'):
            self.assertIsNone(self._record(mode=mode))
        self.assertFalse(os.path.exists(self.log))

    def test_database_is_never_written(self):
        before = hashlib.sha256(Path(self.db).read_bytes()).hexdigest()
        self._record()
        self._record(mode='self_trigger')
        after = hashlib.sha256(Path(self.db).read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_unreadable_database_is_recorded_not_raised(self):
        record = shadow.record_skip(
            mode='morning', hot_reason='no_local_binding', wake_run_id='x',
            db_path=str(Path(self.tmp.name) / 'missing.db'), now=MORNING,
        )
        self.assertFalse(record['would_cold_start'])
        self.assertTrue(any(b.startswith('window_read_failed:') for b in record['blockers']))

    def test_gateway_hook_is_fail_open_on_the_shared_unavailable_skip(self):
        src = Path(__file__).resolve().parents[1].joinpath('gateway.py').read_text(encoding='utf-8')
        start = src.index('        except UnifiedNormalWakeSharedUnavailable as exc:\n            # S1 shadow')
        block = src[start:src.index("'reason': 'NORMAL_WAKE_SHARED_UNAVAILABLE_SKIP'", start)]
        self.assertIn('from chat.proactive_cold_shadow import record_skip', block)
        self.assertIn('except Exception:\n                pass', block)


if __name__ == '__main__':
    unittest.main()
