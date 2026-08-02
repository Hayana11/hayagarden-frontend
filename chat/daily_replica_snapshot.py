"""Read-only Daily Runtime material snapshot for the 9A replica experiment."""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from chat.daily_replica_ab import (
    DailyReplicaPairPlan,
    ReplicaContractError,
    build_daily_replica_pair,
)


SnapshotCopier = Callable[[str, str], None]
MessageContextLoader = Callable[..., Optional[dict[str, Any]]]
ContextLoader = Callable[..., Optional[dict[str, Any]]]
CurrentContextLoader = Callable[..., dict[str, Any]]
AssemblyBuilder = Callable[..., dict[str, Any]]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def copy_sqlite_readonly(source_db_path: str, target_db_path: str) -> None:
    """Create one consistent SQLite backup while opening source in mode=ro."""
    source = Path(source_db_path).resolve()
    if not source.is_file():
        raise ReplicaContractError(
            'formal database does not exist',
            error_code='REPLICA_SOURCE_DB_MISSING',
        )
    uri = source.as_uri() + '?mode=ro'
    src = sqlite3.connect(uri, uri=True)
    dst = sqlite3.connect(str(target_db_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _default_message_context_loader(message_id: int, *, db_path: str):
    from chat.daily_context import get_message_context

    return get_message_context(message_id, db_path=db_path)


def _default_context_loader(context_id: int, *, db_path: str):
    from chat.daily_context import get_daily_context_by_id

    return get_daily_context_by_id(context_id, db_path=db_path)


def _default_current_context_loader(*, db_path: str):
    from chat.context_window import get_current_context_window

    return get_current_context_window(db_path=db_path)


def _default_assembly_builder(**kwargs: Any) -> dict[str, Any]:
    from chat.daily_history import build_daily_window_context

    return build_daily_window_context(**kwargs)


def _truncate_snapshot_at_user_boundary(db_path: Path, user_message_id: int) -> None:
    """Recreate the DB view that existed just before the selected turn ran."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            'DELETE FROM daily_message_contexts WHERE message_id > ?',
            (int(user_message_id),),
        )
        conn.execute(
            'DELETE FROM chat_messages WHERE id > ?',
            (int(user_message_id),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@dataclass
class DailyReplicaSnapshot:
    temp_root: Path
    db_path: Path
    plan: DailyReplicaPairPlan
    user_message_id: int
    context_id: int
    context_epoch: int
    resident_generation: int
    snapshot_sha256: str
    manifest: Mapping[str, Any]

    def close(self) -> None:
        shutil.rmtree(self.temp_root, ignore_errors=True)


def create_daily_replica_snapshot(
    *,
    source_db_path: str,
    user_message_id: int,
    static_system: str,
    static_system_sha256: str,
    persona_sha256: str,
    provider: str,
    model: str,
    tool_profile: str,
    allowed_tools_sha256: str,
    mcp_config_sha256: str,
    snapshot_parent: Optional[str] = None,
    snapshot_copier: Optional[SnapshotCopier] = None,
    message_context_loader: Optional[MessageContextLoader] = None,
    context_loader: Optional[ContextLoader] = None,
    current_context_loader: Optional[CurrentContextLoader] = None,
    assembly_builder: Optional[AssemblyBuilder] = None,
    formal_formatter: Optional[Callable[..., str]] = None,
) -> DailyReplicaSnapshot:
    """Freeze one formal cold-birth input without writing the source DB."""
    mid = int(user_message_id)
    if mid <= 0:
        raise ReplicaContractError(
            'user_message_id must be positive',
            error_code='REPLICA_USER_MESSAGE_ID_INVALID',
        )
    temp_root = Path(tempfile.mkdtemp(prefix='daily-replica-', dir=snapshot_parent))
    snapshot_db = temp_root / 'formal-snapshot.sqlite3'
    copier = snapshot_copier or copy_sqlite_readonly
    message_context_loader = message_context_loader or _default_message_context_loader
    context_loader = context_loader or _default_context_loader
    current_context_loader = current_context_loader or _default_current_context_loader
    assembly_builder = assembly_builder or _default_assembly_builder
    try:
        copier(str(source_db_path), str(snapshot_db))
        conn = sqlite3.connect(str(snapshot_db))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                'SELECT id, author, content FROM chat_messages WHERE id=?',
                (mid,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or str(row['author'] or '').strip().lower() != 'user':
            raise ReplicaContractError(
                'selected message is not a formal user message',
                error_code='REPLICA_USER_MESSAGE_INVALID',
            )
        user_content = str(row['content'] or '')
        if not user_content.strip():
            raise ReplicaContractError(
                'selected user message is empty',
                error_code='REPLICA_CURRENT_USER_EMPTY',
            )

        # Production assembled this turn before its assistant reply or any
        # later messages existed.  The copied DB is intentionally rewound to
        # that same boundary; only the disposable snapshot is modified.
        _truncate_snapshot_at_user_boundary(snapshot_db, mid)

        mapping = message_context_loader(mid, db_path=str(snapshot_db))
        if not mapping or str(mapping.get('role') or '') != 'user':
            raise ReplicaContractError(
                'selected user message lacks formal Daily mapping',
                error_code='REPLICA_MESSAGE_CONTEXT_MISSING',
            )
        context_id = int(mapping['context_id'])
        context = context_loader(context_id, db_path=str(snapshot_db))
        current = current_context_loader(db_path=str(snapshot_db))
        if not context or not current:
            raise ReplicaContractError(
                'formal Daily context is missing',
                error_code='REPLICA_CONTEXT_MISSING',
            )
        identity = (
            int(context['id']),
            int(context['context_epoch']),
            int(context.get('resident_generation') or 1),
        )
        mapped_identity = (
            int(mapping['context_id']),
            int(mapping['context_epoch']),
            int(mapping['resident_generation']),
        )
        current_identity = (
            int(current['id']),
            int(current['context_epoch']),
            int(current.get('resident_generation') or 1),
        )
        if identity != mapped_identity or identity != current_identity:
            raise ReplicaContractError(
                'selected message is not in the current formal window identity',
                error_code='REPLICA_MESSAGE_NOT_CURRENT_CONTEXT',
            )

        assembly = assembly_builder(
            chat_id=str(context.get('chat_id') or 'default'),
            daily_context=dict(context),
            current_user_message_id=mid,
            static_system=str(static_system),
            is_cold=True,
            is_respawn=False,
            last_state_snapshot=None,
            inject_handoff=True,
            inject_carryover=True,
            db_path=str(snapshot_db),
        )
        plan = build_daily_replica_pair(
            assembly=assembly,
            user_content=user_content,
            static_system_sha256=static_system_sha256,
            persona_sha256=persona_sha256,
            provider=provider,
            model=model,
            tool_profile=tool_profile,
            allowed_tools_sha256=allowed_tools_sha256,
            mcp_config_sha256=mcp_config_sha256,
            formal_formatter=formal_formatter,
        )
        snapshot_sha = _sha256_file(snapshot_db)
        manifest = {
            'snapshot_contract_ok': True,
            'source_open_mode': 'ro',
            'source_db_written': False,
            'snapshot_truncated_after_user': True,
            'snapshot_db_sha256': snapshot_sha,
            'user_message_id': mid,
            'context_id': identity[0],
            'context_epoch': identity[1],
            'resident_generation': identity[2],
            'common_material_sha256': plan.manifest['common_material_sha256'],
        }
        return DailyReplicaSnapshot(
            temp_root=temp_root,
            db_path=snapshot_db,
            plan=plan,
            user_message_id=mid,
            context_id=identity[0],
            context_epoch=identity[1],
            resident_generation=identity[2],
            snapshot_sha256=snapshot_sha,
            manifest=manifest,
        )
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise
