"""DayBlock → ContextPlan substitution (current window, flag-gated)."""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from continuity.context_plan import build_context_plan
from continuity.contracts import SourceMember
from continuity.dayblock_context import (
    DayBlockArtifact,
    dayblock_refusal_reason,
    load_ready_dayblocks,
    substitute_dayblock_bindings,
)
from tests.test_continuity_r4 import _binding as _r4_binding

OLD, YESTERDAY, TODAY = '2026-10-10', '2026-10-11', '2026-10-12'
BODY = '十月十号。' + '她和我一起把日块的边界讲清楚了，' * 40


def _m(seq: int, day: str, *, kind: str = 'completed_turn', revision: str | None = None) -> SourceMember:
    return SourceMember(
        seq=seq,
        source_kind=kind,
        source_ref=f'turn:{100 + seq}:{200 + seq}',
        source_revision=revision or f'rev-{seq}',
        role='conversation',
        content_hash=f'hash-{seq}',
        logical_size=20,
        created_at=f'{day} 12:{seq:02d}:00',
        branch_id='active-transcript',
    )


EXPECTED = (
    _m(0, OLD), _m(1, OLD), _m(2, OLD), _m(3, OLD),
    _m(4, YESTERDAY), _m(5, YESTERDAY),
    _m(6, TODAY), _m(7, TODAY),
)


def _chunks():
    return (
        _r4_binding(EXPECTED, seqs=(0, 1), chunk_id='c-old-a', body='old a ' * 50),
        _r4_binding(EXPECTED, seqs=(2, 3), chunk_id='c-old-b', body='old b ' * 50),
        _r4_binding(EXPECTED, seqs=(4, 5), chunk_id='c-yesterday', body='yday ' * 50),
    )


def _dayblock(day: str = OLD, members=None, body: str = BODY) -> DayBlockArtifact:
    if members is None:
        # DayBlock snapshots number members per day; seq differs from the window.
        members = tuple(replace(m, seq=50 + i) for i, m in enumerate(EXPECTED[:4]))
        members += (SourceMember(
            seq=99, source_kind='incomplete_user_turn', source_ref='msg:999',
            source_revision='r', role='user', content_hash='h', created_at=f'{day} 23:00:00',
        ),)
    return DayBlockArtifact(
        source_day=day, generation_id='gen-0123456789abcdef', generation_job_id='job-1',
        body=body, body_hash=hashlib.sha256(body.encode('utf-8')).hexdigest(),
        raw_token_estimate=30000, output_token_estimate=1100, provider='claude_code',
        model_identity='explicit:claude-opus-5-5', actual_executor='claude_code_background_oneshot',
        generator_policy_version='g', prompt_policy_version='p', prompt_hash='ph',
        created_at=f'{day}T19:00:00Z', members=members,
    )


def _status(report, day):
    return next(entry['status'] for entry in report if entry['source_day'] == day)


