"""Provider-neutral canonical mobile health store.

This database is deliberately separate from the Xiaomi credential store. It only
accepts bounded, authenticated canonical samples and keeps source provenance.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import uuid
from typing import Any

SCHEMA_VERSION = 1
METRICS = frozenset({"heart_rate", "steps", "sleep"})
SOURCES = frozenset({"health_connect", "gadgetbridge", "xiaomi_fitness_cloud"})
STATUSES = frozenset({"PASS", "EMPTY", "PERMISSION_DENIED", "UNAVAILABLE", "FAIL"})
MAX_ROWS = 500
MAX_DETAILS_BYTES = 8 * 1024
MAX_FUTURE_SKEW_SECONDS = 0
STALE_AFTER_HOURS = 48


def ensure_schema(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS health_samples (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              metric TEXT NOT NULL,
              sampled_at TEXT NOT NULL,
              data_date TEXT NOT NULL,
              value REAL NOT NULL,
              unit TEXT NOT NULL,
              details_json TEXT NOT NULL DEFAULT '{}',
              source TEXT NOT NULL,
              source_record_id TEXT NOT NULL,
              collected_at TEXT NOT NULL,
              UNIQUE(metric, source, source_record_id, sampled_at, value, unit)
            );
            CREATE INDEX IF NOT EXISTS idx_health_samples_metric_date
              ON health_samples(metric, sampled_at DESC);
            CREATE TABLE IF NOT EXISTS health_metric_status (
              metric TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              source TEXT NOT NULL,
              last_collected_at TEXT,
              last_upload_at TEXT,
              last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS health_ingest_meta (
              id INTEGER PRIMARY KEY CHECK(id = 1),
              last_ingest_at TEXT,
              last_collected_at TEXT,
              accepted_rows INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO health_ingest_meta(id) VALUES (1);
            CREATE TABLE IF NOT EXISTS health_devices (
              device_id TEXT PRIMARY KEY,
              install_id TEXT UNIQUE NOT NULL,
              credential_hash TEXT NOT NULL,
              package_name TEXT NOT NULL,
              build_sha TEXT NOT NULL DEFAULT '',
              build_branch TEXT NOT NULL DEFAULT '',
              label TEXT NOT NULL,
              created_at TEXT NOT NULL,
              rotated_at TEXT,
              last_seen_at TEXT,
              revoked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_health_devices_install_id
              ON health_devices(install_id);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _parse_time(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError(f"invalid {field}")
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid {field}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"invalid {field}")
    parsed = parsed.astimezone(_dt.timezone.utc)
    if parsed > _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
        raise ValueError(f"future {field}")
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _date(value: Any, sampled_at: str) -> str:
    if value is None:
        return sampled_at[:10]
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("invalid data_date")
    try:
        _dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid data_date") from exc
    return value


def _record(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("invalid record")
    metric = row.get("metric")
    if metric not in METRICS:
        raise ValueError("invalid metric")
    source = row.get("source")
    if source not in SOURCES:
        raise ValueError("invalid source")
    sampled_at = _parse_time(row.get("sampled_at", row.get("sampledAt")), "sampled_at")
    collected_at = _parse_time(row.get("collected_at", row.get("collectedAt")), "collected_at")
    value = row.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("invalid value")
    value = float(value)
    if metric == "heart_rate" and not 0 <= value <= 300:
        raise ValueError("invalid heart_rate")
    if metric == "steps" and not 0 <= value <= 10_000_000:
        raise ValueError("invalid steps")
    if metric == "sleep" and not 0 <= value <= 2880:
        raise ValueError("invalid sleep")
    unit = row.get("unit")
    expected = {"heart_rate": "bpm", "steps": "steps", "sleep": "minutes"}[metric]
    if unit != expected:
        raise ValueError("invalid unit")
    source_record_id = row.get("source_record_id", row.get("sourceRecordId"))
    if not isinstance(source_record_id, str) or not source_record_id.strip() or len(source_record_id) > 200:
        raise ValueError("invalid source_record_id")
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    details_json = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
    if len(details_json.encode("utf-8")) > MAX_DETAILS_BYTES:
        raise ValueError("details too large")
    return {
        "metric": metric,
        "sampled_at": sampled_at,
        "data_date": _date(row.get("data_date", row.get("dataDate")), sampled_at),
        "value": value,
        "unit": unit,
        "details_json": details_json,
        "source": source,
        "source_record_id": source_record_id.strip(),
        "collected_at": collected_at,
    }


def _status_item(metric: str, value: Any, collected_at: str) -> dict[str, str]:
    item = value if isinstance(value, dict) else {}
    status = item.get("status") if item.get("status") in STATUSES else "UNAVAILABLE"
    source = item.get("source") if item.get("source") in SOURCES else "health_connect"
    error = item.get("error") if isinstance(item.get("error"), str) else ""
    return {"metric": metric, "status": status, "source": source, "error": error[:160], "collected_at": collected_at}


def ingest_payload(payload: Any, path: str, *, now: str | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError("invalid schemaVersion")
    collected_at = _parse_time(payload.get("collectedAt"), "collected_at")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list) or len(raw_records) > MAX_ROWS:
        raise ValueError("invalid row count")
    records = [_record(row) for row in raw_records]
    statuses = payload.get("metricStatuses", payload.get("metric_status"))
    if not isinstance(statuses, dict):
        raise ValueError("invalid metricStatuses")
    status_rows = [_status_item(metric, statuses.get(metric), collected_at) for metric in sorted(METRICS)]
    ensure_schema(path)
    ingest_at = _parse_time(now or _dt.datetime.now(_dt.timezone.utc).isoformat(), "ingest_at")
    conn = sqlite3.connect(path)
    try:
        conn.execute("BEGIN")
        inserted = 0
        for row in records:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO health_samples
                (metric, sampled_at, data_date, value, unit, details_json, source, source_record_id, collected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(row[key] for key in (
                    "metric", "sampled_at", "data_date", "value", "unit",
                    "details_json", "source", "source_record_id", "collected_at",
                )),
            )
            inserted += int(cursor.rowcount > 0)
        for item in status_rows:
            conn.execute(
                """
                INSERT INTO health_metric_status
                (metric, status, source, last_collected_at, last_upload_at, last_error)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(metric) DO UPDATE SET
                  status=excluded.status,
                  source=excluded.source,
                  last_collected_at=excluded.last_collected_at,
                  last_upload_at=excluded.last_upload_at,
                  last_error=excluded.last_error
                """,
                (item["metric"], item["status"], item["source"], item["collected_at"], ingest_at, item["error"]),
            )
        conn.execute(
            """
            UPDATE health_ingest_meta
            SET last_ingest_at=?, last_collected_at=?, accepted_rows=accepted_rows+?
            WHERE id=1
            """,
            (ingest_at, collected_at, inserted),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"accepted": inserted, "duplicates": len(records) - inserted, "lastIngestAt": ingest_at}


def _stale(sampled_at: str | None, *, now: _dt.datetime) -> bool:
    if not sampled_at:
        return True
    try:
        parsed = _dt.datetime.fromisoformat(sampled_at.replace("Z", "+00:00"))
        return parsed < now - _dt.timedelta(hours=STALE_AFTER_HOURS)
    except ValueError:
        return True


def query_samples(path: str, metric: str, days: int, *, now: str | None = None) -> dict[str, Any]:
    if metric not in METRICS:
        raise ValueError("invalid metric")
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 30:
        raise ValueError("invalid days")
    ensure_schema(path)
    now_dt = _dt.datetime.now(_dt.timezone.utc) if now is None else _dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
    lower = now_dt - _dt.timedelta(days=days)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        status_row = conn.execute("SELECT * FROM health_metric_status WHERE metric=?", (metric,)).fetchone()
        rows = conn.execute(
            """
            SELECT metric, sampled_at, data_date, value, unit, details_json, source, source_record_id, collected_at
            FROM health_samples WHERE metric=? AND sampled_at>=? ORDER BY sampled_at DESC LIMIT 500
            """,
            (metric, lower.isoformat(timespec="milliseconds").replace("+00:00", "Z")),
        ).fetchall()
    finally:
        conn.close()
    records = []
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except (TypeError, json.JSONDecodeError):
            details = {}
        records.append({
            "metric": row["metric"],
            "sampledAt": row["sampled_at"],
            "dataDate": row["data_date"],
            "value": row["value"],
            "unit": row["unit"],
            "details": details,
            "source": row["source"],
            "sourceRecordId": row["source_record_id"],
            "collectedAt": row["collected_at"],
        })
    status = status_row["status"] if status_row else "UNAVAILABLE"
    source = status_row["source"] if status_row else "health_connect"
    return {
        "status": status if status in STATUSES else "UNAVAILABLE",
        "provider": source,
        "source": source,
        "metric": metric,
        "days": days,
        "records": records,
        "stale": _stale(records[0]["sampledAt"] if records else None, now=now_dt),
        "lastCollectedAt": status_row["last_collected_at"] if status_row else None,
        "lastUploadAt": status_row["last_upload_at"] if status_row else None,
    }


def get_local_metric(path: str, metric: str, days: int, *, now: str | None = None) -> dict[str, Any]:
    if not os.path.exists(path):
        return {
            "status": "UNAVAILABLE", "provider": "health_connect", "source": "health_connect",
            "metric": metric, "days": days, "records": [], "stale": True,
            "lastCollectedAt": None, "lastUploadAt": None,
        }
    try:
        return query_samples(path, metric, days, now=now)
    except (OSError, sqlite3.Error, ValueError):
        return {
            "status": "UNAVAILABLE",
            "provider": "health_connect",
            "source": "health_connect",
            "metric": metric,
            "days": days,
            "records": [],
            "stale": True,
            "lastCollectedAt": None,
            "lastUploadAt": None,
        }


def get_status(path: str, *, now: str | None = None) -> dict[str, Any]:
    ensure_schema(path)
    now_dt = _dt.datetime.now(_dt.timezone.utc) if now is None else _dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        meta = conn.execute("SELECT * FROM health_ingest_meta WHERE id=1").fetchone()
        metrics = {}
        for metric in sorted(METRICS):
            item = get_local_metric(path, metric, 30, now=now)
            latest = item["records"][0] if item["records"] else None
            metrics[metric] = {
                "status": item["status"],
                "source": item["source"],
                "stale": item["stale"],
                "sampledAt": latest["sampledAt"] if latest else None,
                "lastCollectedAt": item["lastCollectedAt"],
                "lastUploadAt": item["lastUploadAt"],
            }
        return {
            "schemaVersion": SCHEMA_VERSION,
            "available": True,
            "lastIngestAt": meta["last_ingest_at"] if meta else None,
            "lastCollectedAt": meta["last_collected_at"] if meta else None,
            "acceptedRows": int(meta["accepted_rows"]) if meta else 0,
            "metrics": metrics,
        }
    finally:
        conn.close()


def _device_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _device_text(value: Any, field: str, *, max_length: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {field}")
    value = value.strip()
    if required and not value:
        raise ValueError(f"invalid {field}")
    if len(value) > max_length or any(ord(char) < 32 for char in value):
        raise ValueError(f"invalid {field}")
    return value


def _device_uuid(value: Any, field: str) -> str:
    text = _device_text(value, field, max_length=80)
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid {field}") from exc


def _credential_hash(credential: str) -> str:
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def enroll_device(payload: Any, path: str) -> dict[str, str]:
    if not isinstance(payload, dict) or payload.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError("invalid schemaVersion")
    install_id = _device_uuid(payload.get("installId"), "installId")
    package_name = _device_text(payload.get("packageName"), "packageName", max_length=200)
    build_sha = _device_text(payload.get("buildSha", ""), "buildSha", max_length=200, required=False)
    build_branch = _device_text(payload.get("buildBranch", ""), "buildBranch", max_length=200, required=False)
    label = _device_text(payload.get("label"), "label", max_length=120)
    credential = secrets.token_urlsafe(32)
    issued_at = _device_now()
    device_id = str(uuid.uuid4())
    ensure_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("BEGIN")
        existing = conn.execute(
            "SELECT device_id, created_at FROM health_devices WHERE install_id=?",
            (install_id,),
        ).fetchone()
        if existing:
            device_id = str(existing[0])
            conn.execute(
                """
                UPDATE health_devices
                SET credential_hash=?, package_name=?, build_sha=?, build_branch=?,
                    label=?, rotated_at=?, revoked_at=NULL
                WHERE install_id=?
                """,
                (_credential_hash(credential), package_name, build_sha, build_branch, label, issued_at, install_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO health_devices
                (device_id, install_id, credential_hash, package_name, build_sha, build_branch,
                 label, created_at, rotated_at, last_seen_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                """,
                (device_id, install_id, _credential_hash(credential), package_name, build_sha,
                 build_branch, label, issued_at),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "schemaVersion": str(SCHEMA_VERSION),
        "deviceId": device_id,
        "credential": credential,
        "issuedAt": issued_at,
    }


