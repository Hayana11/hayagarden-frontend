"""Serialized, bounded R4B non-query shadow acceptance harness."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable

from tools import ombre_adapter

RECEIPT_TABLE = "ombre_read_shadow_receipts"
CONTROLLED_TIMEOUT_SECONDS = 20.0
EXPECTED_OPERATIONS = ("handoff", "records", "record", "record", "emotion")


def _db_path() -> Path:
    raw = str(os.environ.get("OMBRE_READ_SHADOW_DB_PATH", "")).strip()
    if not raw:
        raise RuntimeError("OMBRE_READ_SHADOW_DB_PATH is required")
    return Path(raw).expanduser().resolve()


def receipt_count() -> int:
    with sqlite3.connect(str(_db_path())) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM " + RECEIPT_TABLE
        ).fetchone()
    return int(row[0] if row else 0)


def _latest_receipt() -> dict[str, Any] | None:
    with sqlite3.connect(str(_db_path())) as connection:
        row = connection.execute(
            "SELECT rowid, operation, status, error_code, "
            "authoritative_count, shadow_count, exact_id_set, exact_order, "
            "overlap_count, overlap_ratio, metadata_json "
            "FROM " + RECEIPT_TABLE + " ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    try:
        metadata = json.loads(str(row[10] or "{}"))
    except (TypeError, ValueError):
        metadata = {}
    return {
        "rowid": int(row[0]),
        "operation": str(row[1] or ""),
        "status": str(row[2] or ""),
        "error_code": str(row[3] or ""),
        "authoritative_count": row[4],
        "shadow_count": row[5],
        "exact_id_set": row[6],
        "exact_order": row[7],
        "overlap_count": row[8],
        "overlap_ratio": row[9],
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


def _worker_count() -> int:
    count = 0
    proc_root = Path("/proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ")
        except OSError:
            continue
        if b"tools.ombre_read_shadow_worker" in command:
            count += 1
    return count


def wait_for_terminal_receipt(
    before_count: int,
    *,
    timeout: float = CONTROLLED_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        if receipt_count() > before_count:
            receipt = _latest_receipt()
            if receipt is not None:
                while _worker_count() and time.monotonic() < deadline:
                    time.sleep(0.05)
                if _worker_count():
                    raise TimeoutError("shadow worker remained after receipt")
                return receipt
        time.sleep(0.05)
    raise TimeoutError("controlled receipt did not arrive")


def _run_operation(
    operation: str,
    call: Callable[[], Any],
    *,
    timeout: float = CONTROLLED_TIMEOUT_SECONDS,
) -> tuple[Any, dict[str, Any]]:
    before_count = receipt_count()
    authoritative = call()
    receipt = wait_for_terminal_receipt(before_count, timeout=timeout)
    if receipt["operation"] != operation:
        raise RuntimeError(
            "receipt operation mismatch: expected "
            + operation
            + ", got "
            + receipt["operation"]
        )
    return authoritative, receipt


def _ids_hash(records: Any) -> str:
    ids = [
        str(item.get("id") or "")
        for item in records
        if isinstance(item, dict)
    ] if isinstance(records, list) else []
    payload = json.dumps(ids, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _authoritative_summary(operation: str, value: Any) -> dict[str, Any]:
    if operation == "handoff":
        if isinstance(value, str):
            raw = value.encode("utf-8")
            return {
                "available": True,
                "length": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        return {"available": False, "length": 0, "sha256": ""}
    if operation == "records":
        return {
            "count": len(value) if isinstance(value, list) else -1,
            "ids_sha256": _ids_hash(value),
        }
    if operation == "record":
        return {"present": isinstance(value, dict)}
    if operation == "emotion":
        if not isinstance(value, dict):
            return {"count": -1, "valence": None, "arousal": None}
        return {
            "count": int(value.get("count") or 0),
            "valence": value.get("valence"),
            "arousal": value.get("arousal"),
        }
    return {}


def _emotion_valid(receipt: dict[str, Any]) -> tuple[bool, float | None, float | None]:
    metadata = receipt.get("metadata") or {}
    try:
        valence_delta = abs(
            float(metadata["authoritative_valence"])
            - float(metadata["shadow_valence"])
        )
        arousal_delta = abs(
            float(metadata["authoritative_arousal"])
            - float(metadata["shadow_arousal"])
        )
    except (KeyError, TypeError, ValueError):
        return False, None, None
    count_match = (
        receipt.get("authoritative_count") == receipt.get("shadow_count")
    )
    valid = (
        count_match
        and valence_delta <= 0.10
        and arousal_delta <= 0.10
    )
    return valid, valence_delta, arousal_delta


def _receipt_valid(operation: str, receipt: dict[str, Any]) -> bool:
    status = receipt["status"]
    if operation == "handoff":
        return status == "exact"
    if operation == "records":
        return status in {"exact", "same_set_different_order"}
    if operation == "record":
        return status == "exact"
    if operation == "emotion":
        if status == "exact":
            return True
        if status == "divergent_valid":
            return _emotion_valid(receipt)[0]
    return False


def run_controlled_acceptance(
    *,
    timeout: float = CONTROLLED_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    initial_count = receipt_count()
    if initial_count != 0:
        raise RuntimeError("fresh receipt database must start empty")

    calls: tuple[tuple[str, Callable[[], Any]], ...] = (
        ("handoff", lambda: ombre_adapter.get_handoff()),
        (
            "records",
            lambda: ombre_adapter.list_memory_records(
                bucket_type="dynamic",
                limit=15,
                include_content=True,
            ),
        ),
        ("record", lambda: ombre_adapter.get_memory_record("69507e2109ef")),
        ("record", lambda: ombre_adapter.get_memory_record("9e77104ed029")),
        ("emotion", lambda: ombre_adapter.get_emotion_snapshot()),
    )

    results = []
    for operation, call in calls:
        authoritative, receipt = _run_operation(
            operation,
            call,
            timeout=timeout,
        )
        results.append(
            {
                "operation": operation,
                "status": receipt["status"],
                "error_code": receipt["error_code"],
                "authoritative": _authoritative_summary(operation, authoritative),
                "receipt": receipt,
            }
        )

    statuses = Counter(item["status"] for item in results)
    errors = Counter(
        item["error_code"]
        for item in results
        if item["error_code"]
    )
    operation_counts = Counter(item["operation"] for item in results)
    emotion_receipt = next(
        item["receipt"] for item in results if item["operation"] == "emotion"
    )
    emotion_valid, valence_delta, arousal_delta = _emotion_valid(emotion_receipt)
    passed = (
        len(results) == 5
        and operation_counts == Counter(
            {"handoff": 1, "records": 1, "record": 2, "emotion": 1}
        )
        and all(_receipt_valid(item["operation"], item["receipt"]) for item in results)
        and not any(
            errors.get(code, 0)
            for code in (
                "shadow_error",
                "shadow_timeout",
                "shadow_unavailable",
                "auth_401",
                "dropped_busy",
            )
        )
        and receipt_count() == 5
    )
    return {
        "passed": passed,
        "controlled_receipts": len(results),
        "operation_counts": dict(operation_counts),
        "status_counts": dict(statuses),
        "error_counts": dict(errors),
        "handoff_status": results[0]["status"],
        "records_status": results[1]["status"],
        "record_1_status": results[2]["status"],
        "record_2_status": results[3]["status"],
        "emotion_status": results[4]["status"],
        "emotion_count_match": (
            emotion_receipt["authoritative_count"]
            == emotion_receipt["shadow_count"]
        ),
        "emotion_valence_delta": valence_delta,
        "emotion_arousal_delta": arousal_delta,
        "authoritative": [item["authoritative"] for item in results],
    }


def main() -> int:
    report = run_controlled_acceptance()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
