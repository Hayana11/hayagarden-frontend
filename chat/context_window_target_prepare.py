"""Context Window v0.2 — Target context / Registry / staged resident prepare.

Independent prepare unit after Forge publish+bind. Does not close source,
swap formal resident, write owner/cursor/local binding, or run first-turn.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from chat.context_window import (
    INTENT_FORGING,
    INTENT_READY,
    _intent_row,
    _now_s,
    _parse_selected_ids,
    _serialize_context_switch,
    _shanghai_now,
    _update_intent_conn,
)
from chat.context_window_forge_publish import (
    EMPTY_SHA256,
    VerifiedFileIdentity,
    is_native_cold_binding,
    verify_published_candidate_file,
)
from chat.daily_context import (
    CHAT_DAY_START_HOUR,
    DEFAULT_CHAT_ID,
    DEFAULT_TIMEZONE,
    STATUS_PROVISIONAL,
    WINDOW_MODE_MANUAL_STAGED,
    _connect,
    _max_epoch,
    _row_to_dict,
    ensure_schema,
)
from chat.session_registry import (
    SCAN_STATUS_READY,
    derive_transcript_path,
    register_context_claude_session,
)

logger = logging.getLogger(__name__)

REGISTRY_SOURCE = 'context_window_forge'

PREPARE_STATUS_READY = 'READY'
PREPARE_STATUS_ALREADY_READY = 'ALREADY_READY'

# Narrow test hooks (default None). Production never sets these.
_after_target_hook: Optional[Callable[[], None]] = None
_after_registry_hook: Optional[Callable[[], None]] = None
_before_staged_hook: Optional[Callable[[], None]] = None


class TargetPrepareError(Exception):
    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = str(error_code)


class TargetPrepareCrash(Exception):
    """Test-only: simulate hard crash leaving partial prepare artifacts."""


@dataclass(frozen=True)
class TargetPrepareHooks:
    """Process-side staged resident prepare without formal handoff."""

    prepare_staged: Callable[[dict[str, Any], Path, VerifiedFileIdentity], Any]
    discard_staged: Callable[[Any], None]
    forge_cwd: str
    claude_home: Path


@dataclass(frozen=True)
class TargetPrepareResult:
    prepare_status: str
    request_id: str
    target_context_id: int
    target_context_epoch: int
    candidate_session_id: str
    jsonl_path: Path
    jsonl_sha256: str
    jsonl_size: int
    staged_ready_at: str
    recovered: bool


def offline_target_prepare_hooks(work_root: str | Path) -> TargetPrepareHooks:
    """Test-only staged health: alive handle, JSONL identity unchanged, no Claude."""
    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=True)
    cwd = str(root / 'cwd')
    Path(cwd).mkdir(parents=True, exist_ok=True)
    claude_home = root / 'claude_home'
    claude_home.mkdir(parents=True, exist_ok=True)

    class _OfflineStaged:
        def __init__(self, session_id: str, jsonl_path: Path):
            self.session_id = session_id
            self.jsonl_path = jsonl_path
            self.generation = 1
            self.tool_profile = 'text_only'
            self._alive = True

        def is_alive(self) -> bool:
            return self._alive

        def kill(self) -> None:
            self._alive = False

        def _kill(self, quiet: bool = True) -> None:
            self.kill()

    def prepare_staged(
        intent: dict[str, Any],
        forge_path: Path,
        identity: VerifiedFileIdentity,
    ) -> Any:
        st = forge_path.stat()
        if (
            int(st.st_dev) != int(identity.st_dev)
            or int(st.st_ino) != int(identity.st_ino)
            or int(st.st_size) != int(identity.size)
        ):
            raise TargetPrepareError(
                'staged jsonl identity changed',
                error_code='TARGET_STAGED_JSONL_MUTATED',
            )
        before = forge_path.read_bytes()
        import hashlib
        if hashlib.sha256(before).hexdigest() != identity.sha256:
            raise TargetPrepareError(
                'staged jsonl hash mismatch',
                error_code='TARGET_STAGED_JSONL_MUTATED',
            )
        staged = _OfflineStaged(str(intent['target_session_id']), forge_path)
        after = forge_path.read_bytes()
        st2 = forge_path.stat()
        if before != after or (
            int(st2.st_dev) != int(identity.st_dev)
            or int(st2.st_ino) != int(identity.st_ino)
            or int(st2.st_size) != int(identity.size)
        ):
            raise TargetPrepareError(
                'staged jsonl mutated during health',
                error_code='TARGET_STAGED_JSONL_MUTATED',
            )
        if not staged.is_alive():
            raise TargetPrepareError(
                'staged exited during health',
                error_code='TARGET_STAGED_HEALTH_FAILED',
            )
        return staged

    def discard_staged(staged: Any) -> None:
        if staged is not None and hasattr(staged, 'kill'):
            staged.kill()

    return TargetPrepareHooks(
        prepare_staged=prepare_staged,
        discard_staged=discard_staged,
        forge_cwd=cwd,
        claude_home=claude_home,
    )


def _require_hooks(hooks: Optional[TargetPrepareHooks]) -> TargetPrepareHooks:
    if hooks is None:
        raise TargetPrepareError(
            'target prepare hooks required',
            error_code='TARGET_HOOKS_REQUIRED',
        )
    return hooks


def _load_forging_intent(conn: sqlite3.Connection, request_id: str) -> dict[str, Any]:
    live = _intent_row(conn, request_id)
    if live is None:
        raise TargetPrepareError('intent missing', error_code='TARGET_INTENT_MISSING')
    status = str(live.get('status') or '')
    if status not in (INTENT_FORGING, INTENT_READY):
        raise TargetPrepareError(
            'intent not preparable', error_code='TARGET_INTENT_STATUS',
        )
    for field in (
        'target_session_id', 'target_jsonl_sha256', 'target_jsonl_size',
        'preview_id', 'thinking_policy',
    ):
        if live.get(field) is None or live.get(field) == '':
            raise TargetPrepareError(
                'incomplete published binding',
                error_code='TARGET_BINDING_INCOMPLETE',
            )
    if str(live.get('orphan_jsonl_state') or '') != 'none':
        raise TargetPrepareError(
            'published orphan state not none',
            error_code='TARGET_BINDING_INCOMPLETE',
        )
    if live.get('target_context_id') is not None and status == INTENT_FORGING:
        # Partial prepare recovery allowed.
        pass
    return live


def _find_target_by_request(
    conn: sqlite3.Connection,
    *,
    chat_id: str,
    request_id: str,
) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn.execute(
        '''SELECT * FROM daily_contexts
           WHERE chat_id=? AND switch_request_id=? AND window_mode=?''',
        (chat_id, request_id, WINDOW_MODE_MANUAL_STAGED),
    ).fetchone())


def _assert_target_matches_intent(
    target: dict[str, Any],
    intent: dict[str, Any],
) -> None:
    if str(target.get('window_mode') or '') != WINDOW_MODE_MANUAL_STAGED:
        raise TargetPrepareError(
            'target window_mode mismatch', error_code='TARGET_IDENTITY_CONFLICT',
        )
    if str(target.get('status') or '') != STATUS_PROVISIONAL:
        raise TargetPrepareError(
            'target status mismatch', error_code='TARGET_IDENTITY_CONFLICT',
        )
    if int(target.get('is_backfill') or 0) != 0:
        raise TargetPrepareError(
            'target must not be backfill', error_code='TARGET_IDENTITY_CONFLICT',
        )
    if int(target.get('resident_generation') or 0) != 1:
        raise TargetPrepareError(
            'target resident_generation mismatch',
            error_code='TARGET_IDENTITY_CONFLICT',
        )
    if int(target.get('source_context_id') or -1) != int(intent['source_context_id']):
        raise TargetPrepareError(
            'target source_context_id mismatch',
            error_code='TARGET_IDENTITY_CONFLICT',
        )
    if str(target.get('switch_request_id') or '') != str(intent['request_id']):
        raise TargetPrepareError(
            'target switch_request_id mismatch',
            error_code='TARGET_IDENTITY_CONFLICT',
        )
    if str(target.get('claude_session_id') or '') != str(intent['target_session_id']):
        raise TargetPrepareError(
            'target claude_session_id mismatch',
            error_code='TARGET_IDENTITY_CONFLICT',
        )
    if str(target.get('chat_id') or '') != str(intent['chat_id']):
        raise TargetPrepareError(
            'target chat_id mismatch', error_code='TARGET_IDENTITY_CONFLICT',
        )


def _create_staged_target_conn(
    conn: sqlite3.Connection,
    *,
    intent: dict[str, Any],
    now_s: str,
) -> dict[str, Any]:
    chat_id = str(intent['chat_id'])
    source_id = int(intent['source_context_id'])
    source = conn.execute(
        'SELECT * FROM daily_contexts WHERE id=?', (source_id,),
    ).fetchone()
    if source is None:
        raise TargetPrepareError('source missing', error_code='TARGET_SOURCE_MISSING')
    source_d = dict(source)
    if source_d.get('closed_at'):
        raise TargetPrepareError(
            'source already closed', error_code='TARGET_SOURCE_CLOSED',
        )
    # Preserve source identity: do not touch closed_at/version/epoch/resident.
    local_day = str(source_d['local_day'])
    next_epoch = _max_epoch(conn, chat_id) + 1
    selected_ids = _parse_selected_ids(intent.get('selected_message_ids_json'))
    # selected_round_count ≈ user rounds; carryover_count on intent is request count.
    user_rounds = 0
    if selected_ids:
        placeholders = ','.join('?' for _ in selected_ids)
        user_rounds = int(conn.execute(
            f'''SELECT COUNT(*) FROM daily_message_contexts
                WHERE message_id IN ({placeholders}) AND role='user' ''',
            tuple(selected_ids),
        ).fetchone()[0])

    try:
        cur = conn.execute(
            '''INSERT INTO daily_contexts (
                chat_id, local_day, timezone, boundary_hour, context_epoch,
                boundary_message_id, status, carryover_count, carryover_requested_count,
                selection_finalized_at, is_backfill, resident_generation, version,
                created_at, updated_at, window_mode, opened_at, closed_at,
                close_reason, source_context_id, switch_request_id, claude_session_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 1, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)''',
            (
                chat_id, local_day, DEFAULT_TIMEZONE, CHAT_DAY_START_HOUR,
                next_epoch, int(intent['source_boundary_message_id']),
                STATUS_PROVISIONAL,
                user_rounds, int(intent['carryover_count']), now_s,
                now_s, now_s, WINDOW_MODE_MANUAL_STAGED, now_s,
                source_id, str(intent['request_id']),
                str(intent['target_session_id']),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise TargetPrepareError(
            'manual_staged unique conflict',
            error_code='TARGET_STAGED_CONFLICT',
        ) from exc
    target_id = int(cur.lastrowid)
    for ordinal, mid in enumerate(selected_ids):
        conn.execute(
            'INSERT INTO daily_carryover_messages (context_id, ordinal, message_id) '
            'VALUES (?,?,?)',
            (target_id, ordinal, int(mid)),
        )
    row = conn.execute(
        'SELECT * FROM daily_contexts WHERE id=?', (target_id,),
    ).fetchone()
    assert row is not None
    # Source must be unchanged in this transaction.
    source_after = conn.execute(
        'SELECT closed_at, version, context_epoch, resident_generation '
        'FROM daily_contexts WHERE id=?',
        (source_id,),
    ).fetchone()
    assert source_after is not None
    if (
        source_after['closed_at'] != source_d.get('closed_at')
        or int(source_after['version']) != int(source_d['version'])
        or int(source_after['context_epoch']) != int(source_d['context_epoch'])
        or int(source_after['resident_generation']) != int(source_d['resident_generation'])
    ):
        raise TargetPrepareError(
            'source mutated during target create',
            error_code='TARGET_SOURCE_MUTATED',
        )
    return dict(row)


def _delete_registry_for_target(
    conn: sqlite3.Connection,
    *,
    context_id: int,
    resident_generation: int,
) -> None:
    conn.execute(
        'DELETE FROM context_claude_sessions '
        'WHERE context_id=? AND resident_generation=?',
        (int(context_id), int(resident_generation)),
    )


def _delete_staged_target_conn(
    conn: sqlite3.Connection,
    *,
    target_id: int,
    request_id: str,
) -> None:
    row = conn.execute(
        'SELECT * FROM daily_contexts WHERE id=?', (int(target_id),),
    ).fetchone()
    if row is None:
        return
    target = dict(row)
    if str(target.get('window_mode') or '') != WINDOW_MODE_MANUAL_STAGED:
        raise TargetPrepareError(
            'refusing to delete non-staged target',
            error_code='TARGET_CLEANUP_REFUSED',
        )
    if str(target.get('switch_request_id') or '') != str(request_id):
        raise TargetPrepareError(
            'refusing to delete foreign target',
            error_code='TARGET_CLEANUP_REFUSED',
        )
    _delete_registry_for_target(
        conn,
        context_id=int(target_id),
        resident_generation=int(target.get('resident_generation') or 1),
    )
    conn.execute(
        'DELETE FROM daily_carryover_messages WHERE context_id=?',
        (int(target_id),),
    )
    conn.execute('DELETE FROM daily_contexts WHERE id=?', (int(target_id),))


def _clear_intent_prepare_fields(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    now_s: str,
) -> None:
    conn.execute(
        '''UPDATE context_switch_intents
           SET target_context_id=NULL, staged_ready_at=NULL,
               status=?, updated_at=?
           WHERE request_id=? AND status IN (?, ?)''',
        (INTENT_FORGING, now_s, request_id, INTENT_FORGING, INTENT_READY),
    )


def _register_target_registry(
    *,
    target: dict[str, Any],
    intent: dict[str, Any],
    forge_cwd: str,
    claude_home: Path,
    identity: VerifiedFileIdentity,
    db_path: str,
) -> dict[str, Any]:
    derived = derive_transcript_path(
        cwd=forge_cwd,
        claude_session_id=str(intent['target_session_id']),
        claude_home=str(claude_home),
    )
    if Path(derived).resolve(strict=False) != identity.path.resolve(strict=False):
        raise TargetPrepareError(
            'registry path mismatch',
            error_code='TARGET_REGISTRY_PATH_MISMATCH',
        )
    scan_offset = int(intent['target_jsonl_size'])
    if scan_offset != int(identity.size):
        raise TargetPrepareError(
            'scan_offset size mismatch',
            error_code='TARGET_REGISTRY_OFFSET_MISMATCH',
        )
    return register_context_claude_session(
        context_id=int(target['id']),
        context_epoch=int(target['context_epoch']),
        resident_generation=int(target['resident_generation']),
        chat_id=str(target['chat_id']),
        claude_session_id=str(intent['target_session_id']),
        cwd=forge_cwd,
        source=REGISTRY_SOURCE,
        scan_offset=scan_offset,
        claude_home=str(claude_home),
        transcript_path=derived,
        db_path=db_path,
    )


def _register_cold_target_registry(
    *,
    target: dict[str, Any],
    intent: dict[str, Any],
    forge_cwd: str,
    claude_home: Path,
    db_path: str,
) -> tuple[dict[str, Any], Path]:
    """Register future JSONL address for native-cold; file need not exist yet."""
    derived = derive_transcript_path(
        cwd=forge_cwd,
        claude_session_id=str(intent['target_session_id']),
        claude_home=str(claude_home),
    )
    if int(intent['target_jsonl_size']) != 0:
        raise TargetPrepareError(
            'cold binding size must be 0',
            error_code='TARGET_BINDING_INCOMPLETE',
        )
    if str(intent.get('target_jsonl_sha256') or '') != EMPTY_SHA256:
        raise TargetPrepareError(
            'cold binding sha must be empty',
            error_code='TARGET_BINDING_INCOMPLETE',
        )
    reg = register_context_claude_session(
        context_id=int(target['id']),
        context_epoch=int(target['context_epoch']),
        resident_generation=int(target['resident_generation']),
        chat_id=str(target['chat_id']),
        claude_session_id=str(intent['target_session_id']),
        cwd=forge_cwd,
        source=REGISTRY_SOURCE,
        scan_offset=0,
        claude_home=str(claude_home),
        transcript_path=derived,
        db_path=db_path,
    )
    if int(reg['scan_offset']) != 0:
        raise TargetPrepareError(
            'cold registry scan_offset must be 0',
            error_code='TARGET_IDENTITY_CONFLICT',
        )
    if str(reg.get('scan_status') or '') != SCAN_STATUS_READY:
        raise TargetPrepareError(
            'cold registry scan_status must be READY',
            error_code='TARGET_IDENTITY_CONFLICT',
        )
    if str(reg.get('claude_session_id') or '') != str(intent['target_session_id']):
        raise TargetPrepareError(
            'cold registry session mismatch',
            error_code='TARGET_IDENTITY_CONFLICT',
        )
    return reg, Path(derived)


def _assert_registry_chain(
    *,
    reg: dict[str, Any],
    target: dict[str, Any],
    intent: dict[str, Any],
    identity: VerifiedFileIdentity,
) -> None:
    if int(reg['context_id']) != int(target['id']):
        raise TargetPrepareError(
            'registry context_id mismatch',
            error_code='TARGET_IDENTITY_CONFLICT',
        )
    if str(reg['claude_session_id']) != str(intent['target_session_id']):
        raise TargetPrepareError(
            'registry session mismatch',
            error_code='TARGET_IDENTITY_CONFLICT',
        )
    if Path(str(reg['transcript_path'])).resolve(strict=False) != identity.path.resolve(
        strict=False,
    ):
        raise TargetPrepareError(
            'registry transcript mismatch',
            error_code='TARGET_IDENTITY_CONFLICT',
        )
    if int(reg['scan_offset']) != int(intent['target_jsonl_size']):
        raise TargetPrepareError(
            'registry scan_offset mismatch',
            error_code='TARGET_IDENTITY_CONFLICT',
        )
    if str(reg.get('source') or '') != REGISTRY_SOURCE:
        raise TargetPrepareError(
            'registry source mismatch',
            error_code='TARGET_IDENTITY_CONFLICT',
        )
    if str(target.get('switch_request_id') or '') != str(intent['request_id']):
        raise TargetPrepareError(
            'identity chain broken',
            error_code='TARGET_IDENTITY_CONFLICT',
        )


def _mark_intent_ready_conn(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    target_id: int,
    now_s: str,
) -> dict[str, Any]:
    cur = conn.execute(
        '''UPDATE context_switch_intents
           SET status=?, target_context_id=?, staged_ready_at=?,
               error_code=NULL, updated_at=?
           WHERE request_id=?
             AND status=?
             AND orphan_jsonl_state='none'
             AND target_session_id IS NOT NULL
             AND target_jsonl_sha256 IS NOT NULL
             AND target_jsonl_size IS NOT NULL''',
        (
            INTENT_READY, int(target_id), now_s, now_s,
            request_id, INTENT_FORGING,
        ),
    )
    if cur.rowcount != 1:
        # Idempotent ready confirm
        live = _intent_row(conn, request_id)
        if (
            live is not None
            and str(live.get('status') or '') == INTENT_READY
            and int(live.get('target_context_id') or -1) == int(target_id)
            and live.get('staged_ready_at')
        ):
            return live
        raise TargetPrepareError(
            'ready CAS failed', error_code='TARGET_DB_FINALIZE_FAILED',
        )
    live = _intent_row(conn, request_id)
    assert live is not None
    return live


def _prepare_native_cold_target(
    *,
    req_id: str,
    intent0: dict[str, Any],
    hooks: TargetPrepareHooks,
    db_path: str,
    now: Optional[Any],
    now_s: str,
    entered_ready: bool,
) -> TargetPrepareResult:
    """Native-cold prepare: DB target + Registry address only; no file / no Claude."""
    target: Optional[dict[str, Any]] = None
    recovered = False
    jsonl_path: Optional[Path] = None
    intent: dict[str, Any] = intent0
    try:
        conn = _connect(db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            intent = _load_forging_intent(conn, req_id)
            if not is_native_cold_binding(intent):
                raise TargetPrepareError(
                    'cold prepare binding mismatch',
                    error_code='TARGET_BINDING_INCOMPLETE',
                )
            chat_id = str(intent['chat_id'] or DEFAULT_CHAT_ID)
            entered_ready = str(intent.get('status') or '') == INTENT_READY

            existing = _find_target_by_request(
                conn, chat_id=chat_id, request_id=req_id,
            )
            bound_tid = intent.get('target_context_id')
            if existing is None and bound_tid is not None:
                existing = _row_to_dict(conn.execute(
                    'SELECT * FROM daily_contexts WHERE id=?',
                    (int(bound_tid),),
                ).fetchone())
                if existing is not None:
                    _assert_target_matches_intent(existing, intent)
                else:
                    raise TargetPrepareError(
                        'bound target missing',
                        error_code='TARGET_IDENTITY_CONFLICT',
                    )

            if existing is not None:
                _assert_target_matches_intent(existing, intent)
                target = existing
                recovered = True
            else:
                if entered_ready:
                    raise TargetPrepareError(
                        'ready intent missing target',
                        error_code='TARGET_IDENTITY_CONFLICT',
                    )
                target = _create_staged_target_conn(
                    conn, intent=intent, now_s=now_s,
                )
                _update_intent_conn(
                    conn,
                    req_id,
                    fields={'target_context_id': int(target['id'])},
                    now_s=now_s,
                )
            conn.commit()
        except TargetPrepareError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if _after_target_hook is not None:
            _after_target_hook()

        assert target is not None
        try:
            _reg, jsonl_path = _register_cold_target_registry(
                target=target,
                intent=intent,
                forge_cwd=hooks.forge_cwd,
                claude_home=hooks.claude_home,
                db_path=db_path,
            )
        except Exception as exc:
            from chat.session_registry import SessionRegistryConflict, SessionRegistryError
            if isinstance(exc, (SessionRegistryConflict, SessionRegistryError)):
                raise TargetPrepareError(
                    str(exc), error_code='TARGET_IDENTITY_CONFLICT',
                ) from exc
            raise

        if _after_registry_hook is not None:
            _after_registry_hook()

        # Cold READY proves DB/Registry freeze only — never spawn/kill Claude here.
        conn = _connect(db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            live = _mark_intent_ready_conn(
                conn,
                request_id=req_id,
                target_id=int(target['id']),
                now_s=_now_s(_shanghai_now(now)),
            )
            conn.commit()
        except TargetPrepareError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        status = (
            PREPARE_STATUS_ALREADY_READY if entered_ready else PREPARE_STATUS_READY
        )
        assert jsonl_path is not None
        return TargetPrepareResult(
            prepare_status=status,
            request_id=req_id,
            target_context_id=int(target['id']),
            target_context_epoch=int(target['context_epoch']),
            candidate_session_id=str(intent['target_session_id']),
            jsonl_path=jsonl_path,
            jsonl_sha256=EMPTY_SHA256,
            jsonl_size=0,
            staged_ready_at=str(live.get('staged_ready_at') or now_s),
            recovered=bool(recovered),
        )
    except TargetPrepareCrash:
        raise
    except Exception:
        if entered_ready:
            raise
        cleanup_conn = _connect(db_path)
        try:
            cleanup_conn.execute('BEGIN IMMEDIATE')
            live = _intent_row(cleanup_conn, req_id)
            if live is not None and str(live.get('status') or '') == INTENT_READY:
                cleanup_conn.commit()
                raise
            tid = None
            if live is not None and live.get('target_context_id') is not None:
                tid = int(live['target_context_id'])
            if tid is None and target is not None:
                tid = int(target['id'])
            if tid is not None:
                _delete_staged_target_conn(
                    cleanup_conn, target_id=tid, request_id=req_id,
                )
            _clear_intent_prepare_fields(
                cleanup_conn, request_id=req_id, now_s=_now_s(_shanghai_now(now)),
            )
            cleanup_conn.commit()
        except TargetPrepareError:
            cleanup_conn.rollback()
            raise
        except Exception:
            cleanup_conn.rollback()
            logger.info('cold target prepare cleanup failed', exc_info=True)
            raise
        finally:
            cleanup_conn.close()
        raise


@_serialize_context_switch
def prepare_context_window_target(
    *,
    request_id: str,
    db_path: str,
    hooks: Optional[TargetPrepareHooks] = None,
    now: Optional[Any] = None,
) -> TargetPrepareResult:
    """Prepare staged target daily_context + Registry + staged resident health."""
    hooks = _require_hooks(hooks)
    ensure_schema(db_path)
    now_dt = _shanghai_now(now)
    now_s = _now_s(now_dt)
    req_id = str(request_id)

    staged_handle: Any = None
    target: Optional[dict[str, Any]] = None
    identity: Optional[VerifiedFileIdentity] = None
    recovered = False
    entered_ready = False

    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        intent0 = _load_forging_intent(conn, req_id)
        sid = str(intent0['target_session_id'])
        sha = str(intent0['target_jsonl_sha256'])
        size = int(intent0['target_jsonl_size'])
        entered_ready = str(intent0.get('status') or '') == INTENT_READY
        conn.commit()
    except TargetPrepareError:
        conn.rollback()
        conn.close()
        raise
    except Exception:
        conn.rollback()
        conn.close()
        raise
    else:
        conn.close()

    # Native-cold: freeze target + Registry address without JSONL / Claude spawn.
    if is_native_cold_binding(intent0):
        return _prepare_native_cold_target(
            req_id=req_id,
            intent0=intent0,
            hooks=hooks,
            db_path=db_path,
            now=now,
            now_s=now_s,
            entered_ready=entered_ready,
        )

    identity = verify_published_candidate_file(
        forge_cwd=hooks.forge_cwd,
        claude_home=hooks.claude_home,
        target_session_id=sid,
        expected_sha256=sha,
        expected_size=size,
    )
    if identity is None:
        raise TargetPrepareError(
            'candidate file verify failed',
            error_code='TARGET_FILE_VERIFY_FAILED',
        )

    intent: dict[str, Any] = intent0
    try:
        conn = _connect(db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            intent = _load_forging_intent(conn, req_id)
            chat_id = str(intent['chat_id'] or DEFAULT_CHAT_ID)
            entered_ready = str(intent.get('status') or '') == INTENT_READY

            existing = _find_target_by_request(
                conn, chat_id=chat_id, request_id=req_id,
            )
            bound_tid = intent.get('target_context_id')
            if existing is None and bound_tid is not None:
                existing = _row_to_dict(conn.execute(
                    'SELECT * FROM daily_contexts WHERE id=?',
                    (int(bound_tid),),
                ).fetchone())
                if existing is not None:
                    _assert_target_matches_intent(existing, intent)
                else:
                    raise TargetPrepareError(
                        'bound target missing',
                        error_code='TARGET_IDENTITY_CONFLICT',
                    )

            if existing is not None:
                _assert_target_matches_intent(existing, intent)
                target = existing
                recovered = True
            else:
                if entered_ready:
                    raise TargetPrepareError(
                        'ready intent missing target',
                        error_code='TARGET_IDENTITY_CONFLICT',
                    )
                target = _create_staged_target_conn(
                    conn, intent=intent, now_s=now_s,
                )
                _update_intent_conn(
                    conn,
                    req_id,
                    fields={'target_context_id': int(target['id'])},
                    now_s=now_s,
                )
            conn.commit()
        except TargetPrepareError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if _after_target_hook is not None:
            _after_target_hook()

        assert target is not None
        try:
            reg = _register_target_registry(
                target=target,
                intent=intent,
                forge_cwd=hooks.forge_cwd,
                claude_home=hooks.claude_home,
                identity=identity,
                db_path=db_path,
            )
        except Exception as exc:
            from chat.session_registry import SessionRegistryConflict, SessionRegistryError
            if isinstance(exc, (SessionRegistryConflict, SessionRegistryError)):
                raise TargetPrepareError(
                    str(exc), error_code='TARGET_IDENTITY_CONFLICT',
                ) from exc
            raise
        _assert_registry_chain(
            reg=reg, target=target, intent=intent, identity=identity,
        )

        if _after_registry_hook is not None:
            _after_registry_hook()

        if _before_staged_hook is not None:
            _before_staged_hook()

        identity2 = verify_published_candidate_file(
            forge_cwd=hooks.forge_cwd,
            claude_home=hooks.claude_home,
            target_session_id=sid,
            expected_sha256=sha,
            expected_size=size,
        )
        if (
            identity2 is None
            or int(identity2.st_dev) != int(identity.st_dev)
            or int(identity2.st_ino) != int(identity.st_ino)
            or int(identity2.size) != int(identity.size)
            or identity2.sha256 != identity.sha256
        ):
            raise TargetPrepareError(
                'candidate identity drifted before staged',
                error_code='TARGET_FILE_VERIFY_FAILED',
            )
        identity = identity2

        staged_handle = hooks.prepare_staged(intent, identity.path, identity)

        conn = _connect(db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            live = _mark_intent_ready_conn(
                conn,
                request_id=req_id,
                target_id=int(target['id']),
                now_s=_now_s(_shanghai_now(now)),
            )
            conn.commit()
        except TargetPrepareError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        status = (
            PREPARE_STATUS_ALREADY_READY if entered_ready else PREPARE_STATUS_READY
        )
        # Doorway health only — release the staged process after ready CAS.
        # Do not retain handles across stages or leave orphan Claude children.
        if staged_handle is not None:
            handle = staged_handle
            staged_handle = None
            hooks.discard_staged(handle)
        return TargetPrepareResult(
            prepare_status=status,
            request_id=req_id,
            target_context_id=int(target['id']),
            target_context_epoch=int(target['context_epoch']),
            candidate_session_id=sid,
            jsonl_path=identity.path,
            jsonl_sha256=sha,
            jsonl_size=size,
            staged_ready_at=str(live.get('staged_ready_at') or now_s),
            recovered=bool(recovered),
        )
    except TargetPrepareCrash:
        # Simulated hard crash: leave partial artifacts for same-request retry.
        raise
    except Exception:
        # Failure cleanup only while not yet durable-ready.
        try:
            if staged_handle is not None:
                hooks.discard_staged(staged_handle)
        except Exception:
            logger.info('discard_staged failed during cleanup', exc_info=True)
        if entered_ready:
            raise
        cleanup_conn = _connect(db_path)
        try:
            cleanup_conn.execute('BEGIN IMMEDIATE')
            live = _intent_row(cleanup_conn, req_id)
            if live is not None and str(live.get('status') or '') == INTENT_READY:
                cleanup_conn.commit()
                raise
            tid = None
            if live is not None and live.get('target_context_id') is not None:
                tid = int(live['target_context_id'])
            if tid is None and target is not None:
                tid = int(target['id'])
            if tid is not None:
                _delete_staged_target_conn(
                    cleanup_conn, target_id=tid, request_id=req_id,
                )
            _clear_intent_prepare_fields(
                cleanup_conn, request_id=req_id, now_s=_now_s(_shanghai_now(now)),
            )
            cleanup_conn.commit()
        except TargetPrepareError:
            cleanup_conn.rollback()
            raise
        except Exception:
            cleanup_conn.rollback()
            logger.info('target prepare cleanup failed', exc_info=True)
            raise
        finally:
            cleanup_conn.close()
        raise
