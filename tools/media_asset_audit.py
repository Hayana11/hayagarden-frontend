"""Read-only integrity report for durable chat-image evidence assets."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from tools.media_assets import DATA_ROOT, DB_PATH


def audit_media_assets(db_path: str = DB_PATH, data_root: str | Path = DATA_ROOT) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_assets'"
        ).fetchone()
        if not exists:
            return {"total": 0, "missing_file": 0, "hash_mismatch": 0, "duplicate_sha": 0}
        rows = conn.execute("SELECT sha256,storage_uri FROM media_assets").fetchall()
        duplicates = conn.execute(
            "SELECT COUNT(*) FROM (SELECT sha256 FROM media_assets GROUP BY sha256 HAVING COUNT(*)>1)"
        ).fetchone()[0]
    finally:
        conn.close()

    root = Path(data_root).resolve()
    folder = root / "gallery" / "evidence"
    missing = mismatch = 0
    for digest, uri in rows:
        rel = Path(str(uri or ""))
        candidate = root / rel
        if (rel.parts[:2] != ("gallery", "evidence") or len(rel.parts) != 3
                or candidate.is_symlink() or folder.is_symlink() or folder.resolve() != folder):
            missing += 1
            continue
        try:
            path = candidate.resolve(strict=True)
            if folder not in path.parents or not path.is_file():
                missing += 1
                continue
            h = hashlib.sha256()
            with path.open("rb") as image:
                for part in iter(lambda: image.read(1024 * 1024), b""):
                    h.update(part)
            mismatch += h.hexdigest() != str(digest)
        except OSError:
            missing += 1
    return {"total": len(rows), "missing_file": missing,
            "hash_mismatch": mismatch, "duplicate_sha": int(duplicates)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only media asset integrity audit")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--root", default=str(DATA_ROOT))
    args = parser.parse_args(argv)
    print(json.dumps(audit_media_assets(args.db, args.root), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
