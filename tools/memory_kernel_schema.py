"""Explicit, additive SQLite schema for CORE-0; never called on application import."""
from __future__ import annotations

import sqlite3


_TABLES = (
    """CREATE TABLE IF NOT EXISTS memory_kernel_evidence (
        evidence_id TEXT PRIMARY KEY NOT NULL,
        source_type TEXT NOT NULL,
        source_ref TEXT NOT NULL,
        occurred_at TEXT,
        observed_at TEXT NOT NULL,
        content TEXT,
        content_ref TEXT,
        provenance TEXT NOT NULL CHECK(json_type(provenance) = 'object'),
        origin_kind TEXT NOT NULL CHECK(origin_kind IN ('source', 'derived')),
        participants TEXT NOT NULL CHECK(json_type(participants) = 'array'),
        entities TEXT NOT NULL CHECK(json_type(entities) = 'array'),
        integrity TEXT,
        visibility TEXT,
        CHECK(content IS NOT NULL OR content_ref IS NOT NULL)
    )""",
    """CREATE TABLE IF NOT EXISTS memory_kernel_state (
        state_id TEXT PRIMARY KEY NOT NULL,
        lineage_id TEXT,
        scope TEXT NOT NULL,
        subject_ref TEXT NOT NULL,
        representation TEXT NOT NULL,
        content TEXT,
        structured_value TEXT CHECK(
            structured_value IS NULL OR json_valid(structured_value)),
        evidence_refs TEXT NOT NULL CHECK(json_type(evidence_refs) = 'array'),
        confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
        epistemic_status TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK(content IS NOT NULL OR structured_value IS NOT NULL)
    )""",
    """CREATE TABLE IF NOT EXISTS memory_kernel_delta (
        delta_id TEXT PRIMARY KEY NOT NULL,
        target_scope TEXT NOT NULL,
        before_ref TEXT REFERENCES memory_kernel_state(state_id),
        after_ref TEXT NOT NULL UNIQUE REFERENCES memory_kernel_state(state_id),
        trigger_evidence_refs TEXT NOT NULL
            CHECK(json_type(trigger_evidence_refs) = 'array'),
        derived_by TEXT NOT NULL CHECK(json_type(derived_by) = 'object'),
        rationale TEXT,
        rationale_ref TEXT,
        rationale_kind TEXT NOT NULL CHECK(rationale_kind = 'derived'),
        confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
        occurred_at TEXT NOT NULL,
        review_status TEXT NOT NULL,
        CHECK(before_ref IS NULL OR before_ref != after_ref)
    )""",
)


def ensure_memory_kernel_schema(conn: sqlite3.Connection) -> None:
    """Idempotent v1 upgrade, owning one transaction on an idle connection.

    No global user_version, legacy schema, import hook or destructive downgrade.
    JSON1 and foreign keys are required; unavailable capabilities fail visibly.
    """
    if conn.in_transaction:
        raise ValueError("schema upgrade requires an idle connection")
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys are required")
    conn.execute("SELECT json_valid('[]')")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in _TABLES:
            conn.execute(statement)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS memory_kernel_lineage "
            "ON memory_kernel_state(lineage_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS memory_kernel_before "
            "ON memory_kernel_delta(before_ref)"
        )
        # Existing semantic identities are immutable. Deletion governance is deliberately
        # deferred; CORE-0 exposes no delete API and does not make future authorized
        # deletion impossible at the storage-law layer.
        # Explicit insert guards also block INSERT OR REPLACE with recursive_triggers OFF.
        for kind, identity in (
            ("evidence", "evidence_id"), ("state", "state_id"), ("delta", "delta_id")
        ):
            table = "memory_kernel_" + kind
            for operation in ("UPDATE",):
                conn.execute(f"""
                    CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()}
                    BEFORE {operation} ON {table}
                    BEGIN SELECT RAISE(ABORT, 'kernel record is immutable'); END
                """)
            conn.execute(f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_replace
                BEFORE INSERT ON {table}
                WHEN EXISTS(SELECT 1 FROM {table} WHERE {identity} = NEW.{identity})
                BEGIN SELECT RAISE(ABORT, 'kernel identity already exists'); END
            """)
        # References live inside immutable JSON arrays, so adding/removing an
        # evidence basis cannot evade the row's immutability through a link table.
        for kind, field in (
            ("state", "evidence_refs"), ("delta", "trigger_evidence_refs")
        ):
            conn.execute(f"""
                CREATE TRIGGER IF NOT EXISTS memory_kernel_{kind}_evidence_refs
                BEFORE INSERT ON memory_kernel_{kind}
                BEGIN
                    SELECT CASE WHEN EXISTS(
                        SELECT 1 FROM json_each(NEW.{field}) AS ref
                        WHERE ref.type != 'text' OR NOT EXISTS(
                            SELECT 1 FROM memory_kernel_evidence
                            WHERE evidence_id = ref.value
                        )
                    ) THEN RAISE(ABORT, 'missing or invalid evidence reference') END;
                END
            """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS memory_kernel_delta_transition
            BEFORE INSERT ON memory_kernel_delta
            BEGIN
                SELECT CASE WHEN NOT EXISTS(
                    SELECT 1 FROM memory_kernel_state
                    WHERE state_id = NEW.after_ref AND scope = NEW.target_scope
                ) THEN RAISE(ABORT, 'missing after state or target scope mismatch') END;
                SELECT CASE WHEN EXISTS(
                    SELECT 1 FROM memory_kernel_delta WHERE after_ref = NEW.after_ref
                ) THEN RAISE(ABORT, 'state already has a formation transition') END;
                SELECT CASE WHEN NEW.before_ref IS NOT NULL AND NOT EXISTS(
                    SELECT 1 FROM memory_kernel_state AS predecessor
                    JOIN memory_kernel_state AS successor ON successor.state_id = NEW.after_ref
                    JOIN memory_kernel_delta AS formed ON formed.after_ref = predecessor.state_id
                    WHERE predecessor.state_id = NEW.before_ref
                      AND predecessor.lineage_id IS successor.lineage_id
                      AND predecessor.rowid < successor.rowid
                ) THEN RAISE(ABORT, 'invalid predecessor or lineage') END;
            END
        """)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