class SubstitutionTests(unittest.TestCase):
    def test_old_day_chunks_replaced_and_plan_stays_valid(self):
        bindings, report = substitute_dayblock_bindings(
            EXPECTED, _chunks(), (_dayblock(),), keep_chunks_from_day=YESTERDAY,
        )
        self.assertEqual(_status(report, OLD), 'substituted')
        ids = [b.chunk.chunk_id for b in bindings]
        self.assertNotIn('c-old-a', ids)
        self.assertNotIn('c-old-b', ids)
        self.assertIn('c-yesterday', ids)
        entry = report[0]
        self.assertEqual(entry['chunks_replaced'], 2)
        plan = build_context_plan(EXPECTED, raw_members=EXPECTED, chunks=bindings)
        reps = [r.representation_id for r in plan.representations]
        self.assertIn('chunk:dayblock-2026-10-10-gen-01234567', reps)
        self.assertNotIn('chunk_rejected', [e.code for e in plan.exclusions])
        self.assertTrue(plan.valid)
        covered = sorted(int(m.seq) for r in plan.representations for m in r.source_members)
        self.assertEqual(covered, list(range(8)))

    def test_changed_window_member_keeps_chunks(self):
        expected = EXPECTED[:2] + (_m(2, OLD, revision='rev-2-regenerated'),) + EXPECTED[3:]
        chunks = (
            _r4_binding(expected, seqs=(0, 1), chunk_id='c-old-a'),
            _r4_binding(expected, seqs=(2, 3), chunk_id='c-old-b'),
        )
        bindings, report = substitute_dayblock_bindings(
            expected, chunks, (_dayblock(),), keep_chunks_from_day=YESTERDAY,
        )
        self.assertEqual(_status(report, OLD), 'stale_window_member_not_covered')
        self.assertEqual(tuple(bindings), chunks)

    def test_dayblock_covering_removed_turn_keeps_chunks(self):
        members = tuple(replace(m, seq=50 + i) for i, m in enumerate(EXPECTED[:4]))
        members += (replace(_m(9, OLD), seq=60),)  # turn no longer in the window
        bindings, report = substitute_dayblock_bindings(
            EXPECTED, _chunks(), (_dayblock(members=members),), keep_chunks_from_day=YESTERDAY,
        )
        self.assertEqual(_status(report, OLD), 'stale_dayblock_member_not_in_window')
        self.assertEqual(tuple(bindings), _chunks())

    def test_recent_days_always_keep_chunks(self):
        members = tuple(replace(m, seq=50 + i) for i, m in enumerate(EXPECTED[4:6]))
        _bindings, report = substitute_dayblock_bindings(
            EXPECTED, _chunks(), (_dayblock(YESTERDAY, members),), keep_chunks_from_day=YESTERDAY,
        )
        self.assertEqual(_status(report, YESTERDAY), 'recent_day_keeps_chunks')

    def test_chunk_straddling_the_day_blocks_substitution(self):
        chunks = (_r4_binding(EXPECTED, seqs=(3, 4), chunk_id='c-straddle'),)
        bindings, report = substitute_dayblock_bindings(
            EXPECTED, chunks, (_dayblock(),), keep_chunks_from_day=YESTERDAY,
        )
        self.assertEqual(_status(report, OLD), 'chunk_straddles_day')
        self.assertEqual(tuple(bindings), chunks)

    def test_day_outside_window_is_ignored(self):
        bindings, report = substitute_dayblock_bindings(
            EXPECTED, _chunks(), (_dayblock('2026-09-01', members=(replace(_m(0, '2026-09-01'), seq=1),)),),
            keep_chunks_from_day=YESTERDAY,
        )
        self.assertEqual(_status(report, '2026-09-01'), 'not_in_window')
        self.assertEqual(tuple(bindings), _chunks())


class RefusalGateTests(unittest.TestCase):
    def test_2026_10_13_refusal_is_rejected(self):
        body = ('这篇日记我就停在这里，不再继续写，也不换个说法重写。\n\n'
                '如果有用，我可以只整理今天留下的待办，不涉及亲密内容：')
        self.assertEqual(dayblock_refusal_reason(body, 29252), 'refusal_text')

    def test_short_body_for_big_day_is_rejected(self):
        self.assertEqual(dayblock_refusal_reason('今天很好。' * 20, 30000), 'body_too_short')

    def test_real_dayblock_passes(self):
        self.assertIsNone(dayblock_refusal_reason(BODY, 30000))
        self.assertIsNone(dayblock_refusal_reason('十月一号，很安静。', 4000))


def _insert(conn, table, **values):
    cols = conn.execute(f'PRAGMA table_info({table})').fetchall()
    row = {}
    for _cid, name, ctype, notnull, default, _pk in cols:
        if name in values:
            row[name] = values[name]
        elif notnull and default is None:
            row[name] = 0 if 'INT' in str(ctype).upper() or 'REAL' in str(ctype).upper() else ''
    names = ','.join(row)
    conn.execute(f'INSERT INTO {table} ({names}) VALUES ({",".join("?" * len(row))})', tuple(row.values()))


