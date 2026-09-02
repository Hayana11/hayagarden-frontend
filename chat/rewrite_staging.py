"""Staged chat rewrite intents (regen / edit) for activation-atomic switches.

Active transcript stays untouched until a candidate assistant is durable in
staging and activate/finalize commits the authoritative switch in one txn.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any, Mapping, Optional

from chat.daily_schema import ensure_chat_messages_display_segments


STATUS_PREPARED = 'prepared'
STATUS_GENERATING = 'generating'
STATUS_READY = 'ready'
# Legacy alias: treat as activated_needs_replay for resume.
STATUS_ACTIVATED = 'activated'
STATUS_ACTIVATED_NEEDS_REPLAY = 'activated_needs_replay'
STATUS_EFFECTS_DONE = 'effects_done'
STATUS_FAILED = 'failed'
STATUS_STALE = 'stale'

OP_REGEN = 'regen'
OP_EDIT = 'edit'

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_rewrite_staging (
    rewrite_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    source_message_id INTEGER NOT NULL,
    user_message_id INTEGER,
    edited_content TEXT,
    old_branches_json TEXT,
    tail_archive_json TEXT,
    source_snapshot_json TEXT,
    active_tip_id INTEGER,
    source_revision TEXT,
    tail_revision TEXT,
    candidate_content TEXT,
    candidate_thinking TEXT,
    candidate_tool_calls TEXT,
    candidate_cache_info TEXT,
    candidate_choices TEXT,
    candidate_display_segments TEXT,
    side_effects_json TEXT,
    activation_result_json TEXT,
    error TEXT,
    created_at REAL,
    updated_at REAL
)
"""


def _status_transcript_locked(status: str) -> bool:
    """True when active transcript mutation already committed."""
    return status in (
        STATUS_ACTIVATED,
        STATUS_ACTIVATED_NEEDS_REPLAY,
        STATUS_EFFECTS_DONE,
    )


def _status_effects_pending(status: str) -> bool:
    return status in (STATUS_ACTIVATED, STATUS_ACTIVATED_NEEDS_REPLAY)


def _status_effects_done(status: str) -> bool:
    return status == STATUS_EFFECTS_DONE

# Test-only hook: runs after fingerprint assert, still inside BEGIN IMMEDIATE.
_activation_fence_hook = None


class StaleRewriteError(ValueError):
    """Active transcript changed since prepare; activation must refuse."""


class RewriteEffectsError(RuntimeError):
    """Critical post-activate side effects failed; must not mark effects_done."""


def ensure_schema(conn, *, commit: bool = True) -> None:
    """Ensure staging DDL exists.

    ``commit=True`` (default) for prepare/startup paths. Activation must call
    this *before* ``BEGIN IMMEDIATE`` so DDL cannot commit the write fence.
    """
    conn.execute(_SCHEMA_SQL)
    ensure_chat_messages_display_segments(conn)
    cols = {r[1] for r in conn.execute('PRAGMA table_info(chat_rewrite_staging)')}
    for col, decl in (
        ('active_tip_id', 'INTEGER'),
        ('source_revision', 'TEXT'),
        ('tail_revision', 'TEXT'),
        ('side_effects_json', 'TEXT'),
        ('activation_result_json', 'TEXT'),
        # Durable history-rewrite epoch handoff marker (P0 idempotence fix).
        # Nullable/additive; set at most once per rewrite_id, then read-only.
        ('history_epoch', 'TEXT'),
        ('candidate_display_segments', 'TEXT'),
    ):
        if col not in cols:
            conn.execute(f'ALTER TABLE chat_rewrite_staging ADD COLUMN {col} {decl}')
    if commit:
        conn.commit()


def ensure_schema_for_path(db_path: str) -> None:
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn, commit=True)
    finally:
        conn.close()


def _begin_immediate(conn) -> None:
    """Take a reserved write lock so fingerprint + mutate stay atomic."""
    try:
        conn.execute('ROLLBACK')
    except Exception:
        pass
    conn.execute('BEGIN IMMEDIATE')


def _now() -> float:
    return time.time()


def _row_to_dict(row: Any) -> dict:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        return dict(row)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, sort_keys=True)


def _loads(raw: Any, default):
    if raw is None or raw == '':
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def _table_cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}


def _hash_payload(obj: Any) -> str:
    return hashlib.sha256(_dumps(obj).encode('utf-8')).hexdigest()


def _revision_fields(row_d: Mapping[str, Any]) -> dict:
    return {
        'id': row_d.get('id'),
        'author': row_d.get('author'),
        'content': row_d.get('content'),
        'thinking': row_d.get('thinking') or '',
        'tool_calls': row_d.get('tool_calls') or '',
        'display_segments': row_d.get('display_segments') or '',
        'branches': row_d.get('branches') or '',
        'branch_idx': row_d.get('branch_idx') or 0,
    }