def authenticate_device(path: str, device_id: Any, credential: Any) -> bool:
    try:
        normalized_device_id = _device_uuid(device_id, "device_id")
        supplied = _device_text(credential, "credential", max_length=512)
    except ValueError:
        return False
    ensure_schema(path)
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT credential_hash, revoked_at FROM health_devices WHERE device_id=?",
            (normalized_device_id,),
        ).fetchone()
        if not row or row[1] is not None:
            return False
        if not hmac.compare_digest(str(row[0]), _credential_hash(supplied)):
            return False
        conn.execute(
            "UPDATE health_devices SET last_seen_at=? WHERE device_id=?",
            (_device_now(), normalized_device_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def list_devices(path: str) -> list[dict[str, Any]]:
    ensure_schema(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT device_id, install_id, package_name, build_sha, build_branch, label,
                   created_at, rotated_at, last_seen_at, revoked_at
            FROM health_devices
            ORDER BY created_at ASC, device_id ASC
            """
        ).fetchall()
        return [
            {
                "deviceId": row["device_id"],
                "installId": row["install_id"],
                "packageName": row["package_name"],
                "buildSha": row["build_sha"],
                "buildBranch": row["build_branch"],
                "label": row["label"],
                "createdAt": row["created_at"],
                "rotatedAt": row["rotated_at"],
                "lastSeenAt": row["last_seen_at"],
                "revokedAt": row["revoked_at"],
            }
            for row in rows
        ]
    finally:
        conn.close()


def revoke_device(path: str, device_id: Any) -> bool:
    normalized_device_id = _device_uuid(device_id, "device_id")
    ensure_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            UPDATE health_devices
            SET revoked_at=COALESCE(revoked_at, ?)
            WHERE device_id=?
            """,
            (_device_now(), normalized_device_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()
