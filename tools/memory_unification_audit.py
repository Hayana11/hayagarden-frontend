"""Read-only inventory for merging frontend SQLite memories into Ombre Brain.

This tool never updates SQLite or Markdown.  It only emits a JSON report that
can drive a later reviewed migration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

import frontmatter

_HASH_TITLE = re.compile(r"^[0-9a-f]{12}$")
_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


def normalize_content(text: str) -> str:
    text = _WIKILINK.sub(r"\1", str(text or ""))
    text = re.sub(r"\s+", "", text).lower()
    return text


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_content(text).encode("utf-8")).hexdigest()


def _sqlite_columns(conn: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(posts)")}


def load_sqlite_memories(db_path: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    columns = _sqlite_columns(conn)
    wanted = [
        name for name in (
            "id", "content", "type", "layer", "tags", "importance", "pinned",
            "resolved", "created_at", "summary_title",
        ) if name in columns
    ]
    if "id" not in wanted or "content" not in wanted:
        conn.close()
        raise ValueError("posts table must contain id and content")
    rows = conn.execute(f"SELECT {', '.join(wanted)} FROM posts").fetchall()
    conn.close()
    output = []
    for row in rows:
        item = dict(row)
        item["source"] = "sqlite"
        item["content_hash"] = content_hash(item.get("content", ""))
        output.append(item)
    return output


def load_ombre_memories(vault_dir: str) -> list[dict[str, Any]]:
    root = Path(vault_dir).resolve()
    output: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.md")):
        post = frontmatter.load(path)
        meta = dict(post.metadata)
        output.append({
            "source": "ombre",
            "id": str(meta.get("id") or path.stem),
            "content": str(post.content or ""),
            "content_hash": content_hash(str(post.content or "")),
            "name": str(meta.get("name") or ""),
            "type": str(meta.get("type") or ""),
            "domain": meta.get("domain") or [],
            "tags": meta.get("tags") or [],
            "importance": meta.get("importance"),
            "pinned": bool(meta.get("pinned")),
            "resolved": bool(meta.get("resolved")),
            "created": meta.get("created"),
            "last_active": meta.get("last_active"),
            "path": str(path.relative_to(root)),
        })
    return output


def _duplicate_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        if normalize_content(item.get("content", "")):
            groups[item["content_hash"]].append(item)
    result = []
    for digest, group in groups.items():
        if len(group) < 2:
            continue
        result.append({
            "content_hash": digest,
            "count": len(group),
            "ids": [str(item.get("id")) for item in group],
            "preview": str(group[0].get("content") or "")[:160],
        })
    result.sort(key=lambda item: (-item["count"], item["content_hash"]))
    return result


def build_report(sqlite_path: str, vault_dir: str) -> dict[str, Any]:
    sqlite_items = load_sqlite_memories(sqlite_path)
    ombre_items = load_ombre_memories(vault_dir)

    sqlite_by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ombre_by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in sqlite_items:
        if normalize_content(item.get("content", "")):
            sqlite_by_hash[item["content_hash"]].append(item)
    for item in ombre_items:
        if normalize_content(item.get("content", "")):
            ombre_by_hash[item["content_hash"]].append(item)

    shared = sorted(set(sqlite_by_hash) & set(ombre_by_hash))
    cross_matches = [{
        "content_hash": digest,
        "sqlite_ids": [str(item.get("id")) for item in sqlite_by_hash[digest]],
        "ombre_ids": [str(item.get("id")) for item in ombre_by_hash[digest]],
        "preview": str((sqlite_by_hash[digest] or ombre_by_hash[digest])[0].get("content") or "")[:160],
    } for digest in shared]

    def domains(item: dict[str, Any]) -> list[str]:
        value = item.get("domain") or []
        return [value] if isinstance(value, str) else [str(part) for part in value]

    return {
        "schema_version": 1,
        "inputs": {
            "sqlite": str(Path(sqlite_path).resolve()),
            "vault": str(Path(vault_dir).resolve()),
        },
        "counts": {
            "sqlite": len(sqlite_items),
            "ombre": len(ombre_items),
            "cross_matched_content": len(shared),
            "sqlite_only_content": len(set(sqlite_by_hash) - set(ombre_by_hash)),
            "ombre_only_content": len(set(ombre_by_hash) - set(sqlite_by_hash)),
            "ombre_pinned": sum(bool(item.get("pinned")) for item in ombre_items),
            "ombre_hash_titles": sum(bool(_HASH_TITLE.fullmatch(str(item.get("name") or ""))) for item in ombre_items),
            "ombre_unclassified": sum("未分类" in domains(item) for item in ombre_items),
        },
        "sqlite_duplicate_groups": _duplicate_groups(sqlite_items),
        "ombre_duplicate_groups": _duplicate_groups(ombre_items),
        "cross_matches": cross_matches,
        "sqlite_only": [{
            "id": str(item.get("id")),
            "content_hash": digest,
            "preview": str(item.get("content") or "")[:160],
        } for digest in sorted(set(sqlite_by_hash) - set(ombre_by_hash)) for item in sqlite_by_hash[digest]],
        "ombre_only": [{
            "id": str(item.get("id")),
            "content_hash": digest,
            "path": item.get("path"),
            "preview": str(item.get("content") or "")[:160],
        } for digest in sorted(set(ombre_by_hash) - set(sqlite_by_hash)) for item in ombre_by_hash[digest]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only frontend/Ombre memory inventory")
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    report = build_report(args.sqlite, args.vault)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        target = Path(args.json_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