def source_revision_of(row_d: Mapping[str, Any]) -> str:
    return _hash_payload(_revision_fields(row_d))


def tail_revision_of(rows: list[Any]) -> str:
    return _hash_payload([_revision_fields(_row_to_dict(r)) for r in rows])


def active_tip_id(conn) -> int:
    row = conn.execute('SELECT MAX(id) AS m FROM chat_messages').fetchone()
    if row is None:
        return 0
    val = row['m'] if hasattr(row, 'keys') else row[0]
    return int(val or 0)


def load(conn, rewrite_id: str) -> Optional[dict]:
    rid = (rewrite_id or '').strip()
    if not rid:
        return None
    row = conn.execute(
        'SELECT * FROM chat_rewrite_staging WHERE rewrite_id=?', (rid,)
    ).fetchone()
    if not row:
        return None
    return _row_to_dict(row)


def _set_status(conn, rewrite_id: str, status: str, **fields) -> None:
    cols = ['status=?', 'updated_at=?']
    vals: list[Any] = [status, _now()]
    for key, value in fields.items():
        cols.append(f'{key}=?')
        vals.append(value)
    vals.append(rewrite_id)
    conn.execute(
        f"UPDATE chat_rewrite_staging SET {', '.join(cols)} WHERE rewrite_id=?",
        vals,
    )


def mark_generating(conn, rewrite_id: str) -> None:
    _set_status(conn, rewrite_id, STATUS_GENERATING)


def mark_failed(conn, rewrite_id: str, error: str) -> None:
    _set_status(conn, rewrite_id, STATUS_FAILED, error=str(error or '')[:500])


def mark_stale(conn, rewrite_id: str, error: str = 'active transcript changed') -> None:
    _set_status(conn, rewrite_id, STATUS_STALE, error=str(error or '')[:500])


def store_candidate(
    conn,
    rewrite_id: str,
    *,
    content: str,
    thinking: str = '',
    tool_calls: str = '',
    cache_info: str = '',
    choices: str = '',
    display_segments: str = '',
    side_effects: Optional[Mapping[str, Any]] = None,
) -> None:
    text = (content or '').strip()
    if not text:
        raise ValueError('candidate content required')
    row = load(conn, rewrite_id)
    if not row:
        raise ValueError('rewrite not found')
    if row.get('status') in (STATUS_STALE,) or _status_transcript_locked(str(row.get('status') or '')):
        raise ValueError(f"rewrite not writable: {row.get('status')}")
    effects = dict(side_effects or {})
    _set_status(
        conn,
        rewrite_id,
        STATUS_READY,
        candidate_content=text,
        candidate_thinking=thinking or '',
        candidate_tool_calls=tool_calls or '',
        candidate_cache_info=cache_info or '',
        candidate_choices=choices or '',
        candidate_display_segments=display_segments or '',
        side_effects_json=_dumps(effects),
        error='',
    )


def side_effects_of(staging: Mapping[str, Any]) -> dict:
    return dict(_loads(staging.get('side_effects_json'), {}) or {})


def activation_result_of(staging: Mapping[str, Any]) -> dict:
    return dict(_loads(staging.get('activation_result_json'), {}) or {})


def mark_effects_done(conn, rewrite_id: str) -> None:
    _set_status(conn, rewrite_id, STATUS_EFFECTS_DONE)


def resume_activated_result(staging: Mapping[str, Any]) -> dict:
    """Build activate-like result without mutating the active transcript."""
    core = activation_result_of(staging)
    if not core.get('assistant_message_id'):
        raise ValueError('activated rewrite missing activation_result')
    out = {
        'ok': True,
        'side_effects': side_effects_of(staging),
        'staging': dict(staging),
        'resumed': True,
        'candidate_content': (
            core.get('candidate_content')
            or staging.get('candidate_content')
            or ''
        ),
    }
    out.update(core)
    return out


def clear_staged_moments_pending(staging: Mapping[str, Any], db_path: str) -> None:
    """Drop orphan Moment pending left by a staged generation that never activated."""
    effects = side_effects_of(staging)
    turn_key = str(effects.get('turn_key') or '').strip()
    if not turn_key or not db_path:
        return
    try:
        import moments_intent
        moments_intent.clear_pending(db_path, turn_key)
    except Exception:
        pass


