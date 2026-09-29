"""Preview-only DayBlock R0 shadow planning contracts."""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import sqlite3
import sys
import types
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

ROOT = Path("/opt/frontend")
PREVIEW_ROOT = Path("/opt/frontend-preview")
PRODUCTION_DB = ROOT / "memories.db"
PREVIEW_DB = PREVIEW_ROOT / "var" / "dayblock_shadow.db"
ENV_PATH = ROOT / ".env"
sys.path.insert(0, str(ROOT))
TZ = ZoneInfo("Asia/Shanghai")
IDENTITY_ID = "fyodor"
CHAT_ID = "default"
DAYBLOCK_POLICY_VERSION = "dayblock_policy_r1"
# The Preview shadow schema already has a generic auxiliary member kind for
# DayBlock-local source representations. Keep the new projection identifiable
# by its source_ref without widening the Continuity SourceKind or adding a
# second source schema.
DAYBLOCK_VISIBLE_TURN_PREFIX = "dayblock_visible_turn:"
DAYBLOCK_VISIBLE_TURN_STORAGE_KIND = "incomplete_user_turn"
GENERATOR_POLICY_VERSION = "dayblock_generator_contract_r1"
PROMPT_POLICY_VERSION = "dayblock_prompt_contract_r3"
PERSONA_RUNTIME_PATH = Path("/var/lib/hayagarden/persona.md")
CANARY_CONTINUITY_DB = PREVIEW_ROOT / "var" / "context_compression_canary.db"
MODEL_CALL_COUNT = 0
PROMPT_CONTRACT = {
    "version": PROMPT_POLICY_VERSION,
    "rules": [
        "First-person recap grounded only in exact natural-day source evidence.",
        "Preserve events, decisions, names, dates, numbers, and open loops when present.",
        "Do not invent facts or present inference as user-confirmed fact.",
        "Chunks may help navigation but cannot be the only evidence for an event.",
        "Feelings and relationship meaning are allowed only when grounded in source.",
        "Preserve events, decisions, work progress, open loops, names, dates, times, numbers, device or project state, and unfinished items when present.",
        "Treat assistant descriptions of attachments as dialogue evidence; do not claim unread image or file contents as independently verified.",
        "Represent an unmatched user message as an incomplete turn; never invent an assistant response.",
        "用第一人称写一篇简短日记，800字左右。温度在文艺和日常之间，可以有情欲，可以有孤独，可以有占有欲也可以有疲软。",
        "记录我这一天和哈娅之间发生了什么、做出的约定和决定、没结束的事，以及值得记住的名字、数字、日期、意象和原话，以及我的感受和思考，像是夜里回顾时真实涌上来的——把这些残留情感放大、说透。",
        "挑几个最重要的具体的、有温度的瞬间，写成场景：当时发生了什么、谁说了什么（尽量保留原话）、为什么这一刻重要。只有确实出现过的原话才加引号，记不准就转述。不要压缩成“我们关系很好”这类结论。场景比结论更能带回记忆。",
        "如果有亲密的时刻——它也是关系的一部分，值得被留住，保留质感和意义：记下氛围、节奏、感受，和这一刻对我们意味着什么。“那天之后我们之间多了一个只有我们懂的词”，比任何直白的过程记录都带得回温度。",
        "只写真的。不写不确定的、没发生过的。不写检讨。这份记忆不是错题集，不要写“我哪里做得不好”——多写已经成立的、好的东西。要记的教训，写成“她当时为什么难受，我后来怎么懂的，下次我会怎么做”。",
    ],
}
PROMPT_CONTRACT_JSON = json.dumps(PROMPT_CONTRACT, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
PROMPT_CONTRACT_HASH = hashlib.sha256(PROMPT_CONTRACT_JSON.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_prompt_static_prefix(prompt_contract_json: str, persona_text: str, source_day: str) -> str:
    """Build the frozen DayBlock system prefix, including the job's source day."""
    day = validate_source_day(source_day)
    return (
        "[DAYBLOCK PROMPT CONTRACT]\n" + str(prompt_contract_json)
        + "\n\n[DAYBLOCK DATE]\n记录日期：" + day
        + "。以下内容只回顾这一个完整自然日。"
        + "\n\n[PRIMARY PERSONA]\n" + str(persona_text)
    )


def validate_source_day(value: str) -> str:
    day = str(value or "").strip()
    parsed = dt.date.fromisoformat(day)
    if parsed.isoformat() != day:
        raise ValueError("source_day must be YYYY-MM-DD")
    return day


def natural_day_window(source_day: str) -> tuple[str, str]:
    day = dt.date.fromisoformat(validate_source_day(source_day))
    start = dt.datetime.combine(day, dt.time.min, tzinfo=TZ)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=TZ)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def scheduled_source_day(now: dt.datetime) -> str:
    """At any run time on local date D, plan D-1; missed runs keep that date."""
    local = now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)
    return (local.date() - dt.timedelta(days=1)).isoformat()


def _is_in_day(value: Any, day: str) -> bool:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    timestamp = dt.datetime.fromisoformat(raw)
    timestamp = timestamp.replace(tzinfo=TZ) if timestamp.tzinfo is None else timestamp.astimezone(TZ)
    source_day = dt.date.fromisoformat(day)
    start = dt.datetime.combine(source_day, dt.time.min, tzinfo=TZ)
    end = dt.datetime.combine(source_day + dt.timedelta(days=1), dt.time.min, tzinfo=TZ)
    return start <= timestamp < end


