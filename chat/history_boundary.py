"""Shared history boundary computation for build_messages and rolling_summary."""
from __future__ import annotations

from typing import Any, Callable, Optional

from chat.context_lean import (
    cc_history_token_budget,
    lean_history_enabled,
    relay_history_low_water,
    relay_history_high_water,
)

_HISTORY_WHERE = "date(created_at) >= date('now', '+8 hours', '-1 day')"
_WINDOW_BASE = 60
_WINDOW_BLOCK = 20
_BOUNDARY_HEAD_KEY = 'RELAY_HISTORY_HEAD_ID'
_RELAY_TRIM_KEY = 'RELAY_HISTORY_TRIMMED_UP_TO_ID'
_BOUNDARY_TRIM_KEY = 'HISTORY_TRIMMED_UP_TO_ID'
_BOUNDARY_OLDEST_KEY = 'HISTORY_OLDEST_RETAINED_ID'


def history_where_clause() -> str:
    return _HISTORY_WHERE


def legacy_block_limit(available_count: int) -> int:
    limit = available_count
    if available_count > _WINDOW_BASE:
        limit = _WINDOW_BASE + ((available_count - _WINDOW_BASE) % _WINDOW_BLOCK)
    if limit <= 0:
        limit = _WINDOW_BASE
    return limit


def persist_history_boundary(*, trimmed_up_to_id: int, oldest_retained_message_id: int) -> None:
    try:
        import config_store
        config_store.set(_BOUNDARY_TRIM_KEY, str(int(trimmed_up_to_id or 0)))
        config_store.set(_BOUNDARY_OLDEST_KEY, str(int(oldest_retained_message_id or 0)))
    except Exception:
        pass


def read_persisted_boundary() -> dict[str, int]:
    try:
        import config_store
        return {
            'trimmed_up_to_id': config_store.get_int(_BOUNDARY_TRIM_KEY, 0),
            'oldest_retained_message_id': config_store.get_int(_BOUNDARY_OLDEST_KEY, 0),
        }
    except Exception:
        return {'trimmed_up_to_id': 0, 'oldest_retained_message_id': 0}


def relay_history_head_id() -> int:
    try:
        import config_store
        return config_store.get_int(_BOUNDARY_HEAD_KEY, 0)
    except Exception:
        return 0


def set_relay_history_head_id(message_id: int) -> None:
    try:
        import config_store
        config_store.set(_BOUNDARY_HEAD_KEY, str(int(message_id or 0)))
    except Exception:
        pass


def relay_history_trimmed_up_to_id() -> int:
    try:
        import config_store
        return config_store.get_int(_RELAY_TRIM_KEY, 0)
    except Exception:
        return 0


def set_relay_history_trimmed_up_to_id(message_id: int) -> None:
    try:
        import config_store
        config_store.set(_RELAY_TRIM_KEY, str(int(message_id or 0)))
    except Exception:
        pass


def read_relay_trim_state() -> dict[str, int]:
    return {
        'head_id': relay_history_head_id(),
        'trimmed_up_to_id': relay_history_trimmed_up_to_id(),
    }


def has_relay_prior_trim() -> bool:
    return relay_history_trimmed_up_to_id() > 0


def effective_trimmed_up_to_id(*, current_trimmed_up_to_id: int, history_mode: str) -> int:
    if history_mode == 'relay_hysteresis':
        persisted = relay_history_trimmed_up_to_id()
        return max(int(current_trimmed_up_to_id or 0), int(persisted or 0))
    return int(current_trimmed_up_to_id or 0)


def should_inject_rolling_summary(
    *,
    conversation_content_trimmed: bool,
    history_mode: str,
    available_count: int = 0,
    legacy_limit: int = 0,
) -> bool:
    if history_mode == 'legacy_block':
        return available_count > legacy_limit
    if history_mode == 'relay_hysteresis':
        return bool(conversation_content_trimmed) or has_relay_prior_trim()
    return bool(conversation_content_trimmed)