def finalize_rewrite_daily_continuity(
    result: Mapping[str, Any],
    staging: Mapping[str, Any],
    *,
    db_path: Optional[str] = None,
) -> None:
    """After authoritative edit/regen activate: durable daily membership + cursor.

    Rewrites insert formal chat_messages directly (no resident turn). Without
    daily_message_contexts and a resident history cursor advance, cold bootstrap
    can replay only through the pre-rewrite cursor even though the UI shows the
    new authoritative rows.
    """
    from chat import daily_context as dc

    if not dc.enabled():
        return

    operation = str(staging.get('operation') or '')
    if operation not in (OP_EDIT, OP_REGEN):
        return

    source_id = int(staging.get('source_message_id') or 0)
    assistant_id = result.get('assistant_message_id')
    if assistant_id is None:
        return
    assistant_id = int(assistant_id)
    user_id = result.get('message_id') or result.get('user_message_id')
    if user_id is not None:
        user_id = int(user_id)

    ctx_row = _resolve_rewrite_daily_context(source_id, db_path=db_path)
    if ctx_row is None:
        return
    context_id, context_epoch, resident_generation = ctx_row

    if operation == OP_EDIT and user_id is not None:
        dc.record_daily_message_context(
            int(user_id),
            context_id=context_id,
            context_epoch=context_epoch,
            resident_generation=resident_generation,
            role='user',
            db_path=db_path,
        )
        dc.record_daily_message_context(
            assistant_id,
            context_id=context_id,
            context_epoch=context_epoch,
            resident_generation=resident_generation,
            role='assistant',
            db_path=db_path,
        )
        _purge_stale_rewrite_daily_mappings(
            context_id=context_id,
            from_message_id=source_id,
            keep_message_ids=[int(user_id), assistant_id],
            db_path=db_path,
        )
    elif operation == OP_REGEN:
        dc.record_daily_message_context(
            assistant_id,
            context_id=context_id,
            context_epoch=context_epoch,
            resident_generation=resident_generation,
            role='assistant',
            db_path=db_path,
        )

    dc.advance_resident_history_cursor(
        context_id,
        resident_generation,
        assistant_id,
        db_path=db_path,
    )


def _mapping_tuple(row: Any) -> tuple[int, int, int]:
    d = dict(row)
    return (
        int(d['context_id']),
        int(d['context_epoch']),
        int(d['resident_generation']),
    )


def _authoritative_daily_context_tuple(
    *,
    db_path: Optional[str] = None,
) -> Optional[tuple[int, int, int]]:
    """Current formal / authoritative daily context identity."""
    from chat import daily_context as dc

    try:
        from chat.context_window import get_current_context_window
        current = get_current_context_window(db_path=db_path)
    except Exception:
        current = None
    if current is not None:
        daily = dc.get_daily_context_by_id(int(current['id']), db_path=db_path)
        if daily is not None:
            return (
                int(daily['id']),
                int(daily['context_epoch']),
                int(daily['resident_generation']),
            )
    active = dc.get_latest_active_context(db_path=db_path)
    if active is not None:
        return (
            int(active['id']),
            int(active['context_epoch']),
            int(active['resident_generation']),
        )
    return None


def _resolve_rewrite_daily_context(
    source_message_id: int,
    *,
    db_path: Optional[str] = None,
) -> Optional[tuple[int, int, int]]:
    """Resolve daily context for rewrite finalize.

    Priority:
    1. Exact ``daily_message_contexts`` row for ``source_message_id``.
    2. Unmapped source: authoritative formal context; predecessor only when it
       matches authoritative — never bind to a stale predecessor context.
    """
    from chat import daily_context as dc

    sid = int(source_message_id or 0)
    if sid <= 0:
        return None
    conn = dc._connect(db_path)
    try:
        row = conn.execute(
            'SELECT context_id, context_epoch, resident_generation '
            'FROM daily_message_contexts WHERE message_id=?',
            (sid,),
        ).fetchone()
        if row is not None:
            return _mapping_tuple(row)

        pred_row = conn.execute(
            'SELECT context_id, context_epoch, resident_generation '
            'FROM daily_message_contexts WHERE message_id < ? '
            'ORDER BY message_id DESC LIMIT 1',
            (sid,),
        ).fetchone()
    finally:
        conn.close()

    auth = _authoritative_daily_context_tuple(db_path=db_path)
    if auth is None:
        return None
    if pred_row is None:
        return auth
    pred = _mapping_tuple(pred_row)
    if pred == auth:
        return pred
    return auth


def _purge_stale_rewrite_daily_mappings(
    *,
    context_id: int,
    from_message_id: int,
    keep_message_ids: list[int],
    db_path: Optional[str] = None,
) -> None:
    from chat import daily_context as dc

    keep = {int(x) for x in keep_message_ids if int(x) > 0}
    conn = dc._connect(db_path)
    try:
        rows = conn.execute(
            'SELECT message_id FROM daily_message_contexts '
            'WHERE context_id=? AND message_id >= ?',
            (int(context_id), int(from_message_id)),
        ).fetchall()
        stale = [
            int(dict(r)['message_id'])
            for r in rows
            if int(dict(r)['message_id']) not in keep
        ]
        for mid in stale:
            conn.execute(
                'DELETE FROM daily_message_contexts WHERE message_id=?',
                (mid,),
            )
        if stale:
            conn.commit()
    finally:
        conn.close()


