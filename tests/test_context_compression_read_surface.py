"""Production read-only Continuity HTTP/BFF for the context-compression page."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from flask import Flask

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from continuity.chunk_generation import (
    GENERATOR_POLICY_VERSION,
    MEASUREMENT_SEMANTICS,
    PROMPT_POLICY_VERSION,
)
from continuity.read_surface import get_current, open_read_only
from continuity.sealing import SealingPolicy
from continuity.sources import (
    build_source_snapshot,
    derive_autonomous_events,
    derive_completed_turns,
)
from continuity.store import (
    claim_generation_job,
    enqueue_generation_job,
    enqueue_job,
    ensure_schema,
    load_candidates,
    mark_generation_failed,
    materialize_job,
    publish_chunk_atomic,
    save_source_snapshot,
)
from context_compression_routes import create_context_compression_blueprint
from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1


IDENTITY = {
    'chat_id': 'default',
    'context_id': 7,
    'context_epoch': 3,
    'resident_generation': 1,
}


def _row(
    message_id: int,
    author: str,
    content: str,
    created_at: str,
    *,
    source_kind: str = 'chat',
    cache_info: str = '',
) -> tuple:
    return (
        message_id,
        author,
        content,
        'private thinking must never be evidence',
        created_at,
        '',
        '',
        0,
        cache_info,
        source_kind,
        '[]',
        '',
        '',
        '',
    )


def _seed_source_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        'CREATE TABLE chat_messages ('
        'id INTEGER PRIMARY KEY, author TEXT, content TEXT, thinking TEXT, '
        'created_at TEXT, tool_calls TEXT, branches TEXT, branch_idx INTEGER, '
        'cache_info TEXT, source_kind TEXT, attachments TEXT, image_url TEXT, '
        'file_url TEXT, file_name TEXT)'
    )
    conn.execute(
        'CREATE TABLE daily_message_contexts ('
        'message_id INTEGER PRIMARY KEY, context_id INTEGER NOT NULL, '
        'context_epoch INTEGER NOT NULL, resident_generation INTEGER NOT NULL, '
        'role TEXT NOT NULL)'
    )
    conn.execute(
        'CREATE TABLE wake_log ('
        'id INTEGER PRIMARY KEY, wake_run_id TEXT, chat_id TEXT, '
        'context_id INTEGER, context_epoch INTEGER, resident_generation INTEGER)'
    )


def _insert_chat(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.executemany(
        'INSERT INTO chat_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        rows,
    )
    conn.executemany(
        'INSERT INTO daily_message_contexts '
        '(message_id, context_id, context_epoch, resident_generation, role) '
        'VALUES (?,?,?,?,?)',
        [
            (
                row[0],
                IDENTITY['context_id'],
                IDENTITY['context_epoch'],
                1,
                'user' if row[1] == 'hayana' else 'assistant',
            )
            for row in rows
            if row[9] != 'wake'
        ],
    )


def _shanghai_today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone(
        datetime.timezone(datetime.timedelta(hours=8))
    ).strftime('%Y-%m-%d')


def _turns(count: int, *, day: str = '2026-09-14', start_id: int = 1) -> list[tuple]:
    rows = []
    for index in range(count):
        user_id = start_id + index * 2
        assistant_id = user_id + 1
        rows.extend((
            _row(user_id, 'hayana', f'用户第{index}句', f'{day} 01:{index:02d}:00'),
            _row(assistant_id, 'assistant', f'陪伴第{index}句', f'{day} 01:{index:02d}:30'),
        ))
    return rows


def _dict_rows(rows: list[tuple]) -> list[dict[str, object]]:
    keys = (
        'id', 'author', 'content', 'thinking', 'created_at', 'tool_calls',
        'branches', 'branch_idx', 'cache_info', 'source_kind', 'attachments',
        'image_url', 'file_url', 'file_name',
    )
    return [dict(zip(keys, row)) for row in rows]


class ContextCompressionReadSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / 'memories.db')
        self.identity = dict(IDENTITY)
        conn = sqlite3.connect(self.db_path)
        _seed_source_schema(conn)
        conn.commit()
        conn.close()
        self.app = Flask(__name__)
        self.app.register_blueprint(create_context_compression_blueprint(
            db_path=self.db_path,
            window_identity_reader=lambda _conn, _chat_id: self.identity,
        ))

        @self.app.route('/dash/<path:subpath>')
        def spa_fallback(subpath: str):
            return '<html>SPA</html>', 200

        self.client = self.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def _write(self, rows: list[tuple]) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        _insert_chat(conn, rows)
        ensure_schema(conn)
        conn.commit()
        return conn

    def _persist_ready_chunk(self, conn: sqlite3.Connection, rows: list[tuple]):
        snapshot = build_source_snapshot(
            turns=derive_completed_turns(_dict_rows(rows)),
            events=derive_autonomous_events(_dict_rows(rows)),
            local_day='2026-09-14',
            source_watermark=max(row[0] for row in rows),
            created_at='2026-09-14T00:00:00Z',
            context_id=self.identity['context_id'],
            context_epoch=self.identity['context_epoch'],
        )
        policy = SealingPolicy(version='test-read-surface', target_logical_size=1, max_completed_turns=20)
        save_source_snapshot(conn, snapshot)
        job = enqueue_job(conn, snapshot, policy, now='2026-09-14T00:00:00Z')
        materialize_job(conn, job.job_id, policy, now='2026-09-14T00:00:00Z')
        candidate = load_candidates(conn, job.job_id)[0]
        generation = enqueue_generation_job(
            conn, candidate, snapshot,
            generator_policy_version=GENERATOR_POLICY_VERSION,
            prompt_policy_version=PROMPT_POLICY_VERSION,
            measurement_semantics=MEASUREMENT_SEMANTICS,
            now='2026-09-14T00:00:00Z',
        )
        claimed = claim_generation_job(
            conn, generation.generation_job_id,
            frozen_provider='claude_code',
            frozen_model_identity='model-A',
            now='2026-09-14T00:00:01Z',
        )
        body = '压缩总结正文'
        publish_chunk_atomic(
            conn,
            job=claimed,
            candidate=candidate,
            body=body,
            body_hash=hashlib.sha256(body.encode('utf-8')).hexdigest(),
            source_token_estimate=12,
            output_token_estimate=estimate_tokens_heuristic_cjk1_ascii4_v1(body),
            provider='claude_code',
            model_identity='model-A',
            actual_executor='test',
            now='2026-09-14T00:00:02Z',
        )
        conn.commit()
        return candidate

    def test_empty_store_lists_zero_blocks_and_unavailable_current(self):
        listed = self.client.get('/dash/__continuity/blocks')
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.headers.get('Cache-Control'), 'no-store')
        body = listed.get_json()
        self.assertEqual(body['ok'], True)
        self.assertEqual(body['count'], 0)
        self.assertEqual(body['included_authority'], 'unknown')
        self.assertEqual(body['blocks'], [])

        current = self.client.get('/dash/__continuity/current')
        self.assertEqual(current.status_code, 200)
        payload = current.get_json()
        self.assertEqual(payload['ok'], True)
        self.assertFalse(payload['available'])
        self.assertEqual(payload['messages'], [])

    def test_missing_candidate_and_settings_are_json_404_not_spa_html(self):
        missing = self.client.get('/dash/__continuity/blocks/candidate-missing')
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json(), {'ok': False, 'error': 'not_found'})
        self.assertNotIn(b'<html>', missing.data)

        settings = self.client.get('/dash/__continuity/settings')
        self.assertEqual(settings.status_code, 404)
        self.assertEqual(settings.get_json(), {'ok': False, 'error': 'not_found'})
        self.assertEqual(settings.headers.get('Cache-Control'), 'no-store')

        posted = self.client.post('/dash/__continuity/settings', json={'length': 1})
        self.assertEqual(posted.status_code, 404)
        self.assertEqual(posted.get_json()['ok'], False)

    def test_non_get_on_read_routes_is_405(self):
        for path in ('/dash/__continuity/blocks', '/dash/__continuity/current'):
            resp = self.client.post(path, json={})
            self.assertEqual(resp.status_code, 405, path)
            self.assertEqual(resp.get_json()['error'], 'method_not_allowed')
            self.assertIn('GET', resp.headers.get('Allow', ''))

    def test_history_and_detail_use_persisted_candidate_and_chunk(self):
        rows = _turns(1)
        conn = self._write(rows)
        candidate = self._persist_ready_chunk(conn, rows)
        conn.close()

        listed = self.client.get('/dash/__continuity/blocks').get_json()
        self.assertEqual(listed['ok'], True)
        self.assertEqual(listed['count'], 1)
        self.assertEqual(listed['included_authority'], 'unknown')
        block = listed['blocks'][0]
        self.assertEqual(block['candidate_id'], candidate.candidate_id)
        self.assertEqual(block['status'], 'complete')
        self.assertEqual(block['included'], None)
        self.assertEqual(block['provider'], 'claude_code')
        self.assertEqual(block['model'], 'model-A')
        self.assertEqual(block['generation_job_status'], 'ready')
        self.assertEqual(block['chunk_status'], 'ready')
        self.assertTrue(block['materialization_available'])
        self.assertGreater(block['original_char_count'], 0)
        self.assertEqual(block['compressed_char_count'], len('压缩总结正文'))

        detail = self.client.get('/dash/__continuity/blocks/' + candidate.candidate_id)
        self.assertEqual(detail.status_code, 200)
        payload = detail.get_json()
        self.assertEqual(payload['ok'], True)
        self.assertEqual(payload['chunk']['body'], '压缩总结正文')
        contents = [item['content'] for item in payload['messages']]
        self.assertEqual(contents, ['用户第0句', '陪伴第0句'])
        self.assertEqual(payload['block']['start_at'], '2026-09-14 01:00:00')
        self.assertEqual(payload['block']['end_at'], '2026-09-14 01:00:30')
        self.assertTrue(all(item['role'] in ('user', 'assistant') for item in payload['messages']))
        self.assertTrue(all('thinking' not in item for item in payload['messages']))
        self.assertNotIn('private thinking must never be evidence', json.dumps(payload, ensure_ascii=False))

    def test_failed_generation_maps_to_failed_status(self):
        rows = _turns(1)
        conn = self._write(rows)
        snapshot = build_source_snapshot(
            turns=derive_completed_turns(_dict_rows(rows)),
            events=(),
            local_day='2026-09-14',
            source_watermark=2,
            created_at='2026-09-14T00:00:00Z',
            context_id=self.identity['context_id'],
            context_epoch=self.identity['context_epoch'],
        )
        policy = SealingPolicy(version='test-failed', target_logical_size=1, max_completed_turns=20)
        job = enqueue_job(conn, snapshot, policy)
        materialize_job(conn, job.job_id, policy)
        candidate = load_candidates(conn, job.job_id)[0]
        generation = enqueue_generation_job(
            conn, candidate, snapshot,
            generator_policy_version=GENERATOR_POLICY_VERSION,
            prompt_policy_version=PROMPT_POLICY_VERSION,
            measurement_semantics=MEASUREMENT_SEMANTICS,
        )
        mark_generation_failed(conn, generation.generation_job_id, 'generator_failed')
        conn.close()

        block = self.client.get('/dash/__continuity/blocks').get_json()['blocks'][0]
        self.assertEqual(block['status'], 'failed')
        self.assertEqual(block['generation_job_status'], 'failed')

    def test_current_shows_unclaimed_tail_progress_without_writing(self):
        rows = _turns(3, day=_shanghai_today())
        conn = self._write(rows)
        before = conn.execute('SELECT COUNT(*) FROM continuity_generation_jobs').fetchone()[0]
        snapshots_before = conn.execute('SELECT COUNT(*) FROM continuity_source_snapshots').fetchone()[0]
        conn.close()

        current = self.client.get('/dash/__continuity/current')
        self.assertEqual(current.status_code, 200)
        payload = current.get_json()
        self.assertEqual(payload['ok'], True)
        self.assertTrue(payload['available'])
        self.assertEqual(payload['completed_turn_count'], 3)
        self.assertEqual(payload['source_count'], 3)
        self.assertEqual(payload['settings_revision_id'], None)
        self.assertEqual(payload['processing_state'], 'waiting')
        self.assertFalse(payload['threshold_reached'])
        self.assertEqual(payload['turn_target'], 20)
        self.assertEqual(payload['size_target'], 12_000)
        self.assertGreater(payload['turn_progress'], 0)
        self.assertLess(payload['turn_progress'], 100)
        self.assertEqual(
            [item['content'] for item in payload['messages']],
            ['用户第0句', '陪伴第0句', '用户第1句', '陪伴第1句', '用户第2句', '陪伴第2句'],
        )
        self.assertEqual(payload['start_at'], payload['messages'][0]['created_at'])
        self.assertEqual(payload['end_at'], payload['messages'][-1]['created_at'])
        self.assertNotIn('private thinking must never be evidence', json.dumps(payload, ensure_ascii=False))

        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM continuity_generation_jobs').fetchone()[0],
            before,
        )
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM continuity_source_snapshots').fetchone()[0],
            snapshots_before,
        )
        conn.close()

    def test_current_subtracts_claimed_revisions(self):
        rows = _turns(2)
        conn = self._write(rows)
        first = build_source_snapshot(
            turns=derive_completed_turns(_dict_rows(rows[:2])),
            events=(),
            local_day='2026-09-14',
            source_watermark=2,
            created_at='2026-09-14T00:00:00Z',
            context_id=self.identity['context_id'],
            context_epoch=self.identity['context_epoch'],
        )
        save_source_snapshot(conn, first)
        conn.commit()
        conn.close()

        payload = self.client.get('/dash/__continuity/current').get_json()
        self.assertTrue(payload['available'])
        self.assertEqual(payload['completed_turn_count'], 1)
        self.assertEqual(payload['source_refs'], ['turn:3:4'])
        self.assertEqual(
            [item['content'] for item in payload['messages']],
            ['用户第1句', '陪伴第1句'],
        )

    def test_current_threshold_uses_in_memory_sealing_without_producer(self):
        rows = _turns(3)
        conn = self._write(rows)
        conn.close()
        payload = get_current(
            db_path=self.db_path,
            window_identity_reader=lambda _conn, _chat_id: self.identity,
            policy=SealingPolicy(version='tiny', target_logical_size=1, max_completed_turns=20),
            now='2026-09-14T04:00:00+00:00',
        )
        self.assertTrue(payload['available'])
        self.assertTrue(payload['threshold_reached'])
        self.assertEqual(payload['processing_state'], 'threshold_reached')
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM continuity_candidate_blocks').fetchone()[0], 0)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM continuity_generation_jobs').fetchone()[0], 0)
        conn.close()

    def test_query_only_rejects_writes(self):
        conn = self._write(_turns(1))
        ensure_schema(conn)
        conn.close()
        readonly = open_read_only(self.db_path)
        with self.assertRaises(sqlite3.OperationalError):
            readonly.execute(
                "INSERT INTO continuity_generation_jobs("
                "generation_job_id, idempotency_key, candidate_id, snapshot_id, "
                "candidate_source_revision, generator_policy_version, prompt_policy_version, "
                "measurement_semantics, status, attempt, generation_id, created_at, updated_at) "
                "VALUES ('x','x','x','x','x','x','x','x','pending',0,'x','t','t')"
            )
        readonly.close()

    def test_read_modules_do_not_open_producer_or_settings(self):
        read_surface = Path(ROOT, 'continuity/read_surface.py').read_text(encoding='utf-8')
        routes = Path(ROOT, 'context_compression_routes.py').read_text(encoding='utf-8')
        self.assertNotIn('continuity.producer', read_surface)
        self.assertNotIn('run_continuity_producer', read_surface)
        self.assertNotIn('ensure_schema(', read_surface)
        self.assertNotIn('enqueue_generation_job', read_surface)
        self.assertNotIn('preview_context_settings', read_surface)
        self.assertNotIn('continuity.producer', routes)
        self.assertNotIn('/settings', routes)
        self.assertNotIn('from flask', read_surface)
        app_py = Path(ROOT, 'app.py').read_text(encoding='utf-8')
        self.assertIn('create_context_compression_blueprint', app_py)
        self.assertIn("subpath.startswith('__continuity/')", app_py)
        self.assertLess(
            app_py.index('create_context_compression_blueprint'),
            app_py.index("@app.route('/dash/<path:subpath>')"),
        )

        sys.modules.pop('continuity.producer', None)
        sys.modules.pop('continuity.read_surface', None)
        import continuity.read_surface as imported
        self.assertNotIn('continuity.producer', sys.modules)
        self.assertTrue(hasattr(imported, 'list_blocks'))


if __name__ == '__main__':
    unittest.main()
