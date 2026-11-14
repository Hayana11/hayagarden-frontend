"""Explicit, idempotent SELF/HAYA document schema migration."""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.daily_profile_updater import ensure_profile_document_schema


def migrate(db_path):
    if not os.path.isfile(db_path):
        raise RuntimeError("database does not exist")
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        ensure_profile_document_schema(conn)
        expected = {
            "profile_documents": ("target", "body", "version", "updated_at", "source_day"),
            "profile_document_revisions": (
                "id", "target", "version", "before_body", "after_body", "source_day",
                "evidence_refs_json", "provider", "model_identity", "created_at",
            ),
        }
        for table, columns in expected.items():
            actual = tuple(row[1] for row in conn.execute("PRAGMA table_info(" + table + ")"))
            if actual != columns:
                raise RuntimeError("incompatible profile schema: " + table)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default="/opt/frontend/memories.db")
    try:
        migrate(parser.parse_args().db_path)
    except Exception as exc:
        print("PROFILE_DOCUMENT_MIGRATION_FAILED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
    print("PROFILE_DOCUMENT_MIGRATION_OK")