def replay_side_effects_after_activate(
    staging: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    get_db,
    db_path: str,
    mark_done: bool = True,
) -> None:
    """Consume frozen active-only side effects after authoritative activation.

    Candidate generation only stores claims; this runs only after activate
    commits the new active worldline *and* durable epoch has been ensured by
    the caller. Safe to retry: wake/feedback consume are ``consumed=0``-gated;
    Moments pop_pending is a no-op when empty.

    Critical claims (wake / feedback / dream) must succeed before
    ``effects_done``. Memo / Moments remain best-effort.
    """
    if _status_effects_done(str(staging.get('status') or '')):
        return
    effects = side_effects_of(staging)
    wake_ids = effects.get('wake_ids') or []
    one_shot = effects.get('one_shot_claims') or {}
    # Prefer frozen memo text; fall back to activated content.
    memo_user = (effects.get('session_memo_user') or '').strip()
    memo_asst = (effects.get('session_memo_assistant') or '').strip()
    if not memo_asst:
        memo_asst = (
            (result.get('candidate_content') or result.get('edited_content') or '')
        ).strip()
        if staging.get('operation') == OP_EDIT and not memo_asst:
            memo_asst = (result.get('candidate_content') or '').strip()
    if not memo_asst:
        memo_asst = (staging.get('candidate_content') or '').strip()

    critical_errors: list[BaseException] = []
    try:
        from chat.context_continuity import consume_wake_ids
        consume_wake_ids(get_db, wake_ids)
    except Exception as exc:
        critical_errors.append(exc)
    try:
        from chat.system_builder import consume_cc_one_shot_claims
        consume_cc_one_shot_claims(get_db, one_shot, strict=True)
    except Exception as exc:
        critical_errors.append(exc)

    if memo_user and memo_asst:
        try:
            _write_session_memo_best_effort(memo_user, memo_asst)
        except Exception:
            pass

    turn_key = str(effects.get('turn_key') or '').strip()
    asst_id = result.get('assistant_message_id')
    if turn_key and asst_id is not None:
        try:
            from moments_persistence import after_assistant_persisted
            user_mid = result.get('message_id')
            if user_mid is None:
                user_mid = result.get('user_message_id')
            if user_mid is None:
                user_mid = staging.get('user_message_id')
            after_assistant_persisted(
                memories_db_path=db_path,
                turn_data={
                    'turn_key': turn_key,
                    'user_message_id': user_mid,
                },
                assistant_message_id=int(asst_id),
                conversation_id=str(
                    effects.get('conversation_id') or 'hayana-chat'
                ),
            )
        except Exception:
            pass

    if critical_errors:
        raise RewriteEffectsError(
            f'critical rewrite side effects failed: {critical_errors[0]!r}'
        ) from critical_errors[0]

    if mark_done:
        rewrite_id = str(staging.get('rewrite_id') or '').strip()
        if rewrite_id:
            conn = get_db()
            try:
                mark_effects_done(conn, rewrite_id)
                conn.commit()
            finally:
                conn.close()


def _write_session_memo_best_effort(user_msg: str, assistant_msg: str) -> None:
    """Mirror gateway session-memo write without importing the Flask app."""
    import datetime as _dt
    import logging as _mlog
    import threading as _threading

    if not (user_msg or '').strip() or not (assistant_msg or '').strip():
        return

    def _worker():
        try:
            import ombre_adapter
            now = (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).strftime('%m-%d %H:%M')
            memo = (
                f'[网页窗口 {now}] 她：{user_msg.strip()[:80]}… / '
                f'我：{assistant_msg.strip()[:80]}…'
            )
            ombre_adapter.hold_memory(
                memo,
                tags='memo,网页窗口,跨端',
                importance=4,
                pinned=False,
                timeout=10.0,
                wall_timeout=11.0,
            )
        except Exception as exc:
            _mlog.getLogger(__name__).error(
                '[memo] staged-activate write failed: %s', exc, exc_info=True,
            )

    try:
        _threading.Thread(
            target=_worker, daemon=True, name='session-memo-rewrite',
        ).start()
    except Exception:
        pass


