#!/usr/bin/env python3
"""One-shot scheduled DayBlock generation into the DayBlock store.

The store defaults to the production path read by the ContextPlan DayBlock
layer (continuity.dayblock_context); HAYAGARDEN_DAYBLOCK_STORE overrides it.
Source chat rows are always read from production memories.db read-only.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
import types
import uuid
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

PRODUCTION_ROOT = Path('/opt/frontend')
# Import project modules from the checkout this runner lives in only.
REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DB = PRODUCTION_ROOT / 'memories.db'
DAYBLOCK_STORE = Path(
    os.environ.get('HAYAGARDEN_DAYBLOCK_STORE') or '/var/lib/hayagarden/dayblock.db'
)
UTC = dt.timezone.utc
SHANGHAI = ZoneInfo('Asia/Shanghai')
ERROR_DETAIL_MAX_CHARS = 500


def _iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _local_iso(value: dt.datetime) -> str:
    return value.astimezone(SHANGHAI).isoformat(timespec='seconds')


def _split_error(exc: BaseException) -> tuple[str, str | None]:
    """Split '<stable_code>:<diagnostic detail>' without discarding the detail."""
    code, _sep, detail = str(exc).partition(':')
    detail = detail.strip()
    return code[:120] or type(exc).__name__, detail[:ERROR_DETAIL_MAX_CHARS] or None


def _blocking_reasons(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or '[]')
        except (TypeError, ValueError):
            raw = []
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(str(item) for item in raw if item))


def _result(status: str, *, run_id: str, started_at: dt.datetime, source_day: str,
            model_call_count: int = 0, error_code: str | None = None, **extra: Any) -> dict[str, Any]:
    local = started_at.astimezone(SHANGHAI)
    if status == 'blocked':
        reasons = _blocking_reasons(extra.get('blocking_reasons') or ([error_code] if error_code else []))
        extra['blocking_reasons'] = reasons
        error_code = error_code or (reasons[0] if reasons else None)
    return {
        'status': status, 'run_id': run_id, 'started_at_utc': _iso(started_at),
        'started_at_shanghai': _local_iso(started_at), 'source_day': source_day,
        'source_start': f'{source_day}T00:00:00+08:00',
        'source_end': f'{(dt.date.fromisoformat(source_day) + dt.timedelta(days=1)).isoformat()}T00:00:00+08:00',
        'model_call_count': model_call_count, 'error_code': error_code,
        'schedule_local_hour': local.hour, **extra,
    }


def _read_existing(conn: sqlite3.Connection,
                   source_day: str) -> tuple[list[sqlite3.Row], dict[str, list[sqlite3.Row]]]:
    """Jobs of the day and every generation attempt of each job, oldest first."""
    jobs = conn.execute(
        'SELECT * FROM dayblock_shadow_jobs WHERE source_day=? ORDER BY created_at,job_id',
        (source_day,),
    ).fetchall()
    receipts: dict[str, list[sqlite3.Row]] = {}
    for job in jobs:
        attempts = conn.execute(
            'SELECT * FROM dayblock_generation_receipts WHERE generation_job_id=? ORDER BY attempt_no',
            (str(job['job_id']),),
        ).fetchall()
        if attempts:
            receipts[str(job['job_id'])] = list(attempts)
    return jobs, receipts


def _latest_attempt(receipts: dict[str, list[sqlite3.Row]]) -> sqlite3.Row | None:
    attempts = [row for rows in receipts.values() for row in rows]
    if not attempts:
        return None
    return max(attempts, key=lambda row: (str(row['created_at']), int(row['attempt_no'])))


def _existing_result(jobs: list[sqlite3.Row], receipts: dict[str, list[sqlite3.Row]], *,
                     run_id: str, started_at: dt.datetime, source_day: str) -> dict[str, Any] | None:
    if not jobs:
        return None
    attempts = [row for rows in receipts.values() for row in rows]
    ready = [r for r in attempts if str(r['status']) == 'ready']
    if ready:
        receipt = ready[0]
        body = str(receipt['summary_body'] or '')
        if hashlib.sha256(body.encode('utf-8')).hexdigest() != str(receipt['body_hash'] or ''):
            return _result('failed', run_id=run_id, started_at=started_at, source_day=source_day,
                           error_code='ready_receipt_body_hash_mismatch', model_call_count=0)
        job = next((j for j in jobs if str(j['job_id']) == str(receipt['generation_job_id'])), None)
        if job is None or str(job['status']) != 'ready':
            return _result('failed', run_id=run_id, started_at=started_at, source_day=source_day,
                           error_code='ready_job_receipt_mismatch', model_call_count=0)
        return _result(
            'ready', run_id=run_id, started_at=started_at, source_day=source_day,
            model_call_count=0, idempotent_replay=True, job_id=str(job['job_id']),
            candidate_id=str(job['candidate_id']), generation_id=str(receipt['generation_id']),
            provider=str(receipt['provider']), model_identity=str(receipt['model_identity']),
            actual_executor=receipt['actual_executor'], body_hash=str(receipt['body_hash']),
            summary_body=body, attempt_no=int(receipt['attempt_no']),
        )
    processing = [r for r in attempts if str(r['status']) == 'processing']
    if processing:
        receipt = processing[0]
        return _result('processing', run_id=run_id, started_at=started_at, source_day=source_day,
                       model_call_count=0, idempotent_replay=True,
                       job_id=str(receipt['generation_job_id']), generation_id=str(receipt['generation_id']),
                       attempt_no=int(receipt['attempt_no']))
    latest = _latest_attempt(receipts)
    if latest is not None and str(latest['status']) in {'failed', 'stale'}:
        return _result(str(latest['status']), run_id=run_id, started_at=started_at, source_day=source_day,
                       model_call_count=0, idempotent_replay=True,
                       job_id=str(latest['generation_job_id']), generation_id=str(latest['generation_id']),
                       error_code=str(latest['error_code'] or ''),
                       error_detail=latest['error_detail'], attempt_no=int(latest['attempt_no']))
    statuses = {str(row['status']) for row in jobs}
    for state in ('ready', 'blocked', 'failed', 'stale'):
        if state in statuses:
            if state == 'ready':
                # Ready without a receipt is an invariant failure and fails closed.
                return _result('failed', run_id=run_id, started_at=started_at, source_day=source_day,
                               model_call_count=0, idempotent_replay=True,
                               job_id=str(jobs[-1]['job_id']), error_code='ready_job_without_receipt')
            # The job's blocking_reasons_json stays the source of truth for why.
            matching = [row for row in jobs if str(row['status']) == state]
            reasons = _blocking_reasons(matching[-1]['blocking_reasons_json'])
            return _result(state, run_id=run_id, started_at=started_at, source_day=source_day,
                           model_call_count=0, idempotent_replay=True,
                           job_id=str(jobs[-1]['job_id']),
                           error_code=reasons[0] if reasons else None,
                           blocking_reasons=reasons)
    # An existing planned job may be a process that stopped before its atomic
    # claim. Never replace or implicitly retry it on a duplicate scheduled run.
    return _result('blocked', run_id=run_id, started_at=started_at, source_day=source_day,
                   model_call_count=0, idempotent_replay=True,
                   job_id=str(jobs[-1]['job_id']),
                   error_code='existing_unclaimed_or_non_planned_day_job')


def _block_planned_job(conn: sqlite3.Connection, job_id: str, reason: str) -> None:
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute(
            "UPDATE dayblock_shadow_jobs SET status='blocked',blocking_reasons_json=? "
            "WHERE job_id=? AND status='planned'",
            (json.dumps([reason], separators=(',', ':')), job_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _claim(conn: sqlite3.Connection, job_id: str, source_day: str, generation_id: str,
           run_id: str, planned: dict[str, Any], job: sqlite3.Row, started_at: dt.datetime, *,
           attempt_kind: str = 'scheduled', attempt_reason: str | None = None,
           expected_job_status: str = 'planned') -> bool:
    """Insert the next processing attempt for ``job_id``; False when not claimable."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        current = conn.execute('SELECT status FROM dayblock_shadow_jobs WHERE job_id=?', (job_id,)).fetchone()
        if current is None or str(current['status']) != expected_job_status:
            conn.rollback()
            return False
        live = conn.execute(
            "SELECT 1 FROM dayblock_generation_receipts WHERE source_day=? "
            "AND status IN ('processing','ready')",
            (source_day,),
        ).fetchone()
        previous = int(conn.execute(
            'SELECT COALESCE(MAX(attempt_no),0) FROM dayblock_generation_receipts WHERE generation_job_id=?',
            (job_id,),
        ).fetchone()[0])
        # A scheduled run claims only a job's first attempt; later ones are manual.
        if live or (attempt_kind == 'scheduled' and previous):
            conn.rollback()
            return False
        conn.execute(
            '''INSERT INTO dayblock_generation_receipts(
              generation_id,run_id,generation_job_id,candidate_id,source_day,source_hash,
              source_revision,source_fingerprint,provider,model_identity,authority_revision,
              authority_snapshot_id,persona_revision,prompt_policy_version,prompt_hash,
              generator_policy_version,input_mode,source_member_count,source_coverage,
              raw_token_estimate,budget_tokens,input_token_estimate,status,created_at,
              attempt_no,attempt_kind,attempt_reason)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (generation_id, run_id, job_id, str(job['candidate_id']), source_day,
             str(job['source_hash']), str(job['source_revision']),
             str(planned['materialized_source_fingerprint']), str(job['provider']),
             str(job['model_identity']), str(job['authority_revision']),
             str(job['authority_snapshot_id']), str(job['persona_revision']),
             str(job['prompt_policy_version']), str(job['prompt_hash']),
             str(job['generator_policy_version']), str(planned['generation_input_mode']),
             int(planned['source_member_count']), float(planned['source_coverage']),
             int(planned['raw_token_estimate']), int(planned['input_budget_tokens']),
             int(planned['generation_input_token_estimate']), 'processing', _local_iso(started_at),
             previous + 1, attempt_kind, attempt_reason),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Lost a race on the one-live-attempt-per-day index or the attempt number.
        conn.rollback()
        return False
    except Exception:
        conn.rollback()
        raise


def _finish(conn: sqlite3.Connection, *, generation_id: str, job_id: str,
            status: str, finished_at: dt.datetime, error_code: str | None = None,
            error_detail: str | None = None, body: str | None = None, body_hash: str | None = None,
            output_token_estimate: int = 0, actual_executor: str | None = None,
            usage: dict[str, Any] | None = None, expected_job_status: str = 'planned') -> None:
    if status not in {'ready', 'failed', 'stale'}:
        raise ValueError('invalid_final_generation_status')
    usage_json = json.dumps(usage or {}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    conn.execute('BEGIN IMMEDIATE')
    try:
        receipt = conn.execute(
            'SELECT status FROM dayblock_generation_receipts WHERE generation_id=? AND generation_job_id=?',
            (generation_id, job_id),
        ).fetchone()
        if receipt is None or str(receipt['status']) != 'processing':
            raise RuntimeError('generation_claim_not_processing')
        conn.execute(
            '''UPDATE dayblock_generation_receipts SET status=?,summary_body=?,body_hash=?,
               output_token_estimate=?,actual_executor=?,usage_json=?,error_code=?,error_detail=?,
               finished_at=?
               WHERE generation_id=? AND generation_job_id=? AND status='processing' ''',
            (status, body if status == 'ready' else None, body_hash if status == 'ready' else None,
             int(output_token_estimate), actual_executor, usage_json, error_code, error_detail,
             _local_iso(finished_at), generation_id, job_id),
        )
        conn.execute(
            'UPDATE dayblock_shadow_jobs SET status=?,blocking_reasons_json=? '
            'WHERE job_id=? AND status=?',
            (status, json.dumps([error_code] if error_code else [], separators=(',', ':')), job_id,
             expected_job_status),
        )
        updated = conn.execute('SELECT status FROM dayblock_shadow_jobs WHERE job_id=?', (job_id,)).fetchone()
        if updated is None or str(updated['status']) != status:
            raise RuntimeError('generation_job_final_state_mismatch')
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _compose_evidence(shadow: Any, members: tuple[Any, ...], materialized: Any,
                      plan: dict[str, Any]) -> str:
    refs = tuple(str(item.source_ref) for item in members)
    raw = materialized.member_bodies
    if set(raw) != set(refs):
        raise RuntimeError('materialized_membership_mismatch')
    mode = str(plan.get('mode') or '')
    selected = list(plan.get('selected_chunks') or [])
    if mode == 'direct_raw':
        if selected or tuple(plan.get('raw_refs') or ()) != refs or plan.get('source_coverage') != 1.0:
            raise RuntimeError('direct_raw_plan_invalid')
        segments = [raw[ref] for ref in refs]
    elif mode == 'mixed':
        chunks = {str(c['chunk_id']): c for c in shadow.load_ready_continuity_chunks()}
        planned_by_first: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        covered: set[str] = set()
        for frozen in selected:
            chunk_id = str(frozen.get('chunk_id') or '')
            current = chunks.get(chunk_id)
            if current is None or current.get('body_hash') != frozen.get('body_hash'):
                raise RuntimeError('selected_continuity_chunk_changed')
            chunk_refs = [str(x) for x in frozen.get('source_refs') or []]
            current_members = list(current.get('source_members') or [])
            current_refs = [str(x.get('source_ref') or '') for x in current_members]
            if not chunk_refs or current_refs != chunk_refs:
                raise RuntimeError('selected_continuity_membership_changed')
            expected = {str(m.source_ref): str(m.source_revision) for m in members}
            if any(str(x.get('source_revision') or '') != expected.get(str(x.get('source_ref') or ''))
                   for x in current_members):
                raise RuntimeError('selected_continuity_source_revision_mismatch')
            if any(ref in covered or ref not in expected for ref in chunk_refs):
                raise RuntimeError('selected_continuity_coverage_overlap')
            covered.update(chunk_refs)
            planned_by_first[chunk_refs[0]] = (frozen, current)
        planned_chunk_refs = [str(ref) for item in selected for ref in item.get('source_refs') or []]
        if set(planned_chunk_refs) != set(plan.get('chunk_refs') or []):
            raise RuntimeError('selected_continuity_plan_mismatch')
        segments = []
        represented: set[str] = set()
        for ref in refs:
            pair = planned_by_first.get(ref)
            if pair:
                frozen, current = pair
                segments.append('[CONTINUITY CHUNK ' + str(frozen['chunk_id']) + ']\n' + str(current['body']))
                represented.update(str(x) for x in frozen['source_refs'])
            elif ref in covered:
                continue
            else:
                segments.append(raw[ref])
                represented.add(ref)
        if represented != set(refs):
            raise RuntimeError('generation_input_coverage_incomplete')
        expected_raw = set(plan.get('raw_refs') or [])
        if expected_raw != (set(refs) - covered):
            raise RuntimeError('generation_raw_coverage_mismatch')
    else:
        raise RuntimeError('generation_input_mode_unsupported')
    evidence = '\n\n'.join(segments)
    return evidence


def _validate_output(result: Any, expected_provider: str, expected_model: str,
                     source_token_estimate: int) -> tuple[str, int]:
    body = str(getattr(result, 'text', '') or '').strip()
    if not body:
        raise ValueError('empty_generation_output')
    lowered = body[:300].strip().lower()
    if lowered.startswith(('error:', 'api error:', 'request failed', 'timeout:', 'http 5')):
        raise ValueError('error_text_generation_output')
    if body.startswith('{') and '"error"' in lowered[:300]:
        raise ValueError('transport_error_generation_output')
    if any(ord(char) < 32 and char not in '\n\r\t' for char in body):
        raise ValueError('control_garbage_generation_output')
    if len(body) > 100_000:
        raise ValueError('generation_output_too_long')
    if str(getattr(result, 'provider', '') or '') != expected_provider:
        raise ValueError('provider_provenance_mismatch')
    if str(getattr(result, 'model_identity', '') or '') != expected_model:
        raise ValueError('model_provenance_mismatch')
    actual_executor = str(getattr(result, 'actual_executor', '') or '')
    if not actual_executor:
        raise ValueError('executor_provenance_missing')
    from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1
    output_tokens = int(estimate_tokens_heuristic_cjk1_ascii4_v1(body))
    if output_tokens <= 0 or source_token_estimate <= 0:
        raise ValueError('token_estimate_invalid')
    return body, output_tokens


@contextlib.contextmanager
def _formal_config_read_guard(source_db: str | Path = SOURCE_DB):
    """Use the formal adapter while all its production config reads remain read-only."""
    import dayblock_shadow as shadow

    sentinel = object()
    names = ('config_store', 'chat.provider_router', 'chat.background_generation',
             'chat.cc_model', 'relay.manager')
    saved = {name: sys.modules.get(name, sentinel) for name in names}
    readonly_conn = shadow.open_source_read_only(source_db)
    readonly_config = shadow._ReadOnlyConfig(readonly_conn, shadow.ENV_PATH)
    shim = types.ModuleType('config_store')
    shim.get = readonly_config.get  # type: ignore[attr-defined]
    original_connect = sqlite3.connect

    def readonly_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        try:
            is_production = Path(os.fsdecode(database)).resolve() == PRODUCTION_ROOT / 'memories.db'
        except (TypeError, ValueError, OSError):
            is_production = False
        if not is_production:
            return original_connect(database, *args, **kwargs)
        kwargs['uri'] = True
        conn = original_connect('file:' + str(PRODUCTION_ROOT / 'memories.db') + '?mode=ro',
                                *args, **kwargs)
        conn.execute('PRAGMA query_only=ON')
        return conn

    sys.modules['config_store'] = shim
    for name in names[1:]:
        sys.modules.pop(name, None)
    sqlite3.connect = readonly_connect  # type: ignore[assignment]
    try:
        from chat.background_generation import BackgroundGenerationRequest, generate_background
        from chat.provider_router import GenerationAuthoritySnapshot
        from chat.cc_auth import read_cc_oauth_token
        yield BackgroundGenerationRequest, generate_background, GenerationAuthoritySnapshot, read_cc_oauth_token
    finally:
        sqlite3.connect = original_connect  # type: ignore[assignment]
        for name, previous in saved.items():
            if previous is sentinel:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        readonly_conn.close()


def _recheck_source(shadow: Any, source_db: str | Path, source_day: str,
                   original: dict[str, Any]) -> bool:
    rows = shadow.discover_natural_day_rows(source_db, source_day)
    if not rows:
        return False
    snapshot, _turns, _events, members = shadow.derive_source_contract(
        rows, source_day, str(original['created_at']),
    )
    from continuity.contracts import candidate_source_revision
    revision = candidate_source_revision(members)
    if snapshot.source_hash != str(original['snapshot'].source_hash):
        return False
    if revision != str(original['source_revision']):
        return False
    materialized = shadow.materialize_raw_evidence(members, rows)
    return materialized.source_fingerprint == str(original['materialized'].source_fingerprint)


def run_scheduled_dayblock(
    *, source_db: str | Path = SOURCE_DB, preview_db: str | Path = DAYBLOCK_STORE,
    started_at: dt.datetime | None = None,
    authority_capture: Callable[[], Any] | None = None,
    persona_capture: Callable[[], dict[str, Any]] | None = None,
    generate_fn: Callable[[Any, Any], Any] | None = None,
) -> dict[str, Any]:
    """Execute at most one generation for the naturally selected prior Shanghai day."""
    start = started_at or dt.datetime.now(UTC)
    start = start.replace(tzinfo=UTC) if start.tzinfo is None else start.astimezone(UTC)
    run_id = str(uuid.uuid4())
    local = start.astimezone(SHANGHAI)
    source_day = (local.date() - dt.timedelta(days=1)).isoformat()
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import dayblock_shadow as shadow

    try:
        capture = authority_capture or (lambda: shadow.capture_primary_authority(source_db))
        authority = capture()
        persona_reader = persona_capture or shadow.capture_persona_snapshot
        persona = persona_reader()
        # Frozen authority timestamp is the actual scheduled process start, as required by R2.
        authority_at = start
        persona_at = start
    except Exception as exc:
        return _result('failed', run_id=run_id, started_at=start, source_day=source_day,
                       model_call_count=0, error_code='authority_or_persona_capture_failed',
                       capture_error=type(exc).__name__)

    authority_iso = _local_iso(authority_at)
    if not shadow.scheduled_authority_capture_valid(source_day, authority_iso):
        return _result('blocked', run_id=run_id, started_at=start, source_day=source_day,
                       model_call_count=0, error_code='outside_0300_authority_window',
                       provider=str(getattr(authority, 'provider', '')),
                       model_identity=str(getattr(authority, 'model_identity', '')),
                       authority_captured_at=authority_iso,
                       persona_revision=(persona or {}).get('revision'),
                       prompt_policy_version=shadow.PROMPT_POLICY_VERSION,
                       prompt_hash=shadow.PROMPT_CONTRACT_HASH)

    if hashlib.sha256(shadow.PROMPT_CONTRACT_JSON.encode('utf-8')).hexdigest() != shadow.PROMPT_CONTRACT_HASH:
        return _result('blocked', run_id=run_id, started_at=start, source_day=source_day,
                       model_call_count=0, error_code='prompt_contract_hash_mismatch')

    store = shadow.open_preview_store(preview_db)
    try:
        existing_jobs, existing_receipts = _read_existing(store, source_day)
        already = _existing_result(existing_jobs, existing_receipts,
                                   run_id=run_id, started_at=start, source_day=source_day)
        if already is not None:
            return already

        plan = shadow.build_shadow_plan(
            source_db, preview_db, source_day, now=start.astimezone(SHANGHAI),
            capture_authority=lambda: authority,
            frozen_authority=authority, frozen_authority_captured_at=authority_iso,
            persona_snapshot=persona, persona_captured_at=_local_iso(persona_at),
            pre_captured=True, include_runtime=True,
        )
        if not plan.get('ready_to_generate'):
            clean_plan = {key: value for key, value in plan.items() if key != '_runtime'}
            reasons = _blocking_reasons(plan.get('blocking_reasons'))
            return _result(str(plan.get('status') or 'blocked'), run_id=run_id,
                           started_at=start, source_day=source_day, model_call_count=0,
                           plan=clean_plan, error_code=reasons[0] if reasons else None,
                           blocking_reasons=reasons)

        return _generate_planned_job(
            store, shadow, source_db=source_db, plan=plan, persona=persona, run_id=run_id,
            start=start, source_day=source_day, generate_fn=generate_fn,
        )
    finally:
        store.close()


def _generate_planned_job(
    store: sqlite3.Connection, shadow: Any, *, source_db: str | Path, plan: dict[str, Any],
    persona: dict[str, Any] | None, run_id: str, start: dt.datetime, source_day: str,
    generate_fn: Callable[[Any, Any], Any] | None,
    attempt_kind: str = 'scheduled', attempt_reason: str | None = None,
) -> dict[str, Any]:
    """Revalidate a freshly planned job against its frozen bindings, then run attempt 1."""
    runtime = plan.get('_runtime') or {}
    job = store.execute('SELECT * FROM dayblock_shadow_jobs WHERE job_id=?',
                        (plan.get('job_id'),)).fetchone()
    if job is None:
        return _result('failed', run_id=run_id, started_at=start, source_day=source_day,
                       model_call_count=0, error_code='planned_job_missing')
    if str(job['status']) != 'planned':
        return _result(str(job['status']), run_id=run_id, started_at=start,
                       source_day=source_day, model_call_count=0,
                       error_code='job_not_pending')
    if str(job['source_hash']) != str(plan.get('source_snapshot_hash')) or str(job['source_revision']) != str(plan.get('source_revision')):
        _block_planned_job(store, str(job['job_id']), 'frozen_source_binding_mismatch')
        return _result('blocked', run_id=run_id, started_at=start, source_day=source_day,
                       model_call_count=0, error_code='frozen_source_binding_mismatch')
    frozen = dict(plan.get('frozen_primary_authority') or {})
    if (str(job['provider']) != str(frozen.get('provider'))
            or str(job['model_identity']) != str(frozen.get('model_identity'))
            or str(job['authority_revision']) != str(frozen.get('authority_revision'))
            or str(job['authority_snapshot_id']) != str(frozen.get('authority_snapshot_id'))
            or str(job['captured_at']) != str(frozen.get('captured_at'))):
        _block_planned_job(store, str(job['job_id']), 'frozen_authority_binding_mismatch')
        return _result('blocked', run_id=run_id, started_at=start, source_day=source_day,
                       model_call_count=0, error_code='frozen_authority_binding_mismatch')
    if (str(job['prompt_policy_version']) != shadow.PROMPT_POLICY_VERSION
            or str(job['prompt_hash']) != shadow.PROMPT_CONTRACT_HASH
            or str(job['prompt_contract_json']) != shadow.PROMPT_CONTRACT_JSON
            or str(job['persona_revision'] or '') != str((persona or {}).get('revision') or '')):
        _block_planned_job(store, str(job['job_id']), 'frozen_prompt_or_persona_mismatch')
        return _result('blocked', run_id=run_id, started_at=start, source_day=source_day,
                       model_call_count=0, error_code='frozen_prompt_or_persona_mismatch')
    budget = shadow.frozen_model_context_budget(str(job['provider']), str(job['model_identity']))
    input_plan = plan.get('input_plan') or {}
    try:
        persisted_input_plan = json.loads(str(job['input_plan_json'] or '{}'))
    except (TypeError, ValueError):
        persisted_input_plan = {}
    canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if not persisted_input_plan or canonical(persisted_input_plan) != canonical(input_plan):
        _block_planned_job(store, str(job['job_id']), 'frozen_input_plan_mismatch')
        return _result('blocked', run_id=run_id, started_at=start, source_day=source_day,
                       model_call_count=0, error_code='frozen_input_plan_mismatch')
    if (budget is None or int(job['raw_budget_tokens'] or 0) != budget
            or int(input_plan.get('budget_tokens') or 0) != budget
            or input_plan.get('source_coverage') != 1.0
            or input_plan.get('budget_fit') is not True
            or int(input_plan.get('uncovered_source_count') or 0) != 0):
        _block_planned_job(store, str(job['job_id']), 'input_readiness_revalidation_failed')
        return _result('blocked', run_id=run_id, started_at=start, source_day=source_day,
                       model_call_count=0, error_code='input_readiness_revalidation_failed')
    if not runtime or runtime.get('persona') is None or runtime.get('materialized') is None:
        _block_planned_job(store, str(job['job_id']), 'planner_runtime_materialization_missing')
        return _result('blocked', run_id=run_id, started_at=start, source_day=source_day,
                       model_call_count=0, error_code='planner_runtime_materialization_missing')
    if str(runtime['persona']['revision']) != str(job['persona_revision']):
        _block_planned_job(store, str(job['job_id']), 'persona_revision_revalidation_failed')
        return _result('blocked', run_id=run_id, started_at=start, source_day=source_day,
                       model_call_count=0, error_code='persona_revision_revalidation_failed')

    try:
        evidence = _compose_evidence(shadow, runtime['members'], runtime['materialized'], input_plan)
        static_prefix = shadow.build_prompt_static_prefix(
            str(job['prompt_contract_json']),
            str(runtime['persona']['text']),
            str(job['source_day']),
        )
        planned_input_tokens = shadow._token_estimate(static_prefix + '\n\n' + evidence)
        if planned_input_tokens != int(plan['generation_input_token_estimate']):
            raise RuntimeError('generation_input_estimate_mismatch')
    except Exception as exc:
        code = str(exc).split(':', 1)[0][:120] or type(exc).__name__
        _block_planned_job(store, str(job['job_id']), code)
        return _result('blocked', run_id=run_id, started_at=start, source_day=source_day,
                       model_call_count=0, error_code=code)

    return _execute_attempt(
        store, shadow, source_db=source_db, job=job, view=plan, runtime=runtime,
        static_prefix=static_prefix, evidence=evidence, planned_input_tokens=planned_input_tokens,
        budget=budget, input_plan=input_plan, run_id=run_id, start=start, source_day=source_day,
        generate_fn=generate_fn, attempt_kind=attempt_kind, attempt_reason=attempt_reason,
        expected_job_status='planned',
    )


def _execute_attempt(
    store: sqlite3.Connection, shadow: Any, *, source_db: str | Path, job: sqlite3.Row,
    view: dict[str, Any], runtime: dict[str, Any], static_prefix: str, evidence: str,
    planned_input_tokens: int, budget: int, input_plan: dict[str, Any], run_id: str,
    start: dt.datetime, source_day: str, generate_fn: Callable[[Any, Any], Any] | None,
    attempt_kind: str, attempt_reason: str | None, expected_job_status: str,
) -> dict[str, Any]:
    """Claim one attempt for ``job``, call the frozen provider once, and finish it."""
    plan = view
    generation_id = str(uuid.uuid4())
    if not _claim(store, str(job['job_id']), source_day, generation_id, run_id,
                  plan, job, start, attempt_kind=attempt_kind, attempt_reason=attempt_reason,
                  expected_job_status=expected_job_status):
        re_jobs, re_receipts = _read_existing(store, source_day)
        replay = _existing_result(re_jobs, re_receipts,
                                  run_id=run_id, started_at=start, source_day=source_day)
        return replay or _result('processing', run_id=run_id, started_at=start,
                                 source_day=source_day, model_call_count=0,
                                 error_code='claim_already_taken')

    provider_call_count = 0
    generated = None
    try:
        if generate_fn is None:
            with _formal_config_read_guard(source_db) as formal:
                request_cls, generate_background, authority_cls, token_getter = formal
                request = request_cls(
                    system_text=static_prefix, prompt_text=evidence,
                    max_tokens_hint=8192, timeout_sec=300.0, task_kind='dayblock',
                )
                frozen_authority = authority_cls(str(job['provider']), str(job['model_identity']))
                provider_call_count = 1
                generated = generate_background(
                    request, frozen_authority, cc_token_getter=token_getter,
                )
        else:
            request = {
                'system_text': static_prefix, 'prompt_text': evidence,
                'max_tokens_hint': 8192, 'timeout_sec': 300.0, 'task_kind': 'dayblock',
            }
            frozen_authority = types.SimpleNamespace(
                provider=str(job['provider']), model_identity=str(job['model_identity']),
            )
            provider_call_count = 1
            generated = generate_fn(request, frozen_authority)
        body, output_tokens = _validate_output(
            generated, str(job['provider']), str(job['model_identity']),
            int(plan['raw_token_estimate']),
        )
        try:
            source_still_current = _recheck_source(shadow, source_db, source_day, runtime)
        except Exception:
            source_still_current = False
            stale_code = 'source_recheck_failed'
        else:
            stale_code = 'source_changed_after_generation'
        if not source_still_current:
            _finish(store, generation_id=generation_id, job_id=str(job['job_id']),
                    expected_job_status=expected_job_status,
                    status='stale', finished_at=dt.datetime.now(UTC),
                    error_code=stale_code,
                    actual_executor=str(getattr(generated, 'actual_executor', '') or ''),
                    usage=getattr(generated, 'usage', None))
            return _result('stale', run_id=run_id, started_at=start, source_day=source_day,
                           model_call_count=provider_call_count, job_id=str(job['job_id']),
                           generation_id=generation_id, error_code=stale_code,
                           attempt_kind=attempt_kind)
        body_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
        usage = getattr(generated, 'usage', None)
        _finish(store, generation_id=generation_id, job_id=str(job['job_id']),
                expected_job_status=expected_job_status,
                status='ready', finished_at=dt.datetime.now(UTC), body=body,
                body_hash=body_hash, output_token_estimate=output_tokens,
                actual_executor=str(getattr(generated, 'actual_executor', '') or ''),
                usage=usage if isinstance(usage, dict) else None)
        return _result(
            'ready', run_id=run_id, started_at=start, source_day=source_day,
            model_call_count=provider_call_count, idempotent_replay=False,
            job_id=str(job['job_id']), candidate_id=str(job['candidate_id']),
            generation_id=generation_id, provider=str(job['provider']),
            model_identity=str(job['model_identity']),
            authority_captured_at=str(job['captured_at']), persona_revision=str(job['persona_revision']),
            prompt_policy_version=str(job['prompt_policy_version']), prompt_hash=str(job['prompt_hash']),
            source_snapshot_hash=str(job['source_hash']),
            source_fingerprint=str(runtime['materialized'].source_fingerprint),
            source_member_count=int(plan['source_member_count']),
            completed_turn_count=int(plan['completed_turn_count']),
            autonomous_event_count=int(plan['autonomous_event_count']),
            incomplete_user_turn_count=int(plan['incomplete_user_turn_count']),
            attachments=plan['attachments'], raw_token_estimate=int(plan['raw_token_estimate']),
            budget_tokens=budget, generation_input_mode=str(input_plan['mode']),
            generation_input_token_estimate=planned_input_tokens,
            source_coverage=float(input_plan['source_coverage']),
            output_token_estimate=output_tokens,
            actual_executor=str(getattr(generated, 'actual_executor', '') or ''),
            provider_usage=(getattr(generated, 'usage', None) if isinstance(getattr(generated, 'usage', None), dict) else {}),
            body_hash=body_hash, summary_body=body, attempt_kind=attempt_kind,
        )
    except Exception as exc:
        code, detail = _split_error(exc)
        observed_executor = str(getattr(generated, 'actual_executor', '') or '') or None
        observed_usage = getattr(generated, 'usage', None)
        if not isinstance(observed_usage, dict):
            observed_usage = None
        try:
            _finish(store, generation_id=generation_id, job_id=str(job['job_id']),
                    expected_job_status=expected_job_status,
                    status='failed', finished_at=dt.datetime.now(UTC),
                    error_code=code, error_detail=detail, actual_executor=observed_executor,
                    usage=observed_usage)
        except Exception as persist_exc:
            # Report the persistence failure itself, not the provider detail.
            code = 'preview_failure_persistence_error'
            detail = str(persist_exc)[:ERROR_DETAIL_MAX_CHARS] or None
        return _result('failed', run_id=run_id, started_at=start, source_day=source_day,
                       model_call_count=provider_call_count, error_code=code, error_detail=detail,
                       job_id=str(job['job_id']), generation_id=generation_id,
                       actual_executor=observed_executor,
                       provider_usage=observed_usage or {}, attempt_kind=attempt_kind)


MANUAL_LOCK_PATH = Path(
    os.environ.get('HAYAGARDEN_DAYBLOCK_LOCK') or '/run/lock/hayagarden-dayblock.lock'
)
MANUAL_REASON_MAX_CHARS = 500


def _current_source(shadow: Any, source_db: str | Path, source_day: str,
                    created_at: str) -> dict[str, Any] | None:
    rows = shadow.discover_natural_day_rows(source_db, source_day)
    if not rows:
        return None
    snapshot, turns, events, members = shadow.derive_source_contract(rows, source_day, created_at)
    from continuity.contracts import candidate_source_revision
    return {
        'rows': rows, 'snapshot': snapshot, 'turns': turns, 'events': events,
        'members': members, 'materialized': shadow.materialize_raw_evidence(members, rows),
        'source_revision': candidate_source_revision(members), 'created_at': created_at,
    }


def plan_manual_attempt(store: sqlite3.Connection, shadow: Any, *, source_db: str | Path,
                        source_day: str, created_at: str) -> dict[str, Any]:
    """Decide, without writing, whether ``source_day`` gets a retry or a reconciliation.

    retry: the same frozen job failed transiently and the source is unchanged;
    it reruns with the job's frozen prompt, persona text and authority.
    reconciliation: the source projection changed (for example after a bug fix),
    so a new job is planned under the day's frozen 03:00 authority.
    """
    jobs, receipts = _read_existing(store, source_day)
    attempts = [row for rows in receipts.values() for row in rows]
    if any(str(row['status']) == 'ready' for row in attempts):
        return {'action': 'refuse', 'code': 'ready_day_is_final'}
    if any(str(row['status']) == 'processing' for row in attempts):
        return {'action': 'refuse', 'code': 'generation_in_progress'}
    if not jobs:
        return {'action': 'refuse', 'code': 'no_scheduled_job_for_day'}
    job = jobs[-1]
    source = _current_source(shadow, source_db, source_day, created_at)
    if source is None:
        return {'action': 'refuse', 'code': 'no_canonical_source_for_source_day', 'job': job}
    if (str(source['snapshot'].source_hash) != str(job['source_hash'])
            or str(source['source_revision']) != str(job['source_revision'])):
        return {'action': 'reconciliation', 'job': job, 'source': source}
    if str(job['status']) != 'failed':
        return {'action': 'refuse', 'code': 'source_unchanged_job_' + str(job['status']), 'job': job}
    persona_text = job['persona_text']
    if not persona_text:
        # Historical jobs froze only a persona revision; there is no exact retry.
        return {'action': 'refuse', 'code': 'historical_frozen_persona_unavailable', 'job': job}
    digest = 'runtime_persona_sha256:' + hashlib.sha256(str(persona_text).encode('utf-8')).hexdigest()
    if digest != str(job['persona_revision'] or ''):
        return {'action': 'refuse', 'code': 'frozen_persona_text_revision_mismatch', 'job': job}
    return {'action': 'retry', 'job': job, 'source': source,
            'last_attempt': receipts.get(str(job['job_id']), [None])[-1]}


def _retry_failed_job(store: sqlite3.Connection, shadow: Any, *, source_db: str | Path,
                      decision: dict[str, Any], run_id: str, start: dt.datetime,
                      source_day: str, reason: str,
                      generate_fn: Callable[[Any, Any], Any] | None) -> dict[str, Any]:
    job, source, last = decision['job'], decision['source'], decision['last_attempt']

    def refuse(code: str) -> dict[str, Any]:
        return _result('refused', run_id=run_id, started_at=start, source_day=source_day,
                       error_code=code, action='retry', job_id=str(job['job_id']))

    if hashlib.sha256(str(job['prompt_contract_json']).encode('utf-8')).hexdigest() != str(job['prompt_hash']):
        return refuse('frozen_prompt_contract_hash_mismatch')
    if last is None or str(last['source_fingerprint']) != str(source['materialized'].source_fingerprint):
        return refuse('retry_source_fingerprint_changed')
    try:
        input_plan = json.loads(str(job['input_plan_json'] or '{}'))
    except (TypeError, ValueError):
        input_plan = {}
    budget = shadow.frozen_model_context_budget(str(job['provider']), str(job['model_identity']))
    if (not input_plan or budget is None or int(job['raw_budget_tokens'] or 0) != budget
            or int(input_plan.get('budget_tokens') or 0) != budget
            or input_plan.get('source_coverage') != 1.0
            or input_plan.get('budget_fit') is not True
            or int(input_plan.get('uncovered_source_count') or 0) != 0):
        return refuse('frozen_input_readiness_failed')
    try:
        evidence = _compose_evidence(shadow, source['members'], source['materialized'], input_plan)
        static_prefix = shadow.build_prompt_static_prefix(
            str(job['prompt_contract_json']), str(job['persona_text']), str(job['source_day']),
        )
        planned_input_tokens = shadow._token_estimate(static_prefix + '\n\n' + evidence)
        if planned_input_tokens != int(input_plan.get('input_token_estimate') or -1):
            raise RuntimeError('generation_input_estimate_mismatch')
    except Exception as exc:
        return refuse(str(exc).split(':', 1)[0][:120] or type(exc).__name__)
    members, materialized = source['members'], source['materialized']
    view = {
        'materialized_source_fingerprint': materialized.source_fingerprint,
        'generation_input_mode': str(input_plan['mode']),
        'source_member_count': len(members),
        'source_coverage': float(input_plan['source_coverage']),
        'raw_token_estimate': materialized.source_token_estimate,
        'input_budget_tokens': int(input_plan['budget_tokens']),
        'generation_input_token_estimate': planned_input_tokens,
        'completed_turn_count': len(source['turns']),
        'autonomous_event_count': len(source['events']),
        'incomplete_user_turn_count': sum(
            shadow._is_dayblock_incomplete_user_member(member) for member in members
        ),
        'attachments': [
            {key: item.get(key) for key in (
                'source_ref', 'message_id', 'parent_source_ref', 'type', 'name',
                'file_exists', 'byte_length', 'content_status', 'text_context_available',
                'critical_unavailable', 'source_revision',
            )}
            for item in materialized.attachments
        ],
    }
    return _execute_attempt(
        store, shadow, source_db=source_db, job=job, view=view, runtime=source,
        static_prefix=static_prefix, evidence=evidence, planned_input_tokens=planned_input_tokens,
        budget=budget, input_plan=input_plan, run_id=run_id, start=start, source_day=source_day,
        generate_fn=generate_fn, attempt_kind='retry', attempt_reason=reason,
        expected_job_status='failed',
    )


def _reconcile_day(store: sqlite3.Connection, shadow: Any, *, source_db: str | Path,
                   preview_db: str | Path, decision: dict[str, Any], run_id: str,
                   start: dt.datetime, source_day: str, reason: str,
                   persona_capture: Callable[[], dict[str, Any]] | None,
                   generate_fn: Callable[[Any, Any], Any] | None) -> dict[str, Any]:
    previous = decision['job']
    captured_at = str(previous['captured_at'])
    if not shadow.scheduled_authority_capture_valid(source_day, captured_at):
        return _result('refused', run_id=run_id, started_at=start, source_day=source_day,
                       error_code='frozen_authority_not_captured_at_scheduled_0300',
                       action='reconciliation', job_id=str(previous['job_id']))
    # The new job keeps the day's frozen 03:00 authority; it freezes the persona
    # text current at planning time, which an exact retry of it will reuse.
    authority = shadow.AuthorityObservation(
        str(previous['provider']), str(previous['model_identity']),
        str(previous['authority_revision']), str(previous['authority_snapshot_id']),
    )
    try:
        persona = (persona_capture or shadow.capture_persona_snapshot)()
    except Exception as exc:
        return _result('refused', run_id=run_id, started_at=start, source_day=source_day,
                       error_code='persona_capture_failed', capture_error=type(exc).__name__,
                       action='reconciliation')
    plan = shadow.build_shadow_plan(
        source_db, preview_db, source_day, now=start.astimezone(SHANGHAI),
        capture_authority=lambda: authority,
        frozen_authority=authority, frozen_authority_captured_at=captured_at,
        persona_snapshot=persona, persona_captured_at=_local_iso(start),
        pre_captured=True, include_runtime=True,
    )
    if not plan.get('ready_to_generate'):
        clean_plan = {key: value for key, value in plan.items() if key != '_runtime'}
        reasons = _blocking_reasons(plan.get('blocking_reasons'))
        return _result(str(plan.get('status') or 'blocked'), run_id=run_id, started_at=start,
                       source_day=source_day, model_call_count=0, plan=clean_plan,
                       error_code=reasons[0] if reasons else None, blocking_reasons=reasons,
                       action='reconciliation', previous_job_id=str(previous['job_id']))
    result = _generate_planned_job(
        store, shadow, source_db=source_db, plan=plan, persona=persona, run_id=run_id,
        start=start, source_day=source_day, generate_fn=generate_fn,
        attempt_kind='reconciliation', attempt_reason=reason,
    )
    result['previous_job_id'] = str(previous['job_id'])
    return result


@contextlib.contextmanager
def _runner_lock(path: str | Path):
    """Share the cron flock so a manual attempt never overlaps the scheduled run."""
    import fcntl
    handle = open(path, 'a+')
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        handle.close()


def run_manual_attempt(
    *, source_day: str, reason: str, execute: bool = False,
    source_db: str | Path = SOURCE_DB, preview_db: str | Path = DAYBLOCK_STORE,
    started_at: dt.datetime | None = None, lock_path: str | Path = MANUAL_LOCK_PATH,
    persona_capture: Callable[[], dict[str, Any]] | None = None,
    generate_fn: Callable[[Any, Any], Any] | None = None,
) -> dict[str, Any]:
    """Manually retry or reconcile one non-ready day; a dry run writes no attempt."""
    start = started_at or dt.datetime.now(UTC)
    start = start.replace(tzinfo=UTC) if start.tzinfo is None else start.astimezone(UTC)
    run_id = str(uuid.uuid4())
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import dayblock_shadow as shadow
    try:
        source_day = shadow.validate_source_day(source_day)
    except ValueError:
        return _result('refused', run_id=run_id, started_at=start, source_day='1970-01-01',
                       error_code='invalid_source_day', requested_source_day=str(source_day)[:40])
    reason = str(reason or '').strip()
    if not reason:
        return _result('refused', run_id=run_id, started_at=start, source_day=source_day,
                       error_code='manual_reason_required')
    if len(reason) > MANUAL_REASON_MAX_CHARS:
        return _result('refused', run_id=run_id, started_at=start, source_day=source_day,
                       error_code='manual_reason_too_long')
    with _runner_lock(lock_path) as locked:
        if not locked:
            return _result('refused', run_id=run_id, started_at=start, source_day=source_day,
                           error_code='dayblock_runner_busy')
        store = shadow.open_preview_store(preview_db)
        try:
            decision = plan_manual_attempt(
                store, shadow, source_db=source_db, source_day=source_day,
                created_at=_local_iso(start),
            )
            action = decision['action']
            job = decision.get('job')
            summary = {'action': action, 'job_id': str(job['job_id']) if job is not None else None,
                       'reason': reason}
            if action == 'refuse':
                return _result('refused', run_id=run_id, started_at=start, source_day=source_day,
                               error_code=decision['code'], **summary)
            if not execute:
                return _result('dry_run', run_id=run_id, started_at=start, source_day=source_day,
                               **summary)
            if action == 'retry':
                return _retry_failed_job(
                    store, shadow, source_db=source_db, decision=decision, run_id=run_id,
                    start=start, source_day=source_day, reason=reason, generate_fn=generate_fn,
                )
            return _reconcile_day(
                store, shadow, source_db=source_db, preview_db=preview_db, decision=decision,
                run_id=run_id, start=start, source_day=source_day, reason=reason,
                persona_capture=persona_capture, generate_fn=generate_fn,
            )
        finally:
            store.close()


def main() -> int:
    # Capture the real process start before importing project modules.
    started_at = dt.datetime.now(UTC)
    try:
        outcome = run_scheduled_dayblock(started_at=started_at)
    except Exception as exc:
        local = started_at.astimezone(SHANGHAI)
        day = (local.date() - dt.timedelta(days=1)).isoformat()
        outcome = _result('failed', run_id=str(uuid.uuid4()), started_at=started_at,
                          source_day=day, model_call_count=0,
                          error_code='runner_unhandled_error', exception=type(exc).__name__)
    print(json.dumps(outcome, ensure_ascii=False, sort_keys=True, indent=2), flush=True)
    return 0 if outcome.get('status') in {'ready', 'blocked', 'processing', 'stale'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
