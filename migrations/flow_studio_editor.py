"""Explicit, idempotent Flow Studio editor schema migration.

This module is intentionally standalone: importing it never opens a database or
executes DDL. Deployment invokes the CLI after taking a consistent backup and
before restarting services.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import Any

TABLE_NAME = "flow_studio_editor_documents"
DEFAULT_DB_PATH = os.environ.get("HAYA_DB_PATH", "/opt/frontend/memories.db")

_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    flow_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    document_json TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_EXPECTED_COLUMNS = (
    ("flow_id", "TEXT", 0, 1),
    ("schema_version", "INTEGER", 1, 0),
    ("document_json", "TEXT", 1, 0),
    ("revision", "INTEGER", 1, 0),
    ("created_at", "TEXT", 1, 0),
    ("updated_at", "TEXT", 1, 0),
)


class MigrationError(RuntimeError):
    """Raised when the editor schema cannot be safely provisioned."""


def _table_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (TABLE_NAME,),
    ).fetchone()
    return row is not None


def _schema_columns(connection: sqlite3.Connection) -> tuple[tuple[str, str, int, int], ...]:
    rows = connection.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()
    return tuple(
        (
            str(row[1]),
            str(row[2]).upper(),
            int(row[3]),
            int(row[5]),
        )
        for row in rows
    )


def _verify_schema(connection: sqlite3.Connection) -> None:
    actual = _schema_columns(connection)
    if actual != _EXPECTED_COLUMNS:
        raise MigrationError(
            f"{TABLE_NAME} has an incompatible schema: expected {_EXPECTED_COLUMNS!r}, got {actual!r}"
        )


def migrate(db_path: str = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Create or verify the editor table without touching runtime tables."""
    if not os.path.isfile(db_path):
        raise MigrationError(f"database does not exist: {db_path}")

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(db_path, timeout=30)
        connection.execute("BEGIN IMMEDIATE")
        existed = _table_exists(connection)
        if existed:
            _verify_schema(connection)
        else:
            connection.execute(_CREATE_TABLE)
            _verify_schema(connection)
        connection.commit()
        return {"db_path": db_path, "table": TABLE_NAME, "created": not existed}
    except MigrationError:
        if connection is not None:
            connection.rollback()
        raise
    except sqlite3.Error as exc:
        if connection is not None:
            connection.rollback()
        raise MigrationError(f"could not migrate {TABLE_NAME}: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision the Flow Studio editor table")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    try:
        result = migrate(args.db_path)
    except MigrationError as exc:
        print(f"FLOW_STUDIO_MIGRATION_FAILED: {exc}", file=sys.stderr)
        return 1
    action = "created" if result["created"] else "verified"
    print(f"FLOW_STUDIO_MIGRATION_OK: {action} {result['table']} in {result['db_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