def assert_active_matches_prepare(conn, staging: Mapping[str, Any]) -> None:
    """Refuse generation/activation if the active transcript moved since prepare."""
    rewrite_id = str(staging.get('rewrite_id') or '')
    source_id = int(staging.get('source_message_id') or 0)
    expected_tip = int(staging.get('active_tip_id') or 0)
    expected_source_rev = staging.get('source_revision') or ''
    expected_tail_rev = staging.get('tail_revision') or ''

    tip = active_tip_id(conn)
    if tip != expected_tip:
        if rewrite_id:
            mark_stale(conn, rewrite_id, f'active tip changed {expected_tip}->{tip}')
        raise StaleRewriteError('active transcript tip changed since prepare')

    source = conn.execute(
        'SELECT * FROM chat_messages WHERE id=?', (source_id,),
    ).fetchone()
    if not source:
        if rewrite_id:
            mark_stale(conn, rewrite_id, 'source message missing')
        raise StaleRewriteError('source message missing since prepare')

    source_rev = source_revision_of(_row_to_dict(source))
    if expected_source_rev and source_rev != expected_source_rev:
        if rewrite_id:
            mark_stale(conn, rewrite_id, 'source revision changed')
        raise StaleRewriteError('source message changed since prepare')

    if staging.get('operation') == OP_EDIT:
        live_tail = conn.execute(
            'SELECT * FROM chat_messages WHERE id >= ? ORDER BY id ASC',
            (source_id,),
        ).fetchall()
        live_rev = tail_revision_of(live_tail)
        if expected_tail_rev and live_rev != expected_tail_rev:
            if rewrite_id:
                mark_stale(conn, rewrite_id, 'tail revision changed')
            raise StaleRewriteError('active tail changed since prepare')


def prepare_regen(conn, *, source_assistant_id: int) -> dict:
    ensure_schema(conn)
    row = conn.execute(
        'SELECT * FROM chat_messages WHERE id=?', (int(source_assistant_id),)
    ).fetchone()
    if not row:
        raise KeyError('source assistant not found')
    row_d = _row_to_dict(row)
    author = row_d.get('author')
    if author not in ('fyodor', 'assistant', 'claude'):
        raise ValueError('source must be an assistant message')

    old_branches = _loads(row_d.get('branches'), [])
    if not old_branches:
        old_branches = [{
            'content': row_d.get('content') or '',
            'thinking': row_d.get('thinking') or '',
            'display_segments': row_d.get('display_segments') or '',
            'tool_calls': row_d.get('tool_calls') or '',
        }]

    from chat.scoring_identity import find_user_message_before
    user_message_id = find_user_message_before(conn, int(source_assistant_id))
    tip = active_tip_id(conn)
    src_rev = source_revision_of(row_d)

    rewrite_id = secrets.token_hex(16)
    now = _now()
    conn.execute(
        '''INSERT INTO chat_rewrite_staging (
            rewrite_id, operation, status, source_message_id, user_message_id,
            edited_content, old_branches_json, tail_archive_json, source_snapshot_json,
            active_tip_id, source_revision, tail_revision,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            rewrite_id,
            OP_REGEN,
            STATUS_PREPARED,
            int(source_assistant_id),
            int(user_message_id) if user_message_id is not None else None,
            None,
            _dumps(old_branches),
            None,
            _dumps(row_d),
            tip,
            src_rev,
            src_rev,
            now,
            now,
        ),
    )
    return {
        'rewrite_id': rewrite_id,
        'operation': OP_REGEN,
        'source_assistant_id': int(source_assistant_id),
        'user_message_id': int(user_message_id) if user_message_id is not None else None,
        'old_branches': old_branches,
    }


def prepare_edit(conn, *, source_message_id: int, edited_content: str) -> dict:
    ensure_schema(conn)
    text = (edited_content or '').strip()
    if not text:
        raise ValueError('edited content required')
    row = conn.execute(
        'SELECT * FROM chat_messages WHERE id=?', (int(source_message_id),)
    ).fetchone()
    if not row:
        raise KeyError('source message not found')
    row_d = _row_to_dict(row)
    author = row_d.get('author')
    if author in ('fyodor', 'assistant', 'claude'):
        raise ValueError('edit target must be a user message')

    tail_rows = conn.execute(
        'SELECT * FROM chat_messages WHERE id >= ? ORDER BY id ASC',
        (int(source_message_id),),
    ).fetchall()
    tail_archive = [_row_to_dict(r) for r in tail_rows]
    tip = active_tip_id(conn)
    src_rev = source_revision_of(row_d)
    tail_rev = tail_revision_of(tail_rows)

    rewrite_id = secrets.token_hex(16)
    now = _now()
    conn.execute(
        '''INSERT INTO chat_rewrite_staging (
            rewrite_id, operation, status, source_message_id, user_message_id,
            edited_content, old_branches_json, tail_archive_json, source_snapshot_json,
            active_tip_id, source_revision, tail_revision,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            rewrite_id,
            OP_EDIT,
            STATUS_PREPARED,
            int(source_message_id),
            int(source_message_id),
            text,
            None,
            _dumps(tail_archive),
            _dumps(row_d),
            tip,
            src_rev,
            tail_rev,
            now,
            now,
        ),
    )
    return {
        'rewrite_id': rewrite_id,
        'operation': OP_EDIT,
        'source_message_id': int(source_message_id),
        'edited_content': text,
    }


