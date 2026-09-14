"""SQLite persistence and read-only artifact validation for Continuity.

The store owns durable source, candidate, generation-job, and chunk records.
It remains path-agnostic: callers provide connections (or an explicit path
for the read-only surface), while source discovery and runtime assembly stay
outside this module.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

from continuity.contracts import (
    ContinuityChunk,
    ContinuityGenerationJob,
    SourceMember,
    SourceSnapshot,
    candidate_source_revision,
)
from continuity.coverage import source_hash
from continuity.sealing import CandidateBlock, SealingPolicy, policy_identity, seal_snapshot
from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1


TABLES = (
    'continuity_source_snapshots',
    'continuity_source_members',
    'continuity_jobs',
    'continuity_candidate_blocks',
    'continuity_candidate_members',
    'continuity_generation_jobs',
    'continuity_chunks',
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


GENERATION_JOB_STATUSES = ('pending', 'generating', 'failed', 'stale', 'ready')
CHUNK_STATUSES = ('ready', 'stale', 'superseded')

READY_SURFACE_STATUSES = ('ready', 'empty', 'unavailable', 'corrupt')


@dataclass(frozen=True)
class ContinuityReadySurface:
    """Validated production chunk surface returned without mutating storage."""

    status: str
    error_code: str | None = None
    artifacts: tuple[ContinuityChunk, ...] = ()


_REQUIRED_SCHEMA_COLUMNS = {
    'continuity_source_snapshots': {
        'snapshot_id', 'identity_id', 'chat_id', 'branch_id', 'local_day',
        'source_watermark', 'source_policy_version', 'source_hash', 'status',
        'created_at',
    },
    'continuity_source_members': {
        'snapshot_id', 'seq', 'source_kind', 'source_ref', 'source_revision',
        'role', 'content_hash', 'span_start', 'span_end', 'logical_size',
        'created_at', 'branch_id',
    },
    'continuity_jobs': {
        'job_id', 'idempotency_key', 'snapshot_id', 'policy_version', 'source_hash',
        'status', 'candidate_count', 'error_code', 'created_at', 'updated_at',
    },
    'continuity_candidate_blocks': {
        'candidate_id', 'job_id', 'snapshot_id', 'policy_version', 'block_seq',
        'local_day', 'branch_id', 'source_start_seq', 'source_end_seq',
        'source_revision', 'snapshot_source_hash', 'logical_size',
        'completed_turn_count', 'oversize', 'close_reason', 'status', 'created_at',
    },
    'continuity_candidate_members': {
        'candidate_id', 'ordinal', 'source_seq', 'source_ref', 'source_revision',
        'content_hash', 'logical_size',
    },
    'continuity_generation_jobs': {
        'generation_job_id', 'idempotency_key', 'candidate_id', 'snapshot_id',
        'candidate_source_revision', 'generator_policy_version',
        'prompt_policy_version', 'measurement_semantics', 'frozen_provider',
        'frozen_model_identity', 'status', 'attempt', 'error_code', 'generation_id',
        'created_at', 'updated_at',
    },
    'continuity_chunks': {
        'chunk_id', 'generation_job_id', 'candidate_id', 'snapshot_id',
        'artifact_revision', 'body', 'body_hash', 'source_token_estimate',
        'output_token_estimate', 'generator_policy_version',
        'prompt_policy_version', 'provider', 'model_identity', 'actual_executor',
        'generation_id', 'status', 'created_at',
    },
}

_REQUIRED_PRIMARY_KEYS = {
    'continuity_source_snapshots': ('snapshot_id',),
    'continuity_source_members': ('snapshot_id', 'seq'),
    'continuity_jobs': ('job_id',),
    'continuity_candidate_blocks': ('candidate_id',),
    'continuity_candidate_members': ('candidate_id', 'ordinal'),
    'continuity_generation_jobs': ('generation_job_id',),
    'continuity_chunks': ('chunk_id',),
}

_REQUIRED_UNIQUES = {
    'continuity_jobs': {('idempotency_key',), ('snapshot_id', 'policy_version')},
    'continuity_candidate_blocks': {('job_id', 'block_seq')},
    'continuity_candidate_members': {('candidate_id', 'source_seq')},
    'continuity_generation_jobs': {('idempotency_key',)},
    'continuity_chunks': {('candidate_id', 'artifact_revision')},
}

_REQUIRED_FOREIGN_KEYS = {
    'continuity_source_members': {('snapshot_id', 'continuity_source_snapshots', 'snapshot_id')},
    'continuity_jobs': {('snapshot_id', 'continuity_source_snapshots', 'snapshot_id')},
    'continuity_candidate_blocks': {
        ('job_id', 'continuity_jobs', 'job_id'),
        ('snapshot_id', 'continuity_source_snapshots', 'snapshot_id'),
    },
    'continuity_candidate_members': {
        ('candidate_id', 'continuity_candidate_blocks', 'candidate_id'),
    },
    'continuity_generation_jobs': {
        ('candidate_id', 'continuity_candidate_blocks', 'candidate_id'),
        ('snapshot_id', 'continuity_source_snapshots', 'snapshot_id'),
    },
    'continuity_chunks': {
        ('generation_job_id', 'continuity_generation_jobs', 'generation_job_id'),
        ('candidate_id', 'continuity_candidate_blocks', 'candidate_id'),
        ('snapshot_id', 'continuity_source_snapshots', 'snapshot_id'),
    },
}

_IMMUTABLE_CHUNK_COLUMNS = (
    'chunk_id', 'generation_job_id', 'candidate_id', 'snapshot_id',
    'artifact_revision', 'body', 'body_hash', 'source_token_estimate',
    'output_token_estimate', 'generator_policy_version',
    'prompt_policy_version', 'provider', 'model_identity', 'actual_executor',
    'generation_id', 'created_at',
)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')



def open_continuity_read_only(path: str | Path) -> sqlite3.Connection:
    """Open an explicitly selected SQLite store without creating or mutating it."""
    if path is None or not str(path).strip():
        raise ValueError('continuity store path is required')
    resolved = Path(path).expanduser().resolve()
    uri = f'file:{quote(resolved.as_posix(), safe="/:")}?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _primary_key_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return tuple(
        str(row[1])
        for row in sorted(rows, key=lambda row: int(row[5]))
        if int(row[5])
    )


def _unique_column_sets(conn: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    indexes = conn.execute(f'PRAGMA index_list("{table}")').fetchall()
    result: set[tuple[str, ...]] = set()
    for index in indexes:
        if not int(index[2]):
            continue
        name = str(index[1])
        columns = conn.execute(f'PRAGMA index_info("{name}")').fetchall()
        result.add(tuple(
            str(column[2])
            for column in sorted(columns, key=lambda row: int(row[0]))
        ))
    return result


def _foreign_key_triples(conn: sqlite3.Connection, table: str) -> set[tuple[str, str, str]]:
    rows = conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    return {(str(row[3]), str(row[2]), str(row[4])) for row in rows}


def _schema_contract_status(conn: sqlite3.Connection) -> tuple[str, str | None]:
    """Return the strict surface status without attempting schema repair."""
    names = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    expected = set(TABLES)
    if not names.intersection(expected):
        return 'unavailable', 'continuity_schema_missing'
    if not expected.issubset(names):
        return 'corrupt', 'continuity_schema_partial'
    try:
        for table, required in _REQUIRED_SCHEMA_COLUMNS.items():
            columns = {
                str(row[1])
                for row in conn.execute(f'PRAGMA table_info("{table}")')
            }
            if not required.issubset(columns):
                return 'corrupt', f'continuity_schema_missing_columns:{table}'
            if _primary_key_columns(conn, table) != _REQUIRED_PRIMARY_KEYS[table]:
                return 'corrupt', f'continuity_schema_primary_key:{table}'
            if not _REQUIRED_UNIQUES.get(table, set()).issubset(
                _unique_column_sets(conn, table)
            ):
                return 'corrupt', f'continuity_schema_unique_key:{table}'
            if not _REQUIRED_FOREIGN_KEYS.get(table, set()).issubset(
                _foreign_key_triples(conn, table)
            ):
                return 'corrupt', f'continuity_schema_foreign_key:{table}'
        trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            ('continuity_chunks_immutable_body',),
        ).fetchone()
        trigger_sql = str(trigger[0] or '').lower() if trigger is not None else ''
        if (
            not trigger_sql
            or 'before update of' not in trigger_sql
            or any(column.lower() not in trigger_sql for column in _IMMUTABLE_CHUNK_COLUMNS)
        ):
            return 'corrupt', 'continuity_schema_immutability_trigger'
    except sqlite3.Error:
        return 'corrupt', 'continuity_schema_introspection_failed'
    return 'ready', None


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the R2 schema idempotently on the caller-provided connection."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS continuity_source_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            identity_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            context_id INTEGER,
            context_epoch INTEGER,
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

        CREATE TABLE IF NOT EXISTS continuity_generation_jobs (
            generation_job_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            candidate_source_revision TEXT NOT NULL,
            generator_policy_version TEXT NOT NULL,
            prompt_policy_version TEXT NOT NULL,
            measurement_semantics TEXT NOT NULL,
            frozen_provider TEXT,
            frozen_model_identity TEXT,
            status TEXT NOT NULL CHECK (status IN ('pending', 'generating', 'failed', 'stale', 'ready')),
            attempt INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            generation_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (candidate_id) REFERENCES continuity_candidate_blocks(candidate_id),
            FOREIGN KEY (snapshot_id) REFERENCES continuity_source_snapshots(snapshot_id)
        );
        CREATE INDEX IF NOT EXISTS idx_continuity_generation_jobs_candidate
            ON continuity_generation_jobs(candidate_id);
        CREATE INDEX IF NOT EXISTS idx_continuity_generation_jobs_snapshot
            ON continuity_generation_jobs(snapshot_id);

        CREATE TABLE IF NOT EXISTS continuity_chunks (
            chunk_id TEXT PRIMARY KEY,
            generation_job_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            artifact_revision TEXT NOT NULL,
            body TEXT NOT NULL,
            body_hash TEXT NOT NULL,
            source_token_estimate INTEGER NOT NULL,
            output_token_estimate INTEGER NOT NULL,
            generator_policy_version TEXT NOT NULL,
            prompt_policy_version TEXT NOT NULL,
            provider TEXT NOT NULL,
            model_identity TEXT NOT NULL,
            actual_executor TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('ready', 'stale', 'superseded')),
            created_at TEXT NOT NULL,
            UNIQUE (candidate_id, artifact_revision),
            FOREIGN KEY (generation_job_id) REFERENCES continuity_generation_jobs(generation_job_id),
            FOREIGN KEY (candidate_id) REFERENCES continuity_candidate_blocks(candidate_id),
            FOREIGN KEY (snapshot_id) REFERENCES continuity_source_snapshots(snapshot_id)
        );
        CREATE INDEX IF NOT EXISTS idx_continuity_chunks_candidate
            ON continuity_chunks(candidate_id, artifact_revision);
        CREATE INDEX IF NOT EXISTS idx_continuity_chunks_generation_job
            ON continuity_chunks(generation_job_id);
        CREATE TRIGGER IF NOT EXISTS continuity_chunks_immutable_body
        BEFORE UPDATE OF chunk_id, generation_job_id, candidate_id, snapshot_id,
            artifact_revision, body, body_hash, source_token_estimate,
            output_token_estimate, generator_policy_version, prompt_policy_version,
            provider, model_identity, actual_executor, generation_id, created_at
        ON continuity_chunks
        BEGIN
            SELECT RAISE(ABORT, 'continuity chunk body/provenance is immutable');
        END;
        """
    )
    columns = {
        str(row[1])
        for row in conn.execute('PRAGMA table_info(continuity_source_snapshots)').fetchall()
    }
    for column, definition in (
        ('context_id', 'INTEGER'),
        ('context_epoch', 'INTEGER'),
    ):
        if column not in columns:
            conn.execute(
                f'ALTER TABLE continuity_source_snapshots ADD COLUMN {column} {definition}'
            )
    conn.commit()


