"""Manual DayBlock retry / reconciliation and the multi-attempt receipt contract."""
import datetime as dt
import fcntl
import hashlib
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

TEST_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_ROOT / 'tools'))
sys.path.insert(0, str(TEST_ROOT / 'tests'))
sys.path.insert(0, str(TEST_ROOT))

import dayblock_shadow as shadow
import dayblock_scheduled_runner as runner
from continuity import dayblock_context
from test_dayblock_shadow import authority, make_source_db

TZ = ZoneInfo('Asia/Shanghai')
DAY = '2026-09-26'

# The receipt table as shipped before attempts: one receipt per day and per job.
LEGACY_RECEIPTS_SQL = """
CREATE TABLE dayblock_generation_receipts(
 generation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, generation_job_id TEXT NOT NULL UNIQUE,
 candidate_id TEXT NOT NULL, source_day TEXT NOT NULL UNIQUE, source_hash TEXT NOT NULL,
 source_revision TEXT NOT NULL, source_fingerprint TEXT NOT NULL,
 provider TEXT NOT NULL, model_identity TEXT NOT NULL, authority_revision TEXT NOT NULL,
 authority_snapshot_id TEXT NOT NULL, persona_revision TEXT NOT NULL,
 prompt_policy_version TEXT NOT NULL, prompt_hash TEXT NOT NULL,
 generator_policy_version TEXT NOT NULL, input_mode TEXT NOT NULL,
 source_member_count INTEGER NOT NULL, source_coverage REAL NOT NULL,
 raw_token_estimate INTEGER NOT NULL, budget_tokens INTEGER NOT NULL,
 input_token_estimate INTEGER NOT NULL, output_token_estimate INTEGER NOT NULL DEFAULT 0,
 actual_executor TEXT, usage_json TEXT NOT NULL DEFAULT '{}', summary_body TEXT, body_hash TEXT,
 status TEXT NOT NULL CHECK(status IN ('processing','ready','failed','stale')),
 error_code TEXT, error_detail TEXT, created_at TEXT NOT NULL, finished_at TEXT,
 FOREIGN KEY(generation_job_id) REFERENCES dayblock_shadow_jobs(job_id));
CREATE TRIGGER dayblock_receipt_provenance_immutable
 BEFORE UPDATE OF generation_id,run_id,generation_job_id,candidate_id,source_day,source_hash,
 source_revision,source_fingerprint,provider,model_identity,authority_revision,
 authority_snapshot_id,persona_revision,prompt_policy_version,prompt_hash,
 generator_policy_version,input_mode,source_member_count,source_coverage,
 raw_token_estimate,budget_tokens,input_token_estimate,created_at
 ON dayblock_generation_receipts
 BEGIN SELECT RAISE(ABORT,'immutable DayBlock generation receipt provenance'); END;
CREATE TRIGGER dayblock_ready_receipt_no_update
 BEFORE UPDATE ON dayblock_generation_receipts WHEN OLD.status='ready'
 BEGIN SELECT RAISE(ABORT,'immutable ready DayBlock generation receipt'); END;
CREATE TRIGGER dayblock_receipt_no_delete
 BEFORE DELETE ON dayblock_generation_receipts
 BEGIN SELECT RAISE(ABORT,'immutable DayBlock generation receipt'); END;
"""
LEGACY_COLUMNS = (
    'generation_id,run_id,generation_job_id,candidate_id,source_day,source_hash,source_revision,'
    'source_fingerprint,provider,model_identity,authority_revision,authority_snapshot_id,'
    'persona_revision,prompt_policy_version,prompt_hash,generator_policy_version,input_mode,'
    'source_member_count,source_coverage,raw_token_estimate,budget_tokens,input_token_estimate,'
    'output_token_estimate,actual_executor,usage_json,summary_body,body_hash,status,error_code,'
    'error_detail,created_at,finished_at'
)


def persona_snapshot(text):
    return {
        'revision': 'runtime_persona_sha256:' + hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'token_estimate': shadow._token_estimate(text), 'source_mtime_ns': 1, 'text': text,
    }


class DayBlockAttemptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dayblock-attempts-')
        self.root = Path(self.temp.name)
        self.source_db = self.root / 'source.db'
        self.preview_db = self.root / 'preview.db'
        self.lock_path = self.root / 'runner.lock'
        make_source_db(self.source_db)
        self.persona_text = 'Persona frozen at 03:00 for this fixture.'
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def scheduled(self, generate, day=DAY):
        started = dt.datetime.combine(
            dt.date.fromisoformat(day) + dt.timedelta(days=1), dt.time(3, 0, 5), tzinfo=TZ,
        )
        return runner.run_scheduled_dayblock(
            source_db=self.source_db, preview_db=self.preview_db, started_at=started,
            authority_capture=lambda: authority(model='explicit:claude-opus-5-5'),
            persona_capture=lambda: persona_snapshot(self.persona_text),
            generate_fn=generate,
        )

    def manual(self, generate=None, *, execute=True, reason='spawn failure on 03:00 run',
               persona_text='Persona edited after the day was frozen.', day=DAY):
        return runner.run_manual_attempt(
            source_day=day, reason=reason, execute=execute,
            source_db=self.source_db, preview_db=self.preview_db,
            started_at=dt.datetime(2026, 10, 27, 12, 0, tzinfo=TZ), lock_path=self.lock_path,
            persona_capture=lambda: persona_snapshot(persona_text),
            generate_fn=generate or self.unexpected_call,
        )

    def unexpected_call(self, *_):
        self.fail('no model call expected')

    def ok(self, request, frozen):
        self.calls.append(request)
        return types.SimpleNamespace(
            text='今天我完成了两项工作，并记录了下一步。', provider=frozen.provider,
            model_identity=frozen.model_identity, actual_executor='fake_executor',
            usage={'input_tokens': 1, 'output_tokens': 1},
        )

    def fail_with(self, message):
        def generate(request, _frozen):
            self.calls.append(request)
            raise RuntimeError(message)
        return generate

    def db(self):
        conn = sqlite3.connect(self.preview_db)
        conn.row_factory = sqlite3.Row
        return conn

    def receipts(self):
        conn = self.db()
        try:
            return conn.execute(
                'SELECT * FROM dayblock_generation_receipts ORDER BY generation_job_id, attempt_no'
            ).fetchall()
        finally:
            conn.close()

    def jobs(self, day=DAY):
        conn = self.db()
        try:
            return conn.execute(
                'SELECT * FROM dayblock_shadow_jobs WHERE source_day=? ORDER BY created_at, job_id', (day,),
            ).fetchall()
        finally:
            conn.close()

    # --- migration ---------------------------------------------------------

    def _downgrade_to_legacy_receipts(self):
        conn = sqlite3.connect(self.preview_db)
        try:
            conn.executescript(
                'DROP INDEX dayblock_receipt_one_live_per_day;'
                'DROP TRIGGER dayblock_receipt_provenance_immutable;'
                'DROP TRIGGER dayblock_ready_receipt_no_update;'
                'DROP TRIGGER dayblock_receipt_no_delete;'
                'ALTER TABLE dayblock_generation_receipts RENAME TO receipts_current;'
                + LEGACY_RECEIPTS_SQL
                + f'INSERT INTO dayblock_generation_receipts({LEGACY_COLUMNS}) '
                  f'SELECT {LEGACY_COLUMNS} FROM receipts_current;'
                'DROP TABLE receipts_current;'
            )
        finally:
            conn.close()

    def test_legacy_single_receipt_store_migrates_losslessly_and_idempotently(self):
        self.assertEqual(self.scheduled(self.ok, day='2026-09-25')['status'], 'ready')
        self.assertEqual(self.scheduled(self.fail_with('cc_background_exit_1:boom'))['status'], 'failed')
        self._downgrade_to_legacy_receipts()
        conn = sqlite3.connect(self.preview_db)
        legacy_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='dayblock_generation_receipts'"
        ).fetchone()[0]
        before = conn.execute(
            f'SELECT {LEGACY_COLUMNS} FROM dayblock_generation_receipts ORDER BY generation_id'
        ).fetchall()
        conn.close()
        self.assertIn('source_day TEXT NOT NULL UNIQUE', legacy_sql)
        self.assertEqual({row[27] for row in before}, {'ready', 'failed'})

        shadow.open_preview_store(self.preview_db).close()
        conn = sqlite3.connect(self.preview_db)
        try:
            table_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='dayblock_generation_receipts'"
            ).fetchone()[0]
            after = conn.execute(
                f'SELECT {LEGACY_COLUMNS} FROM dayblock_generation_receipts ORDER BY generation_id'
            ).fetchall()
            self.assertEqual(before, after)  # every legacy value, byte for byte
            self.assertEqual(
                conn.execute('SELECT attempt_no, attempt_kind, attempt_reason '
                             'FROM dayblock_generation_receipts').fetchall(),
                [(1, 'scheduled', None)] * 2,
            )
            self.assertNotIn('source_day TEXT NOT NULL UNIQUE', table_sql)
            self.assertNotIn('generation_job_id TEXT NOT NULL UNIQUE', table_sql)
            self.assertIn('UNIQUE(generation_job_id, attempt_no)', table_sql)
            objects = {row[0]: row[1] for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE tbl_name='dayblock_generation_receipts'"
            )}
            self.assertIn('dayblock_receipt_one_live_per_day', objects)
            self.assertIn('attempt_reason', objects['dayblock_receipt_provenance_immutable'])
            self.assertIn('dayblock_ready_receipt_no_update', objects)
            self.assertIn('dayblock_receipt_no_delete', objects)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE dayblock_generation_receipts SET finished_at='x' WHERE status='ready'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('DELETE FROM dayblock_generation_receipts')
        finally:
            conn.close()

        shadow.open_preview_store(self.preview_db).close()  # second run is a no-op
        conn = sqlite3.connect(self.preview_db)
        try:
            self.assertEqual(table_sql, conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='dayblock_generation_receipts'"
            ).fetchone()[0])
            self.assertEqual(before, conn.execute(
                f'SELECT {LEGACY_COLUMNS} FROM dayblock_generation_receipts ORDER BY generation_id'
            ).fetchall())
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='dayblock_generation_receipts_r2'"
            ).fetchone())
        finally:
            conn.close()

    # --- retry -------------------------------------------------------------

    def test_failed_job_retries_as_new_attempts_of_the_same_job(self):
        self.assertEqual(self.scheduled(self.fail_with('cc_background_spawn_failed'))['status'], 'failed')
        second = self.manual(self.fail_with('cc_background_exit_1:still broken'))
        third = self.manual(self.ok, reason='transient spawn failure fixed by #613')
        self.assertEqual(second['status'], 'failed')
        self.assertEqual(second['attempt_kind'], 'retry')
        self.assertEqual(third['status'], 'ready')
        receipts = self.receipts()
        self.assertEqual(len({row['generation_job_id'] for row in receipts}), 1)
        self.assertEqual([row['attempt_no'] for row in receipts], [1, 2, 3])
        self.assertEqual([row['attempt_kind'] for row in receipts], ['scheduled', 'retry', 'retry'])
        self.assertEqual([row['status'] for row in receipts], ['failed', 'failed', 'ready'])
        self.assertEqual(receipts[1]['attempt_reason'], 'spawn failure on 03:00 run')
        self.assertEqual(receipts[2]['attempt_reason'], 'transient spawn failure fixed by #613')
        self.assertEqual([job['status'] for job in self.jobs()], ['ready'])
        self.assertEqual(len(self.calls), 3)
        replay = self.scheduled(self.unexpected_call)
        self.assertEqual((replay['status'], replay['attempt_no']), ('ready', 3))

    def test_retry_uses_frozen_persona_text_not_current_persona(self):
        self.scheduled(self.fail_with('cc_background_spawn_failed'))
        result = self.manual(self.ok, persona_text='CURRENT PERSONA MUST NOT BE USED')
        self.assertEqual((result['status'], result['attempt_kind']), ('ready', 'retry'))
        self.assertEqual(len(self.calls), 2)
        system_text = self.calls[-1]['system_text']
        self.assertIn(self.persona_text, system_text)
        self.assertNotIn('CURRENT PERSONA MUST NOT BE USED', system_text)
        self.assertEqual(self.calls[0]['system_text'], system_text)
        job = self.jobs()[0]
        self.assertEqual(job['persona_text'], self.persona_text)
        with self.db() as conn, self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE dayblock_shadow_jobs SET persona_text='edited'")

    def test_historical_job_without_persona_text_fails_closed(self):
        self.scheduled(self.fail_with('cc_background_spawn_failed'))
        conn = self.db()
        try:  # simulate a job frozen before persona text existed
            conn.execute('DROP TRIGGER dayblock_job_authority_immutable')
            conn.execute('UPDATE dayblock_shadow_jobs SET persona_text=NULL')
            conn.commit()
        finally:
            conn.close()
        result = self.manual()
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(result['error_code'], 'historical_frozen_persona_unavailable')
        self.assertEqual(len(self.receipts()), 1)
        self.assertEqual(len(self.calls), 1)  # only the scheduled attempt

    def test_unchanged_source_with_non_failed_job_is_refused(self):
        conn = sqlite3.connect(self.source_db)
        conn.execute("INSERT INTO chat_messages VALUES(8,'fyodor','orphan reply','',"
                     "'2026-09-26 08:00:00','[]','[]',0,'{}','chat','[]','','','')")
        conn.execute('INSERT INTO daily_message_contexts VALUES(8,31,9)')
        conn.commit()
        conn.close()
        self.assertEqual(self.scheduled(self.unexpected_call)['status'], 'blocked')
        result = self.manual()
        self.assertEqual((result['status'], result['error_code']),
                         ('refused', 'source_unchanged_job_blocked'))

    # --- reconciliation ------------------------------------------------------

    def test_revised_source_reconciles_into_a_new_job(self):
        self.scheduled(self.fail_with('cc_background_spawn_failed'))
        old_job = self.jobs()[0]
        conn = sqlite3.connect(self.source_db)
        conn.execute("UPDATE chat_messages SET content='today, corrected projection' WHERE id=5")
        conn.commit()
        conn.close()
        dry = self.manual(execute=False, reason='source projection fixed by #646')
        self.assertEqual((dry['status'], dry['action']), ('dry_run', 'reconciliation'))
        self.assertEqual(len(self.jobs()), 1)
        result = self.manual(self.ok, reason='source projection fixed by #646')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['previous_job_id'], old_job['job_id'])
        jobs = self.jobs()
        self.assertEqual(len(jobs), 2)
        old, new = (jobs[0], jobs[1]) if jobs[0]['job_id'] == old_job['job_id'] else (jobs[1], jobs[0])
        self.assertEqual(old['status'], 'failed')
        self.assertEqual(new['status'], 'ready')
        self.assertNotEqual(new['source_revision'], old['source_revision'])
        for key in ('provider', 'model_identity', 'authority_revision', 'captured_at'):
            self.assertEqual(new[key], old[key])
        self.assertEqual(new['persona_text'], 'Persona edited after the day was frozen.')
        by_job = {row['generation_job_id']: row for row in self.receipts()}
        self.assertEqual(by_job[old['job_id']]['status'], 'failed')
        self.assertEqual(
            (by_job[new['job_id']]['attempt_no'], by_job[new['job_id']]['attempt_kind'],
             by_job[new['job_id']]['attempt_reason']),
            (1, 'reconciliation', 'source projection fixed by #646'),
        )

    # --- one live attempt per day / ready is final ------------------------------

    def test_ready_day_refuses_any_new_attempt(self):
        self.assertEqual(self.scheduled(self.ok)['status'], 'ready')
        result = self.manual()
        self.assertEqual((result['status'], result['error_code']), ('refused', 'ready_day_is_final'))
        ready = self.receipts()[0]
        with self.db() as conn, self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                f'INSERT INTO dayblock_generation_receipts({LEGACY_COLUMNS},attempt_no,attempt_kind,attempt_reason) '
                f"SELECT 'gen-x',run_id,generation_job_id,candidate_id,source_day,source_hash,"
                f'source_revision,source_fingerprint,provider,model_identity,authority_revision,'
                f'authority_snapshot_id,persona_revision,prompt_policy_version,prompt_hash,'
                f'generator_policy_version,input_mode,source_member_count,source_coverage,'
                f'raw_token_estimate,budget_tokens,input_token_estimate,0,NULL,"{{}}",NULL,NULL,'
                f"'processing',NULL,NULL,created_at,NULL,2,'retry','race' "
                f'FROM dayblock_generation_receipts WHERE generation_id=?', (ready['generation_id'],),
            )

    def test_one_processing_attempt_per_day(self):
        self.scheduled(self.fail_with('cc_background_spawn_failed'))
        first = self.receipts()[0]

        def insert_processing(conn, generation_id, attempt_no):
            conn.execute(
                f'INSERT INTO dayblock_generation_receipts({LEGACY_COLUMNS},attempt_no,attempt_kind,attempt_reason) '
                f"SELECT ?,run_id,generation_job_id,candidate_id,source_day,source_hash,"
                f'source_revision,source_fingerprint,provider,model_identity,authority_revision,'
                f'authority_snapshot_id,persona_revision,prompt_policy_version,prompt_hash,'
                f'generator_policy_version,input_mode,source_member_count,source_coverage,'
                f'raw_token_estimate,budget_tokens,input_token_estimate,0,NULL,"{{}}",NULL,NULL,'
                f"'processing',NULL,NULL,created_at,NULL,?,'retry','concurrent manual' "
                f'FROM dayblock_generation_receipts WHERE generation_id=?',
                (generation_id, attempt_no, first['generation_id']),
            )

        conn = self.db()
        try:
            insert_processing(conn, 'gen-live', 2)
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                insert_processing(conn, 'gen-second-live', 3)
        finally:
            conn.close()
        result = self.manual()
        self.assertEqual((result['status'], result['error_code']), ('refused', 'generation_in_progress'))
        store = shadow.open_preview_store(self.preview_db)
        try:
            job = store.execute('SELECT * FROM dayblock_shadow_jobs').fetchone()
            view = {
                'materialized_source_fingerprint': first['source_fingerprint'],
                'generation_input_mode': first['input_mode'],
                'source_member_count': first['source_member_count'],
                'source_coverage': first['source_coverage'],
                'raw_token_estimate': first['raw_token_estimate'],
                'input_budget_tokens': first['budget_tokens'],
                'generation_input_token_estimate': first['input_token_estimate'],
            }
            self.assertFalse(runner._claim(
                store, job['job_id'], DAY, 'gen-claim', 'run', view, job,
                dt.datetime.now(dt.timezone.utc), attempt_kind='retry',
                attempt_reason='race', expected_job_status='failed',
            ))
        finally:
            store.close()

    def _claim_view(self, receipt):
        return {
            'materialized_source_fingerprint': receipt['source_fingerprint'],
            'generation_input_mode': receipt['input_mode'],
            'source_member_count': receipt['source_member_count'],
            'source_coverage': receipt['source_coverage'],
            'raw_token_estimate': receipt['raw_token_estimate'],
            'input_budget_tokens': receipt['budget_tokens'],
            'generation_input_token_estimate': receipt['input_token_estimate'],
        }

    def test_claim_treats_only_unique_collisions_as_a_lost_race(self):
        self.scheduled(self.fail_with('cc_background_spawn_failed'))
        first = self.receipts()[0]
        conn = self.db()
        try:  # another claim already holds the day's live attempt
            conn.execute(
                f'INSERT INTO dayblock_generation_receipts({LEGACY_COLUMNS},attempt_no,attempt_kind,attempt_reason) '
                f"SELECT 'gen-live',run_id,generation_job_id,candidate_id,source_day,source_hash,"
                f'source_revision,source_fingerprint,provider,model_identity,authority_revision,'
                f'authority_snapshot_id,persona_revision,prompt_policy_version,prompt_hash,'
                f'generator_policy_version,input_mode,source_member_count,source_coverage,'
                f'raw_token_estimate,budget_tokens,input_token_estimate,0,NULL,"{{}}",NULL,NULL,'
                f"'processing',NULL,NULL,created_at,NULL,2,'retry','winner' "
                f'FROM dayblock_generation_receipts WHERE generation_id=?', (first['generation_id'],),
            )
            conn.commit()
        finally:
            conn.close()

        class MissesLiveCheck:
            """Hide the pre-insert live check so the insert itself loses the race."""
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                if sql.lstrip().startswith('SELECT 1') and "('processing','ready')" in sql:
                    return types.SimpleNamespace(fetchone=lambda: None)
                return self._conn.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        store = shadow.open_preview_store(self.preview_db)
        try:
            job = store.execute('SELECT * FROM dayblock_shadow_jobs').fetchone()
            self.assertFalse(runner._claim(
                MissesLiveCheck(store), job['job_id'], DAY, 'gen-loser', 'run',
                self._claim_view(first), job, dt.datetime.now(dt.timezone.utc),
                attempt_kind='retry', attempt_reason='loser', expected_job_status='failed',
            ))
        finally:
            store.close()
        self.assertEqual([row['generation_id'] for row in self.receipts()],
                         [first['generation_id'], 'gen-live'])

    def test_claim_surfaces_non_unique_integrity_errors(self):
        self.scheduled(self.fail_with('cc_background_spawn_failed'))
        first = self.receipts()[0]
        store = shadow.open_preview_store(self.preview_db)
        try:
            job = store.execute('SELECT * FROM dayblock_shadow_jobs').fetchone()
            # A manual attempt without a reason violates the CHECK constraint:
            # a bug, not a lost race, so it must not be replayed as one.
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'CHECK constraint failed'):
                runner._claim(
                    store, job['job_id'], DAY, 'gen-check', 'run', self._claim_view(first), job,
                    dt.datetime.now(dt.timezone.utc), attempt_kind='retry', attempt_reason=None,
                    expected_job_status='failed',
                )
            self.assertFalse(store.in_transaction)
        finally:
            store.close()
        self.assertEqual(len(self.receipts()), 1)

    # --- reading -------------------------------------------------------------

    def test_page_and_replay_read_ready_else_latest_attempt(self):
        self.scheduled(self.fail_with('cc_background_spawn_failed'))
        self.manual(self.fail_with('cc_background_exit_1:second'))
        entry = dayblock_context.get_dayblock_day(self.preview_db, DAY)
        self.assertEqual((entry['status'], entry['reason']), ('failed', 'cc_background_exit_1'))
        replay = self.scheduled(self.unexpected_call)
        self.assertEqual(
            (replay['status'], replay['attempt_no'], replay['error_detail']), ('failed', 2, 'second'),
        )
        self.manual(self.ok)
        entry = dayblock_context.get_dayblock_day(self.preview_db, DAY)
        self.assertEqual(entry['body'], '今天我完成了两项工作，并记录了下一步。')
        ready, rejected = dayblock_context.load_ready_dayblocks(self.preview_db)
        self.assertEqual([item.source_day for item in ready] + [r['source_day'] for r in rejected], [DAY])
        self.assertEqual([day['source_day'] for day in dayblock_context.list_dayblock_days(self.preview_db)], [DAY])

    # --- manual provenance ------------------------------------------------------

    def test_manual_reason_is_required_and_immutable(self):
        self.scheduled(self.fail_with('cc_background_spawn_failed'))
        for reason in ('', '   '):
            result = self.manual(reason=reason)
            self.assertEqual((result['status'], result['error_code']), ('refused', 'manual_reason_required'))
        self.manual(self.fail_with('cc_background_exit_1:x'), reason='rerun after spawn fix')
        retry = self.receipts()[1]
        with self.db() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE dayblock_generation_receipts SET attempt_reason='rewritten' "
                             'WHERE generation_id=?', (retry['generation_id'],))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE dayblock_generation_receipts SET attempt_kind='scheduled' "
                             'WHERE generation_id=?', (retry['generation_id'],))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    f'INSERT INTO dayblock_generation_receipts({LEGACY_COLUMNS},attempt_no,attempt_kind,attempt_reason) '
                    f"SELECT 'gen-noreason',run_id,generation_job_id,candidate_id,source_day,source_hash,"
                    f'source_revision,source_fingerprint,provider,model_identity,authority_revision,'
                    f'authority_snapshot_id,persona_revision,prompt_policy_version,prompt_hash,'
                    f'generator_policy_version,input_mode,source_member_count,source_coverage,'
                    f'raw_token_estimate,budget_tokens,input_token_estimate,0,NULL,"{{}}",NULL,NULL,'
                    f"'failed',NULL,NULL,created_at,NULL,9,'retry',NULL "
                    f'FROM dayblock_generation_receipts WHERE generation_id=?', (retry['generation_id'],),
                )

    def test_dry_run_writes_no_attempt_and_makes_no_call(self):
        self.scheduled(self.fail_with('cc_background_spawn_failed'))
        before = [tuple(row) for row in self.receipts()]
        result = self.manual(execute=False)
        self.assertEqual((result['status'], result['action']), ('dry_run', 'retry'))
        self.assertEqual(before, [tuple(row) for row in self.receipts()])
        self.assertEqual(len(self.calls), 1)

    def test_manual_attempt_shares_the_runner_lock(self):
        self.scheduled(self.fail_with('cc_background_spawn_failed'))
        with open(self.lock_path, 'a+') as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.manual()
        self.assertEqual((result['status'], result['error_code']), ('refused', 'dayblock_runner_busy'))
        self.assertEqual(len(self.receipts()), 1)

    def test_day_without_scheduled_job_is_refused(self):
        result = self.manual()
        self.assertEqual((result['status'], result['error_code']), ('refused', 'no_scheduled_job_for_day'))


if __name__ == '__main__':
    unittest.main()