def apply_history_overlay(rows: list[Any], staging: Mapping[str, Any]) -> list[Any]:
    """Return history rows as the model should see them for this staged rewrite.

    Caller should already fetch a source-aware prefix (id < source). This still
    filters defensively and appends the synthetic edited user for edit.
    """
    op = staging.get('operation')
    source_id = int(staging.get('source_message_id') or 0)
    if source_id <= 0:
        return list(rows)

    def _id(r: Any) -> int:
        if isinstance(r, dict):
            return int(r.get('id') or 0)
        try:
            return int(r['id'])
        except Exception:
            return int(r[0])

    kept = [r for r in rows if _id(r) < source_id]
    if op == OP_REGEN:
        return kept
    if op == OP_EDIT:
        snap = _loads(staging.get('source_snapshot_json'), {})
        edited = {
            'id': source_id,
            'author': snap.get('author') or 'hayana',
            'content': staging.get('edited_content') or '',
            'image_url': snap.get('image_url') or '',
            'created_at': snap.get('created_at') or '',
            'tool_calls': '',
            'file_url': snap.get('file_url') or '',
            'file_name': snap.get('file_name') or '',
            'attachments': snap.get('attachments') or '[]',
        }
        return kept + [edited]
    return list(rows)


def fetch_rewrite_prefix_rows(
    get_db,
    *,
    source_id: int,
    fetch_limit: int,
    history_where: str,
    min_id: int = 0,
) -> tuple[list[Any], int]:
    """Fetch recent history strictly before the rewrite source id."""
    conn = get_db()
    try:
        available = conn.execute(
            'SELECT COUNT(*) FROM chat_messages WHERE ' + history_where + ' AND id < ?',
            (int(source_id),),
        ).fetchone()[0] or 0
        sql = (
            'SELECT id, author, content, image_url, created_at, tool_calls, file_url, file_name, display_segments '
            'FROM chat_messages WHERE ' + history_where + ' AND id < ?'
        )
        params: list[Any] = [int(source_id)]
        if min_id > 0:
            sql += ' AND id >= ?'
            params.append(int(min_id))
        sql += ' ORDER BY id DESC LIMIT ?'
        params.append(int(fetch_limit))
        rows = list(reversed(conn.execute(sql, params).fetchall()))
    finally:
        conn.close()
    return rows, int(available)


