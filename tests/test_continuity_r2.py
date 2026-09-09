"""CONTINUITY-R2 — deterministic sealing and shadow store contracts."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest

from continuity.contracts import SourceMember, SourceSnapshot
from continuity.coverage import source_hash
from continuity.sealing import (
    SealingPolicy,
    validate_candidate_coverage,
    seal_snapshot,
)
from continuity.store import (
    TABLES,
    enqueue_job,
    ensure_schema,
    load_candidates,
    load_job,
    materialize_job,
)


def member(
    seq: int,
    size: int,
    *,
    source_kind: str = 'completed_turn',
    day: str = '2026-09-08',
    branch: str = 'active-transcript',
) -> SourceMember:
    prefix = 'turn' if source_kind == 'completed_turn' else 'wake'
    return SourceMember(
        seq=seq,
        source_kind=source_kind,
        source_ref=f'{prefix}:{seq}',
        source_revision=f'revision-{seq}',
        role='conversation' if source_kind == 'completed_turn' else 'assistant',
        content_hash=f'hash-{seq}',
        logical_size=size,
        created_at=f'{day} 04:00:00',
        branch_id=branch,
    )


def snapshot(members: tuple[SourceMember, ...], *, source_id: str = 'source:test') -> SourceSnapshot:
    return SourceSnapshot(
        snapshot_id=source_id,
        identity_id='fyodor',
        chat_id='default',
        branch_id='active-transcript',
        local_day='2026-09-08',
        source_watermark=max((item.seq for item in members), default=0),
        policy_version='continuity_source_v1',
        source_hash=source_hash(members),
        status='ready',
        created_at='2026-09-09T00:00:00Z',
        members=members,
    )


class SealingTests(unittest.TestCase):
    def test_target_size_seals_after_unit_reaches_12k(self):
        blocks = seal_snapshot(snapshot((member(0, 7000), member(1, 5000), member(2, 1))))
        self.assertEqual([block.logical_size for block in blocks], [12000, 1])
        self.assertEqual(blocks[0].close_reason, 'target_logical_size')

    def test_completed_turn_hard_boundary_is_twenty(self):
        blocks = seal_snapshot(snapshot(tuple(member(i, 10) for i in range(21))))
        self.assertEqual([block.completed_turn_count for block in blocks], [20, 1])
        self.assertTrue(all(block.completed_turn_count <= 20 for block in blocks))

    def test_natural_day_boundary_seals_partial_block(self):
        blocks = seal_snapshot(snapshot((
            member(0, 100, day='2026-09-08'),
            member(1, 100, day='2026-09-09'),
        )))
        self.assertEqual([block.local_day for block in blocks], ['2026-09-08', '2026-09-09'])
        self.assertEqual(blocks[0].close_reason, 'day_boundary')

    def test_oversize_turn_is_one_marked_unsplit_block(self):
        blocks = seal_snapshot(snapshot((member(0, 12001),)))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].source_refs, ('turn:0',))
        self.assertTrue(blocks[0].oversize)
        self.assertEqual(blocks[0].close_reason, 'oversize_single_turn')

    def test_wake_is_preserved_but_does_not_increment_turn_count(self):
        blocks = seal_snapshot(snapshot((
            member(0, 100),
            member(1, 100, source_kind='autonomous_event'),
            member(2, 100, source_kind='autonomous_event'),
        )))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].completed_turn_count, 1)
        self.assertEqual(blocks[0].source_refs, ('turn:0', 'wake:1', 'wake:2'))

    def test_branch_change_never_crosses_candidate_boundary(self):
        blocks = seal_snapshot(snapshot((
            member(0, 100, branch='branch-a'),
            member(1, 100, branch='branch-b'),
        )))
        self.assertEqual([block.branch_id for block in blocks], ['branch-a', 'branch-b'])
        self.assertEqual([block.source_refs for block in blocks], [('turn:0',), ('turn:1',)])

    def test_repeated_sealing_has_same_identity_and_boundaries(self):
        snap = snapshot(tuple(member(i, 4000) for i in range(7)))
        policy = SealingPolicy()
        self.assertEqual(seal_snapshot(snap, policy), seal_snapshot(snap, policy))

    def test_policy_version_changes_candidate_identity(self):
        snap = snapshot((member(0, 100),))
        first = seal_snapshot(snap, SealingPolicy(version='continuity_sealing_v1'))
        second = seal_snapshot(snap, SealingPolicy(version='continuity_sealing_v2'))
        self.assertNotEqual(first[0].candidate_id, second[0].candidate_id)
        self.assertNotEqual(first[0].policy_version, second[0].policy_version)

    def test_candidate_membership_is_an_exact_partition(self):
        snap = snapshot(tuple(member(i, 100) for i in range(3)))
        blocks = seal_snapshot(snap)
        report = validate_candidate_coverage(snap, blocks)
        self.assertTrue(report.valid)
        self.assertEqual(report.issues, ())


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_schema_is_idempotent_and_only_contains_r2_tables(self):
        ensure_schema(self.conn)
        tables = {
            str(row[0]) for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'continuity_%'"
            )
        }
        self.assertEqual(tables, set(TABLES))

    def test_enqueue_and_materialize_are_idempotent(self):
        snap = snapshot(tuple(member(i, 100) for i in range(2)), source_id='source:idempotent')
        policy = SealingPolicy()
        first_job = enqueue_job(self.conn, snap, policy, now='2026-09-09T00:00:00Z')
        second_job = enqueue_job(self.conn, snap, policy, now='2026-09-09T00:01:00Z')
        self.assertEqual(first_job, second_job)

        first_candidates = materialize_job(
            self.conn, first_job.job_id, policy, now='2026-09-09T00:02:00Z'
        )
        second_candidates = materialize_job(
            self.conn, first_job.job_id, policy, now='2026-09-09T00:03:00Z'
        )
        self.assertEqual(first_candidates, second_candidates)
        self.assertEqual(load_job(self.conn, first_job.job_id).status, 'shadow')
        self.assertEqual(len(load_candidates(self.conn, first_job.job_id)), 1)
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM continuity_jobs').fetchone()[0], 1
        )
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM continuity_candidate_blocks').fetchone()[0], 1
        )
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM continuity_candidate_members').fetchone()[0], 2
        )


class ReplayFixtureTests(unittest.TestCase):
    def test_incomplete_source_is_not_replayed_into_candidates(self):
        from continuity.sources import build_source_members, derive_completed_turns

        rows = [
            {
                'id': 1, 'author': 'hayana', 'content': 'u', 'source_kind': 'chat',
                'created_at': '2026-09-08 04:01:00', 'attachments': '[]',
                'cache_info': '', 'tool_calls': '', 'branches': '', 'branch_idx': 0,
            },
            {
                'id': 2, 'author': 'assistant', 'content': 'partial', 'source_kind': 'chat',
                'created_at': '2026-09-08 04:02:00', 'attachments': '[]',
                'cache_info': json.dumps({'stream_interrupted': True}),
                'tool_calls': '', 'branches': '', 'branch_idx': 0,
            },
        ]
        self.assertEqual(build_source_members(derive_completed_turns(rows), ()), ())

    def test_replay_reports_candidate_distribution_and_determinism(self):
        from scripts.replay_continuity_sources import replay

        with tempfile.TemporaryDirectory() as directory:
            db_path = f'{directory}/replay.db'
            conn = sqlite3.connect(db_path)
            conn.execute(
                '''CREATE TABLE chat_messages (
                    id INTEGER PRIMARY KEY,
                    author TEXT,
                    content TEXT,
                    thinking TEXT,
                    created_at TEXT,
                    tool_calls TEXT,
                    branches TEXT,
                    branch_idx INTEGER,
                    cache_info TEXT,
                    source_kind TEXT,
                    attachments TEXT,
                    image_url TEXT,
                    file_url TEXT,
                    file_name TEXT
                )'''
            )
            conn.executemany(
                'INSERT INTO chat_messages '
                '(id, author, content, thinking, created_at, tool_calls, branches, branch_idx, '
                'cache_info, source_kind, attachments, image_url, file_url, file_name) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                [
                    (1, 'hayana', 'u', '', '2026-09-08 04:01:00', '', '', 0, '', 'chat', '[]', '', '', ''),
                    (2, 'assistant', 'a', '', '2026-09-08 04:02:00', '', '', 0, '', 'chat', '[]', '', '', ''),
                    (3, 'assistant', 'wake', '', '2026-09-08 04:03:00', '', '', 0,
                     json.dumps({
                         'wake_mode': 'normal', 'canonical_chat_history': True,
                         'unified_chat_resident': True, 'b3_authority': True,
                         'source': 'wake', 'provider': 'claude_code',
                     }), 'wake', '[]', '', '', ''),
                ],
            )
            conn.commit()
            conn.close()

            result = replay(db_path, days=30)

        self.assertEqual(result['source_unit_count'], 2)
        self.assertEqual(result['candidate_count'], 1)
        self.assertEqual(result['daily_candidate_distribution'], {'2026-09-08': 1})
        self.assertTrue(result['candidate_coverage_valid'])
        self.assertTrue(result['determinism_valid'])


if __name__ == '__main__':
    unittest.main()

