"""Offline MEMORY-INTEROP R1 shadow comparison CLI.

This is not a daemon, cron job, HTTP route, MCP tool, or startup hook.
Invoke it explicitly against a temporary or fixture database:

  python3 tools/memory_interop_legacy_shadow.py --db-path FILE --keyword TEXT
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.memory_interop import (
    MEMORY_INTEROP_PROTOCOL_VERSION,
    InteropRequestContext,
)
from tools.memory_interop_legacy import (
    LEGACY_POSTS_ADAPTER_ID,
    shadow_compare_legacy_search,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare legacy search_memory_posts rows to an Interop ContextBundle.",
    )
    parser.add_argument("--db-path", required=True, help="explicit fixture SQLite path")
    parser.add_argument("--keyword", default="", help="legacy memory.search keyword")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--bundle-id", default="shadow-bundle")
    parser.add_argument(
        "--generated-at",
        default="2026-09-29T08:00:00+00:00",
        help="timezone-bearing ContextBundle timestamp",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    db_path = str(Path(args.db_path).expanduser().resolve())
    context = InteropRequestContext(
        protocol_version=MEMORY_INTEROP_PROTOCOL_VERSION,
        request_id="shadow-request",
        trigger_kind="offline_shadow",
        adapter_id=LEGACY_POSTS_ADAPTER_ID,
        requested_at=args.generated_at,
        capability_id="memory.search",
    )
    result = shadow_compare_legacy_search(
        db_path,
        keyword=args.keyword,
        request_context=context,
        bundle_id=args.bundle_id,
        generated_at=args.generated_at,
        limit=args.limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if (
        result["same_ids"]
        and result["same_order"]
        and result["same_content"]
        and result["same_count"]
        and result["same_source_refs"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
