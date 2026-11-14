"""Phone system calendar mirror and Fyodor's calendar write queue.

The Elpis Canary shell (P2I) reads the Android system calendar and uploads a
bounded window of events; it also applies queued writes to one local calendar
that belongs to Fyodor. The server never touches the phone directly: writes
are commands that the phone fetches on its next sync and answers with a
result. Only events in Fyodor's own calendar can be changed or deleted.

Tables live in the main memories database; device authentication stays in
the health device registry (``health_store.authenticate_device``).
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
import uuid
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 1
MAX_EVENTS = 1000
MAX_RESULTS = 50
MAX_COMMANDS_PER_SYNC = 20
MAX_TITLE = 200
MAX_LOCATION = 200
MAX_NOTES = 2000
MAX_DESCRIPTION_STORED = 500
MAX_READ_DAYS = 30
MAX_REMINDER_MINUTES = 7 * 24 * 60
DEFAULT_TIMED_REMINDER_MINUTES = 10
DEFAULT_DURATION_MINUTES = 60
RESEND_AFTER_SECONDS = 10 * 60
MAX_SEND_ATTEMPTS = 3
STALE_AFTER_SECONDS = 2 * 3600
DEFAULT_TIMEZONE = "Asia/Shanghai"
OPS = frozenset({"create", "update", "delete"})

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOCAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$")


class CalendarError(ValueError):
    """Rejected input; the message is safe to show to the model."""


def ensure_schema(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS calendar_sync_meta (
              device_id TEXT PRIMARY KEY,
              last_sync_at TEXT NOT NULL,
              read_granted INTEGER NOT NULL DEFAULT 0,
              write_granted INTEGER NOT NULL DEFAULT 0,
              own_calendar_id TEXT NOT NULL DEFAULT '',
              timezone TEXT NOT NULL DEFAULT '',
              window_start_ms INTEGER,
              window_end_ms INTEGER,
              event_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS calendar_events (
              device_id TEXT NOT NULL,
              event_id TEXT NOT NULL,
              begin_ms INTEGER NOT NULL,
              end_ms INTEGER NOT NULL,
              all_day INTEGER NOT NULL DEFAULT 0,
              calendar_id TEXT NOT NULL DEFAULT '',
              calendar_name TEXT NOT NULL DEFAULT '',
              own INTEGER NOT NULL DEFAULT 0,
              title TEXT NOT NULL DEFAULT '',
              location TEXT NOT NULL DEFAULT '',
              description TEXT NOT NULL DEFAULT '',
              PRIMARY KEY (device_id, event_id, begin_ms)
            );
            CREATE INDEX IF NOT EXISTS idx_calendar_events_begin
              ON calendar_events(device_id, begin_ms);
            CREATE TABLE IF NOT EXISTS calendar_commands (
              id TEXT PRIMARY KEY,
              op TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending',
              created_at TEXT NOT NULL,
              device_id TEXT,
              sent_at TEXT,
              send_attempts INTEGER NOT NULL DEFAULT 0,
              done_at TEXT,
              result_event_id TEXT,
              error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_calendar_commands_state
              ON calendar_commands(state, created_at);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _connect(path: str) -> sqlite3.Connection:
    ensure_schema(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(value: _dt.datetime) -> str:
    return value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc,
        )
    except (TypeError, ValueError):
        return None


def _zone(name: Any) -> _dt.tzinfo:
    try:
        return ZoneInfo(str(name or "").strip() or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def _text(value: Any, field: str, max_length: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise CalendarError(f"{field} must be a string")
    text = value.strip()
    if required and not text:
        raise CalendarError(f"{field} is required")
    if len(text) > max_length:
        raise CalendarError(f"{field} is longer than {max_length} characters")
    return text


def _ms(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CalendarError(f"{field} must be epoch milliseconds")
    if value < 0 or value > 32503680000000:  # year 3000
        raise CalendarError(f"{field} is out of range")
    return value


# ── phone sync ──────────────────────────────────────────────


def _clean_event(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CalendarError("event must be an object")
    event_id = str(raw.get("eventId") or "").strip()
    if not event_id or len(event_id) > 40:
        raise CalendarError("eventId is required")
    begin = _ms(raw.get("beginMs"), "beginMs")
    end = _ms(raw.get("endMs"), "endMs")
    if end < begin:
        end = begin
    return {
        "event_id": event_id,
        "begin_ms": begin,
        "end_ms": end,
        "all_day": 1 if raw.get("allDay") is True else 0,
        "calendar_id": str(raw.get("calendarId") or "")[:40],
        "calendar_name": str(raw.get("calendarName") or "")[:MAX_TITLE],
        "own": 1 if raw.get("own") is True else 0,
        "title": str(raw.get("title") or "")[:MAX_TITLE],
        "location": str(raw.get("location") or "")[:MAX_LOCATION],
        "description": str(raw.get("description") or "")[:MAX_DESCRIPTION_STORED],
    }


def _clean_result(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CalendarError("result must be an object")
    command_id = str(raw.get("id") or "").strip()
    try:
        uuid.UUID(command_id)
    except ValueError as exc:
        raise CalendarError("result id is invalid") from exc
    event_id = str(raw.get("eventId") or "").strip()[:40]
    return {
        "id": command_id,
        "ok": raw.get("ok") is True,
        "event_id": event_id or None,
        "error": str(raw.get("error") or "")[:160],
    }


def sync_device(
    path: str,
    device_id: str,
    payload: Any,
    *,
    now: Optional[_dt.datetime] = None,
) -> dict[str, Any]:
    """Store one phone snapshot, record command results, hand out commands."""
    if not isinstance(payload, dict):
        raise CalendarError("payload must be an object")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise CalendarError("unsupported schemaVersion")
    permission = payload.get("permission")
    permission = permission if isinstance(permission, dict) else {}
    read_granted = permission.get("read") is True
    write_granted = permission.get("write") is True
    raw_events = payload.get("events")
    raw_results = payload.get("results") or []
    if read_granted and not isinstance(raw_events, list):
        raise CalendarError("events must be a list")
    if not isinstance(raw_results, list) or len(raw_results) > MAX_RESULTS:
        raise CalendarError("results must be a list of at most %d" % MAX_RESULTS)
    events = [_clean_event(item) for item in (raw_events or [])] if read_granted else []
    if len(events) > MAX_EVENTS:
        raise CalendarError("too many events")
    results = [_clean_result(item) for item in raw_results]
    window_start = payload.get("windowStartMs")
    window_end = payload.get("windowEndMs")
    window_start = _ms(window_start, "windowStartMs") if window_start is not None else None
    window_end = _ms(window_end, "windowEndMs") if window_end is not None else None
    timezone = str(payload.get("timezone") or "")[:64]
    own_calendar_id = str(payload.get("ownCalendarId") or "")[:40]

    current = now or _utcnow()
    stamp = _iso(current)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO calendar_sync_meta (
                 device_id, last_sync_at, read_granted, write_granted,
                 own_calendar_id, timezone, window_start_ms, window_end_ms, event_count)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(device_id) DO UPDATE SET
                 last_sync_at=excluded.last_sync_at,
                 read_granted=excluded.read_granted,
                 write_granted=excluded.write_granted,
                 own_calendar_id=excluded.own_calendar_id,
                 timezone=excluded.timezone,
                 window_start_ms=excluded.window_start_ms,
                 window_end_ms=excluded.window_end_ms,
                 event_count=excluded.event_count""",
            (
                device_id, stamp, int(read_granted), int(write_granted),
                own_calendar_id, timezone, window_start, window_end, len(events),
            ),
        )
        # The snapshot replaces this device's mirror; a revoked read
        # permission therefore also clears what we had.
        conn.execute("DELETE FROM calendar_events WHERE device_id=?", (device_id,))
        conn.executemany(
            """INSERT OR REPLACE INTO calendar_events (
                 device_id, event_id, begin_ms, end_ms, all_day, calendar_id,
                 calendar_name, own, title, location, description)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    device_id, e["event_id"], e["begin_ms"], e["end_ms"], e["all_day"],
                    e["calendar_id"], e["calendar_name"], e["own"], e["title"],
                    e["location"], e["description"],
                )
                for e in events
            ],
        )
        applied = 0
        for result in results:
            # Only a command handed to this device can be answered by it;
            # a repeated answer for a finished command changes nothing.
            cursor = conn.execute(
                """UPDATE calendar_commands
                   SET state=?, done_at=?, result_event_id=?, error=?
                   WHERE id=? AND device_id=? AND state='sent'""",
                (
                    "done" if result["ok"] else "failed",
                    stamp,
                    result["event_id"],
                    "" if result["ok"] else (result["error"] or "failed"),
                    result["id"],
                    device_id,
                ),
            )
            applied += cursor.rowcount
        commands = _take_commands(conn, device_id, current) if write_granted else []
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"acceptedEvents": len(events), "appliedResults": applied, "commands": commands}


def _take_commands(
    conn: sqlite3.Connection,
    device_id: str,
    now: _dt.datetime,
) -> list[dict[str, Any]]:
    stamp = _iso(now)
    resend_before = _iso(now - _dt.timedelta(seconds=RESEND_AFTER_SECONDS))
    # A command sent too many times without an answer will not be retried.
    conn.execute(
        """UPDATE calendar_commands SET state='failed', done_at=?, error='undelivered'
           WHERE state='sent' AND sent_at<? AND send_attempts>=?""",
        (stamp, resend_before, MAX_SEND_ATTEMPTS),
    )
    rows = conn.execute(
        """SELECT id, op, payload_json FROM calendar_commands
           WHERE state='pending' OR (state='sent' AND sent_at<?)
           ORDER BY created_at ASC, id ASC LIMIT ?""",
        (resend_before, MAX_COMMANDS_PER_SYNC),
    ).fetchall()
    out = []
    for row in rows:
        conn.execute(
            """UPDATE calendar_commands
               SET state='sent', device_id=?, sent_at=?, send_attempts=send_attempts+1
               WHERE id=?""",
            (device_id, stamp, row["id"]),
        )
        out.append({"id": row["id"], "op": row["op"], **json.loads(row["payload_json"])})
    return out


def has_pending_commands(path: str, device_id: str) -> bool:
    """True when this device may write and a command has never been handed out.

    Drives the push long poll's ``calendar`` flag, so the phone syncs within
    seconds of a write. Re-sends after a lost answer ride the periodic sync.
    """
    conn = _connect(path)
    try:
        meta = conn.execute(
            "SELECT write_granted FROM calendar_sync_meta WHERE device_id=?",
            (device_id,),
        ).fetchone()
        if not meta or not meta["write_granted"]:
            return False
        row = conn.execute(
            "SELECT 1 FROM calendar_commands WHERE state='pending' LIMIT 1",
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ── Fyodor's side ───────────────────────────────────────────


def _latest_meta(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM calendar_sync_meta ORDER BY last_sync_at DESC LIMIT 1",
    ).fetchone()


def _format_ms(value: int, zone: _dt.tzinfo, *, all_day: bool) -> str:
    if all_day:
        # Android stores all-day events at UTC midnight.
        return _dt.datetime.fromtimestamp(value / 1000, _dt.timezone.utc).strftime("%Y-%m-%d")
    return _dt.datetime.fromtimestamp(value / 1000, zone).strftime("%Y-%m-%d %H:%M")


def _parse_local(value: Any, field: str, zone: _dt.tzinfo) -> tuple[int, bool]:
    """Return (epoch ms, is_date_only). Date-only means midnight UTC."""
    text = _text(value, field, 40, required=True)
    if _DATE_RE.match(text):
        try:
            day = _dt.datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc:
            raise CalendarError(f"{field} is not a valid date") from exc
        return int(day.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000), True
    if _LOCAL_RE.match(text):
        normalized = text.replace("T", " ")
        fmt = "%Y-%m-%d %H:%M:%S" if normalized.count(":") == 2 else "%Y-%m-%d %H:%M"
        try:
            local = _dt.datetime.strptime(normalized, fmt)
        except ValueError as exc:
            raise CalendarError(f"{field} is not a valid time") from exc
        return int(local.replace(tzinfo=zone).timestamp() * 1000), False
    raise CalendarError(f"{field} must be 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD'")


def _reminder(value: Any, *, all_day: bool) -> Optional[int]:
    if value is None:
        return None if all_day else DEFAULT_TIMED_REMINDER_MINUTES
    if isinstance(value, bool) or not isinstance(value, int):
        raise CalendarError("reminder_minutes must be an integer")
    if value < 0:
        return None
    if value > MAX_REMINDER_MINUTES:
        raise CalendarError("reminder_minutes is too large")
    return value


def read_events(
    path: str,
    *,
    start_date: Any = None,
    days: Any = None,
    now: Optional[_dt.datetime] = None,
) -> dict[str, Any]:
    current = now or _utcnow()
    try:
        span = int(days) if days is not None else 1
    except (TypeError, ValueError):
        span = 1
    span = max(1, min(MAX_READ_DAYS, span))
    conn = _connect(path)
    try:
        meta = _latest_meta(conn)
        if meta is None:
            return {
                "status": "NOT_CONNECTED",
                "message": "手机还没有同步过日历：需要在 Elpis 里授权日历。",
                "events": [],
            }
        zone = _zone(meta["timezone"])
        if start_date:
            text = _text(start_date, "start_date", 10)
            if not _DATE_RE.match(text):
                raise CalendarError("start_date must be YYYY-MM-DD")
            first_day = _dt.datetime.strptime(text, "%Y-%m-%d").date()
        else:
            first_day = current.astimezone(zone).date()
        start = _dt.datetime.combine(first_day, _dt.time(), zone)
        end = start + _dt.timedelta(days=span)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        # All-day events sit on UTC midnight; widen by a day to catch them,
        # then keep only the dates inside the asked range.
        rows = conn.execute(
            """SELECT * FROM calendar_events
               WHERE device_id=? AND begin_ms<? AND end_ms>?
               ORDER BY begin_ms ASC, event_id ASC""",
            (meta["device_id"], end_ms + 86400000, start_ms - 86400000),
        ).fetchall()
        events = []
        for row in rows:
            all_day = bool(row["all_day"])
            if all_day:
                first = _dt.datetime.fromtimestamp(row["begin_ms"] / 1000, _dt.timezone.utc).date()
                last = _dt.datetime.fromtimestamp(row["end_ms"] / 1000, _dt.timezone.utc).date()
                if not (first < end.date() and max(last, first + _dt.timedelta(days=1)) > first_day):
                    continue
            elif not (row["begin_ms"] < end_ms and max(row["end_ms"], row["begin_ms"] + 1) > start_ms):
                continue
            item = {
                "event_id": row["event_id"],
                "title": row["title"],
                "start": _format_ms(row["begin_ms"], zone, all_day=all_day),
                "end": _format_ms(row["end_ms"], zone, all_day=all_day),
                "all_day": all_day,
                "calendar": row["calendar_name"],
                "mine": bool(row["own"]),
            }
            if row["location"]:
                item["location"] = row["location"]
            if row["description"]:
                item["notes"] = row["description"]
            events.append(item)
        last_sync = _parse_iso(meta["last_sync_at"])
        age = (current - last_sync).total_seconds() if last_sync else None
        pending = [
            {"command_id": r["id"], "op": r["op"], **json.loads(r["payload_json"]), "state": r["state"]}
            for r in conn.execute(
                "SELECT id, op, payload_json, state FROM calendar_commands "
                "WHERE state IN ('pending','sent') ORDER BY created_at ASC LIMIT 20",
            ).fetchall()
        ]
        day_ago = _iso(current - _dt.timedelta(days=1))
        failed = [
            {"command_id": r["id"], "op": r["op"], "error": r["error"]}
            for r in conn.execute(
                "SELECT id, op, error FROM calendar_commands "
                "WHERE state='failed' AND done_at>=? ORDER BY done_at DESC LIMIT 10",
                (day_ago,),
            ).fetchall()
        ]
    finally:
        conn.close()
    out: dict[str, Any] = {
        "status": "OK" if meta["read_granted"] else "READ_PERMISSION_MISSING",
        "range": {
            "from": first_day.isoformat(),
            "to": (first_day + _dt.timedelta(days=span - 1)).isoformat(),
            "timezone": str(zone),
        },
        "synced_at": _format_ms(int(last_sync.timestamp() * 1000), zone, all_day=False) if last_sync else None,
        "stale": age is None or age > STALE_AFTER_SECONDS,
        "can_write": bool(meta["write_granted"]),
        "events": events,
    }
    if pending:
        out["pending_writes"] = [_present_command(item, zone) for item in pending]
    if failed:
        out["recent_failed_writes"] = failed
    return out


def _present_command(item: dict[str, Any], zone: _dt.tzinfo) -> dict[str, Any]:
    shown = {k: v for k, v in item.items() if k not in {"startMs", "endMs", "allDay", "reminderMinutes", "eventId"}}
    all_day = bool(item.get("allDay"))
    if "startMs" in item:
        shown["start"] = _format_ms(item["startMs"], zone, all_day=all_day)
    if "endMs" in item:
        shown["end"] = _format_ms(item["endMs"], zone, all_day=all_day)
    if item.get("eventId"):
        shown["event_id"] = item["eventId"]
    return shown


def queue_write(
    path: str,
    *,
    op: Any,
    event_id: Any = None,
    title: Any = None,
    start: Any = None,
    end: Any = None,
    all_day: Any = None,
    location: Any = None,
    notes: Any = None,
    reminder_minutes: Any = None,
    now: Optional[_dt.datetime] = None,
) -> dict[str, Any]:
    """Validate one write against the latest snapshot and queue it for the phone."""
    if op not in OPS:
        raise CalendarError("op must be create, update or delete")
    if all_day is not None and not isinstance(all_day, bool):
        raise CalendarError("all_day must be a boolean")
    current = now or _utcnow()
    conn = _connect(path)
    try:
        meta = _latest_meta(conn)
        if meta is None or not meta["write_granted"]:
            return {
                "status": "WRITE_UNAVAILABLE",
                "message": "手机还没有允许 Elpis 写日历，没有写入。",
            }
        zone = _zone(meta["timezone"])
        payload: dict[str, Any] = {}
        if op in {"update", "delete"}:
            target = _text(event_id, "event_id", 40, required=True)
            if not _is_own_event(conn, meta["device_id"], target):
                raise CalendarError("event_id is not an event in Fyodor's own calendar")
            payload["eventId"] = target
        if op == "delete":
            if any(v is not None for v in (title, start, end, all_day, location, notes, reminder_minutes)):
                raise CalendarError("delete takes only event_id")
        elif op == "create" or any(
            v is not None for v in (title, start, end, all_day, location, notes, reminder_minutes)
        ):
            if op == "create":
                payload["title"] = _text(title, "title", MAX_TITLE, required=True)
            elif title is not None:
                payload["title"] = _text(title, "title", MAX_TITLE, required=True)
            if op == "create" and start is None:
                raise CalendarError("start is required")
            if start is not None:
                start_ms, date_only = _parse_local(start, "start", zone)
                is_all_day = date_only if all_day is None else all_day
                if is_all_day and not date_only:
                    raise CalendarError("an all-day start must be YYYY-MM-DD")
                if not is_all_day and date_only:
                    raise CalendarError("a timed start must be 'YYYY-MM-DD HH:MM'")
                if end is not None:
                    end_ms, end_date_only = _parse_local(end, "end", zone)
                    if end_date_only != is_all_day:
                        raise CalendarError("start and end must both be dates or both be times")
                    if is_all_day:
                        end_ms += 86400000  # inclusive last day → exclusive end
                else:
                    end_ms = start_ms + (86400000 if is_all_day else DEFAULT_DURATION_MINUTES * 60000)
                if end_ms <= start_ms:
                    raise CalendarError("end must be after start")
                payload.update({"startMs": start_ms, "endMs": end_ms, "allDay": is_all_day})
                if op == "create" or reminder_minutes is not None:
                    payload["reminderMinutes"] = _reminder(reminder_minutes, all_day=is_all_day)
            elif end is not None or all_day is not None:
                raise CalendarError("changing end or all_day needs start too")
            elif reminder_minutes is not None:
                payload["reminderMinutes"] = _reminder(reminder_minutes, all_day=False)
            if location is not None:
                payload["location"] = _text(location, "location", MAX_LOCATION)
            if notes is not None:
                payload["notes"] = _text(notes, "notes", MAX_NOTES)
        if op == "update" and set(payload) == {"eventId"}:
            raise CalendarError("update needs at least one field to change")
        command_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO calendar_commands (id, op, payload_json, created_at) VALUES (?,?,?,?)",
            (command_id, op, json.dumps(payload, ensure_ascii=False, sort_keys=True), _iso(current)),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "status": "QUEUED",
        "command_id": command_id,
        "op": op,
        "message": "已排进手机日历队列；手机在线时几秒内写入，结果下次查看日历时可见。",
        "write": _present_command(payload, zone),
    }


def _is_own_event(conn: sqlite3.Connection, device_id: str, event_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM calendar_events WHERE device_id=? AND event_id=? AND own=1 LIMIT 1",
        (device_id, event_id),
    ).fetchone()
    if row is not None:
        return True
    # Created by Fyodor but not yet in a snapshot.
    row = conn.execute(
        "SELECT 1 FROM calendar_commands WHERE op='create' AND state='done' "
        "AND result_event_id=? LIMIT 1",
        (event_id,),
    ).fetchone()
    return row is not None


def get_status(path: str) -> dict[str, Any]:
    conn = _connect(path)
    try:
        meta = _latest_meta(conn)
        counts = {
            row["state"]: row["n"]
            for row in conn.execute(
                "SELECT state, COUNT(*) AS n FROM calendar_commands GROUP BY state",
            ).fetchall()
        }
    finally:
        conn.close()
    return {
        "schemaVersion": SCHEMA_VERSION,
        "connected": meta is not None,
        "lastSyncAt": meta["last_sync_at"] if meta else None,
        "readGranted": bool(meta["read_granted"]) if meta else False,
        "writeGranted": bool(meta["write_granted"]) if meta else False,
        "eventCount": int(meta["event_count"]) if meta else 0,
        "commands": {state: int(counts.get(state, 0)) for state in ("pending", "sent", "done", "failed")},
    }