class StoreLoadTests(unittest.TestCase):
    def _store(self, tmp: str, receipts):
        import dayblock_shadow
        path = Path(tmp) / 'dayblock.db'
        conn = dayblock_shadow.open_preview_store(path)
        for day, body, status, body_hash in receipts:
            snap = f'snap-{day}'
            _insert(conn, 'dayblock_source_snapshots', snapshot_id=snap, source_day=day,
                    source_hash=f'sh-{day}', chat_id='default', completed_turn_count=4)
            for i, m in enumerate(EXPECTED[:4]):
                _insert(conn, 'dayblock_source_members', snapshot_id=snap, seq=i,
                        source_kind=m.source_kind, source_ref=m.source_ref,
                        source_revision=m.source_revision, content_hash=m.content_hash,
                        role=m.role, logical_size=20, created_at=m.created_at,
                        branch_id=m.branch_id)
            job_blocked = status == 'job_blocked'
            _insert(conn, 'dayblock_shadow_jobs', job_id=f'job-{day}', candidate_id=f'cand-{day}',
                    snapshot_id=snap, source_day=day, provider='claude_code',
                    status='blocked' if job_blocked else 'ready',
                    blocking_reasons_json='["uncovered_formal_source_rows"]' if job_blocked else '[]',
                    source_revision=f'sr-{day}', policy_version='p', chat_id='default')
            if job_blocked:
                continue
            _insert(conn, 'dayblock_generation_receipts', generation_id=f'gen-{day}-0000000000',
                    generation_job_id=f'job-{day}', source_day=day, provider='claude_code',
                    summary_body=body, body_hash=body_hash or hashlib.sha256(body.encode()).hexdigest(),
                    status=status, raw_token_estimate=30000,
                    output_token_estimate=1100 if body else 0,
                    error_code='cc_background_spawn_failed' if status == 'failed' else None)
        conn.commit()
        conn.close()
        return path

    def test_loads_ready_and_rejects_refusal_hash_and_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._store(tmp, (
                (OLD, BODY, 'ready', None),
                ('2026-10-13', '这篇日记我就停在这里，不再继续写。', 'ready', None),
                ('2026-10-14', BODY, 'ready', 'f' * 64),
                ('2026-10-15', '', 'failed', None),
            ))
            usable, rejected = load_ready_dayblocks(path)
            self.assertEqual([d.source_day for d in usable], [OLD])
            self.assertEqual(len(usable[0].members), 4)
            self.assertEqual(
                {(r['source_day'], r['reason']) for r in rejected},
                {('2026-10-13', 'refusal_text'), ('2026-10-14', 'body_hash_mismatch')},
            )
            # read-only: the loader never writes
            before = path.stat().st_mtime_ns
            load_ready_dayblocks(path)
            self.assertEqual(path.stat().st_mtime_ns, before)

    def test_missing_store_is_empty_not_error(self):
        self.assertEqual(load_ready_dayblocks('/nonexistent/dayblock.db'), ((), ()))


class RuntimeHookTests(unittest.TestCase):
    def _plan(self, frozen=None):
        import datetime
        return SimpleNamespace(
            user_created_at=datetime.datetime(2026, 10, 12, 15, 0, 0),
            hot_receipt_frozen=frozen, request_id='req', context_id=32, manifest={},
        )

    def _hook(self, plan, *, mode, enabled, dayblocks=None):
        from chat import daily_runtime as dr
        box = {}
        config = SimpleNamespace(get_bool=lambda key, default=False: enabled)
        with mock.patch.object(dr, 'config_store', config), \
             mock.patch('continuity.dayblock_context.load_ready_dayblocks',
                        return_value=((dayblocks or (_dayblock(),)), ())):
            hook = dr._dayblock_binding_transform(plan, mode=mode, box=box)
        return hook, box

    def test_flag_off_cold_observes_but_keeps_bindings(self):
        plan = self._plan()
        hook, box = self._hook(plan, mode='cold', enabled=False)
        out = hook(EXPECTED, _chunks())
        self.assertEqual(tuple(out), _chunks())
        self.assertEqual(_status(box['report'], OLD), 'substituted')
        self.assertNotIn('chunks', box)
        from chat import daily_runtime as dr
        dr._log_dayblock_observation(plan, box, SimpleNamespace(representations=(), exclusions=()))
        self.assertEqual(plan.manifest['dayblock_context_substituted_days'], [])
        self.assertEqual(plan.manifest['dayblock_context_eligible_days'], [OLD])

    def test_flag_on_cold_substitutes(self):
        hook, box = self._hook(self._plan(), mode='cold', enabled=True)
        out = hook(EXPECTED, _chunks())
        self.assertIn('dayblock-2026-10-10-gen-01234567', [b.chunk.chunk_id for b in out])
        self.assertIn('dayblock-2026-10-10-gen-01234567', box['chunks'])

    def test_keep_window_is_today_and_yesterday(self):
        from chat import daily_runtime as dr
        self.assertEqual(dr._dayblock_keep_chunks_from_day(self._plan()), YESTERDAY)

    def test_hot_flag_off_has_no_hook(self):
        hook, _box = self._hook(self._plan(), mode='hot', enabled=False)
        self.assertIsNone(hook)

    def test_hot_only_sees_installed_dayblocks(self):
        installed = SimpleNamespace(representation_kind='chunk',
                                    representation_id='chunk:dayblock-2026-10-10-gen-01234567')
        frozen_ok = {'receipt': object(), 'receipt_members_complete': True,
                     'membership_valid': True, 'members': (installed,)}
        frozen_without = dict(frozen_ok, members=())
        hook, _ = self._hook(self._plan(frozen_without), mode='hot', enabled=True)
        self.assertIsNone(hook, 'a DayBlock born after the cold start stays invisible on hot turns')
        hook, _ = self._hook(self._plan(frozen_ok), mode='hot', enabled=True)
        self.assertIsNotNone(hook)
        hook, _ = self._hook(self._plan(None), mode='hot', enabled=True)
        self.assertIsNone(hook, 'no provable receipt → no DayBlocks on hot turns')

    def test_hook_failure_returns_original_bindings(self):
        hook, box = self._hook(self._plan(), mode='cold', enabled=True)
        bad = SimpleNamespace()  # not a binding: substitution raises inside
        out = hook(EXPECTED, (bad,))
        self.assertEqual(out, (bad,))
        self.assertEqual(box['error'], 'AttributeError')