def _snapshot_row(snapshot: SourceSnapshot) -> tuple:
    return (
        snapshot.snapshot_id,
        snapshot.identity_id,
        snapshot.chat_id,
        int(snapshot.context_id) if snapshot.context_id is not None else None,
        int(snapshot.context_epoch) if snapshot.context_epoch is not None else None,
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
        'SELECT identity_id, chat_id, context_id, context_epoch, branch_id, local_day, '
        'source_watermark, source_policy_version, source_hash, status, created_at '
        'FROM continuity_source_snapshots WHERE snapshot_id=?',
        (snapshot.snapshot_id,),
    ).fetchone()
    if row is None:
        conn.execute(
            'INSERT INTO continuity_source_snapshots '
            '(snapshot_id, identity_id, chat_id, context_id, context_epoch, branch_id, '
            'local_day, source_watermark, source_policy_version, source_hash, status, created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
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

    stored = tuple(row[:9])
    requested = (
        snapshot.identity_id,
        snapshot.chat_id,
        snapshot.context_id,
        snapshot.context_epoch,
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
        'policy': policy_identity(policy),
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


def _snapshot_select(conn: sqlite3.Connection) -> tuple[str, bool]:
    """Return a compatibility-aware snapshot projection for old stores."""
    columns = {
        str(row[1])
        for row in conn.execute('PRAGMA table_info(continuity_source_snapshots)').fetchall()
    }
    has_scope = {'context_id', 'context_epoch'}.issubset(columns)
    fields = ['snapshot_id', 'identity_id', 'chat_id']
    if has_scope:
        fields.extend(('context_id', 'context_epoch'))
    fields.extend((
        'branch_id', 'local_day', 'source_watermark', 'source_policy_version',
        'source_hash', 'status', 'created_at',
    ))
    return ', '.join(fields), has_scope


def _snapshot_from_row(
    row: sqlite3.Row | tuple,
    members: Iterable[SourceMember],
    *,
    has_scope: bool,
) -> SourceSnapshot:
    values = tuple(row)
    offset = 0
    snapshot_id = str(values[offset]); offset += 1
    identity_id = str(values[offset]); offset += 1
    chat_id = str(values[offset]); offset += 1
    context_id = values[offset] if has_scope else None
    context_epoch = values[offset + 1] if has_scope else None
    offset += 2 if has_scope else 0
    return SourceSnapshot(
        snapshot_id=snapshot_id,
        identity_id=identity_id,
        chat_id=chat_id,
        branch_id=str(values[offset]),
        local_day=str(values[offset + 1]),
        source_watermark=int(values[offset + 2]),
        policy_version=str(values[offset + 3]),
        source_hash=str(values[offset + 4]),
        status=str(values[offset + 5]),
        created_at=str(values[offset + 6]),
        members=tuple(members),
        context_id=int(context_id) if context_id is not None else None,
        context_epoch=int(context_epoch) if context_epoch is not None else None,
    )


def _load_snapshot_for_job(conn: sqlite3.Connection, job: ContinuityJob) -> SourceSnapshot:
    projection, has_scope = _snapshot_select(conn)
    row = conn.execute(
        f'SELECT {projection} '
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
    return _snapshot_from_row(row, (
        SourceMember(
            seq=int(item[0]), source_kind=item[1], source_ref=str(item[2]),
            source_revision=str(item[3]), role=str(item[4]), content_hash=str(item[5]),
            span_start=item[6], span_end=item[7], logical_size=int(item[8]),
            created_at=str(item[9]), branch_id=str(item[10]),
        ) for item in members
    ), has_scope=has_scope)


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
    include_end_of_snapshot: bool = True,
    close_partial_before_day: str | None = None,
) -> tuple[CandidateBlock, ...]:
    """Materialize deterministic candidate metadata; repeat calls are idempotent."""
    job = load_job(conn, job_id)
    if job is None:
        raise ContinuityStoreError('continuity job not found')
    if job.policy_version != policy.version:
        raise ContinuityStoreConflict('policy version does not match job')
    snapshot = _load_snapshot_for_job(conn, job)
    expected_job_id, expected_idempotency_key = _job_identity(snapshot, policy)
    if (
        expected_job_id != job.job_id
        or expected_idempotency_key != job.idempotency_key
    ):
        raise ContinuityStoreConflict('continuity job identity does not match policy or snapshot')
    candidates = seal_snapshot(
        snapshot,
        policy,
        include_end_of_snapshot=include_end_of_snapshot,
        close_partial_before_day=close_partial_before_day,
    )
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
                        (
                            candidate.candidate_id,
                            ordinal,
                            seq,
                            ref,
                            revision,
                            snapshot.members[seq].content_hash,
                            int(snapshot.members[seq].logical_size),
                        )
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
                        snapshot.members[seq].content_hash,
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


def load_candidate(conn: sqlite3.Connection, candidate_id: str) -> CandidateBlock | None:
    row = conn.execute(
        'SELECT job_id FROM continuity_candidate_blocks WHERE candidate_id=?',
        (candidate_id,),
    ).fetchone()
    if row is None:
        return None
    candidates = load_candidates(conn, str(row[0]))
    return next((candidate for candidate in candidates if candidate.candidate_id == candidate_id), None)


def load_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> SourceSnapshot | None:
    projection, has_scope = _snapshot_select(conn)
    row = conn.execute(
        f'SELECT {projection} '
        'FROM continuity_source_snapshots WHERE snapshot_id=?',
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    members = conn.execute(
        'SELECT seq, source_kind, source_ref, source_revision, role, content_hash, '
        'span_start, span_end, logical_size, created_at, branch_id '
        'FROM continuity_source_members WHERE snapshot_id=? ORDER BY seq',
        (snapshot_id,),
    ).fetchall()
    return _snapshot_from_row(row, (
        SourceMember(
            seq=int(item[0]), source_kind=item[1], source_ref=str(item[2]),
            source_revision=str(item[3]), role=str(item[4]), content_hash=str(item[5]),
            span_start=item[6], span_end=item[7], logical_size=int(item[8]),
            created_at=str(item[9]), branch_id=str(item[10]),
        ) for item in members
    ), has_scope=has_scope)


def _generation_identity(
    candidate: CandidateBlock,
    snapshot: SourceSnapshot,
    *,
    generator_policy_version: str,
    prompt_policy_version: str,
    measurement_semantics: str,
) -> tuple[str, str, str]:
    identity = {
        'candidate_id': candidate.candidate_id,
        'snapshot_id': snapshot.snapshot_id,
        'snapshot_source_hash': snapshot.source_hash,
        'candidate_source_revision': candidate.source_revision,
        'generator_policy_version': generator_policy_version,
        'prompt_policy_version': prompt_policy_version,
        'measurement_semantics': measurement_semantics,
    }
    digest = _sha256(identity)
    return (
        f'generation-job:{digest[:32]}',
        f'continuity-generation:{digest}',
        f'generation:{digest[:32]}',
    )


def _generation_job_from_row(row: sqlite3.Row | tuple) -> ContinuityGenerationJob:
    values = tuple(row)
    return ContinuityGenerationJob(
        generation_job_id=str(values[0]),
        idempotency_key=str(values[1]),
        candidate_id=str(values[2]),
        snapshot_id=str(values[3]),
        candidate_source_revision=str(values[4]),
        generator_policy_version=str(values[5]),
        prompt_policy_version=str(values[6]),
        measurement_semantics=str(values[7]),
        frozen_provider=str(values[8]) if values[8] is not None else None,
        frozen_model_identity=str(values[9]) if values[9] is not None else None,
        status=str(values[10]),
        attempt=int(values[11]),
        error_code=str(values[12]) if values[12] is not None else None,
        generation_id=str(values[13]),
        created_at=str(values[14]),
        updated_at=str(values[15]),
    )


_GENERATION_JOB_COLUMNS = (
    'generation_job_id, idempotency_key, candidate_id, snapshot_id, '
    'candidate_source_revision, generator_policy_version, prompt_policy_version, '
    'measurement_semantics, frozen_provider, frozen_model_identity, status, attempt, '
    'error_code, generation_id, created_at, updated_at'
)


def load_generation_job(
    conn: sqlite3.Connection,
    generation_job_id: str,
) -> ContinuityGenerationJob | None:
    row = conn.execute(
        f'SELECT {_GENERATION_JOB_COLUMNS} FROM continuity_generation_jobs '
        'WHERE generation_job_id=?',
        (generation_job_id,),
    ).fetchone()
    return _generation_job_from_row(row) if row is not None else None


def load_generation_jobs(
    conn: sqlite3.Connection,
    *,
    statuses: Iterable[str] | None = None,
) -> tuple[ContinuityGenerationJob, ...]:
    """Load generation jobs in deterministic order for explicit runners."""
    normalized = None if statuses is None else tuple(str(status) for status in statuses)
    if normalized == ():
        return ()
    query = f'SELECT {_GENERATION_JOB_COLUMNS} FROM continuity_generation_jobs'
    params: tuple[object, ...] = ()
    if normalized is not None:
        invalid = set(normalized) - set(GENERATION_JOB_STATUSES)
        if invalid:
            raise ValueError('unknown generation job status: ' + ','.join(sorted(invalid)))
        query += ' WHERE status IN (' + ','.join('?' for _ in normalized) + ')'
        params = tuple(normalized)
    query += ' ORDER BY created_at ASC, generation_job_id ASC'
    return tuple(
        _generation_job_from_row(row)
        for row in conn.execute(query, params).fetchall()
    )


def load_claimed_source_revisions(
    conn: sqlite3.Connection,
    *,
    source_refs: Iterable[str] | None = None,
) -> frozenset[tuple[str, str]]:
    """Return exact source identities already claimed by immutable snapshots."""
    refs = None if source_refs is None else tuple(str(ref) for ref in source_refs)
    if refs == ():
        return frozenset()
    query = (
        'SELECT DISTINCT source_ref, source_revision '
        'FROM continuity_source_members'
    )
    params: tuple[object, ...] = ()
    if refs is not None:
        query += ' WHERE source_ref IN (' + ','.join('?' for _ in refs) + ')'
        params = tuple(refs)
    return frozenset(
        (str(row[0]), str(row[1]))
        for row in conn.execute(query, params).fetchall()
    )


def enqueue_generation_job(
    conn: sqlite3.Connection,
    candidate: CandidateBlock,
    snapshot: SourceSnapshot,
    *,
    generator_policy_version: str,
    prompt_policy_version: str,
    measurement_semantics: str,
    now: str | None = None,
) -> ContinuityGenerationJob:
    """Create one idempotent candidate-level generation job.

    This table is intentionally separate from the R2 snapshot sealing job.
    """
    generation_job_id, idempotency_key, generation_id = _generation_identity(
        candidate,
        snapshot,
        generator_policy_version=generator_policy_version,
        prompt_policy_version=prompt_policy_version,
        measurement_semantics=measurement_semantics,
    )
    stamp = str(now or _stamp())
    conn.execute('BEGIN IMMEDIATE')
    try:
        row = conn.execute(
            f'SELECT {_GENERATION_JOB_COLUMNS} FROM continuity_generation_jobs '
            'WHERE candidate_id=? AND candidate_source_revision=? AND '
            'generator_policy_version=? AND prompt_policy_version=? AND measurement_semantics=?',
            (
                candidate.candidate_id,
                candidate.source_revision,
                generator_policy_version,
                prompt_policy_version,
                measurement_semantics,
            ),
        ).fetchone()
        if row is None:
            conn.execute(
                'INSERT INTO continuity_generation_jobs '
                '(generation_job_id, idempotency_key, candidate_id, snapshot_id, '
                'candidate_source_revision, generator_policy_version, prompt_policy_version, '
                'measurement_semantics, status, attempt, generation_id, created_at, updated_at) '
                "VALUES (?,?,?,?,?,?,?,?, 'pending',0,?,?,?)",
                (
                    generation_job_id,
                    idempotency_key,
                    candidate.candidate_id,
                    snapshot.snapshot_id,
                    candidate.source_revision,
                    generator_policy_version,
                    prompt_policy_version,
                    measurement_semantics,
                    generation_id,
                    stamp,
                    stamp,
                ),
            )
            row = conn.execute(
                f'SELECT {_GENERATION_JOB_COLUMNS} FROM continuity_generation_jobs '
                'WHERE generation_job_id=?',
                (generation_job_id,),
            ).fetchone()
        elif (
            tuple(row)[0] != generation_job_id
            or tuple(row)[1] != idempotency_key
            or tuple(row)[3] != snapshot.snapshot_id
        ):
            raise ContinuityStoreConflict('continuity generation job identity conflict')
        conflict = conn.execute(
            f'SELECT {_GENERATION_JOB_COLUMNS} FROM continuity_generation_jobs '
            'WHERE idempotency_key=?',
            (idempotency_key,),
        ).fetchone()
        if conflict is not None and tuple(conflict) != tuple(row):
            raise ContinuityStoreConflict('continuity generation idempotency conflict')
        conn.commit()
        return _generation_job_from_row(row)
    except Exception:
        conn.rollback()
        raise


def claim_generation_job(
    conn: sqlite3.Connection,
    generation_job_id: str,
    *,
    frozen_provider: str,
    frozen_model_identity: str,
    now: str | None = None,
) -> ContinuityGenerationJob:
    """Freeze authority exactly once and transition a job to generating."""
    stamp = str(now or _stamp())
    conn.execute('BEGIN IMMEDIATE')
    try:
        job = load_generation_job(conn, generation_job_id)
        if job is None:
            raise ContinuityStoreError('continuity generation job not found')
        if job.status == 'ready':
            conn.commit()
            return job
        if job.status == 'generating':
            if (
                job.frozen_provider != frozen_provider
                or job.frozen_model_identity != frozen_model_identity
            ):
                raise ContinuityStoreConflict('generation job authority is already frozen')
            raise ContinuityStoreConflict('continuity generation job is already generating')
        if job.status == 'stale':
            raise ContinuityStoreConflict('stale generation job cannot be retried')
        if job.frozen_provider is not None or job.frozen_model_identity is not None:
            if (
                job.frozen_provider != frozen_provider
                or job.frozen_model_identity != frozen_model_identity
            ):
                raise ContinuityStoreConflict('generation job authority drift')
        conn.execute(
            'UPDATE continuity_generation_jobs SET frozen_provider=?, '
            'frozen_model_identity=?, status=\'generating\', attempt=attempt+1, '
            'error_code=NULL, updated_at=? WHERE generation_job_id=?',
            (frozen_provider, frozen_model_identity, stamp, generation_job_id),
        )
        conn.commit()
        result = load_generation_job(conn, generation_job_id)
        if result is None:
            raise ContinuityStoreError('generation job disappeared after claim')
        return result
    except Exception:
        conn.rollback()
        raise


def mark_generation_failed(
    conn: sqlite3.Connection,
    generation_job_id: str,
    error_code: str,
    *,
    now: str | None = None,
) -> ContinuityGenerationJob:
    stamp = str(now or _stamp())
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute(
            'UPDATE continuity_generation_jobs SET status=\'failed\', error_code=?, '
            'updated_at=? WHERE generation_job_id=? AND status != \'ready\'',
            (str(error_code), stamp, generation_job_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    job = load_generation_job(conn, generation_job_id)
    if job is None:
        raise ContinuityStoreError('continuity generation job not found')
    return job


def mark_generation_stale(
    conn: sqlite3.Connection,
    generation_job_id: str,
    *,
    error_code: str = 'source_stale',
    now: str | None = None,
) -> ContinuityGenerationJob:
    stamp = str(now or _stamp())
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute(
            'UPDATE continuity_generation_jobs SET status=\'stale\', error_code=?, '
            'updated_at=? WHERE generation_job_id=? AND status != \'ready\'',
            (str(error_code), stamp, generation_job_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    job = load_generation_job(conn, generation_job_id)
    if job is None:
        raise ContinuityStoreError('continuity generation job not found')
    return job


def load_chunk(conn: sqlite3.Connection, chunk_id: str) -> ContinuityChunk | None:
    row = conn.execute(
        'SELECT chunk_id, generation_job_id, candidate_id, snapshot_id, artifact_revision, '
        'body, body_hash, source_token_estimate, output_token_estimate, '
        'generator_policy_version, prompt_policy_version, provider, model_identity, '
        'actual_executor, generation_id, status, created_at '
        'FROM continuity_chunks WHERE chunk_id=?',
        (chunk_id,),
    ).fetchone()
    if row is None:
        return None
    values = tuple(row)
    text_value = lambda value: '' if value is None else str(value)
    return ContinuityChunk(
        chunk_id=text_value(values[0]), generation_job_id=text_value(values[1]),
        candidate_id=text_value(values[2]), snapshot_id=text_value(values[3]),
        artifact_revision=text_value(values[4]), body=text_value(values[5]),
        body_hash=text_value(values[6]), source_token_estimate=int(values[7]),
        output_token_estimate=int(values[8]), generator_policy_version=text_value(values[9]),
        prompt_policy_version=text_value(values[10]), provider=text_value(values[11]),
        model_identity=text_value(values[12]), actual_executor=text_value(values[13]),
        generation_id=text_value(values[14]), status=text_value(values[15]),
        created_at=text_value(values[16]),
    )



def _artifact_revision(
    *,
    generation_id: str,
    body_hash: str,
    provider: str,
    model_identity: str,
    actual_executor: str,
) -> str:
    """Canonical immutable artifact revision used by writer and readers."""
    return _sha256({
        'generation_id': generation_id,
        'body_hash': body_hash,
        'provider': provider,
        'model_identity': model_identity,
        'actual_executor': actual_executor,
    })[:32]


def _corrupt(code: str) -> None:
    raise ContinuityStoreConflict(code)


def _validate_ready_chunk(conn: sqlite3.Connection, chunk: ContinuityChunk) -> None:
    """Validate one complete, internally consistent ready artifact."""
    if chunk.status != 'ready':
        _corrupt('chunk_not_ready')
    for field in (
        'chunk_id', 'generation_job_id', 'candidate_id', 'snapshot_id',
        'artifact_revision', 'body_hash', 'generator_policy_version',
        'prompt_policy_version', 'provider', 'model_identity',
        'actual_executor', 'generation_id',
    ):
        if not str(getattr(chunk, field) or '').strip():
            _corrupt(f'chunk_missing_{field}')
    if not chunk.body:
        _corrupt('chunk_body_empty')
    if hashlib.sha256(chunk.body.encode('utf-8')).hexdigest() != chunk.body_hash:
        _corrupt('chunk_body_hash_mismatch')
    if int(chunk.source_token_estimate) <= 0:
        _corrupt('chunk_source_token_estimate_invalid')
    expected_output_tokens = estimate_tokens_heuristic_cjk1_ascii4_v1(chunk.body)
    if expected_output_tokens <= 0 or int(chunk.output_token_estimate) != expected_output_tokens:
        _corrupt('chunk_output_token_estimate_mismatch')

    job = load_generation_job(conn, chunk.generation_job_id)
    if job is None:
        _corrupt('generation_job_missing')
    if job.status != 'ready':
        _corrupt('generation_job_not_ready')
    if (
        job.generation_job_id != chunk.generation_job_id
        or job.candidate_id != chunk.candidate_id
        or job.snapshot_id != chunk.snapshot_id
        or job.generation_id != chunk.generation_id
        or job.generator_policy_version != chunk.generator_policy_version
        or job.prompt_policy_version != chunk.prompt_policy_version
        or not job.measurement_semantics
        or not job.frozen_provider
        or not job.frozen_model_identity
        or job.frozen_provider != chunk.provider
        or job.frozen_model_identity != chunk.model_identity
    ):
        _corrupt('generation_job_provenance_mismatch')

    candidate_row = conn.execute(
        'SELECT job_id, snapshot_id, policy_version, source_revision, '
        'snapshot_source_hash, status FROM continuity_candidate_blocks WHERE candidate_id=?',
        (chunk.candidate_id,),
    ).fetchone()
    if candidate_row is None:
        _corrupt('candidate_missing')
    if str(candidate_row[1]) != chunk.snapshot_id:
        _corrupt('candidate_snapshot_lineage_mismatch')

    snapshot = load_snapshot(conn, chunk.snapshot_id)
    candidate = load_candidate(conn, chunk.candidate_id)
    if snapshot is None:
        _corrupt('snapshot_missing')
    if candidate is None:
        _corrupt('candidate_missing')
    if str(candidate_row[5]) != 'shadow':
        _corrupt('candidate_not_materialized')
    if snapshot.source_hash != source_hash(snapshot.members):
        _corrupt('snapshot_source_hash_mismatch')
    if str(candidate_row[4]) != snapshot.source_hash:
        _corrupt('candidate_snapshot_hash_mismatch')
    if candidate.snapshot_id != snapshot.snapshot_id or candidate.source_revision != job.candidate_source_revision:
        _corrupt('candidate_revision_lineage_mismatch')

    sealing_job = load_job(conn, str(candidate_row[0]))
    if sealing_job is None:
        _corrupt('sealing_job_missing')
    if sealing_job.status != 'shadow':
        _corrupt('sealing_job_not_materialized')
    if (
        sealing_job.snapshot_id != candidate.snapshot_id
        or sealing_job.snapshot_id != snapshot.snapshot_id
    ):
        _corrupt('sealing_job_snapshot_mismatch')
    if sealing_job.policy_version != candidate.policy_version:
        _corrupt('sealing_job_policy_mismatch')
    if sealing_job.source_hash != snapshot.source_hash:
        _corrupt('sealing_job_source_hash_mismatch')

    member_rows = conn.execute(
        'SELECT ordinal, source_seq, source_ref, source_revision, content_hash, logical_size '
        'FROM continuity_candidate_members WHERE candidate_id=? ORDER BY ordinal',
        (candidate.candidate_id,),
    ).fetchall()
    if len(member_rows) != len(candidate.source_seqs):
        _corrupt('candidate_member_count_mismatch')
    source_by_seq = {int(member.seq): member for member in snapshot.members}
    exact_members: list[SourceMember] = []
    for ordinal, row in enumerate(member_rows):
        if int(row[0]) != ordinal:
            _corrupt('candidate_member_ordinal_mismatch')
        seq = int(row[1])
        if (
            seq != candidate.source_seqs[ordinal]
            or str(row[2]) != candidate.source_refs[ordinal]
            or str(row[3]) != candidate.source_revisions[ordinal]
        ):
            _corrupt('candidate_member_identity_mismatch')
        member = source_by_seq.get(seq)
        if member is None or member.source_ref != str(row[2]) or member.source_revision != str(row[3]):
            _corrupt('candidate_member_source_missing')
        if member.content_hash != str(row[4]) or int(member.logical_size) != int(row[5]):
            _corrupt('candidate_member_measurement_mismatch')
        exact_members.append(member)
    if candidate_source_revision(tuple(exact_members)) != candidate.source_revision:
        _corrupt('candidate_source_revision_mismatch')

    artifact_revision = _artifact_revision(
        generation_id=job.generation_id,
        body_hash=chunk.body_hash,
        provider=chunk.provider,
        model_identity=chunk.model_identity,
        actual_executor=chunk.actual_executor,
    )
    if chunk.artifact_revision != artifact_revision or chunk.chunk_id != f'chunk:{artifact_revision}':
        _corrupt('artifact_revision_mismatch')


def _read_ready_surface_connection(conn: sqlite3.Connection) -> ContinuityReadySurface:
    orphaned_ready_job = conn.execute(
        "SELECT 1 FROM continuity_generation_jobs AS job "
        "WHERE job.status='ready' AND NOT EXISTS ("
        "SELECT 1 FROM continuity_chunks AS chunk "
        "WHERE chunk.generation_job_id=job.generation_job_id AND chunk.status='ready')"
    ).fetchone()
    if orphaned_ready_job is not None:
        _corrupt('ready_generation_job_without_chunk')
    rows = conn.execute(
        "SELECT chunk_id FROM continuity_chunks WHERE status='ready' ORDER BY chunk_id ASC"
    ).fetchall()
    if not rows:
        return ContinuityReadySurface(status='empty')
    chunks: list[ContinuityChunk] = []
    generation_jobs: set[str] = set()
    candidates: set[str] = set()
    for row in rows:
        chunk = load_chunk(conn, str(row[0]))
        if chunk is None:
            _corrupt('ready_chunk_disappeared')
        if chunk.generation_job_id in generation_jobs:
            _corrupt('multiple_ready_chunks_for_generation_job')
        if chunk.candidate_id in candidates:
            _corrupt('competing_ready_chunks_for_candidate')
        _validate_ready_chunk(conn, chunk)
        generation_jobs.add(chunk.generation_job_id)
        candidates.add(chunk.candidate_id)
        chunks.append(chunk)
    return ContinuityReadySurface(status='ready', artifacts=tuple(chunks))


def read_ready_surface(path: str | Path) -> ContinuityReadySurface:
    """Read the strict production chunk surface from an explicit SQLite path."""
    try:
        conn = open_continuity_read_only(path)
    except (OSError, sqlite3.Error, ValueError):
        return ContinuityReadySurface(status='unavailable', error_code='continuity_store_unavailable')
    try:
        try:
            status, error_code = _schema_contract_status(conn)
        except sqlite3.Error:
            return ContinuityReadySurface(status='corrupt', error_code='continuity_schema_read_error')
        if status != 'ready':
            return ContinuityReadySurface(status=status, error_code=error_code)
        try:
            return _read_ready_surface_connection(conn)
        except ContinuityStoreConflict as exc:
            return ContinuityReadySurface(status='corrupt', error_code=str(exc))
        except (sqlite3.Error, ValueError, TypeError):
            return ContinuityReadySurface(status='corrupt', error_code='continuity_store_read_error')
    finally:
        conn.close()


def load_ready_chunk_for_job(
    conn: sqlite3.Connection,
    generation_job_id: str,
) -> ContinuityChunk | None:
    row = conn.execute(
        'SELECT chunk_id FROM continuity_chunks WHERE generation_job_id=? AND status=\'ready\' '
        'ORDER BY artifact_revision DESC LIMIT 1',
        (generation_job_id,),
    ).fetchone()
    return load_chunk(conn, str(row[0])) if row is not None else None


def load_ready_chunks(conn: sqlite3.Connection) -> tuple[ContinuityChunk, ...]:
    """Read every ready chunk in deterministic order without changing the store."""
    rows = conn.execute(
        "SELECT chunk_id FROM continuity_chunks WHERE status='ready' "
        'ORDER BY chunk_id ASC'
    ).fetchall()
    chunks: list[ContinuityChunk] = []
    for row in rows:
        chunk = load_chunk(conn, str(row[0]))
        if chunk is None:
            raise ContinuityStoreError('ready chunk disappeared during read')
        chunks.append(chunk)
    return tuple(chunks)


def publish_chunk_atomic(
    conn: sqlite3.Connection,
    *,
    job: ContinuityGenerationJob,
    candidate: CandidateBlock,
    body: str,
    body_hash: str,
    source_token_estimate: int,
    output_token_estimate: int,
    provider: str,
    model_identity: str,
    actual_executor: str,
    now: str | None = None,
) -> ContinuityChunk:
    """Insert an immutable ready chunk and mark its job ready atomically."""
    if job.status != 'generating':
        raise ContinuityStoreConflict('generation job is not generating')
    if job.frozen_provider != provider or job.frozen_model_identity != model_identity:
        raise ContinuityStoreConflict('chunk provenance does not match frozen authority')
    stamp = str(now or _stamp())
    artifact_revision = _artifact_revision(
        generation_id=job.generation_id,
        body_hash=body_hash,
        provider=provider,
        model_identity=model_identity,
        actual_executor=actual_executor,
    )
    chunk_id = f'chunk:{artifact_revision}'
    conn.execute('BEGIN IMMEDIATE')
    try:
        existing = conn.execute(
            'SELECT chunk_id, generation_job_id, candidate_id, snapshot_id, artifact_revision, '
            'body, body_hash, source_token_estimate, output_token_estimate, '
            'generator_policy_version, prompt_policy_version, provider, model_identity, '
            'actual_executor, generation_id, status, created_at '
            'FROM continuity_chunks WHERE candidate_id=? AND artifact_revision=?',
            (candidate.candidate_id, artifact_revision),
        ).fetchone()
        if existing is not None:
            stored = load_chunk(conn, str(existing[0]))
            expected = (
                str(existing[1]), str(existing[2]), str(existing[3]), str(existing[4]),
                str(existing[5]), str(existing[6]), int(existing[7]), int(existing[8]),
                str(existing[11]), str(existing[12]), str(existing[13]), str(existing[14]),
            )
            requested = (
                job.generation_job_id, candidate.candidate_id, candidate.snapshot_id,
                artifact_revision, body, body_hash, int(source_token_estimate),
                int(output_token_estimate), provider, model_identity, actual_executor,
                job.generation_id,
            )
            if stored is None or expected != requested:
                raise ContinuityStoreConflict('immutable chunk identity conflict')
            conn.execute(
                'UPDATE continuity_generation_jobs SET status=\'ready\', error_code=NULL, '
                'updated_at=? WHERE generation_job_id=?',
                (stamp, job.generation_job_id),
            )
            conn.commit()
            return stored

        conn.execute(
            'INSERT INTO continuity_chunks '
            '(chunk_id, generation_job_id, candidate_id, snapshot_id, artifact_revision, body, '
            'body_hash, source_token_estimate, output_token_estimate, generator_policy_version, '
            'prompt_policy_version, provider, model_identity, actual_executor, generation_id, '
            'status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,\'ready\',?)',
            (
                chunk_id, job.generation_job_id, candidate.candidate_id, candidate.snapshot_id,
                artifact_revision, body, body_hash, int(source_token_estimate),
                int(output_token_estimate), job.generator_policy_version,
                job.prompt_policy_version, provider, model_identity, actual_executor,
                job.generation_id, stamp,
            ),
        )
        conn.execute(
            'UPDATE continuity_generation_jobs SET status=\'ready\', error_code=NULL, '
            'updated_at=? WHERE generation_job_id=?',
            (stamp, job.generation_job_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    result = load_chunk(conn, chunk_id)
    if result is None:
        raise ContinuityStoreError('chunk disappeared after publish')
    return result

