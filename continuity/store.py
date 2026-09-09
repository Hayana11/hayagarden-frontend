"""Minimal SQLite persistence for Continuity Compression R2.

The store persists source identity, candidate membership, and a shadow job
skeleton only.  It deliberately has no generated text or runtime consumer.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from continuity.contracts import SourceMember, SourceSnapshot
from continuity.sealing import CandidateBlock, SealingPolicy, seal_snapshot


TABLES = (
    'continuity_source_snapshots',
    'continuity_source_members',
    'continuity_jobs',
    'continuity_candidate_blocks',
    'continuity_candidate_members',
)


class ContinuityStoreError(RuntimeError):
    """Base error for source/candidate persistence conflicts."""


class ContinuityStoreConflict(ContinuityStoreError):
    """Persisted identity differs from the requested immutable identity."""


@dataclass(frozen=True)
class ContinuityJob:
    job_id: str
    idempotency_key: str
    snapshot_id: str
    policy_version: str
    source_hash: str
    status: str
    candidate_count: int


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the R2 schema idempotently on the caller-provided connection."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS continuity_source_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            identity_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            branch_id TEXT NOT NULL,
            local_day TEXT NOT NULL,
            source_watermark INTEGER NOT NULL,
            source_policy_version TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS continuity_source_members (
            snapshot_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            source_kind TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            role TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            span_start INTEGER,
            span_end INTEGER,
            logical_size INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            branch_id TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, seq),
            UNIQUE (snapshot_id, source_ref),
            FOREIGN KEY (snapshot_id) REFERENCES continuity_source_snapshots(snapshot_id)
        );
        CREATE INDEX IF NOT EXISTS idx_continuity_source_members_ref
            ON continuity_source_members(snapshot_id, source_ref);

        CREATE TABLE IF NOT EXISTS continuity_jobs (
            job_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            snapshot_id TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'shadow')),
            candidate_count INTEGER NOT NULL DEFAULT 0,
            error_code TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (snapshot_id, policy_version),
            FOREIGN KEY (snapshot_id) REFERENCES continuity_source_snapshots(snapshot_id)
        );
        CREATE INDEX IF NOT EXISTS idx_continuity_jobs_snapshot
            ON continuity_jobs(snapshot_id, policy_version);

        CREATE TABLE IF NOT EXISTS continuity_candidate_blocks (
            candidate_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            block_seq INTEGER NOT NULL,
            local_day TEXT NOT NULL,
            branch_id TEXT NOT NULL,
            source_start_seq INTEGER NOT NULL,
            source_end_seq INTEGER NOT NULL,
            source_revision TEXT NOT NULL,
            snapshot_source_hash TEXT NOT NULL,
            logical_size INTEGER NOT NULL,
            completed_turn_count INTEGER NOT NULL,
            oversize INTEGER NOT NULL CHECK (oversize IN (0, 1)),
            close_reason TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'shadow')),
            created_at TEXT NOT NULL,
            UNIQUE (job_id, block_seq),
            FOREIGN KEY (job_id) REFERENCES continuity_jobs(job_id),
            FOREIGN KEY (snapshot_id) REFERENCES continuity_source_snapshots(snapshot_id)
        );
        CREATE INDEX IF NOT EXISTS idx_continuity_candidates_job
            ON continuity_candidate_blocks(job_id, block_seq);

        CREATE TABLE IF NOT EXISTS continuity_candidate_members (
            candidate_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            source_seq INTEGER NOT NULL,
            source_ref TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            logical_size INTEGER NOT NULL,
            PRIMARY KEY (candidate_id, ordinal),
            UNIQUE (candidate_id, source_seq),
            FOREIGN KEY (candidate_id) REFERENCES continuity_candidate_blocks(candidate_id)
        );
        CREATE INDEX IF NOT EXISTS idx_continuity_candidate_members_ref
            ON continuity_candidate_members(source_ref);
        """
    )
    conn.commit()


def _snapshot_row(snapshot: SourceSnapshot) -> tuple:
    return (
        snapshot.snapshot_id,
        snapshot.identity_id,
        snapshot.chat_id,
        snapshot.branch_id,
        snapshot.local_day,
        int(snapshot.source_watermark),
        snapshot.policy_version,
        snapshot.source_hash,
        snapshot.status,
        snapshot.created_at,
    )