class PageSurfaceTests(unittest.TestCase):
    _store = StoreLoadTests._store
    RECEIPTS = (
        (OLD, BODY, 'ready', None),
        ('2026-10-13', '这篇日记我就停在这里，不再继续写。', 'ready', None),
        ('2026-10-15', '', 'failed', None),
        ('2026-10-16', '', 'job_blocked', None),
    )

    def test_list_reports_every_day_state(self):
        from continuity.dayblock_context import list_dayblock_days
        with tempfile.TemporaryDirectory() as tmp:
            days = {d['source_day']: d for d in list_dayblock_days(self._store(tmp, self.RECEIPTS))}
        self.assertEqual(sorted(days), [OLD, '2026-10-13', '2026-10-15', '2026-10-16'])
        self.assertEqual(days[OLD]['status'], 'ready')
        self.assertEqual(days[OLD]['completed_turn_count'], 4)
        self.assertEqual(days[OLD]['output_token_estimate'], 1100)
        self.assertEqual(days[OLD]['body_char_count'], len(BODY))
        self.assertNotIn('body', days[OLD])
        self.assertEqual((days['2026-10-13']['status'], days['2026-10-13']['reason']), ('rejected', 'refusal_text'))
        self.assertEqual((days['2026-10-15']['status'], days['2026-10-15']['reason']),
                         ('failed', 'cc_background_spawn_failed'))
        self.assertIsNone(days['2026-10-15']['output_token_estimate'])
        self.assertEqual((days['2026-10-16']['status'], days['2026-10-16']['reason']),
                         ('blocked', 'uncovered_formal_source_rows'))

    def test_detail_has_body_and_validates_day(self):
        from continuity.dayblock_context import get_dayblock_day
        with tempfile.TemporaryDirectory() as tmp:
            path = self._store(tmp, self.RECEIPTS)
            detail = get_dayblock_day(path, OLD)
            refused = get_dayblock_day(path, '2026-10-13')
            self.assertIsNone(get_dayblock_day(path, '2026-10-30'))
            self.assertIsNone(get_dayblock_day(path, "2026-10-10' OR 1=1"))
        self.assertEqual(detail['body'], BODY)
        self.assertEqual(refused['status'], 'rejected')
        self.assertIn('不再继续写', refused['body'])

    def test_missing_store_lists_nothing(self):
        from continuity.dayblock_context import get_dayblock_day, list_dayblock_days
        self.assertEqual(list_dayblock_days('/nonexistent/dayblock.db'), [])
        self.assertIsNone(get_dayblock_day('/nonexistent/dayblock.db', OLD))

    def test_routes(self):
        from flask import Flask
        from context_compression_routes import create_context_compression_blueprint
        with tempfile.TemporaryDirectory() as tmp:
            app = Flask(__name__)
            app.register_blueprint(create_context_compression_blueprint(
                db_path=str(Path(tmp) / 'memories.db'),
                dayblock_store_path=str(self._store(tmp, self.RECEIPTS)),
            ))
            client = app.test_client()
            listing = client.get('/dash/__continuity/dayblocks')
            self.assertEqual(listing.status_code, 200)
            self.assertEqual(listing.headers['Cache-Control'], 'no-store')
            self.assertEqual(len(listing.get_json()['dayblocks']), 4)
            detail = client.get('/dash/__continuity/dayblocks/' + OLD)
            self.assertEqual(detail.get_json()['dayblock']['body'], BODY)
            self.assertEqual(client.get('/dash/__continuity/dayblocks/2026-10-30').status_code, 404)
            self.assertEqual(client.post('/dash/__continuity/dayblocks').status_code, 405)


if __name__ == '__main__':
    unittest.main()
