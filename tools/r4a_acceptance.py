"""Isolated R4A acceptance calls; emits aggregate metadata only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import time

from chat.relationship_context import _read_relationship_anchor
from tools import ombre_adapter
from tools import ombre_read_shadow as shadow
from tools import thought_gen

DB_PATH = Path(os.environ["OMBRE_READ_SHADOW_DB_PATH"])


def receipt_summary() -> dict[str, int]:
    if not DB_PATH.exists():
        return {"TOTAL_RECEIPTS": 0}
    try:
        connection = sqlite3.connect(str(DB_PATH))
        rows = connection.execute(
            "SELECT status, COUNT(*) FROM ombre_read_shadow_receipts GROUP BY status"
        ).fetchall()
        total = connection.execute(
            "SELECT COUNT(*) FROM ombre_read_shadow_receipts"
        ).fetchone()[0]
        connection.close()
        result = {"TOTAL_RECEIPTS": int(total)}
        for status, count in rows:
            result[str(status).upper()] = int(count)
        return result
    except Exception:
        return {"TOTAL_RECEIPTS": 0, "RECEIPT_AUDIT_ERROR": 1}


def receipt_total() -> int:
    return int(receipt_summary().get("TOTAL_RECEIPTS", 0))


def wait_for_one_receipt(before: int, seconds: float = 15.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if receipt_total() > before:
            return
        time.sleep(0.25)


def wait_for_workers(seconds: float = 45.0) -> dict[str, int]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        summary = receipt_summary()
        if summary.get("TOTAL_RECEIPTS", 0) >= 8:
            return summary
        time.sleep(0.25)
    return receipt_summary()


def main() -> None:
    unchanged = []
    before = receipt_total()
    handoff = ombre_adapter.get_handoff(timeout=5.0, wall_timeout=8.0)
    returned = shadow.observe_handoff(handoff)
    wait_for_one_receipt(before)
    unchanged.append(returned is handoff)

    records = ombre_adapter.list_memory_records(
        bucket_type="dynamic",
        limit=15,
        include_content=True,
    )
    before = receipt_total()
    returned = shadow.observe_records(
        records,
        bucket_type="dynamic",
        limit=15,
        include_content=True,
    )
    wait_for_one_receipt(before)
    unchanged.append(returned is records)

    record_results = {}
    for bucket_id in ("69507e2109ef", "9e77104ed029"):
        before = receipt_total()
        record = ombre_adapter.get_memory_record(bucket_id)
        returned = shadow.observe_record(record, bucket_id)
        wait_for_one_receipt(before)
        unchanged.append(returned is record)
        record_results[bucket_id] = record is not None

    before = receipt_total()
    emotion = ombre_adapter.get_emotion_snapshot(timeout=3.0)
    returned = shadow.observe_emotion(emotion)
    wait_for_one_receipt(before)
    unchanged.append(returned is emotion)

    anchor, _, anchor_status = _read_relationship_anchor(
        prose_path="/tmp/r4a-no-prose-anchor.md",
    )
    thought = thought_gen.get_emotional_buckets()

    old_search_gate = os.environ.get("OMBRE_READ_SHADOW_SEARCH_ENABLED")
    os.environ["OMBRE_READ_SHADOW_SEARCH_ENABLED"] = "1"
    canary_results = []
    try:
        for query in ("记忆系统", "房间装修", "上下文压缩"):
            before = receipt_total()
            result = ombre_adapter.search_memories(
                query,
                limit=5,
                touch=False,
            )
            returned = shadow.observe_search(result, query, limit=5, touch=False)
            wait_for_one_receipt(before)
            unchanged.append(returned is result)
            canary_results.append(len(result))
    finally:
        if old_search_gate is None:
            os.environ.pop("OMBRE_READ_SHADOW_SEARCH_ENABLED", None)
        else:
            os.environ["OMBRE_READ_SHADOW_SEARCH_ENABLED"] = old_search_gate

    receipts = wait_for_workers()
    summary = {
        "AUTHORITATIVE_OUTPUT_CHANGED": "NO" if all(unchanged) else "YES",
        "HANDOFF_AUTHORITATIVE_AVAILABLE": isinstance(handoff, str),
        "RECORD_AUTHORITATIVE_COUNT": len(records),
        "RECORD_LOOKUP_69507E2109EF": record_results["69507e2109ef"],
        "RECORD_LOOKUP_9E77104ED029": record_results["9e77104ed029"],
        "EMOTION_AUTHORITATIVE_COUNT": int(emotion.get("count") or 0),
        "RELATIONSHIP_ANCHOR_STATUS": anchor_status,
        "RELATIONSHIP_ANCHOR_LENGTH": len(anchor),
        "THOUGHT_AUTHORITATIVE_COUNT": len(thought or []),
        "SEARCH_CANARY_COUNT": len(canary_results),
        "SEARCH_CANARY_RESULT_COUNTS": canary_results,
        "SEMANTIC_CANARY_DISPATCHES": 3,
        "SEMANTIC_CANARY_PROVIDER_CALLS_EXPECTED": 3,
        "RECEIPTS": receipts,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
