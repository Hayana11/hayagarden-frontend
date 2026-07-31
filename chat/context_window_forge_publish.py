"""Context Window v0.2 — Forge file publish + Intent DB bind (R1).

Publishes Preview-validated candidate bytes to a never-activated Claude JSONL
and binds candidate identity onto an existing ``context_switch_intents`` row.

Does not create target daily_context, Session Registry, resident, or Claude calls.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from chat.claude_transcript_model import ThinkingPolicy
from chat.context_window import (
    CLOSE_REASON_MANUAL,
    INTENT_FORGING,
    INTENT_RELEASED,
    NoOpenContextWindowError,
    StaleSourceContextError,
    SwitchInProgressError,
    WindowBusyError,
    _claim_forge_owner,
    _intent_row,
    _now_s,
    _shanghai_now,
    _update_intent_conn,
    reserve_or_load_intent,
)
from chat.context_window_preview import (
    PREVIEW_STATUS_NATIVE_COLD,
    PREVIEW_STATUS_READY,
    PreparedPreviewCandidate,
    _snapshot_transcript_prefix,
    prepare_context_window_candidate,
)
from chat.daily_context import DEFAULT_CHAT_ID, _connect, ensure_schema
from chat.session_registry import derive_transcript_path
from tools.cc_jsonl_usage import claude_project_slug

logger = logging.getLogger(__name__)

PUBLISH_STATUS_PUBLISHED = 'PUBLISHED'
PUBLISH_STATUS_NATIVE_COLD_BOUND = 'NATIVE_COLD_BOUND'
PUBLISH_STATUS_ALREADY_PUBLISHED = 'ALREADY_PUBLISHED'

# Narrow test hooks (default no-ops). Production never sets these.
_post_publish_hook: Optional[Callable[[], None]] = None
_finalize_fault_hook: Optional[Callable[[], None]] = None


class ForgePublishError(Exception):
    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = str(error_code)


@dataclass(frozen=True)
class PublishedFile:
    path: Path
    sha256: str
    size: int
    recovered_existing_file: bool


@dataclass(frozen=True)
class ForgePublishResult:
    publish_status: str
    request_id: str
    preview_id: str
    candidate_session_id: str
    jsonl_path: Optional[Path]
    jsonl_sha256: str
    jsonl_size: int
    event_count: int
    selected_round_count: int
    proof_kind: str
    recovered_existing_file: bool


def _canon_uuid(value: str, *, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise ForgePublishError(
            f'{field} must be a UUID', error_code='FORGE_PREVIEW_REQUEST_ID_MISMATCH',
        ) from exc


def _is_unset(value: Any) -> bool:
    return value is None or value == ''


def _intent_selected_ids(intent: dict[str, Any]) -> list[int]:
    raw = intent.get('selected_message_ids_json') or '[]'
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [int(x) for x in data]


def _candidate_binding_fields(
    artifact: PreparedPreviewCandidate,
) -> dict[str, Any]:
    return {
        'preview_id': artifact.preview_id,
        'thinking_policy': artifact.thinking_policy.value,
        'target_session_id': artifact.candidate_session_id,
        'target_jsonl_sha256': artifact.output_sha256,
        'target_jsonl_size': int(artifact.serialized_bytes),
    }


def _bindings_match(intent: dict[str, Any], fields: dict[str, Any]) -> bool:
    if str(intent.get('preview_id') or '') != str(fields['preview_id']):
        return False
    if str(intent.get('thinking_policy') or '') != str(fields['thinking_policy']):
        return False
    if str(intent.get('target_session_id') or '') != str(fields['target_session_id']):
        return False
    if str(intent.get('target_jsonl_sha256') or '') != str(fields['target_jsonl_sha256']):
        return False
    try:
        size = intent.get('target_jsonl_size')
        if size is None:
            return False
        if int(size) != int(fields['target_jsonl_size']):
            return False
    except (TypeError, ValueError):
        return False
    return True


def _bindings_all_unset(intent: dict[str, Any]) -> bool:
    return (
        _is_unset(intent.get('preview_id'))
        and _is_unset(intent.get('thinking_policy'))
        and _is_unset(intent.get('target_session_id'))
        and _is_unset(intent.get('target_jsonl_sha256'))
        and intent.get('target_jsonl_size') is None
    )


def _assert_intent_matches_artifact(
    intent: dict[str, Any],
    artifact: PreparedPreviewCandidate,
    *,
    count: int,
) -> None:
    if int(intent['source_context_id']) != int(artifact.source_context_id):
        raise ForgePublishError(
            'intent source mismatch', error_code='FORGE_INTENT_CANDIDATE_CONFLICT',
        )
    if int(intent['source_context_epoch']) != int(artifact.source_context_epoch):
        raise ForgePublishError(
            'intent source mismatch', error_code='FORGE_INTENT_CANDIDATE_CONFLICT',
        )
    if int(intent['source_version']) != int(artifact.source_version):
        raise ForgePublishError(
            'intent source mismatch', error_code='FORGE_INTENT_CANDIDATE_CONFLICT',
        )
    if int(intent['source_resident_generation']) != int(
        artifact.source_resident_generation
    ):
        raise ForgePublishError(
            'intent source mismatch', error_code='FORGE_INTENT_CANDIDATE_CONFLICT',
        )
    if int(intent['source_boundary_message_id']) != int(
        artifact.source_boundary_message_id
    ):
        raise ForgePublishError(
            'intent source mismatch', error_code='FORGE_INTENT_CANDIDATE_CONFLICT',
        )
    if int(intent['carryover_count']) != int(count):
        raise ForgePublishError(
            'intent count mismatch', error_code='FORGE_INTENT_CANDIDATE_CONFLICT',
        )
    if _intent_selected_ids(intent) != list(artifact.selected_message_ids):
        raise ForgePublishError(
            'intent selection mismatch', error_code='FORGE_INTENT_CANDIDATE_CONFLICT',
        )
    if intent.get('target_context_id') is not None:
        raise ForgePublishError(
            'target_context already set', error_code='FORGE_INTENT_CANDIDATE_CONFLICT',
        )
    if intent.get('staged_ready_at') is not None:
        raise ForgePublishError(
            'staged_ready_at already set', error_code='FORGE_INTENT_CANDIDATE_CONFLICT',
        )


def _resolve_under_root(path: Path, root: Path) -> Path:
    root_r = root.resolve(strict=False)
    # Resolve parent; final may not exist yet.
    parent = path.parent.resolve(strict=False)
    try:
        parent.relative_to(root_r)
    except ValueError as exc:
        raise ForgePublishError(
            'path escapes allowed root', error_code='FORGE_WRITE_FAILED',
        ) from exc
    final = parent / path.name
    if final.exists() and final.is_symlink():
        raise ForgePublishError(
            'final path is symlink', error_code='FORGE_WRITE_FAILED',
        )
    return final


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        written = os.write(fd, view[offset:])
        if written <= 0:
            raise OSError(errno.EIO, 'short write')
        offset += written


def _publish_jsonl_noreplace(
    *,
    final_path: Path,
    payload: bytes,
    expected_sha256: str,
    expected_size: int,
    allowed_root: Path,
) -> PublishedFile:
    """Atomically publish payload to final_path without replacing an existing file."""
    if int(expected_size) != len(payload):
        raise ForgePublishError(
            'payload size mismatch', error_code='FORGE_FILE_VERIFY_FAILED',
        )
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected_sha256:
        raise ForgePublishError(
            'payload hash mismatch', error_code='FORGE_FILE_VERIFY_FAILED',
        )

    final_path = _resolve_under_root(final_path, allowed_root)
    parent = final_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.info('forge mkdir failed errno=%s', getattr(exc, 'errno', None))
        raise ForgePublishError(
            'cannot create parent directory', error_code='FORGE_WRITE_FAILED',
        ) from None
    final_path = _resolve_under_root(final_path, allowed_root)
    parent = final_path.parent
    if parent.is_symlink():
        raise ForgePublishError(
            'parent is symlink', error_code='FORGE_WRITE_FAILED',
        )

    tmp_name = f'.forge-tmp-{uuid.uuid4().hex}'
    final_name = final_path.name
    dir_flags = os.O_RDONLY
    if hasattr(os, 'O_DIRECTORY'):
        dir_flags |= os.O_DIRECTORY
    if hasattr(os, 'O_NOFOLLOW'):
        dir_flags |= os.O_NOFOLLOW
    try:
        dir_fd = os.open(str(parent), dir_flags)
    except OSError as exc:
        logger.info('forge open parent failed errno=%s', getattr(exc, 'errno', None))
        raise ForgePublishError(
            'cannot open parent directory', error_code='FORGE_WRITE_FAILED',
        ) from None

    tmp_created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        try:
            tmp_fd = os.open(tmp_name, flags, 0o600, dir_fd=dir_fd)
        except OSError as exc:
            logger.info('forge temp open failed errno=%s', getattr(exc, 'errno', None))
            raise ForgePublishError(
                'cannot create temp file', error_code='FORGE_WRITE_FAILED',
            ) from None
        tmp_created = True
        try:
            _write_all(tmp_fd, payload)
            os.fsync(tmp_fd)
        finally:
            os.close(tmp_fd)

        try:
            os.link(
                tmp_name,
                final_name,
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise ForgePublishError(
                'target already exists', error_code='FORGE_TARGET_EXISTS',
            ) from None
        except OSError as exc:
            if getattr(exc, 'errno', None) == errno.EEXIST:
                raise ForgePublishError(
                    'target already exists', error_code='FORGE_TARGET_EXISTS',
                ) from None
            logger.info('forge link failed errno=%s', getattr(exc, 'errno', None))
            raise ForgePublishError(
                'atomic publish failed', error_code='FORGE_WRITE_FAILED',
            ) from None

        os.fsync(dir_fd)
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except OSError:
            pass
        tmp_created = False
        os.fsync(dir_fd)
    finally:
        if tmp_created:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except OSError:
                pass
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
        os.close(dir_fd)

    verified = _verify_final_file(
        final_path,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
    )
    if not verified:
        raise ForgePublishError(
            'final file verify failed', error_code='FORGE_FILE_VERIFY_FAILED',
        )
    return PublishedFile(
        path=final_path,
        sha256=expected_sha256,
        size=expected_size,
        recovered_existing_file=False,
    )


def _verify_final_file(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if (st.st_mode & 0o777) & ~0o600:
        return False
    if int(st.st_size) != int(expected_size):
        return False
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest() == expected_sha256


def _delete_owned_final(path: Path, allowed_root: Path) -> bool:
    try:
        final_path = _resolve_under_root(path, allowed_root)
    except ForgePublishError:
        return False
    parent = final_path.parent
    dir_flags = os.O_RDONLY
    if hasattr(os, 'O_DIRECTORY'):
        dir_flags |= os.O_DIRECTORY
    if hasattr(os, 'O_NOFOLLOW'):
        dir_flags |= os.O_NOFOLLOW
    try:
        dir_fd = os.open(str(parent), dir_flags)
    except OSError:
        return False
    try:
        try:
            os.unlink(final_path.name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        return True
    finally:
        os.close(dir_fd)


def _prebind_candidate(
    *,
    db_path: str,
    request_id: str,
    artifact: PreparedPreviewCandidate,
    now_s: str,
) -> tuple[dict[str, Any], bool]:
    """Bind candidate identity with orphan_jsonl_state='pending'.

    Returns (intent_row, binding_created).
    """
    fields = _candidate_binding_fields(artifact)
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        live = _intent_row(conn, request_id)
        if live is None:
            conn.rollback()
            raise ForgePublishError(
                'intent missing', error_code='FORGE_IN_PROGRESS',
            )
        if str(live.get('status') or '') != INTENT_FORGING:
            conn.rollback()
            raise ForgePublishError(
                'intent not forging', error_code='FORGE_IN_PROGRESS',
            )
        _assert_intent_matches_artifact(
            live, artifact, count=int(live['carryover_count']),
        )
        if _bindings_all_unset(live):
            _update_intent_conn(
                conn,
                request_id,
                fields={
                    **fields,
                    'orphan_jsonl_state': 'pending',
                    'error_code': None,
                },
                now_s=now_s,
            )
            row = _intent_row(conn, request_id)
            conn.commit()
            assert row is not None
            return row, True
        if _bindings_match(live, fields):
            orphan = str(live.get('orphan_jsonl_state') or '')
            if orphan not in {'pending', 'none'}:
                conn.rollback()
                raise ForgePublishError(
                    'orphan state conflict',
                    error_code='FORGE_INTENT_CANDIDATE_CONFLICT',
                )
            if orphan == 'none':
                # Already finalized — caller handles ALREADY_PUBLISHED.
                conn.commit()
                return live, False
            # pending recovery path
            conn.commit()
            return live, False
        conn.rollback()
        raise ForgePublishError(
            'candidate identity conflict',
            error_code='FORGE_INTENT_CANDIDATE_CONFLICT',
        )
    except ForgePublishError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _finalize_native_cold(
    *,
    db_path: str,
    request_id: str,
    artifact: PreparedPreviewCandidate,
    now_s: str,
) -> dict[str, Any]:
    fields = _candidate_binding_fields(artifact)
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        live = _intent_row(conn, request_id)
        if live is None or str(live.get('status') or '') != INTENT_FORGING:
            conn.rollback()
            raise ForgePublishError(
                'intent not forging', error_code='FORGE_DB_FINALIZE_FAILED',
            )
        _assert_intent_matches_artifact(
            live, artifact, count=int(live['carryover_count']),
        )
        if _bindings_all_unset(live):
            _update_intent_conn(
                conn,
                request_id,
                fields={
                    **fields,
                    'orphan_jsonl_state': 'none',
                    'error_code': None,
                },
                now_s=now_s,
            )
        elif _bindings_match(live, fields):
            orphan = str(live.get('orphan_jsonl_state') or '')
            if orphan == 'none':
                conn.commit()
                return live
            if orphan != 'pending':
                conn.rollback()
                raise ForgePublishError(
                    'orphan state conflict',
                    error_code='FORGE_INTENT_CANDIDATE_CONFLICT',
                )
            _update_intent_conn(
                conn,
                request_id,
                fields={'orphan_jsonl_state': 'none', 'error_code': None},
                now_s=now_s,
            )
        else:
            conn.rollback()
            raise ForgePublishError(
                'candidate identity conflict',
                error_code='FORGE_INTENT_CANDIDATE_CONFLICT',
            )
        row = _intent_row(conn, request_id)
        conn.commit()
        assert row is not None
        return row
    except ForgePublishError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _finalize_published(
    *,
    db_path: str,
    request_id: str,
    artifact: PreparedPreviewCandidate,
    now_s: str,
) -> dict[str, Any]:
    fields = _candidate_binding_fields(artifact)
    if _finalize_fault_hook is not None:
        _finalize_fault_hook()
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        live = _intent_row(conn, request_id)
        if live is None:
            conn.rollback()
            raise ForgePublishError(
                'intent missing', error_code='FORGE_DB_FINALIZE_FAILED',
            )
        if str(live.get('status') or '') != INTENT_FORGING:
            conn.rollback()
            raise ForgePublishError(
                'intent not forging', error_code='FORGE_DB_FINALIZE_FAILED',
            )
        if not _bindings_match(live, fields):
            conn.rollback()
            raise ForgePublishError(
                'binding mismatch at finalize',
                error_code='FORGE_DB_FINALIZE_FAILED',
            )
        if str(live.get('orphan_jsonl_state') or '') != 'pending':
            conn.rollback()
            raise ForgePublishError(
                'orphan not pending', error_code='FORGE_DB_FINALIZE_FAILED',
            )
        if live.get('target_context_id') is not None or live.get('staged_ready_at') is not None:
            conn.rollback()
            raise ForgePublishError(
                'target already advanced', error_code='FORGE_DB_FINALIZE_FAILED',
            )
        cur = conn.execute(
            '''UPDATE context_switch_intents
               SET orphan_jsonl_state='none', error_code=NULL, updated_at=?
               WHERE request_id=?
                 AND status=?
                 AND preview_id=?
                 AND thinking_policy=?
                 AND target_session_id=?
                 AND target_jsonl_sha256=?
                 AND target_jsonl_size=?
                 AND orphan_jsonl_state='pending'
                 AND target_context_id IS NULL
                 AND staged_ready_at IS NULL''',
            (
                now_s,
                request_id,
                INTENT_FORGING,
                fields['preview_id'],
                fields['thinking_policy'],
                fields['target_session_id'],
                fields['target_jsonl_sha256'],
                fields['target_jsonl_size'],
            ),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise ForgePublishError(
                'finalize CAS failed', error_code='FORGE_DB_FINALIZE_FAILED',
            )
        row = _intent_row(conn, request_id)
        conn.commit()
        assert row is not None
        return row
    except ForgePublishError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _release_intent(
    *,
    db_path: str,
    request_id: str,
    error_code: str,
    orphan_jsonl_state: str,
    now_s: str,
) -> None:
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        _update_intent_conn(
            conn,
            request_id,
            status=INTENT_RELEASED,
            fields={
                'error_code': error_code,
                'orphan_jsonl_state': orphan_jsonl_state,
            },
            now_s=now_s,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _recheck_source_after_publish(
    *,
    db_path: str,
    chat_id: str,
    request_id: str,
    artifact: PreparedPreviewCandidate,
    now,
) -> None:
    """Raise FORGE_SOURCE_CHANGED if source drifted after file publish."""
    from chat.context_window_preview import (
        _get_registry_conn,
        _open_preview_db_readonly,
        _close_preview_readonly,
        _resolve_canonical_context_readonly_conn,
        _has_active_switch_intent_conn,
        _collect_context_formal_messages,
        _select_rounds_locked,
        _is_resident_turn_active_conn,
    )

    now_dt = _shanghai_now(now)
    conn = _open_preview_db_readonly(db_path)
    try:
        try:
            current = _resolve_canonical_context_readonly_conn(conn, chat_id=chat_id)
        except NoOpenContextWindowError as exc:
            raise ForgePublishError(
                'source changed', error_code='FORGE_SOURCE_CHANGED',
            ) from exc
        if (
            int(current['id']) != int(artifact.source_context_id)
            or int(current['context_epoch']) != int(artifact.source_context_epoch)
            or int(current.get('version') or 1) != int(artifact.source_version)
            or int(current.get('resident_generation') or 1)
            != int(artifact.source_resident_generation)
            or current.get('closed_at')
        ):
            raise ForgePublishError(
                'source changed', error_code='FORGE_SOURCE_CHANGED',
            )
        gen = int(artifact.source_resident_generation)
        if _is_resident_turn_active_conn(
            conn, int(artifact.source_context_id), gen, now_dt,
        ):
            raise ForgePublishError(
                'source changed', error_code='FORGE_SOURCE_CHANGED',
            )
        if _has_active_switch_intent_conn(
            conn, chat_id, allowed_switch_request_id=request_id,
        ):
            raise ForgePublishError(
                'source changed', error_code='FORGE_SOURCE_CHANGED',
            )
        messages = _collect_context_formal_messages(
            conn,
            context_id=int(artifact.source_context_id),
            context_epoch=int(artifact.source_context_epoch),
        )
        from chat.context_window import _last_formal_message_id
        if int(_last_formal_message_id(messages)) != int(
            artifact.source_boundary_message_id
        ):
            raise ForgePublishError(
                'source changed', error_code='FORGE_SOURCE_CHANGED',
            )
        _rounds, fresh_ids, _n = _select_rounds_locked(
            messages, int(artifact.requested_round_count),
        )
        if list(fresh_ids) != list(artifact.selected_message_ids):
            raise ForgePublishError(
                'source changed', error_code='FORGE_SOURCE_CHANGED',
            )
        if artifact.source_transcript_path is not None:
            reg = _get_registry_conn(
                conn, artifact.source_context_id, artifact.source_resident_generation,
            )
            if reg is None:
                raise ForgePublishError(
                    'source changed', error_code='FORGE_SOURCE_CHANGED',
                )
            if int(reg.get('scan_offset') or -1) != int(artifact.source_scan_offset):
                raise ForgePublishError(
                    'source changed', error_code='FORGE_SOURCE_CHANGED',
                )
            if str(reg.get('transcript_path') or '') != str(
                artifact.source_transcript_path
            ):
                raise ForgePublishError(
                    'source changed', error_code='FORGE_SOURCE_CHANGED',
                )
    finally:
        _close_preview_readonly(conn)

    if (
        artifact.source_transcript_path is not None
        and artifact.source_prefix_sha256 is not None
        and int(artifact.source_scan_offset) > 0
    ):
        try:
            snap = _snapshot_transcript_prefix(
                str(artifact.source_transcript_path),
                int(artifact.source_scan_offset),
            )
        except Exception as exc:
            logger.info('forge source prefix recheck failed: %s', type(exc).__name__)
            raise ForgePublishError(
                'source changed', error_code='FORGE_SOURCE_CHANGED',
            ) from None
        if snap.sha256 != artifact.source_prefix_sha256:
            raise ForgePublishError(
                'source changed', error_code='FORGE_SOURCE_CHANGED',
            )


def publish_context_window_forge_candidate(
    *,
    source_context_id: int,
    source_context_epoch: int,
    count: int,
    preview_id: str,
    request_id: str,
    thinking_policy: ThinkingPolicy,
    forge_cwd: str,
    claude_home: Path,
    chat_id: str = DEFAULT_CHAT_ID,
    db_path: str,
    now: Optional[Any] = None,
) -> ForgePublishResult:
    """Publish Preview-validated candidate bytes and bind intent identity."""
    if thinking_policy not in (ThinkingPolicy.KEEP, ThinkingPolicy.DROP):
        raise ForgePublishError(
            'invalid thinking_policy', error_code='FORGE_INTENT_CANDIDATE_CONFLICT',
        )
    preview_uuid = _canon_uuid(preview_id, field='preview_id')
    request_uuid = _canon_uuid(request_id, field='request_id')
    if preview_uuid != request_uuid:
        raise ForgePublishError(
            'preview_id must equal request_id',
            error_code='FORGE_PREVIEW_REQUEST_ID_MISMATCH',
        )

    ensure_schema(db_path)
    now_dt = _shanghai_now(now)
    now_s = _now_s(now_dt)

    try:
        intent = reserve_or_load_intent(
            source_context_id=int(source_context_id),
            source_context_epoch=int(source_context_epoch),
            count=int(count),
            request_id=request_uuid,
            chat_id=chat_id,
            close_reason=CLOSE_REASON_MANUAL,
            db_path=db_path,
            now=now_dt,
        )
    except (StaleSourceContextError, WindowBusyError, SwitchInProgressError) as exc:
        raise ForgePublishError(str(exc), error_code='FORGE_IN_PROGRESS') from exc

    prep = prepare_context_window_candidate(
        source_context_id=int(source_context_id),
        source_context_epoch=int(source_context_epoch),
        count=int(count),
        preview_id=preview_uuid,
        thinking_policy=thinking_policy,
        chat_id=chat_id,
        db_path=db_path,
        now=now_dt,
        allowed_switch_request_id=request_uuid,
    )
    status = str(prep.response.get('preview_status') or '')
    if status not in (PREVIEW_STATUS_READY, PREVIEW_STATUS_NATIVE_COLD):
        raise ForgePublishError(
            'preview blocked', error_code='FORGE_PREVIEW_BLOCKED',
        )
    artifact = prep.artifact
    if artifact is None:
        raise ForgePublishError(
            'preview blocked', error_code='FORGE_PREVIEW_BLOCKED',
        )

    _assert_intent_matches_artifact(intent, artifact, count=int(count))

    live, claimed = _claim_forge_owner(
        request_uuid, db_path=db_path, now=now_dt,
    )
    if str(live.get('status') or '') != INTENT_FORGING:
        raise ForgePublishError(
            'forge ownership unavailable', error_code='FORGE_IN_PROGRESS',
        )
    _ = claimed

    # Fast path: already published
    fields = _candidate_binding_fields(artifact)
    if (
        _bindings_match(live, fields)
        and str(live.get('orphan_jsonl_state') or '') == 'none'
    ):
        jsonl_path: Optional[Path] = None
        if status == PREVIEW_STATUS_READY:
            derived = derive_transcript_path(
                cwd=forge_cwd,
                claude_session_id=artifact.candidate_session_id,
                claude_home=str(claude_home),
            )
            jsonl_path = Path(derived)
            if not _verify_final_file(
                jsonl_path,
                expected_sha256=artifact.output_sha256,
                expected_size=artifact.serialized_bytes,
            ):
                raise ForgePublishError(
                    'published file mismatch',
                    error_code='FORGE_BOUND_FILE_MISMATCH',
                )
        return ForgePublishResult(
            publish_status=PUBLISH_STATUS_ALREADY_PUBLISHED,
            request_id=request_uuid,
            preview_id=preview_uuid,
            candidate_session_id=artifact.candidate_session_id,
            jsonl_path=jsonl_path,
            jsonl_sha256=artifact.output_sha256,
            jsonl_size=int(artifact.serialized_bytes),
            event_count=int(artifact.event_count),
            selected_round_count=int(artifact.selected_round_count),
            proof_kind=str(artifact.proof_kind),
            recovered_existing_file=False,
        )

    allowed_root = Path(claude_home).resolve(strict=False)

    # ----- NATIVE_COLD -----
    if status == PREVIEW_STATUS_NATIVE_COLD:
        _finalize_native_cold(
            db_path=db_path,
            request_id=request_uuid,
            artifact=artifact,
            now_s=now_s,
        )
        # First success → NATIVE_COLD_BOUND. Repeat hits ALREADY_PUBLISHED above.
        return ForgePublishResult(
            publish_status=PUBLISH_STATUS_NATIVE_COLD_BOUND,
            request_id=request_uuid,
            preview_id=preview_uuid,
            candidate_session_id=artifact.candidate_session_id,
            jsonl_path=None,
            jsonl_sha256=artifact.output_sha256,
            jsonl_size=0,
            event_count=0,
            selected_round_count=0,
            proof_kind='native_cold_contract',
            recovered_existing_file=False,
        )

    # ----- READY file publish -----
    derived = derive_transcript_path(
        cwd=forge_cwd,
        claude_session_id=artifact.candidate_session_id,
        claude_home=str(claude_home),
    )
    final_path = Path(derived)
    slug = claude_project_slug(forge_cwd)
    expected_parent = allowed_root / 'projects' / slug
    if final_path.parent.resolve(strict=False) != expected_parent.resolve(strict=False):
        raise ForgePublishError(
            'derived path invalid', error_code='FORGE_WRITE_FAILED',
        )

    intent2, binding_created = _prebind_candidate(
        db_path=db_path,
        request_id=request_uuid,
        artifact=artifact,
        now_s=now_s,
    )
    orphan = str(intent2.get('orphan_jsonl_state') or '')
    recovered = False

    final_exists = final_path.exists() or final_path.is_symlink()
    if final_exists:
        if binding_created:
            _release_intent(
                db_path=db_path,
                request_id=request_uuid,
                error_code='FORGE_TARGET_EXISTS',
                orphan_jsonl_state='foreign_exists',
                now_s=_now_s(_shanghai_now(now)),
            )
            raise ForgePublishError(
                'target already exists', error_code='FORGE_TARGET_EXISTS',
            )
        # Recovery only when binding pre-existed as pending
        if orphan != 'pending' or not _bindings_match(intent2, fields):
            raise ForgePublishError(
                'bound file mismatch', error_code='FORGE_BOUND_FILE_MISMATCH',
            )
        if not _verify_final_file(
            final_path,
            expected_sha256=artifact.output_sha256,
            expected_size=artifact.serialized_bytes,
        ):
            raise ForgePublishError(
                'bound file mismatch', error_code='FORGE_BOUND_FILE_MISMATCH',
            )
        recovered = True
        published = PublishedFile(
            path=final_path,
            sha256=artifact.output_sha256,
            size=int(artifact.serialized_bytes),
            recovered_existing_file=True,
        )
    else:
        if orphan == 'pending' and not binding_created and _bindings_match(intent2, fields):
            # Pending but file missing — republish
            pass
        try:
            published = _publish_jsonl_noreplace(
                final_path=final_path,
                payload=artifact.serialized_jsonl,
                expected_sha256=artifact.output_sha256,
                expected_size=int(artifact.serialized_bytes),
                allowed_root=allowed_root,
            )
        except ForgePublishError as exc:
            if exc.error_code == 'FORGE_TARGET_EXISTS' and binding_created:
                _release_intent(
                    db_path=db_path,
                    request_id=request_uuid,
                    error_code='FORGE_TARGET_EXISTS',
                    orphan_jsonl_state='foreign_exists',
                    now_s=_now_s(_shanghai_now(now)),
                )
            raise

    if _post_publish_hook is not None:
        _post_publish_hook()

    try:
        _recheck_source_after_publish(
            db_path=db_path,
            chat_id=chat_id,
            request_id=request_uuid,
            artifact=artifact,
            now=now,
        )
    except ForgePublishError as exc:
        if exc.error_code == 'FORGE_SOURCE_CHANGED':
            # Pre-bound matching identity ⇒ this request owns the candidate file.
            if binding_created or _bindings_match(intent2, fields):
                deleted = _delete_owned_final(final_path, allowed_root)
                _release_intent(
                    db_path=db_path,
                    request_id=request_uuid,
                    error_code='FORGE_SOURCE_CHANGED',
                    orphan_jsonl_state='deleted' if deleted else 'delete_blocked',
                    now_s=_now_s(_shanghai_now(now)),
                )
            else:
                _release_intent(
                    db_path=db_path,
                    request_id=request_uuid,
                    error_code='FORGE_SOURCE_CHANGED',
                    orphan_jsonl_state='delete_blocked',
                    now_s=_now_s(_shanghai_now(now)),
                )
        raise

    try:
        _finalize_published(
            db_path=db_path,
            request_id=request_uuid,
            artifact=artifact,
            now_s=_now_s(_shanghai_now(now)),
        )
    except ForgePublishError:
        # Keep pending + published file for same-request recovery.
        raise

    return ForgePublishResult(
        publish_status=PUBLISH_STATUS_PUBLISHED,
        request_id=request_uuid,
        preview_id=preview_uuid,
        candidate_session_id=artifact.candidate_session_id,
        jsonl_path=published.path,
        jsonl_sha256=artifact.output_sha256,
        jsonl_size=int(artifact.serialized_bytes),
        event_count=int(artifact.event_count),
        selected_round_count=int(artifact.selected_round_count),
        proof_kind=str(artifact.proof_kind),
        recovered_existing_file=bool(recovered),
    )