def activate_regen(conn, rewrite_id: str) -> dict:
    """Activate regen inside a reserved write transaction (BEGIN IMMEDIATE).

    Status contract:
      READY → (mutate) → activated_needs_replay
      activated_needs_replay / legacy activated → resume, no transcript mutate
      effects_done → resume done, no mutate / caller skips replay
    """
    # DDL must not run inside the activation txn (ensure_schema commits).
    ensure_schema(conn, commit=True)
    _begin_immediate(conn)
    try:
        row = load(conn, rewrite_id)
        if not row:
            raise KeyError('rewrite not found')
        if row.get('operation') != OP_REGEN:
            raise ValueError('not a regen rewrite')
        status = str(row.get('status') or '')
        if status == STATUS_STALE:
            raise StaleRewriteError('rewrite is stale')
        if _status_effects_done(status):
            result = resume_activated_result(row)
            result['finalize_mode'] = 'done'
            conn.commit()
            return result
        if _status_effects_pending(status):
            result = resume_activated_result(row)
            result['finalize_mode'] = 'replay_only'
            conn.commit()
            return result
        if status != STATUS_READY or not (row.get('candidate_content') or '').strip():
            raise ValueError('candidate not ready')

        assert_active_matches_prepare(conn, row)
        hook = _activation_fence_hook
        if callable(hook):
            hook(conn)

        source_id = int(row['source_message_id'])
        source = conn.execute(
            'SELECT * FROM chat_messages WHERE id=?', (source_id,)
        ).fetchone()
        if not source:
            raise KeyError('source assistant missing; refusing activate')

        old_branches = _loads(row.get('old_branches_json'), [])
        if not old_branches:
            src = _row_to_dict(source)
            old_branches = [{
                'content': src.get('content') or '',
                'thinking': src.get('thinking') or '',
                'display_segments': src.get('display_segments') or '',
                'tool_calls': src.get('tool_calls') or '',
            }]
        new_branch = {
            'content': row.get('candidate_content') or '',
            'thinking': row.get('candidate_thinking') or '',
            'tool_calls': row.get('candidate_tool_calls') or '',
            'display_segments': row.get('candidate_display_segments') or '',
        }
        all_branches = list(old_branches) + [new_branch]
        branch_idx = len(all_branches) - 1
        cols = _table_cols(conn, 'chat_messages')
        updates = {
            'content': new_branch['content'],
            'thinking': new_branch['thinking'],
            'tool_calls': new_branch['tool_calls'],
            'display_segments': new_branch['display_segments'],
            'branches': _dumps(all_branches),
            'branch_idx': branch_idx,
        }
        if 'cache_info' in cols:
            updates['cache_info'] = row.get('candidate_cache_info') or ''
        if 'choices' in cols:
            updates['choices'] = row.get('candidate_choices') or ''
        if 'display_segments' in cols:
            updates['display_segments'] = row.get('candidate_display_segments') or ''
        assignments = ', '.join(f'{k}=?' for k in updates)
        conn.execute(
            f'UPDATE chat_messages SET {assignments} WHERE id=?',
            (*updates.values(), source_id),
        )
        result_core = {
            'assistant_message_id': source_id,
            'branch_idx': branch_idx,
            'total': len(all_branches),
            'user_message_id': row.get('user_message_id'),
            'candidate_content': new_branch['content'],
        }
        _set_status(
            conn,
            rewrite_id,
            STATUS_ACTIVATED_NEEDS_REPLAY,
            activation_result_json=_dumps(result_core),
        )
        row_after = load(conn, rewrite_id) or row
        result = {
            'ok': True,
            **result_core,
            'side_effects': side_effects_of(row_after),
            'staging': row_after,
            'finalize_mode': 'activate',
        }
        conn.commit()
        return result
    except StaleRewriteError:
        try:
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def activate_edit(conn, rewrite_id: str) -> dict:
    """Activate edit inside a reserved write transaction (BEGIN IMMEDIATE).

    Same status contract as ``activate_regen`` — resume never re-DELETEs.
    """
    ensure_schema(conn, commit=True)
    _begin_immediate(conn)
    try:
        row = load(conn, rewrite_id)
        if not row:
            raise KeyError('rewrite not found')
        if row.get('operation') != OP_EDIT:
            raise ValueError('not an edit rewrite')
        status = str(row.get('status') or '')
        if status == STATUS_STALE:
            raise StaleRewriteError('rewrite is stale')
        if _status_effects_done(status):
            result = resume_activated_result(row)
            result['finalize_mode'] = 'done'
            conn.commit()
            return result
        if _status_effects_pending(status):
            result = resume_activated_result(row)
            result['finalize_mode'] = 'replay_only'
            conn.commit()
            return result
        if status != STATUS_READY or not (row.get('candidate_content') or '').strip():
            raise ValueError('candidate not ready')

        assert_active_matches_prepare(conn, row)
        hook = _activation_fence_hook
        if callable(hook):
            hook(conn)

        source_id = int(row['source_message_id'])
        source = conn.execute(
            'SELECT * FROM chat_messages WHERE id=?', (source_id,)
        ).fetchone()
        if not source:
            raise KeyError('source message missing; refusing activate')

        # Fingerprint matched: archive the prepare-time snapshot (not later tip).
        tail_archive = _loads(row.get('tail_archive_json'), [])
        if not tail_archive:
            live_tail = conn.execute(
                'SELECT * FROM chat_messages WHERE id >= ? ORDER BY id ASC',
                (source_id,),
            ).fetchall()
            tail_archive = [_row_to_dict(r) for r in live_tail]
        snap = _loads(row.get('source_snapshot_json'), {})
        original_content = snap.get('content') or _row_to_dict(source).get('content') or ''

        conn.execute(
            'INSERT INTO chat_edit_branches (fork_msg_id, original_content, messages_json) '
            'VALUES (?, ?, ?)',
            (source_id, original_content, _dumps(tail_archive)),
        )

        author = snap.get('author') or _row_to_dict(source).get('author') or 'hayana'
        image_url = snap.get('image_url') or ''
        file_url = snap.get('file_url') or ''
        file_name = snap.get('file_name') or ''
        attachments = snap.get('attachments') or '[]'
        edited = (row.get('edited_content') or '').strip()

        previous_user_at = None
        try:
            from chat.interaction_state import USER_AUTHOR_SQL
            prev = conn.execute(
                f"SELECT created_at FROM chat_messages WHERE {USER_AUTHOR_SQL} "
                "AND id < ? ORDER BY id DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            if prev is not None:
                previous_user_at = prev['created_at'] if hasattr(prev, 'keys') else prev[0]
                previous_user_at = str(previous_user_at) if previous_user_at else None
        except Exception:
            previous_user_at = None

        conn.execute('DELETE FROM chat_messages WHERE id >= ?', (source_id,))
        cols = _table_cols(conn, 'chat_messages')
        user_cols = ['author', 'content']
        user_vals: list[Any] = [author, edited]
        for col, val in (
            ('image_url', image_url),
            ('file_url', file_url),
            ('file_name', file_name),
            ('attachments', attachments),
        ):
            if col in cols:
                user_cols.append(col)
                user_vals.append(val)
        cur_u = conn.execute(
            f"INSERT INTO chat_messages ({', '.join(user_cols)}) VALUES ({', '.join('?' for _ in user_cols)})",
            user_vals,
        )
        new_user_id = int(cur_u.lastrowid)
        created_at = None
        row2 = conn.execute(
            'SELECT created_at FROM chat_messages WHERE id=?', (new_user_id,),
        ).fetchone()
        if row2 is not None:
            created_at = row2['created_at'] if hasattr(row2, 'keys') else row2[0]
            created_at = str(created_at) if created_at else None

        # Preserve prior edit-route Internal State user_rule capture in the same txn.
        _user_events_requested = False
        try:
            import os
            _user_events_requested = all(
                str(os.environ.get(name, '0')).strip() == '1'
                for name in (
                    'INTERNAL_STATE_V3_SHADOW_ENABLED',
                    'INTERNAL_STATE_V3_SCORE_PROOF_ENABLED',
                    'INTERNAL_STATE_V3_USER_EVENTS_ENABLED',
                )
            )
        except Exception:
            _user_events_requested = False
        if _user_events_requested and created_at:
            try:
                import internal_state_shadow as _shadow
                if _shadow.is_user_events_enabled():
                    try:
                        _shadow.enqueue_user_rule_in_txn(
                            conn,
                            message_id=int(new_user_id),
                            text=edited or '',
                            created_at=created_at,
                            previous_user_at=previous_user_at,
                        )
                    except Exception:
                        try:
                            _shadow.mark_proof_gap(
                                conn,
                                failed_message_id=int(new_user_id),
                                error_code='outbox_capture_gap',
                            )
                        except Exception:
                            pass
            except Exception:
                pass

        asst_cols = ['author', 'content']
        asst_vals: list[Any] = ['assistant', row.get('candidate_content') or '']
        for col, val in (
            ('thinking', row.get('candidate_thinking') or ''),
            ('tool_calls', row.get('candidate_tool_calls') or ''),
            ('cache_info', row.get('candidate_cache_info') or ''),
            ('choices', row.get('candidate_choices') or ''),
            ('display_segments', row.get('candidate_display_segments') or ''),
        ):
            if col in cols:
                asst_cols.append(col)
                asst_vals.append(val)
        cur_a = conn.execute(
            f"INSERT INTO chat_messages ({', '.join(asst_cols)}) VALUES ({', '.join('?' for _ in asst_cols)})",
            asst_vals,
        )
        new_assistant_id = int(cur_a.lastrowid)
        result_core = {
            'message_id': new_user_id,
            'assistant_message_id': new_assistant_id,
            'created_at': created_at,
            'edited_content': edited,
            'candidate_content': row.get('candidate_content') or '',
            'candidate_display_segments': row.get('candidate_display_segments') or '',
            'user_message_id': new_user_id,
        }
        _set_status(
            conn,
            rewrite_id,
            STATUS_ACTIVATED_NEEDS_REPLAY,
            activation_result_json=_dumps(result_core),
        )
        row_after = load(conn, rewrite_id) or row
        result = {
            'ok': True,
            **result_core,
            'side_effects': side_effects_of(row_after),
            'staging': row_after,
            'finalize_mode': 'activate',
        }
        conn.commit()
        return result
    except StaleRewriteError:
        try:
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def history_epoch_of(staging: Mapping[str, Any]) -> str:
    """Durable history-rewrite epoch already handed off for this rewrite, or ''.

    Once set, this value is a read-only fact for the rewrite's lifetime —
    callers must never overwrite it, even when a newer rewrite has since
    advanced the global epoch further (see ``persist_history_epoch_if_absent``).
    """
    return str((staging or {}).get('history_epoch') or '').strip()


def persist_history_epoch_if_absent(conn, rewrite_id: str, epoch: str) -> bool:
    """Best-effort, once-only persist of the durable epoch handoff marker.

    Only writes when the column is still empty for this rewrite_id — never
    overwrites an existing value. This is what makes epoch advancement
    per-rewrite idempotent across effects_pending / replay_only retries, and
    guarantees an older rewrite's retry can never roll back a newer epoch
    (it simply never gets a chance to write once its own marker is set).
    """
    rid = str(rewrite_id or '').strip()
    epoch = str(epoch or '').strip()
    if not rid or not epoch:
        return False
    cur = conn.execute(
        "UPDATE chat_rewrite_staging SET history_epoch=?, updated_at=? "
        "WHERE rewrite_id=? AND (history_epoch IS NULL OR history_epoch='')",
        (epoch, _now(), rid),
    )
    conn.commit()
    if int(cur.rowcount or 0) > 0:
        return True
    row = conn.execute(
        'SELECT history_epoch FROM chat_rewrite_staging WHERE rewrite_id=?',
        (rid,),
    ).fetchone()
    return bool(row and str(row['history_epoch'] if hasattr(row, 'keys') else row[0] or '').strip())


def active_transcript(conn) -> list[tuple]:
    """Helper for tests: (id, author, content) ordered."""
    rows = conn.execute(
        'SELECT id, author, content FROM chat_messages ORDER BY id ASC'
    ).fetchall()
    out = []
    for r in rows:
        if hasattr(r, 'keys'):
            out.append((int(r['id']), r['author'], r['content']))
        else:
            out.append((int(r[0]), r[1], r[2]))
    return out
