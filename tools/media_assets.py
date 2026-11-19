"""Durable evidence copies for chat images cited by memory Candidates."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA_ROOT = Path(os.environ.get("HAYAGARDEN_GALLERY_ROOT", "/opt/frontend")).resolve()
DB_PATH = str(DATA_ROOT / "gallery.db")
_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def ensure_schema(db_path: str = DB_PATH) -> None:
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS media_assets (
            asset_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, storage_uri TEXT NOT NULL,
            mime TEXT NOT NULL, size_bytes INTEGER NOT NULL, caption TEXT NOT NULL DEFAULT '',
            ocr_text TEXT NOT NULL DEFAULT '', entities_json TEXT NOT NULL DEFAULT '[]',
            model_identity TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        )""")
    finally:
        conn.close()


def save_media_asset(
    image_bytes: bytes, mime: str, annotation: dict[str, Any] | None = None, *,
    db_path: str = DB_PATH, data_root: str | Path = DATA_ROOT,
) -> str:
    data = bytes(image_bytes or b"")
    mime = str(mime or "").strip().lower()
    mime = "image/jpeg" if mime == "image/jpg" else mime
    if not data or mime not in _EXTENSIONS:
        raise ValueError("media asset requires readable supported image bytes")
    digest = hashlib.sha256(data).hexdigest()
    asset_id = "sha256-" + digest
    root = Path(data_root).resolve()
    folder = root / "gallery" / "evidence"
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink() or folder.resolve() != folder:
        raise ValueError("media evidence directory is unsafe")
    path = folder / (digest + _EXTENSIONS[mime])
    uri = "gallery/evidence/" + path.name
    ann = annotation or {}
    entities = ann.get("entities") if isinstance(ann.get("entities"), list) else []
    ensure_schema(db_path)
    with sqlite3.connect(db_path, timeout=10) as conn:
        existing = conn.execute(
            "SELECT storage_uri FROM media_assets WHERE sha256=?", (digest,),
        ).fetchone()
        if existing:
            saved = root / str(existing[0])
            if (saved.is_symlink() or folder not in saved.resolve().parents
                    or not saved.is_file() or hashlib.sha256(saved.read_bytes()).hexdigest() != digest):
                raise ValueError("existing media asset file is missing or corrupt")
            return asset_id
        if path.is_symlink():
            raise ValueError("media evidence destination is unsafe")
        try:
            with path.open("xb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
        except FileExistsError:
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("media evidence destination hash mismatch")
        conn.execute(
            """INSERT OR IGNORE INTO media_assets
               (asset_id,sha256,storage_uri,mime,size_bytes,caption,ocr_text,entities_json,model_identity,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (asset_id, digest, uri, mime, len(data), str(ann.get("caption") or "")[:1000],
             str(ann.get("ocr_text") or "")[:2000], json.dumps(entities[:32], ensure_ascii=False),
             str(ann.get("model_identity") or "")[:200],
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
    return asset_id