def _member_row(snapshot_id: str, member: SourceMember) -> tuple:
    return (
        snapshot_id,
        int(member.seq),
        member.source_kind,
        member.source_ref,
        member.source_revision,
        member.role,
        member.content_hash,
        member.span_start,
        member.span_end,
        int(member.logical_size),
        member.created_at,
        member.branch_id,
    )


def _assert_snapshot_identity(conn: sqlite3.Connection, snapshot: SourceSnapshot) -> None:
    row = conn.execute(
        'SELECT identity_id, chat_id, branch_id, local_day, source_watermark, '
        'source_policy_version, source_hash, status, created_at '
        'FROM continuity_source_snapshots WHERE snapshot_id=?',
        (snapshot.snapshot_id,),
    ).fetchone()
    if row is None:
        conn.execute(
            'INSERT INTO continuity_source_snapshots '
            '(snapshot_id, identity_id, chat_id, branch_id, local_day, source_watermark, '
            'source_policy_version, source_hash, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
            _snapshot_row(snapshot),
        )
        conn.executemany(
            'INSERT INTO continuity_source_members '
            '(snapshot_id, seq, source_kind, source_ref, source_revision, role, content_hash, '
            'span_start, span_end, logical_size, created_at, branch_id) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (_member_row(snapshot.snapshot_id, member) for member in snapshot.members),
        )
        return

    stored = tuple(row[:7])
    requested = (
        snapshot.identity_id,
        snapshot.chat_id,
        snapshot.branch_id,
        snapshot.local_day,
        int(snapshot.source_watermark),
        snapshot.policy_version,
        snapshot.source_hash,
    )
    if stored != requested:
        raise ContinuityStoreConflict('source snapshot identity conflict')
    stored_members = conn.execute(
        'SELECT seq, source_kind, source_ref, source_revision, role, content_hash, '
        'span_start, span_end, logical_size, created_at, branch_id '
        'FROM continuity_source_members WHERE snapshot_id=? ORDER BY seq',
        (snapshot.snapshot_id,),
    ).fetchall()
    requested_members = tuple(
        row[1:] for row in (_member_row(snapshot.snapshot_id, member) for member in snapshot.members)
    )
    if tuple(tuple(item) for item in stored_members) != requested_members:
        raise ContinuityStoreConflict('source snapshot membership conflict')


