#!/usr/bin/env python3.11
"""Replay Claude session JSONL into deduplicated cache accounting."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.cc_jsonl_usage import replay_jsonl_paths


def _public_report(report):
    totals = dict(report.get("totals") or {})
    return {
        "ok": True,
        "source": "claude_session_jsonl",
        "file_count": int(report.get("file_count") or 0),
        "assistant_usage_rows": int(report.get("assistant_usage_rows") or 0),
        "request_count": int(report.get("request_count") or 0),
        "request_ids": list(report.get("request_ids") or []),
        "duplicate_rows_ignored": int(report.get("duplicate_rows_ignored") or 0),
        "conflicting_duplicate_rows": int(
            report.get("conflicting_duplicate_rows") or 0
        ),
        "invalid_json_rows": int(report.get("invalid_json_rows") or 0),
        "models": list(report.get("models") or []),
        "input_tokens": int(totals.get("input_tokens") or 0),
        "output_tokens": int(totals.get("output_tokens") or 0),
        "cache_read": int(totals.get("cache_read") or 0),
        "cache_creation": int(totals.get("cache_creation") or 0),
        "cache_creation_5m": int(totals.get("cache_creation_5m") or 0),
        "cache_creation_1h": int(totals.get("cache_creation_1h") or 0),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only requestId-deduplicated Claude JSONL replay"
    )
    parser.add_argument("paths", nargs="+", help="session JSONL files")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    report = _public_report(replay_jsonl_paths(args.paths))
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
