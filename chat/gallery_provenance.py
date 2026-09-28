"""Resolve Gallery source provenance from one verified Daily turn.

The model never supplies message or chat identifiers.  This module follows the
server-owned UH-A0 turn id to the active provider lease and proves the message,
context, generation, role, and chat binding before any image is selected.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class GalleryProvenanceError(ValueError):
    pass


@dataclass(frozen=True)
class TrustedGalleryTurn:
    turn_id: str
    request_message_id: int
    context_id: int
    context_epoch: int
    resident_generation: int
    chat_id: str


def _now_local_text(now: dt.datetime | str | None = None) -> str:
    if isinstance(now, str):
        value = now.strip()
        if value:
            return value
    stamp = now if isinstance(now, dt.datetime) else dt.datetime.now(dt.timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _parse_local(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone(dt.timedelta(hours=8))).replace(tzinfo=None)
    return parsed


def resolve_trusted_gallery_turn(
    db_path: str | Path,
    verified_turn_id: Any,
    *,
    now: dt.datetime | str | None = None,
    connect_fn: Callable[..., sqlite3.Connection] = sqlite3.connect,
) -> TrustedGalleryTurn:
    """Resolve exactly one live provider turn lease; never scan recent messages."""
    turn_id = str(verified_turn_id or "").strip()
    if not turn_id:
        raise GalleryProvenanceError("verified turn id missing")
    path = Path(db_path).expanduser().resolve()
    try:
        conn = connect_fn(f"file:{path.as_posix()}?mode=ro", uri=True)
    except (OSError, sqlite3.Error) as exc:
        raise GalleryProvenanceError("gallery provenance database unavailable") from exc
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT l.context_id, l.resident_generation, l.lease_owner,
                   l.request_message_id, l.expires_at,
                   c.chat_id, c.context_epoch,
                   mc.context_id AS message_context_id,
                   mc.context_epoch AS message_context_epoch,
                   mc.resident_generation AS message_resident_generation,
                   mc.role AS message_role,
                   m.author AS message_author
              FROM daily_resident_turn_leases AS l
              JOIN daily_contexts AS c ON c.id=l.context_id
              JOIN daily_message_contexts AS mc
                ON mc.message_id=l.request_message_id
              JOIN chat_messages AS m ON m.id=l.request_message_id
             WHERE l.lease_owner=?
            """,
            (turn_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise GalleryProvenanceError("gallery provenance query failed") from exc
    finally:
        conn.close()
    if len(rows) != 1:
        raise GalleryProvenanceError("provider turn lease is missing or ambiguous")
    row = dict(rows[0])
    if str(row.get("lease_owner") or "") != turn_id:
        raise GalleryProvenanceError("provider turn lease owner mismatch")
    try:
        if _parse_local(str(row.get("expires_at") or "")) <= _parse_local(_now_local_text(now)):
            raise GalleryProvenanceError("provider turn lease expired")
        context_id = int(row["context_id"])
        context_epoch = int(row["context_epoch"])
        generation = int(row["resident_generation"])
        message_id = int(row["request_message_id"])
    except (TypeError, ValueError, KeyError) as exc:
        raise GalleryProvenanceError("provider turn lease malformed") from exc
    if (
        int(row.get("message_context_id") or -1) != context_id
        or int(row.get("message_context_epoch") or -1) != context_epoch
        or int(row.get("message_resident_generation") or -1) != generation
    ):
        raise GalleryProvenanceError("message context does not match provider turn lease")
    if str(row.get("message_role") or "").strip().lower() != "user":
        raise GalleryProvenanceError("bound message role is not user")
    if str(row.get("message_author") or "").strip().lower() not in {"hayana", "haya", "user"}:
        raise GalleryProvenanceError("bound message author is not user")
    chat_id = str(row.get("chat_id") or "").strip()
    if not chat_id:
        raise GalleryProvenanceError("authoritative chat id missing")
    return TrustedGalleryTurn(
        turn_id=turn_id,
        request_message_id=message_id,
        context_id=context_id,
        context_epoch=context_epoch,
        resident_generation=generation,
        chat_id=chat_id,
    )


__all__ = [
    "GalleryProvenanceError",
    "TrustedGalleryTurn",
    "resolve_trusted_gallery_turn",
]

