#!/usr/bin/env python3
"""Read-only normalized Ombre consumer CLI."""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.environ.get("HAYAGARDEN_ROOT", "/opt/frontend")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    latest = subparsers.add_parser("latest-domain")
    latest.add_argument("--type", required=True)
    latest.add_argument("--domain", required=True)
    args = parser.parse_args()

    if args.command != "latest-domain":
        return 0
    try:
        from tools import ombre_adapter

        records = ombre_adapter.list_memory_records(
            bucket_type=args.type,
            domain=args.domain,
            include_content=True,
            limit=1,
            sort="last_active_desc",
        )
        if records:
            sys.stdout.write(str(records[0].get("content") or "").strip())
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