def compute_boundary_ids(
    all_row_ids: list[int],
    retained_row_ids: list[int],
) -> tuple[int, int]:
    """Return (trimmed_up_to_id, oldest_retained_message_id)."""
    if not all_row_ids:
        return 0, 0
    retained = set(retained_row_ids or [])
    if not retained or len(retained) >= len(all_row_ids):
        return 0, min(all_row_ids)
    dropped = [rid for rid in all_row_ids if rid not in retained]
    trimmed_up_to = max(dropped) if dropped else 0
    oldest_retained = min(retained) if retained else 0
    return trimmed_up_to, oldest_retained


def rolling_summary_covers_boundary(
    summary_up_to_id: int,
    trimmed_up_to_id: int,
) -> bool:
    if trimmed_up_to_id <= 0:
        return True
    if not summary_up_to_id:
        return False
    return int(summary_up_to_id) >= int(trimmed_up_to_id)


def fetch_history_rows(
    get_db: Callable[[], Any],
    *,
    fetch_limit: int,
    min_id: int = 0,
) -> tuple[list[Any], int]:
    conn = get_db()
    try:
        available = conn.execute(
            'SELECT COUNT(*) FROM chat_messages WHERE ' + _HISTORY_WHERE
        ).fetchone()[0] or 0
        rows = list(reversed(conn.execute(
            'SELECT id, author, content, image_url, created_at, tool_calls, file_url, file_name, attachments '
            'FROM chat_messages WHERE ' + _HISTORY_WHERE + (
                ' AND id >= ?' if min_id > 0 else ''
            ) + ' ORDER BY id DESC LIMIT ?',
            ((min_id, fetch_limit) if min_id > 0 else (fetch_limit,)),
        ).fetchall()))
    finally:
        conn.close()
    return rows, int(available)


def resolve_fetch_plan(
    *,
    available_count: int,
    for_cc: bool,
    lean_history: Optional[bool] = None,
    history_mode: Optional[str] = None,
    history_token_budget: Optional[int] = None,
) -> dict[str, Any]:
    if history_mode is not None:
        mode = str(history_mode)
        if mode == 'legacy_block':
            limit = legacy_block_limit(available_count)
            return {
                'mode': 'legacy_block',
                'fetch_limit': limit,
                'history_token_budget': 0,
                'relay_high_water': 0,
                'relay_low_water': 0,
                'relay_head_id': 0,
            }
        if mode == 'relay_hysteresis':
            return {
                'mode': 'relay_hysteresis',
                'fetch_limit': max(available_count, _WINDOW_BASE + _WINDOW_BLOCK),
                'history_token_budget': 0,
                'relay_high_water': relay_history_high_water(),
                'relay_low_water': relay_history_low_water(),
                'relay_head_id': relay_history_head_id(),
            }
        if mode == 'cc_token_budget':
            budget = (
                cc_history_token_budget()
                if history_token_budget is None
                else max(0, int(history_token_budget))
            )
            return {
                'mode': 'cc_token_budget',
                'fetch_limit': max(available_count, _WINDOW_BASE + _WINDOW_BLOCK),
                'history_token_budget': budget,
                'relay_high_water': 0,
                'relay_low_water': 0,
                'relay_head_id': 0,
            }
        raise ValueError('unknown history_mode: %r' % mode)

    lean = lean_history_enabled() if lean_history is None else bool(lean_history)
    if not lean:
        limit = legacy_block_limit(available_count)
        return {
            'mode': 'legacy_block',
            'fetch_limit': limit,
            'history_token_budget': 0,
            'relay_high_water': 0,
            'relay_low_water': 0,
            'relay_head_id': 0,
        }
    if for_cc:
        return {
            'mode': 'cc_token_budget',
            'fetch_limit': max(available_count, _WINDOW_BASE + _WINDOW_BLOCK),
            'history_token_budget': cc_history_token_budget(),
            'relay_high_water': 0,
            'relay_low_water': 0,
            'relay_head_id': 0,
        }
    return {
        'mode': 'relay_hysteresis',
        'fetch_limit': max(available_count, _WINDOW_BASE + _WINDOW_BLOCK),
        'history_token_budget': 0,
        'relay_high_water': relay_history_high_water(),
        'relay_low_water': relay_history_low_water(),
        'relay_head_id': relay_history_head_id(),
    }


def boundary_rows_for_summary(
    get_db: Callable[[], Any],
    *,
    horizon_days: int = 3,
    for_cc: bool = False,
    history_mode: Optional[str] = None,
    static_dir: str = '/opt/frontend/static',
    read_file_fn: Optional[Callable[[str, str], Optional[str]]] = None,
) -> tuple[int, int, list[Any]]:
    """Recompute production boundary and return rows to summarize."""
    from chat.history_assembly import assemble_history_from_rows
    from chat.context_lean import lean_file_dedup_enabled, lean_tool_budget_enabled

    if read_file_fn is None:
        def read_file_fn(sd, url):
            try:
                import os
                from chat.attachment_contract import ALLOWED_TEXT_FILE_EXTENSIONS, resolve_uploaded_file_url
                files_dir = os.path.join(sd, 'uploads', 'files')
                path = resolve_uploaded_file_url(str(url or ''), files_dir)
                if (
                    path is None or not path.is_file()
                    or path.suffix.lower() not in ALLOWED_TEXT_FILE_EXTENSIONS
                ):
                    return None
                with path.open('r', encoding='utf-8', errors='replace') as ff:
                    return ff.read()
            except Exception:
                return None

    rows, available = fetch_history_rows(get_db, fetch_limit=100000)
    if not rows:
        return 0, 0, []

    plan = resolve_fetch_plan(
        available_count=available,
        for_cc=for_cc,
        history_mode=history_mode,
    )
    all_ids = [int(_row_id(r)) for r in rows]

    if plan['mode'] == 'legacy_block':
        limit = plan['fetch_limit']
        if len(rows) <= limit:
            return 0, min(all_ids), []
        kept = rows[-limit:]
        retained_ids = [int(_row_id(r)) for r in kept]
        trimmed_up_to, oldest = compute_boundary_ids(all_ids, retained_ids)
        summarize = _rows_before_id(get_db, oldest, horizon_days)
        return trimmed_up_to, oldest, summarize

    # Lean modes: run same assembly path used in production.
    def _noop_img(_u):
        return None

    msgs, stats = assemble_history_from_rows(
        rows,
        available_count=available,
        history_token_budget=plan['history_token_budget'],
        history_mode=plan['mode'],
        relay_high_water=plan['relay_high_water'],
        relay_low_water=plan['relay_low_water'],
        relay_head_id=plan['relay_head_id'],
        static_dir=static_dir,
        read_file_fn=read_file_fn,
        img_block_fn=_noop_img,
        is_ai_author=lambda author: author in ('fyodor', 'claude', 'assistant'),
        resident_file_hashes=set() if not lean_file_dedup_enabled() else set(),
        apply_tool_budget=lean_tool_budget_enabled(),
    )
    _ = msgs
    trimmed_up_to = int(stats.trimmed_up_to_id or 0)
    oldest = int(stats.oldest_retained_message_id or 0)
    if trimmed_up_to <= 0 and oldest <= 0:
        return 0, 0, []
    if oldest <= 0:
        return trimmed_up_to, oldest, []
    summarize = _rows_before_id(get_db, oldest, horizon_days)
    return trimmed_up_to, oldest, summarize


def _row_id(row: Any) -> int:
    if hasattr(row, 'keys') and 'id' in row.keys():
        return int(row['id'])
    return int(getattr(row, 'id', 0) or 0)


def _rows_before_id(get_db: Callable[[], Any], boundary_id: int, horizon_days: int) -> list[Any]:
    conn = get_db()
    try:
        return list(conn.execute(
            'SELECT id, author, content FROM chat_messages '
            'WHERE id < ? AND created_at >= datetime(\'now\',\'+8 hours\', ?) '
            'ORDER BY id ASC',
            (boundary_id, '-%d days' % horizon_days),
        ).fetchall())
    finally:
        conn.close()