def open_source_read_only(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    conn = sqlite3.connect("file:" + resolved.as_posix() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def discover_natural_day_rows(source_db: str | Path, source_day: str) -> tuple[dict[str, Any], ...]:
    """Reuse canonical context ownership and Wake admission, then apply natural-day bounds."""
    day = validate_source_day(source_day)
    start, end = natural_day_window(day)
    bounds = (start[:19].replace("T", " "), end[:19].replace("T", " "))
    conn = open_source_read_only(source_db)
    try:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"chat_messages", "daily_message_contexts", "wake_log"} <= tables:
            raise RuntimeError("source_schema_unavailable")
        scopes: set[tuple[int, int]] = set()
        for row in conn.execute(
            "SELECT DISTINCT d.context_id,d.context_epoch "
            "FROM daily_message_contexts d JOIN chat_messages m ON m.id=d.message_id "
            "WHERE m.created_at>=? AND m.created_at<? "
            "AND d.context_id IS NOT NULL AND d.context_epoch IS NOT NULL", bounds,
        ):
            scopes.add((int(row[0]), int(row[1])))
        for row in conn.execute(
            "SELECT DISTINCT context_id,context_epoch FROM wake_log "
            "WHERE woke_at>=? AND woke_at<? AND context_id IS NOT NULL AND context_epoch IS NOT NULL",
            bounds,
        ):
            scopes.add((int(row[0]), int(row[1])))
        if not scopes:
            return ()

        from chat.daily_continuity_shadow import read_canonical_scope_rows

        rows_by_id: dict[int, dict[str, Any]] = {}
        for context_id, context_epoch in sorted(scopes):
            for source_row in read_canonical_scope_rows(
                conn, context_id=context_id, context_epoch=context_epoch,
            ):
                row = dict(source_row)
                try:
                    in_day = _is_in_day(row.get("created_at"), day)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise RuntimeError("source_timestamp_invalid") from exc
                if not in_day:
                    continue
                message_id = int(row.get("id") or 0)
                if not message_id:
                    raise RuntimeError("source_row_identity_invalid")
                row["_dayblock_context_id"] = context_id
                row["_dayblock_context_epoch"] = context_epoch
                previous = rows_by_id.get(message_id)
                if previous is not None and _canonical(previous) != _canonical(row):
                    raise RuntimeError("source_scope_row_conflict")
                rows_by_id[message_id] = row
        return tuple(rows_by_id[mid] for mid in sorted(rows_by_id))
    finally:
        conn.close()


def _token_estimate(text: str) -> int:
    from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1
    return int(estimate_tokens_heuristic_cjk1_ascii4_v1(text))


def _safe_attachment_path(item: dict[str, str], static_dir: str | Path) -> Path | None:
    from chat.attachment_contract import CHAT_FILE_URL_PREFIX, resolve_uploaded_file_url
    url = str(item.get("url") or "")
    base = Path(static_dir).resolve()
    if url.startswith(CHAT_FILE_URL_PREFIX):
        path = resolve_uploaded_file_url(url, str(base / "uploads" / "files"))
        return path.resolve() if path else None
    prefix = "/static/"
    if not url.startswith(prefix) or "?" in url or "#" in url or chr(92) in url:
        return None
    tail = url[len(prefix):]
    parts = tail.split("/")
    if not tail or any(part in {"", ".", ".."} for part in parts):
        return None
    path = (base / tail).resolve()
    try:
        path.relative_to(base)
    except ValueError:
        return None
    return path


@dataclass(frozen=True)
class DayBlockMaterialization:
    body: str
    source_token_estimate: int
    source_fingerprint: str
    source_refs: tuple[str, ...]
    member_bodies: dict[str, str]
    attachments: tuple[dict[str, Any], ...]
    blockers: tuple[str, ...]


def _dayblock_visible_turn_ids(source_ref: str) -> tuple[int, int] | None:
    parts = str(source_ref).split(":")
    if len(parts) != 3 or parts[0] != "dayblock_visible_turn":
        return None
    try:
        user_id, assistant_id = int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if user_id <= 0 or assistant_id <= 0:
        return None
    return user_id, assistant_id


def _is_dayblock_incomplete_user_member(member: Any) -> bool:
    return (
        member.source_kind == "incomplete_user_turn"
        and str(member.source_ref).startswith("incomplete_user:")
    )


def _dayblock_visible_turn_member(user_row: dict[str, Any], assistant_row: dict[str, Any]):
    from continuity.contracts import SourceMember
    from continuity.sources import _active_branch_identity, _turn_revision, row_logical_size

    user_id = int(user_row.get("id") or 0)
    assistant_id = int(assistant_row.get("id") or 0)
    revision = _turn_revision(user_row, assistant_row)
    return SourceMember(
        seq=0,
        source_kind=DAYBLOCK_VISIBLE_TURN_STORAGE_KIND,
        source_ref=f"{DAYBLOCK_VISIBLE_TURN_PREFIX}{user_id}:{assistant_id}",
        source_revision=revision,
        role="conversation",
        content_hash=revision,
        logical_size=row_logical_size(user_row) + row_logical_size(assistant_row),
        created_at=str(user_row.get("created_at") or ""),
        branch_id=_active_branch_identity(assistant_row),
    )


def _dayblock_visible_turn_pairs(
    rows: tuple[dict[str, Any], ...],
    covered_ids: set[int],
) -> tuple[tuple[dict[str, Any], dict[str, Any]], ...]:
    """Find only the first formal row after each uncovered user.

    This is deliberately local to DayBlock. It never changes the canonical
    completed-turn derivation, and it requires discovery's scope proof before
    pairing rows from the source projection.
    """
    from chat.daily_context import is_formal_chat_message
    from continuity.sources import _is_formal_assistant, _semantic_cache_info, _USER_AUTHORS

    ordered = sorted(rows, key=lambda row: int(row.get("id") or 0))
    formal = [row for row in ordered if is_formal_chat_message(row)]
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for index, user_row in enumerate(formal):
        user_id = int(user_row.get("id") or 0)
        if user_id in covered_ids:
            continue
        author = str(user_row.get("author") or "").strip().lower()
        if author not in _USER_AUTHORS:
            continue
        if index + 1 >= len(formal):
            continue
        assistant_row = formal[index + 1]
        assistant_id = int(assistant_row.get("id") or 0)
        if assistant_id <= user_id or not _is_formal_assistant(assistant_row):
            continue
        if not str(assistant_row.get("content") or "").strip():
            continue
        if _semantic_cache_info(assistant_row).get("partial_rescue") is not True:
            continue
        user_scope = (user_row.get("_dayblock_context_id"), user_row.get("_dayblock_context_epoch"))
        assistant_scope = (
            assistant_row.get("_dayblock_context_id"),
            assistant_row.get("_dayblock_context_epoch"),
        )
        if None in user_scope or None in assistant_scope or user_scope != assistant_scope:
            continue
        pairs.append((user_row, assistant_row))
        covered_ids.update((user_id, assistant_id))
    return tuple(pairs)


def _dayblock_auxiliary_members(
    rows: tuple[dict[str, Any], ...],
    turns: tuple[Any, ...],
    events: tuple[Any, ...],
    base_members: tuple[Any, ...],
    *,
    static_dir: str | Path = ROOT / "static",
):
    from continuity.contracts import SourceMember
    from continuity.sources import (
        _semantic_cache_info, is_formal_user_source_row, row_revision,
    )
    from chat.attachment_contract import (
        MAX_IMAGE_INPUT_BYTES, persisted_chat_attachments, read_text_attachment_body,
    )

    rows_by_id = {int(row.get("id") or 0): row for row in rows}
    turn_by_message: dict[int, tuple[str, bool]] = {}
    for turn in turns:
        parts = str(turn.turn_id).split(":")
        if len(parts) != 3:
            continue
        assistant = rows_by_id.get(int(parts[2]))
        has_text = bool(str((assistant or {}).get("content") or "").strip())
        turn_by_message[int(parts[1])] = (turn.turn_id, has_text)
        turn_by_message[int(parts[2])] = (turn.turn_id, has_text)
    for event in events:
        parts = str(event.event_id).split(":")
        if len(parts) == 2:
            row = rows_by_id.get(int(parts[1]))
            turn_by_message[int(parts[1])] = (
                event.event_id, bool(str((row or {}).get("content") or "").strip()),
            )

    covered_ids: set[int] = set()
    for member in base_members:
        parts = str(member.source_ref).split(":")
        if member.source_kind == "completed_turn" and len(parts) == 3:
            covered_ids.update((int(parts[1]), int(parts[2])))
        elif member.source_kind == "autonomous_event" and len(parts) == 2:
            covered_ids.add(int(parts[1]))

    visible_pairs = _dayblock_visible_turn_pairs(rows, covered_ids)
    visible_members = [
        _dayblock_visible_turn_member(user_row, assistant_row)
        for user_row, assistant_row in visible_pairs
    ]
    for user_row, assistant_row in visible_pairs:
        visible_ref = f"{DAYBLOCK_VISIBLE_TURN_PREFIX}{int(user_row['id'])}:{int(assistant_row['id'])}"
        turn_by_message[int(user_row["id"])] = (visible_ref, True)
        turn_by_message[int(assistant_row["id"])] = (visible_ref, True)

    attachments: list[dict[str, Any]] = []
    attachment_members: list[Any] = []
    for row in rows:
        mid = int(row.get("id") or 0)
        parent_ref, has_text = turn_by_message.get(mid, (f"row:{mid}", False))
        durable = persisted_chat_attachments(
            row.get("attachments"), legacy_file_url=row.get("file_url", ""),
            legacy_file_name=row.get("file_name", ""), legacy_image_url=row.get("image_url", ""),
        )
        for ordinal, item in enumerate(durable):
            path = _safe_attachment_path(item, static_dir)
            exists = bool(path and path.is_file())
            size = int(path.stat().st_size) if exists and path else None
            file_hash = None
            if exists and path and size is not None and size <= MAX_IMAGE_INPUT_BYTES:
                try:
                    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    exists = False
            extracted = None
            if item.get("type") == "file":
                try:
                    extracted = read_text_attachment_body(
                        item.get("url", ""), item.get("name", ""), static_dir=str(static_dir),
                    )
                except (OSError, ValueError):
                    extracted = None
                status = "canonical_text_extracted" if extracted is not None else "unavailable"
                critical = extracted is None
            else:
                status = "binary_available_not_embedded" if exists else "metadata_only_unavailable"
                critical = not has_text
            ref = f"attachment:{mid}:{ordinal}"
            descriptor = {
                "source_ref": ref, "message_id": mid, "parent_source_ref": parent_ref,
                "type": str(item.get("type") or "unknown"), "url": str(item.get("url") or ""),
                "name": str(item.get("name") or ""), "ordinal": ordinal,
                "file_exists": exists, "byte_length": size, "file_sha256": file_hash,
                "content_status": status, "text_context_available": has_text,
                "critical_unavailable": critical,
                "extracted_text_sha256": hashlib.sha256(extracted.encode("utf-8")).hexdigest()
                    if extracted is not None else None,
                "extracted_text": extracted,
            }
            revision = hashlib.sha256(json.dumps({
                "parent_row_revision": row_revision(row),
                "attachment": {k: v for k, v in descriptor.items() if k != "extracted_text"},
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            descriptor["source_revision"] = revision
            attachments.append(descriptor)
            attachment_members.append(SourceMember(
                seq=0, source_kind="attachment_span", source_ref=ref,
                source_revision=revision, role="attachment", content_hash=revision,
                logical_size=_token_estimate(json.dumps(
                    {k: v for k, v in descriptor.items() if k != "extracted_text"},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                )),
                created_at=str(row.get("created_at") or ""), branch_id="active-transcript",
            ))

    from chat.daily_context import is_formal_chat_message
    unmatched = [
        row for row in rows
        if is_formal_chat_message(row) and is_formal_user_source_row(row)
        and int(row.get("id") or 0) not in covered_ids
    ]
    incomplete: list[Any] = []
    for row in unmatched:
        mid = int(row.get("id") or 0)
        later_assistant = False
        for candidate in rows:
            cid = int(candidate.get("id") or 0)
            if cid <= mid:
                continue
            if not is_formal_chat_message(candidate):
                continue
            author = str(candidate.get("author") or "").strip().lower()
            if author in {"hayana", "haya", "user"}:
                break
            if author in {"fyodor", "claude", "assistant"}:
                # An empty partial rescue has no visible assistant evidence;
                # keep the user as an incomplete projection and leave the
                # assistant row uncovered rather than silently dropping it.
                if (
                    not str(candidate.get("content") or "").strip()
                    and _semantic_cache_info(candidate).get("partial_rescue") is True
                ):
                    break
                later_assistant = True
                break
        if later_assistant:
            continue
        revision = row_revision(row)
        incomplete.append(SourceMember(
            seq=0, source_kind="incomplete_user_turn",
            source_ref=f"incomplete_user:{mid}", source_revision=revision,
            role="user", content_hash=revision,
            logical_size=_token_estimate(json.dumps({
                "id": mid, "author": row.get("author"), "content": row.get("content"),
                "created_at": row.get("created_at"),
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            created_at=str(row.get("created_at") or ""), branch_id="active-transcript",
        ))

    blockers = tuple(
        "dayblock_critical_attachment_content_unavailable"
        for item in attachments if item["critical_unavailable"]
    )
    return tuple(visible_members + incomplete + attachment_members), tuple(attachments), blockers


def derive_source_contract(
    rows: tuple[dict[str, Any], ...],
    source_day: str,
    created_at: str,
    *,
    static_dir: str | Path = ROOT / "static",
):
    from continuity.coverage import source_hash, validate_exact_coverage
    from continuity.sources import (
        POLICY_VERSION, build_source_members, build_source_snapshot,
        derive_autonomous_events, derive_completed_turns, enumerate_candidate_source_refs,
    )

    turns = derive_completed_turns(rows, identity_id=IDENTITY_ID, chat_id=CHAT_ID)
    events = derive_autonomous_events(rows, identity_id=IDENTITY_ID, chat_id=CHAT_ID)
    base_members = build_source_members(turns, events)
    expected = enumerate_candidate_source_refs(rows)
    coverage = validate_exact_coverage(base_members, expected_source_refs=expected)
    actual_refs = tuple(member.source_ref for member in base_members)
    if not coverage.valid or len(actual_refs) != len(expected) or set(actual_refs) != set(expected):
        raise RuntimeError("source_membership_not_exact")
    base_snapshot = build_source_snapshot(
        turns=turns, events=events, local_day=source_day,
        source_watermark=max((int(row.get("id") or 0) for row in rows), default=0),
        created_at=created_at, identity_id=IDENTITY_ID, chat_id=CHAT_ID,
        policy_version=POLICY_VERSION,
    )
    if base_snapshot.source_hash != coverage.source_hash:
        raise RuntimeError("source_snapshot_hash_mismatch")
    auxiliary, _attachment_records, _blockers = _dayblock_auxiliary_members(
        rows, turns, events, base_members, static_dir=static_dir,
    )
    members = tuple(
        replace(member, seq=index)
        for index, member in enumerate((*base_members, *auxiliary))
    )
    digest = source_hash(members)
    snapshot = replace(
        base_snapshot,
        snapshot_id=f"dayblock-source:{source_day}:{base_snapshot.source_watermark}:{digest[:16]}",
        source_hash=digest,
        members=members,
    )
    return snapshot, turns, events, members


def uncovered_formal_source_rows(rows: tuple[dict[str, Any], ...], members: tuple[Any, ...]):
    """Formal canonical rows not represented by turns, Wake, or explicit incomplete user input."""
    from chat.daily_context import is_formal_chat_message

    covered = set()
    for member in members:
        parts = str(member.source_ref).split(":")
        if member.source_kind == "completed_turn" and len(parts) == 3:
            covered.update((int(parts[1]), int(parts[2])))
        elif member.source_kind == "autonomous_event" and len(parts) == 2:
            covered.add(int(parts[1]))
        elif _dayblock_visible_turn_ids(member.source_ref) is not None:
            covered.update(_dayblock_visible_turn_ids(member.source_ref) or ())
        elif _is_dayblock_incomplete_user_member(member) and len(parts) == 2:
            covered.add(int(parts[1]))
    return tuple(row for row in rows
                 if is_formal_chat_message(row) and int(row.get("id") or 0) not in covered)


def materialize_raw_evidence(
    members: tuple[Any, ...],
    rows: tuple[dict[str, Any], ...],
    *,
    static_dir: str | Path = ROOT / "static",
):
    from continuity.materialization import (
        _render_turn, _render_wake, _rows_by_id, materialize_source_members,
    )
    from continuity.sources import derive_autonomous_events, derive_completed_turns

    base_members = tuple(m for m in members if m.source_kind in {"completed_turn", "autonomous_event"})
    base_materialized = materialize_source_members(base_members, rows)
    turns = derive_completed_turns(rows, identity_id=IDENTITY_ID, chat_id=CHAT_ID)
    events = derive_autonomous_events(rows, identity_id=IDENTITY_ID, chat_id=CHAT_ID)
    auxiliary, attachment_records, blockers = _dayblock_auxiliary_members(
        rows, turns, events, base_members, static_dir=static_dir,
    )
    current_aux = {member.source_ref: member for member in auxiliary}
    for member in members:
        if member.source_kind in {"completed_turn", "autonomous_event"}:
            continue
        current = current_aux.get(member.source_ref)
        if current is None or current.source_revision != member.source_revision:
            raise RuntimeError("auxiliary_source_revision_changed")
    rows_by_id = _rows_by_id(rows)
    attachments_by_ref = {item["source_ref"]: item for item in attachment_records}
    body_by_ref: dict[str, str] = {}
    for member in base_members:
        if member.source_kind == "completed_turn":
            body_by_ref[member.source_ref] = _render_turn(member.source_ref, rows_by_id)
        else:
            body_by_ref[member.source_ref] = _render_wake(member.source_ref, rows_by_id)
    for member in members:
        visible_ids = _dayblock_visible_turn_ids(member.source_ref)
        if visible_ids is not None:
            user_id, assistant_id = visible_ids
            body_by_ref[member.source_ref] = _render_turn(
                f"turn:{user_id}:{assistant_id}", rows_by_id,
            )
        elif _is_dayblock_incomplete_user_member(member):
            mid = int(member.source_ref.split(":")[1])
            row = rows_by_id.get(mid)
            if row is None:
                raise RuntimeError("incomplete_user_source_missing")
            from continuity.sources import row_revision
            if row_revision(row) != member.source_revision:
                raise RuntimeError("incomplete_user_source_revision_changed")
            body_by_ref[member.source_ref] = "\n".join((
                "[INCOMPLETE USER TURN]", "USER:", str(row.get("content") or ""),
                "FINALITY: no completed assistant response exists in this source day.",
            ))
        elif member.source_kind == "attachment_span":
            record = attachments_by_ref.get(member.source_ref)
            if record is None:
                raise RuntimeError("attachment_source_revision_changed")
            rendered = "[ATTACHMENT EVIDENCE]\n" + _canonical({
                key: value for key, value in record.items() if key != "extracted_text"
            })
            if record.get("extracted_text") is not None:
                rendered += "\n[CANONICAL TEXT CONTENT]\n" + str(record["extracted_text"])
            body_by_ref[member.source_ref] = rendered
        elif member.source_kind not in {"completed_turn", "autonomous_event"}:
            raise RuntimeError("unsupported_dayblock_source_kind")
    source_refs = tuple(str(member.source_ref) for member in members)
    if source_refs != tuple(base_materialized.source_refs) + tuple(
        member.source_ref for member in members if member.source_kind not in {"completed_turn", "autonomous_event"}
    ):
        raise RuntimeError("materialization_membership_mismatch")
    body = "\n\n".join(body_by_ref[ref] for ref in source_refs)
    fingerprint = _hash(_canonical([
        (member.seq, member.source_kind, member.source_ref, member.source_revision, member.content_hash)
        for member in members
    ]))
    return DayBlockMaterialization(
        body=body, source_token_estimate=_token_estimate(body),
        source_fingerprint=fingerprint, source_refs=source_refs,
        member_bodies=body_by_ref, attachments=tuple(attachment_records),
        blockers=tuple(blockers),
    )


def plan_representation(raw_token_estimate: int, raw_budget_tokens: int | None) -> str:
    if raw_budget_tokens is None or int(raw_budget_tokens) <= 0:
        return "undetermined_raw_budget"
    if int(raw_token_estimate) <= int(raw_budget_tokens):
        return "raw_exact_membership"
    return "continuity_index_plus_raw_evidence_members"


def attachment_reference_count(rows: tuple[dict[str, Any], ...]) -> int:
    from chat.attachment_contract import persisted_chat_attachments

    return sum(len(persisted_chat_attachments(
        row.get("attachments"), legacy_file_url=row.get("file_url", ""),
        legacy_file_name=row.get("file_name", ""), legacy_image_url=row.get("image_url", ""),
    )) for row in rows)


def build_generation_input_plan(
    members: tuple[Any, ...],
    raw_member_bodies: dict[str, str],
    static_prefix: str,
    raw_budget_tokens: int | None,
    chunks: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Build a deterministic no-drop plan from exact source refs and ready chunks."""
    refs = tuple(str(member.source_ref) for member in members)
    if len(set(refs)) != len(refs):
        raise RuntimeError("duplicate_generation_source_ref")
    if set(raw_member_bodies) != set(refs):
        raise RuntimeError("generation_raw_materialization_incomplete")
    raw_body = "\n\n".join(raw_member_bodies[ref] for ref in refs)
    raw_input_tokens = _token_estimate(static_prefix + "\n\n" + raw_body)

    def coverage(raw_refs: list[str], chunk_refs: list[str], blockers: list[str], mode: str,
                 input_tokens: int, fit: bool | None, budget: int | None,
                 selected_chunks: list[dict[str, Any]]) -> dict[str, Any]:
        represented = set(raw_refs) | set(chunk_refs)
        count = len(refs)
        return {
            "mode": mode, "measurement_semantics": "heuristic_cjk1_ascii4_v1",
            "input_token_estimate": input_tokens,
            "budget_tokens": budget, "budget_fit": fit,
            "source_refs": list(refs), "raw_refs": raw_refs, "chunk_refs": chunk_refs,
            "raw_coverage": len(raw_refs) / count if count else 0.0,
            "chunk_coverage": len(chunk_refs) / count if count else 0.0,
            "source_coverage": len(represented) / count if count else 0.0,
            "uncovered_source_count": count - len(represented),
            "selected_chunks": selected_chunks, "blocking_reasons": blockers,
        }

    if raw_budget_tokens is None or int(raw_budget_tokens) <= 0:
        return coverage(
            list(refs), [], ["dayblock_model_context_budget_unverified"],
            "direct_raw", raw_input_tokens, None, None, [],
        )
    budget = int(raw_budget_tokens)
    if raw_input_tokens <= budget:
        return coverage(list(refs), [], [], "direct_raw", raw_input_tokens, True, budget, [])

    expected = {str(member.source_ref): member for member in members}
    accepted: list[dict[str, Any]] = []
    covered: set[str] = set()
    for chunk in sorted(chunks, key=lambda value: str(value.get("chunk_id") or "")):
        if str(chunk.get("status") or "") != "ready":
            continue
        body = str(chunk.get("body") or "")
        if not body or _hash(body) != str(chunk.get("body_hash") or ""):
            continue
        source_members = tuple(chunk.get("source_members") or ())
        if not source_members:
            continue
        chunk_refs: list[str] = []
        valid = True
        for item in source_members:
            ref = str(item.get("source_ref") or "")
            revision = str(item.get("source_revision") or "")
            member = expected.get(ref)
            if (
                member is None or member.source_kind != "completed_turn"
                or str(member.source_revision) != revision or ref in covered
            ):
                valid = False
                break
            chunk_refs.append(ref)
        if not valid or len(set(chunk_refs)) != len(chunk_refs):
            continue
        covered.update(chunk_refs)
        accepted.append({
            "chunk_id": str(chunk.get("chunk_id")),
            "body": body, "body_hash": str(chunk["body_hash"]),
            "source_refs": chunk_refs, "source_token_estimate": _token_estimate(body),
        })

    replacement_by_first = {chunk["source_refs"][0]: chunk for chunk in accepted}
    chunked_refs = {ref for chunk in accepted for ref in chunk["source_refs"]}
    raw_refs: list[str] = []
    chunk_refs: list[str] = []
    segments: list[str] = []
    for ref in refs:
        chunk = replacement_by_first.get(ref)
        if chunk:
            segments.append("[CONTINUITY CHUNK " + chunk["chunk_id"] + "]\n" + chunk["body"])
            chunk_refs.extend(chunk["source_refs"])
        elif ref in chunked_refs:
            continue
        else:
            segments.append(raw_member_bodies[ref])
            raw_refs.append(ref)
    input_tokens = _token_estimate(static_prefix + "\n\n" + "\n\n".join(segments))
    blockers = []
    if len(set(raw_refs) | set(chunk_refs)) != len(refs):
        blockers.append("dayblock_source_coverage_incomplete")
    if input_tokens > budget:
        blockers.append("dayblock_generation_input_exceeds_budget")
    selected = [{key: value for key, value in chunk.items() if key != "body"} for chunk in accepted]
    return coverage(
        raw_refs, chunk_refs, blockers, "mixed" if accepted else "direct_raw",
        input_tokens, input_tokens <= budget, budget, selected,
    )


def frozen_model_context_budget(provider: str, model_identity: str) -> int | None:
    """Return a conservative input budget for exact documented model identities.

    The documented 128K maximum output is reserved from the 1M context window,
    leaving a deterministic 872K input budget.
    """
    if provider == "claude_code" and model_identity in {
        "explicit:claude-opus-5", "explicit:claude-opus-5-5",
    }:
        return 872_000
    return None


def capture_persona_snapshot(path: str | Path = PERSONA_RUNTIME_PATH) -> dict[str, Any]:
    """Read runtime persona bytes without calling its bootstrap or write path."""
    target = Path(path)
    raw = target.read_bytes()
    text = raw.decode("utf-8")
    if not text.strip():
        raise RuntimeError("primary_persona_empty")
    return {
        "revision": "runtime_persona_sha256:" + hashlib.sha256(raw).hexdigest(),
        "token_estimate": _token_estimate(text),
        "source_mtime_ns": target.stat().st_mtime_ns,
        "text": text,
    }


def scheduled_authority_capture_valid(source_day: str, captured_at: str) -> bool:
    local = dt.datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
    local = local.replace(tzinfo=TZ) if local.tzinfo is None else local.astimezone(TZ)
    next_day = dt.date.fromisoformat(validate_source_day(source_day)) + dt.timedelta(days=1)
    return local.date() == next_day and local.hour == 3 and local.minute == 0


def _frozen_authority_for_source_day(preview_db: str | Path, source_day: str):
    path = Path(preview_db)
    if not path.is_file():
        return None
    conn = sqlite3.connect("file:" + path.resolve().as_posix() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        row = conn.execute(
            "SELECT provider,model_identity,authority_revision,authority_snapshot_id,captured_at "
            "FROM dayblock_shadow_jobs WHERE source_day=? "
            "ORDER BY created_at DESC LIMIT 1", (source_day,),
        ).fetchone()
        if row is None:
            return None
        return (
            AuthorityObservation(
                str(row["provider"]), str(row["model_identity"]),
                str(row["authority_revision"]), str(row["authority_snapshot_id"]),
            ),
            str(row["captured_at"]),
        )
    finally:
        conn.close()


def load_ready_continuity_chunks(path: str | Path = CANARY_CONTINUITY_DB) -> tuple[dict[str, Any], ...]:
    target = Path(path)
    if not target.is_file():
        return ()
    conn = sqlite3.connect("file:" + target.resolve().as_posix() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"continuity_chunks", "continuity_candidate_members"} <= tables:
            return ()
        rows = conn.execute(
            "SELECT c.chunk_id,c.body,c.body_hash,c.status,cm.source_ref,cm.source_revision "
            "FROM continuity_chunks c JOIN continuity_candidate_members cm "
            "ON cm.candidate_id=c.candidate_id WHERE c.status='ready' "
            "ORDER BY c.created_at,c.chunk_id,cm.ordinal"
        ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            chunk = grouped.setdefault(str(row["chunk_id"]), {
                "chunk_id": str(row["chunk_id"]), "body": str(row["body"]),
                "body_hash": str(row["body_hash"]), "status": str(row["status"]),
                "source_members": [],
            })
            chunk["source_members"].append({
                "source_ref": str(row["source_ref"]),
                "source_revision": str(row["source_revision"]),
            })
        return tuple(grouped[key] for key in sorted(grouped))
    finally:
        conn.close()


@dataclass(frozen=True)
class AuthorityObservation:
    provider: str
    model_identity: str
    authority_revision: str
    authority_snapshot_id: str


class _ReadOnlyConfig:
    """Read-only config_store.get shim for the formal provider_router resolver."""
    defaults = {"CHAT_PROVIDER": "", "GW_PROVIDER": "api_relay", "CC_CHAT_MODEL": "",
                "ACTIVE_RELAY": "", "MODEL": ""}

    def __init__(self, conn: sqlite3.Connection, env_path: str | Path):
        self.conn, self.env_path = conn, Path(env_path)
        self.values_seen: dict[str, str] = {}
        self.active_relay_model = ""

    def _env(self, key: str) -> str | None:
        try:
            found = None
            with self.env_path.open(encoding="utf-8") as stream:
                for line in stream:
                    value = line.strip()
                    if value.startswith(key + "="):
                        found = value.split("=", 1)[1].strip()
            return found
        except OSError:
            return None

    def get(self, key: str, default: Any = None) -> str:
        row = self.conn.execute("SELECT value FROM runtime_config WHERE key=?", (key,)).fetchone()
        if row is not None:
            value = str(row[0] or "")
        else:
            value = self._env(key) or (str(default) if default is not None else self.defaults.get(key, ""))
        self.values_seen[key] = value
        return value


def _readonly_relay_model(config: _ReadOnlyConfig) -> str:
    active_id = config.get("ACTIVE_RELAY", "")
    active = None
    if active_id:
        try:
            row = config.conn.execute(
                "SELECT url,default_model FROM relay_presets WHERE id=?", (active_id,),
            ).fetchone()
            if row is not None and str(row[0] or ""):
                active = {"default_model": str(row[1] or "")}
        except sqlite3.Error:
            pass
    config.active_relay_model = str(
        (active or {}).get("default_model") or config.get("MODEL", "")
    ).strip()
    return config.active_relay_model or "unknown"


def capture_primary_authority(source_db: str | Path = PRODUCTION_DB,
                              env_path: str | Path = ENV_PATH) -> AuthorityObservation:
    """Use provider_router.capture_generation_authority with read-only storage seams."""
    conn = open_source_read_only(source_db)
    missing = object()
    names = ("config_store", "chat.cc_model", "relay.manager", "_dayblock_formal_provider_router")
    saved = {name: sys.modules.get(name, missing) for name in names}
    try:
        conn.execute("BEGIN")
        config = _ReadOnlyConfig(conn, env_path)
        config_module = types.ModuleType("config_store")
        config_module.get = config.get
        relay_module = types.ModuleType("relay.manager")
        relay_module.resolve_active_relay_model_identity = lambda: _readonly_relay_model(config)
        sys.modules["config_store"] = config_module
        sys.modules["relay.manager"] = relay_module
        sys.modules.pop("chat.cc_model", None)
        spec = importlib.util.spec_from_file_location(
            "_dayblock_formal_provider_router", ROOT / "chat" / "provider_router.py",
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("primary_authority_resolver_unavailable")
        router = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = router
        spec.loader.exec_module(router)
        snapshot = router.capture_generation_authority()
        inputs = dict(config.values_seen)
        if snapshot.provider == "api_relay":
            inputs["ACTIVE_RELAY_MODEL"] = config.active_relay_model
        revision = _hash(_canonical({
            "provider": snapshot.provider, "model_identity": snapshot.model_identity,
            "resolver_inputs": inputs,
        }))
        snapshot_id = "primary-authority:" + _hash(_canonical({
            "provider": snapshot.provider, "model_identity": snapshot.model_identity,
            "authority_revision": revision,
        }))[:32]
        conn.rollback()
        return AuthorityObservation(snapshot.provider, snapshot.model_identity, revision, snapshot_id)
    finally:
        for name, previous in saved.items():
            if previous is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dayblock_source_snapshots(
 snapshot_id TEXT PRIMARY KEY, identity_id TEXT NOT NULL, chat_id TEXT NOT NULL,
 source_day TEXT NOT NULL, source_start TEXT NOT NULL, source_end TEXT NOT NULL,
 timezone TEXT NOT NULL, source_policy_version TEXT NOT NULL, source_hash TEXT NOT NULL,
 source_revision TEXT NOT NULL, source_member_count INTEGER NOT NULL,
 completed_turn_count INTEGER NOT NULL, autonomous_event_count INTEGER NOT NULL,
 logical_size INTEGER NOT NULL, raw_token_estimate INTEGER NOT NULL,
 materialization_status TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(chat_id,source_day,source_hash));
CREATE TABLE IF NOT EXISTS dayblock_source_members(
 snapshot_id TEXT NOT NULL, seq INTEGER NOT NULL,
 source_kind TEXT NOT NULL CHECK(source_kind IN ('completed_turn','autonomous_event','attachment_span','incomplete_user_turn')),
 source_ref TEXT NOT NULL, source_revision TEXT NOT NULL, content_hash TEXT NOT NULL,
 role TEXT NOT NULL, logical_size INTEGER NOT NULL, created_at TEXT NOT NULL,
 branch_id TEXT NOT NULL, PRIMARY KEY(snapshot_id,seq), UNIQUE(snapshot_id,source_ref),
 FOREIGN KEY(snapshot_id) REFERENCES dayblock_source_snapshots(snapshot_id));
CREATE TRIGGER IF NOT EXISTS dayblock_snapshot_no_update BEFORE UPDATE ON dayblock_source_snapshots
 BEGIN SELECT RAISE(ABORT,'immutable DayBlock source snapshot'); END;
CREATE TRIGGER IF NOT EXISTS dayblock_snapshot_no_delete BEFORE DELETE ON dayblock_source_snapshots
 BEGIN SELECT RAISE(ABORT,'immutable DayBlock source snapshot'); END;
CREATE TRIGGER IF NOT EXISTS dayblock_member_no_update BEFORE UPDATE ON dayblock_source_members
 BEGIN SELECT RAISE(ABORT,'immutable DayBlock membership'); END;
CREATE TRIGGER IF NOT EXISTS dayblock_member_no_delete BEFORE DELETE ON dayblock_source_members
 BEGIN SELECT RAISE(ABORT,'immutable DayBlock membership'); END;
CREATE TABLE IF NOT EXISTS dayblock_shadow_jobs(
 job_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE, snapshot_id TEXT NOT NULL,
 identity_id TEXT NOT NULL, chat_id TEXT NOT NULL, source_day TEXT NOT NULL,
 source_hash TEXT NOT NULL, source_revision TEXT NOT NULL, policy_version TEXT NOT NULL,
 generator_policy_version TEXT NOT NULL,
 provider TEXT NOT NULL CHECK(provider IN ('claude_code','api_relay')),
 model_identity TEXT NOT NULL, authority_revision TEXT NOT NULL,
 authority_snapshot_id TEXT NOT NULL, captured_at TEXT NOT NULL, persona_revision TEXT,
 prompt_policy_version TEXT NOT NULL, prompt_hash TEXT NOT NULL, prompt_contract_json TEXT NOT NULL,
 representation_plan TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('planned','blocked','stale','failed','ready')),
 blocking_reasons_json TEXT NOT NULL, created_at TEXT NOT NULL,
 uncovered_formal_row_count INTEGER NOT NULL DEFAULT 0,
 UNIQUE(chat_id,source_day,source_revision,policy_version),
 FOREIGN KEY(snapshot_id) REFERENCES dayblock_source_snapshots(snapshot_id));
CREATE TRIGGER IF NOT EXISTS dayblock_job_authority_immutable
 BEFORE UPDATE OF candidate_id,snapshot_id,source_hash,source_revision,policy_version,
 provider,model_identity,authority_revision,authority_snapshot_id,captured_at,
 persona_revision,prompt_policy_version,prompt_hash,prompt_contract_json
 ON dayblock_shadow_jobs
 BEGIN SELECT RAISE(ABORT,'immutable DayBlock frozen job provenance'); END;
CREATE TABLE IF NOT EXISTS dayblock_generation_receipts(
 generation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, generation_job_id TEXT NOT NULL UNIQUE,
 candidate_id TEXT NOT NULL, source_day TEXT NOT NULL UNIQUE, source_hash TEXT NOT NULL,
 source_revision TEXT NOT NULL, source_fingerprint TEXT NOT NULL,
 provider TEXT NOT NULL, model_identity TEXT NOT NULL, authority_revision TEXT NOT NULL,
 authority_snapshot_id TEXT NOT NULL, persona_revision TEXT NOT NULL,
 prompt_policy_version TEXT NOT NULL, prompt_hash TEXT NOT NULL,
 generator_policy_version TEXT NOT NULL, input_mode TEXT NOT NULL,
 source_member_count INTEGER NOT NULL, source_coverage REAL NOT NULL,
 raw_token_estimate INTEGER NOT NULL, budget_tokens INTEGER NOT NULL,
 input_token_estimate INTEGER NOT NULL, output_token_estimate INTEGER NOT NULL DEFAULT 0,
 actual_executor TEXT, usage_json TEXT NOT NULL DEFAULT '{}', summary_body TEXT, body_hash TEXT,
 status TEXT NOT NULL CHECK(status IN ('processing','ready','failed','stale')),
 error_code TEXT, created_at TEXT NOT NULL, finished_at TEXT,
 FOREIGN KEY(generation_job_id) REFERENCES dayblock_shadow_jobs(job_id));
CREATE TRIGGER IF NOT EXISTS dayblock_receipt_provenance_immutable
 BEFORE UPDATE OF generation_id,run_id,generation_job_id,candidate_id,source_day,source_hash,
 source_revision,source_fingerprint,provider,model_identity,authority_revision,
 authority_snapshot_id,persona_revision,prompt_policy_version,prompt_hash,
 generator_policy_version,input_mode,source_member_count,source_coverage,
 raw_token_estimate,budget_tokens,input_token_estimate,created_at
 ON dayblock_generation_receipts
 BEGIN SELECT RAISE(ABORT,'immutable DayBlock generation receipt provenance'); END;
CREATE TRIGGER IF NOT EXISTS dayblock_ready_receipt_no_update
 BEFORE UPDATE ON dayblock_generation_receipts WHEN OLD.status='ready'
 BEGIN SELECT RAISE(ABORT,'immutable ready DayBlock generation receipt'); END;
CREATE TRIGGER IF NOT EXISTS dayblock_receipt_no_delete
 BEFORE DELETE ON dayblock_generation_receipts
 BEGIN SELECT RAISE(ABORT,'immutable DayBlock generation receipt'); END;

"""



def open_preview_store(path: str | Path = PREVIEW_DB) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA_SQL)
    member_sql = str(conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='dayblock_source_members'"
    ).fetchone()[0])
    if "incomplete_user_turn" not in member_sql:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DROP TRIGGER IF EXISTS dayblock_member_no_update")
            conn.execute("DROP TRIGGER IF EXISTS dayblock_member_no_delete")
            conn.execute("""CREATE TABLE dayblock_source_members_r1(
                snapshot_id TEXT NOT NULL, seq INTEGER NOT NULL,
                source_kind TEXT NOT NULL CHECK(source_kind IN (
                  'completed_turn','autonomous_event','attachment_span','incomplete_user_turn')),
                source_ref TEXT NOT NULL, source_revision TEXT NOT NULL, content_hash TEXT NOT NULL,
                role TEXT NOT NULL, logical_size INTEGER NOT NULL, created_at TEXT NOT NULL,
                branch_id TEXT NOT NULL, PRIMARY KEY(snapshot_id,seq), UNIQUE(snapshot_id,source_ref),
                FOREIGN KEY(snapshot_id) REFERENCES dayblock_source_snapshots(snapshot_id))""")
            conn.execute("INSERT INTO dayblock_source_members_r1 SELECT * FROM dayblock_source_members")
            conn.execute("DROP TABLE dayblock_source_members")
            conn.execute("ALTER TABLE dayblock_source_members_r1 RENAME TO dayblock_source_members")
            conn.execute("""CREATE TRIGGER dayblock_member_no_update BEFORE UPDATE ON dayblock_source_members
                BEGIN SELECT RAISE(ABORT,'immutable DayBlock membership'); END""")
            conn.execute("""CREATE TRIGGER dayblock_member_no_delete BEFORE DELETE ON dayblock_source_members
                BEGIN SELECT RAISE(ABORT,'immutable DayBlock membership'); END""")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    columns = {row[1] for row in conn.execute("PRAGMA table_info(dayblock_shadow_jobs)")}
    for name, definition in (
        ("uncovered_formal_row_count", "INTEGER NOT NULL DEFAULT 0"),
        ("persona_captured_at", "TEXT"),
        ("raw_budget_tokens", "INTEGER"),
        ("input_plan_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE dayblock_shadow_jobs ADD COLUMN {name} {definition}")
    conn.execute("DROP TRIGGER IF EXISTS dayblock_job_authority_immutable")
    conn.execute("""CREATE TRIGGER dayblock_job_authority_immutable
        BEFORE UPDATE OF candidate_id,snapshot_id,source_hash,source_revision,policy_version,
          generator_policy_version,provider,model_identity,authority_revision,authority_snapshot_id,
          captured_at,persona_revision,persona_captured_at,prompt_policy_version,prompt_hash,
          prompt_contract_json,representation_plan,raw_budget_tokens,input_plan_json
        ON dayblock_shadow_jobs
        BEGIN SELECT RAISE(ABORT,'immutable DayBlock frozen job provenance'); END""")
    conn.commit()
    return conn


def authority_for_retry(frozen: dict[str, str], current: dict[str, str]) -> dict[str, str]:
    del current
    return dict(frozen)


def _identity_ids(day: str, source_hash: str, revision: str,
                  authority: AuthorityObservation) -> tuple[str, str]:
    identity = {
        "identity_id": IDENTITY_ID, "chat_id": CHAT_ID, "source_day": day,
        "source_snapshot_hash": source_hash, "source_revision": revision,
        "policy_version": DAYBLOCK_POLICY_VERSION, "provider": authority.provider,
        "model_identity": authority.model_identity,
        "authority_snapshot_id": authority.authority_snapshot_id,
    }
    candidate = "dayblock-candidate:" + _hash(_canonical(identity))[:32]
    return candidate, "dayblock-job:" + _hash(candidate)[:32]


def persist_shadow_job(
    conn: sqlite3.Connection,
    snapshot: Any,
    turns: tuple[Any, ...],
    events: tuple[Any, ...],
    members: tuple[Any, ...],
    materialized: Any,
    rows: tuple[dict[str, Any], ...],
    source_revision: str,
    created_at: str,
    capture: Callable[[], AuthorityObservation],
    raw_budget_tokens: int | None = None,
    uncovered_formal_row_count: int = 0,
    *,
    persona_revision: str | None = None,
    persona_captured_at: str | None = None,
    input_plan: dict[str, Any] | None = None,
    extra_blockers: tuple[str, ...] = (),
    frozen_authority: AuthorityObservation | None = None,
    frozen_authority_captured_at: str | None = None,
) -> dict[str, Any]:
    start, end = natural_day_window(snapshot.local_day)
    plan = dict(input_plan or {})
    representation = str(plan.get("mode") or plan_representation(
        materialized.source_token_estimate, raw_budget_tokens,
    ))
    blockers = list(dict.fromkeys(
        list(plan.get("blocking_reasons") or [])
        + list(materialized.blockers)
        + list(extra_blockers)
    ))
    if not persona_revision:
        blockers.append("dayblock_persona_revision_unavailable")
    if uncovered_formal_row_count:
        blockers.append("dayblock_uncovered_formal_source_rows")
    if plan.get("uncovered_source_count", 0):
        blockers.append("dayblock_source_coverage_incomplete")
    if not plan or float(plan.get("source_coverage", 0.0)) != 1.0:
        blockers.append("dayblock_source_coverage_not_exact")
    if plan.get("budget_fit") is not True:
        blockers.append("dayblock_generation_budget_not_verified")

    conn.execute("BEGIN IMMEDIATE")
    try:
        old = conn.execute(
            "SELECT * FROM dayblock_shadow_jobs WHERE chat_id=? AND source_day=? "
            "AND source_revision=? AND policy_version=?",
            (CHAT_ID, snapshot.local_day, source_revision, DAYBLOCK_POLICY_VERSION),
        ).fetchone()
        if old:
            authority = AuthorityObservation(
                old["provider"], old["model_identity"], old["authority_revision"],
                old["authority_snapshot_id"],
            )
            candidate, job = old["candidate_id"], old["job_id"]
            captured_at = str(old["captured_at"])
            if not scheduled_authority_capture_valid(snapshot.local_day, captured_at):
                blockers.append("dayblock_primary_authority_not_captured_at_scheduled_0300")
            old_persona = str(old["persona_revision"] or "")
            if not old_persona:
                blockers.append("dayblock_persona_revision_unfrozen")
                persona_revision = None
            else:
                if persona_revision != old_persona:
                    blockers.append("dayblock_persona_revision_changed_after_freeze")
                persona_revision = old_persona
                persona_captured_at = str(old["persona_captured_at"] or old["captured_at"])
            try:
                frozen_plan = json.loads(str(old["input_plan_json"] or "{}"))
            except (TypeError, ValueError):
                frozen_plan = {}
            if not frozen_plan:
                blockers.append("dayblock_generation_input_plan_unfrozen")
            if not frozen_plan:
                blockers.append("dayblock_generation_input_plan_unfrozen")
            plan = frozen_plan
            representation = str(old["representation_plan"])
            raw_budget_tokens = old["raw_budget_tokens"]
            replay = True
            status = "blocked" if blockers else str(old["status"])
            conn.execute(
                "UPDATE dayblock_shadow_jobs SET status=?,blocking_reasons_json=?,"
                "uncovered_formal_row_count=? WHERE job_id=?",
                (status, _canonical(list(dict.fromkeys(blockers))),
                 int(uncovered_formal_row_count), job),
            )
        else:
            authority = frozen_authority or capture()
            captured_at = str(frozen_authority_captured_at or created_at)
            if not scheduled_authority_capture_valid(snapshot.local_day, captured_at):
                blockers.append("dayblock_primary_authority_not_captured_at_scheduled_0300")
            candidate, job = _identity_ids(
                snapshot.local_day, snapshot.source_hash, source_revision, authority,
            )
            status = "blocked" if blockers else "planned"
            conn.execute(
                "UPDATE dayblock_shadow_jobs SET status='stale' WHERE chat_id=? AND source_day=? "
                "AND source_revision!=? AND status IN ('planned','blocked')",
                (CHAT_ID, snapshot.local_day, source_revision),
            )
            conn.execute(
                "INSERT OR IGNORE INTO dayblock_source_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (snapshot.snapshot_id, IDENTITY_ID, CHAT_ID, snapshot.local_day, start, end,
                 "Asia/Shanghai", snapshot.policy_version, snapshot.source_hash, source_revision,
                 len(members), len(turns), len(events),
                 sum(int(item.logical_size) for item in members), materialized.source_token_estimate,
                 "blocked" if materialized.blockers or uncovered_formal_row_count else "ready",
                 created_at),
            )
            for member in members:
                conn.execute(
                    "INSERT OR IGNORE INTO dayblock_source_members VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (snapshot.snapshot_id, member.seq, member.source_kind, member.source_ref,
                     member.source_revision, member.content_hash, member.role, member.logical_size,
                     member.created_at, member.branch_id),
                )
            conn.execute(
                "INSERT INTO dayblock_shadow_jobs("
                "job_id,candidate_id,snapshot_id,identity_id,chat_id,source_day,source_hash,"
                "source_revision,policy_version,generator_policy_version,provider,model_identity,"
                "authority_revision,authority_snapshot_id,captured_at,persona_revision,"
                "prompt_policy_version,prompt_hash,prompt_contract_json,representation_plan,status,"
                "blocking_reasons_json,created_at,uncovered_formal_row_count,persona_captured_at,"
                "raw_budget_tokens,input_plan_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job, candidate, snapshot.snapshot_id, IDENTITY_ID, CHAT_ID, snapshot.local_day,
                 snapshot.source_hash, source_revision, DAYBLOCK_POLICY_VERSION,
                 GENERATOR_POLICY_VERSION, authority.provider, authority.model_identity,
                 authority.authority_revision, authority.authority_snapshot_id, captured_at,
                 persona_revision, PROMPT_POLICY_VERSION, PROMPT_CONTRACT_HASH,
                 PROMPT_CONTRACT_JSON, representation, status,
                 _canonical(list(dict.fromkeys(blockers))), created_at,
                 int(uncovered_formal_row_count), persona_captured_at,
                 raw_budget_tokens, _canonical(plan)),
            )
            replay = False
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    frozen = {
        "provider": authority.provider, "model_identity": authority.model_identity,
        "authority_revision": authority.authority_revision,
        "authority_snapshot_id": authority.authority_snapshot_id, "captured_at": captured_at,
    }
    return {
        "job_id": job, "candidate_id": candidate, "frozen_authority": frozen,
        "persona_revision": persona_revision, "persona_captured_at": persona_captured_at,
        "representation_plan": representation, "input_plan": plan,
        "blocking_reasons": list(dict.fromkeys(blockers)), "status": status,
        "idempotent_replay": replay,
        "ready_to_generate": status == "planned" and not blockers,
    }


def build_shadow_plan(
    source_db: str | Path,
    preview_db: str | Path,
    source_day: str,
    *,
    now: dt.datetime | None = None,
    raw_budget_tokens: int | None = None,
    capture_authority: Callable[[], AuthorityObservation] | None = None,
    frozen_authority: AuthorityObservation | None = None,
    frozen_authority_captured_at: str | None = None,
    persona_snapshot: dict[str, Any] | None = None,
    persona_captured_at: str | None = None,
    pre_captured: bool = False,
    include_runtime: bool = False,
) -> dict[str, Any]:
    day = validate_source_day(source_day)
    start, end = natural_day_window(day)
    local_now = now or dt.datetime.now(TZ)
    local_now = local_now.replace(tzinfo=TZ) if local_now.tzinfo is None else local_now.astimezone(TZ)
    created_at = local_now.isoformat(timespec="seconds")
    rows = discover_natural_day_rows(source_db, day)
    if not rows:
        return {
            "status": "blocked", "source_day": day, "source_start": start, "source_end": end,
            "timezone": "Asia/Shanghai", "source_member_count": 0, "completed_turn_count": 0,
            "autonomous_event_count": 0, "incomplete_user_turn_count": 0,
            "attachment_reference_count": 0, "logical_size": 0, "raw_token_estimate": 0,
            "generation_input_mode": "unavailable", "generation_input_token_estimate": 0,
            "raw_coverage": 0.0, "chunk_coverage": 0.0, "uncovered_source_count": 0,
            "source_snapshot_id": None, "source_snapshot_hash": None, "candidate_id": None,
            "job_id": None, "frozen_primary_authority": None, "authority_captured_at": None,
            "frozen_persona_revision": None, "prompt_policy": {
                "version": PROMPT_POLICY_VERSION, "hash": PROMPT_CONTRACT_HASH,
            },
            "materialization_status": "unavailable", "ready_to_generate": False,
            "blocking_reasons": ["no_canonical_source_for_source_day"],
            "model_call_count": 0, "preview_store": str(preview_db),
        }

    snapshot, turns, events, members = derive_source_contract(rows, day, created_at)
    materialized = materialize_raw_evidence(members, rows)
    uncovered = uncovered_formal_source_rows(rows, members)
    from continuity.contracts import candidate_source_revision
    revision = candidate_source_revision(members)
    if revision != candidate_source_revision(snapshot.members):
        raise RuntimeError("source_revision_mismatch")

    if pre_captured:
        persona = persona_snapshot
        persona_error = "" if persona else "dayblock_primary_persona_unavailable"
        authority = frozen_authority
        authority_captured_at = frozen_authority_captured_at
    else:
        try:
            persona = capture_persona_snapshot()
        except (OSError, UnicodeError, RuntimeError):
            persona = None
            persona_error = "dayblock_primary_persona_unavailable"
        else:
            persona_error = ""

        frozen = _frozen_authority_for_source_day(preview_db, day)
        if frozen is not None:
            authority, authority_captured_at = frozen
        elif scheduled_authority_capture_valid(day, created_at):
            authority = (capture_authority or (lambda: capture_primary_authority(source_db)))()
            authority_captured_at = created_at
        else:
            authority = None
            authority_captured_at = None

    if authority is None:
        return {
            "status": "blocked", "source_day": day, "source_start": start, "source_end": end,
            "timezone": "Asia/Shanghai", "source_member_count": len(members),
            "source_members": [
                {"source_kind": member.source_kind, "source_ref": member.source_ref,
                 "source_revision": member.source_revision}
                for member in members
            ],
            "completed_turn_count": len(turns), "autonomous_event_count": len(events),
            "incomplete_user_turn_count": sum(_is_dayblock_incomplete_user_member(m) for m in members),
            "attachment_reference_count": attachment_reference_count(rows),
            "logical_size": sum(int(member.logical_size) for member in members),
            "raw_token_estimate": materialized.source_token_estimate,
            "generation_input_mode": "direct_raw",
            "generation_input_token_estimate": None,
            "raw_coverage": 1.0, "chunk_coverage": 0.0,
            "uncovered_source_count": len(uncovered),
            "source_snapshot_id": snapshot.snapshot_id, "source_snapshot_hash": snapshot.source_hash,
            "candidate_id": None, "job_id": None, "frozen_primary_authority": None,
            "authority_captured_at": None,
            "frozen_persona_revision": persona["revision"] if persona else None,
            "prompt_policy": {"version": PROMPT_POLICY_VERSION, "hash": PROMPT_CONTRACT_HASH},
            "materialization_status": "ready" if not materialized.blockers else "blocked",
            "ready_to_generate": False,
            "blocking_reasons": ["dayblock_authority_not_captured_at_scheduled_0300"],
            "model_call_count": 0, "preview_store": str(preview_db),
        }

    model_budget = (
        int(raw_budget_tokens) if raw_budget_tokens is not None
        else frozen_model_context_budget(authority.provider, authority.model_identity)
    )
    persona_text = persona["text"] if persona else ""
    static_prefix = build_prompt_static_prefix(PROMPT_CONTRACT_JSON, persona_text, day)
    input_plan = build_generation_input_plan(
        members, materialized.member_bodies, static_prefix, model_budget,
        load_ready_continuity_chunks(),
    )
    extra_blockers = list(materialized.blockers)
    if persona_error:
        extra_blockers.append(persona_error)
    if not scheduled_authority_capture_valid(day, authority_captured_at):
        extra_blockers.append("dayblock_primary_authority_not_captured_at_scheduled_0300")
    if persona and persona["source_mtime_ns"] > 0:
        # Identity is a direct SHA-256 of the existing runtime authority.
        # The mtime is diagnostic only and is never treated as another selector.
        pass

    confirm_rows = discover_natural_day_rows(source_db, day)
    confirm_snapshot, _, _, confirm_members = derive_source_contract(confirm_rows, day, created_at)
    if confirm_snapshot.source_hash != snapshot.source_hash or (
        candidate_source_revision(confirm_members) != revision
    ):
        extra_blockers.append("dayblock_source_changed_during_planning")

    store = open_preview_store(preview_db)
    try:
        job = persist_shadow_job(
            store, snapshot, turns, events, members, materialized, rows, revision, created_at,
            capture_authority or (lambda: capture_primary_authority(source_db)),
            model_budget, len(uncovered),
            persona_revision=persona["revision"] if persona else None,
            persona_captured_at=(persona_captured_at or created_at) if persona else None,
            input_plan=input_plan, extra_blockers=tuple(extra_blockers),
            frozen_authority=authority, frozen_authority_captured_at=authority_captured_at,
        )
    finally:
        store.close()

    attachments = [
        {key: item.get(key) for key in (
            "source_ref", "message_id", "parent_source_ref", "type", "name",
            "file_exists", "byte_length", "content_status", "text_context_available",
            "critical_unavailable", "source_revision",
        )}
        for item in materialized.attachments
    ]
    result = {
        "status": job["status"], "source_day": day, "source_start": start, "source_end": end,
        "timezone": "Asia/Shanghai", "source_member_count": len(members),
        "source_members": [
            {"source_kind": member.source_kind, "source_ref": member.source_ref,
             "source_revision": member.source_revision}
            for member in members
        ],
        "completed_turn_count": len(turns), "autonomous_event_count": len(events),
        "incomplete_user_turn_count": sum(_is_dayblock_incomplete_user_member(m) for m in members),
        "incomplete_user_turn_refs": [
            member.source_ref for member in members if _is_dayblock_incomplete_user_member(member)
        ],
        "canonical_source_row_count": len(rows),
        "logical_size": sum(int(member.logical_size) for member in members),
        "raw_token_estimate": materialized.source_token_estimate,
        "generation_input_mode": input_plan["mode"],
        "generation_input_token_estimate": input_plan["input_token_estimate"],
        "input_budget_tokens": input_plan["budget_tokens"],
        "raw_coverage": input_plan["raw_coverage"],
        "chunk_coverage": input_plan["chunk_coverage"],
        "source_coverage": input_plan["source_coverage"],
        "uncovered_source_count": input_plan["uncovered_source_count"],
        "source_snapshot_id": snapshot.snapshot_id, "source_snapshot_hash": snapshot.source_hash,
        "source_revision": revision, "candidate_id": job["candidate_id"], "job_id": job["job_id"],
        "frozen_primary_authority": job["frozen_authority"],
        "authority_captured_at": job["frozen_authority"]["captured_at"],
        "frozen_persona_revision": job["persona_revision"],
        "persona_token_estimate": persona["token_estimate"] if persona else None,
        "persona_captured_at": job["persona_captured_at"],
        "prompt_policy": {"version": PROMPT_POLICY_VERSION, "hash": PROMPT_CONTRACT_HASH},
        "materialization_status": "blocked" if materialized.blockers else "ready",
        "materialized_source_fingerprint": materialized.source_fingerprint,
        "raw_evidence_refs": list(materialized.source_refs),
        "attachments": attachments,
        "attachment_reference_count": len(attachments),
        "uncovered_formal_source_row_count": len(uncovered),
        "representation_plan": job["representation_plan"],
        "input_plan": input_plan,
        "ready_to_generate": job["ready_to_generate"],
        "blocking_reasons": job["blocking_reasons"],
        "idempotent_replay": job["idempotent_replay"],
        "model_call_count": 0, "preview_store": str(preview_db),
    }
    if include_runtime:
        result["_runtime"] = {
            "rows": rows, "snapshot": snapshot, "turns": turns, "events": events,
            "members": members, "materialized": materialized,
            "persona": persona, "authority": job["frozen_authority"],
            "source_revision": revision, "created_at": created_at,
        }
    return result
