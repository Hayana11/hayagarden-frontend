#!/usr/bin/env python3
"""Preview-only DayBlock shadow planner. This command never calls a model."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PREVIEW_ROOT = Path("/opt/frontend-preview")
sys.path.insert(0, "/opt/frontend")
sys.path.insert(0, str(PREVIEW_ROOT))

import dayblock_shadow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan a Preview-only DayBlock for one natural day.")
    parser.add_argument("--plan", required=True, metavar="YYYY-MM-DD",
                        help="source natural day to plan; no model generation is performed")
    parser.add_argument("--source-db", default=str(dayblock_shadow.PRODUCTION_DB))
    parser.add_argument("--preview-db", default=str(dayblock_shadow.PREVIEW_DB))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = dayblock_shadow.build_shadow_plan(args.source_db, args.preview_db, args.plan)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if result["status"] in ("blocked", "planned") else 2
    except Exception as exc:
        print(json.dumps({
            "status": "failed",
            "source_day": args.plan,
            "error_code": type(exc).__name__,
            "model_call_count": 0,
        }, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