def save_source_snapshot(conn: sqlite3.Connection, snapshot: SourceSnapshot) -> None:
    """Persist one immutable snapshot, rejecting any identity or membership drift."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        _assert_snapshot_identity(conn, snapshot)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _job_identity(snapshot: SourceSnapshot, policy: SealingPolicy) -> tuple[str, str]:
    digest = _sha256({
        'snapshot_id': snapshot.snapshot_id,
        'source_hash': snapshot.source_hash,
        'policy_version': policy.version,
    })
    return f'job:{digest[:32]}', f'continuity:{digest}'


def _job_from_row(row: sqlite3.Row | tuple) -> ContinuityJob:
    values = tuple(row)
    return ContinuityJob(
        job_id=str(values[0]),
        idempotency_key=str(values[1]),
        snapshot_id=str(values[2]),
        policy_version=str(values[3]),
        source_hash=str(values[4]),
        status=str(values[5]),
        candidate_count=int(values[6]),
    )


def enqueue_job(
    conn: sqlite3.Connection,
    snapshot: SourceSnapshot,
    policy: SealingPolicy,
    *,
    now: str | None = None,
) -> ContinuityJob:
    """Atomically persist the snapshot and idempotently enqueue one shadow job."""
    job_id, idempotency_key = _job_identity(snapshot, policy)
    stamp = str(now or _stamp())
    conn.execute('BEGIN IMMEDIATE')
    try:
        _assert_snapshot_identity(conn, snapshot)
        row = conn.execute(
            'SELECT job_id, idempotency_key, snapshot_id, policy_version, source_hash, '
            'status, candidate_count FROM continuity_jobs '
            'WHERE snapshot_id=? AND policy_version=?',
            (snapshot.snapshot_id, policy.version),
        ).fetchone()
        if row is None:
            conn.execute(
                'INSERT INTO continuity_jobs '
                '(job_id, idempotency_key, snapshot_id, policy_version, source_hash, '
                'status, candidate_count, created_at, updated_at) '
                "VALUES (?,?,?,?,?,'pending',0,?,?)",
                (job_id, idempotency_key, snapshot.snapshot_id, policy.version,
                 snapshot.source_hash, stamp, stamp),
            )
            row = conn.execute(
                'SELECT job_id, idempotency_key, snapshot_id, policy_version, source_hash, '
                'status, candidate_count FROM continuity_jobs WHERE job_id=?',
                (job_id,),
            ).fetchone()
        elif tuple(row)[1] != idempotency_key or tuple(row)[4] != snapshot.source_hash:
            raise ContinuityStoreConflict('continuity job identity conflict')
        conn.commit()
        return _job_from_row(row)
    except Exception:
        conn.rollback()
        raise


def _load_snapshot_for_job(conn: sqlite3.Connection, job: ContinuityJob) -> SourceSnapshot:
    row = conn.execute(
        'SELECT snapshot_id, identity_id, chat_id, branch_id, local_day, source_watermark, '
        'source_policy_version, source_hash, status, created_at '
        'FROM continuity_source_snapshots WHERE snapshot_id=?',
        (job.snapshot_id,),
    ).fetchone()
    if row is None:
        raise ContinuityStoreError('source snapshot missing for job')
    members = conn.execute(
        'SELECT seq, source_kind, source_ref, source_revision, role, content_hash, '
        'span_start, span_end, logical_size, created_at, branch_id '
        'FROM continuity_source_members WHERE snapshot_id=? ORDER BY seq',
        (job.snapshot_id,),
    ).fetchall()
    return SourceSnapshot(
        snapshot_id=str(row[0]),
        identity_id=str(row[1]),
        chat_id=str(row[2]),
        branch_id=str(row[3]),
        local_day=str(row[4]),
        source_watermark=int(row[5]),
        policy_version=str(row[6]),
        source_hash=str(row[7]),
        status=str(row[8]),
        created_at=str(row[9]),
        members=tuple(SourceMember(
            seq=int(item[0]), source_kind=item[1], source_ref=str(item[2]),
            source_revision=str(item[3]), role=str(item[4]), content_hash=str(item[5]),
            span_start=item[6], span_end=item[7], logical_size=int(item[8]),
            created_at=str(item[9]), branch_id=str(item[10]),
        ) for item in members),
    )


def load_job(conn: sqlite3.Connection, job_id: str) -> ContinuityJob | None:
    row = conn.execute(
        'SELECT job_id, idempotency_key, snapshot_id, policy_version, source_hash, '
        'status, candidate_count FROM continuity_jobs WHERE job_id=?',
        (job_id,),
    ).fetchone()
    return _job_from_row(row) if row is not None else None


def materialize_job(
    conn: sqlite3.Connection,
    job_id: str,
    policy: SealingPolicy,
    *,
    now: str | None = None,
) -> tuple[CandidateBlock, ...]:
    """Materialize deterministic candidate metadata; repeat calls are idempotent."""
    job = load_job(conn, job_id)
    if job is None:
        raise ContinuityStoreError('continuity job not found')
    if job.policy_version != policy.version:
        raise ContinuityStoreConflict('policy version does not match job')
    snapshot = _load_snapshot_for_job(conn, job)
    candidates = seal_snapshot(snapshot, policy)
    stamp = str(now or _stamp())

    conn.execute('BEGIN IMMEDIATE')
    try:
        for candidate in candidates:
            row = conn.execute(
                'SELECT job_id, snapshot_id, policy_version, block_seq, local_day, branch_id, '
                'source_start_seq, source_end_seq, source_revision, snapshot_source_hash, '
                'logical_size, completed_turn_count, oversize, close_reason, status '
                'FROM continuity_candidate_blocks WHERE candidate_id=?',
                (candidate.candidate_id,),
            ).fetchone()
            expected = (
                job.job_id, snapshot.snapshot_id, policy.version, candidate.block_seq,
                candidate.local_day, candidate.branch_id, candidate.source_start_seq,
                candidate.source_end_seq, candidate.source_revision, snapshot.source_hash,
                candidate.logical_size, candidate.completed_turn_count,
                int(candidate.oversize), candidate.close_reason, 'shadow',
            )
            if row is None:
                conn.execute(
                    'INSERT INTO continuity_candidate_blocks '
                    '(candidate_id, job_id, snapshot_id, policy_version, block_seq, local_day, '
                    'branch_id, source_start_seq, source_end_seq, source_revision, '
                    'snapshot_source_hash, logical_size, completed_turn_count, oversize, '
                    'close_reason, status, created_at) '
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'shadow', ?)",
                    (
                        candidate.candidate_id, job.job_id, snapshot.snapshot_id,
                        policy.version, candidate.block_seq, candidate.local_day,
                        candidate.branch_id, candidate.source_start_seq, candidate.source_end_seq,
                        candidate.source_revision, snapshot.source_hash, candidate.logical_size,
                        candidate.completed_turn_count, int(candidate.oversize),
                        candidate.close_reason, stamp,
                    ),
                )
                conn.executemany(
                    'INSERT INTO continuity_candidate_members '
                    '(candidate_id, ordinal, source_seq, source_ref, source_revision, '
                    'content_hash, logical_size) VALUES (?,?,?,?,?,?,?)',
                    (
                        (candidate.candidate_id, ordinal, seq, ref, revision, revision,
                         int(snapshot.members[seq].logical_size))
                        for ordinal, (seq, ref, revision)
                        in enumerate(zip(candidate.source_seqs, candidate.source_refs, candidate.source_revisions))
                    ),
                )
            elif tuple(row) != expected:
                raise ContinuityStoreConflict('candidate identity conflict')
            else:
                stored_members = conn.execute(
                    'SELECT candidate_id, ordinal, source_seq, source_ref, source_revision, '
                    'content_hash, logical_size FROM continuity_candidate_members '
                    'WHERE candidate_id=? ORDER BY ordinal',
                    (candidate.candidate_id,),
                ).fetchall()
                expected_members = tuple(
                    (
                        candidate.candidate_id,
                        ordinal,
                        seq,
                        ref,
                        revision,
                        revision,
                        int(snapshot.members[seq].logical_size),
                    )
                    for ordinal, (seq, ref, revision)
                    in enumerate(zip(
                        candidate.source_seqs,
                        candidate.source_refs,
                        candidate.source_revisions,
                    ))
                )
                if tuple(tuple(item) for item in stored_members) != expected_members:
                    raise ContinuityStoreConflict('candidate membership conflict')

        stored_candidate_ids = {
            str(item[0]) for item in conn.execute(
                'SELECT candidate_id FROM continuity_candidate_blocks WHERE job_id=?',
                (job.job_id,),
            ).fetchall()
        }
        if stored_candidate_ids != {candidate.candidate_id for candidate in candidates}:
            raise ContinuityStoreConflict('candidate set conflict')

        conn.execute(
            "UPDATE continuity_jobs SET status='shadow', candidate_count=?, updated_at=? "
            'WHERE job_id=?',
            (len(candidates), stamp, job.job_id),
        )
        conn.commit()
        return candidates
    except Exception:
        conn.rollback()
        raise


def load_candidates(conn: sqlite3.Connection, job_id: str) -> tuple[CandidateBlock, ...]:
    rows = conn.execute(
        'SELECT candidate_id, snapshot_id, policy_version, block_seq, local_day, branch_id, '
        'source_start_seq, source_end_seq, source_revision, logical_size, '
        'completed_turn_count, oversize, close_reason '
        'FROM continuity_candidate_blocks WHERE job_id=? ORDER BY block_seq',
        (job_id,),
    ).fetchall()
    output: list[CandidateBlock] = []
    for row in rows:
        members = conn.execute(
            'SELECT source_seq, source_ref, source_revision FROM continuity_candidate_members '
            'WHERE candidate_id=? ORDER BY ordinal',
            (row[0],),
        ).fetchall()
        seqs = tuple(int(item[0]) for item in members)
        refs = tuple(str(item[1]) for item in members)
        revisions = tuple(str(item[2]) for item in members)
        output.append(CandidateBlock(
            candidate_id=str(row[0]), snapshot_id=str(row[1]), policy_version=str(row[2]),
            block_seq=int(row[3]), local_day=str(row[4]), branch_id=str(row[5]),
            source_start_seq=int(row[6]), source_end_seq=int(row[7]), source_seqs=seqs,
            source_refs=refs, source_revisions=revisions, logical_size=int(row[9]),
            completed_turn_count=int(row[10]), oversize=bool(row[11]),
            close_reason=str(row[12]), source_revision=str(row[8]),
        ))
    return tuple(output)

