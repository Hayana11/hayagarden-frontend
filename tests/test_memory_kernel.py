"""CORE-0 semantic, persistence, migration, and transaction contracts."""
from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tools.memory_kernel import Delta, Evidence, MemoryKernel, State
from tools.memory_kernel_schema import ensure_memory_kernel_schema


NOW = "2026-09-06T12:00:00+08:00"


def evidence(identity="e1", **changes):
    values = dict(
        evidence_id=identity, source_type="conversation",
        source_ref="fixture://conversation/1", observed_at=NOW,
        occurred_at=NOW, content="原话\n她喜欢惊喜。",
        provenance={"recorded_by": "fixture", "source_message": "1"},
        origin_kind="source",
    )
    return Evidence(**(values | changes))


def state(identity="s1", **changes):
    values = dict(
        state_id=identity, lineage_id="preference-1", scope="preference",
        subject_ref="person:fixture", representation="judgment",
        content="她喜欢惊喜。", evidence_refs=("e1",),
        confidence=0.6, epistemic_status="tentative", status="active", created_at=NOW,
    )
    return State(**(values | changes))


def delta(identity="d1", **changes):
    values = dict(
        delta_id=identity, target_scope="preference", before_ref=None,
        after_ref="s1", trigger_evidence_refs=("e1",),
        derived_by={"processor": "fixture-reviewer"},
        confidence=0.6, occurred_at=NOW, review_status="accepted",
    )
    return Delta(**(values | changes))


class MemoryKernelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "kernel.db"
        self.kernel = MemoryKernel(self.path)
        self.kernel.initialize()
        self.kernel.create_evidence(evidence())

    def sql(self, statement, params=()):
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            return conn.execute(statement, params).fetchall()

    def initial(self, **changes):
        return self.kernel.create_state(state(**changes), delta())

    def revision(self, **changes):
        self.kernel.create_evidence(evidence("e2", content="不喜欢未经商量改变安排"))
        revised = state(
            "s2", content="她喜欢小礼物，但不喜欢未经商量改变安排。",
            evidence_refs=("e1", "e2"), confidence=0.9,
        )
        transition = delta("d2", before_ref="s1", after_ref="s2",
                           trigger_evidence_refs=("e2",))
        return self.kernel.revise_state(dataclasses.replace(revised, **changes), transition)

    def test_case_1_evidence_durability_across_reopen(self):
        reopened = MemoryKernel(self.path)
        self.assertEqual(reopened.get_evidence("e1"), evidence())
        external = evidence(
            "external", content=None, content_ref="fixture://archive/2",
            participants=("person:a",), entities=("project:b",),
            integrity="sha256:fixture", visibility="private",
        )
        self.kernel.create_evidence(external)
        self.assertEqual(reopened.get_evidence("external"), external)

    def test_case_2_initial_state_and_lineage(self):
        first, formation = self.initial()
        self.assertEqual(first, state())
        self.assertEqual(formation, delta())
        self.assertEqual(self.kernel.lineage_history("preference-1"), [first])
        self.assertEqual(self.kernel.lineage_history("missing"), [])

    def test_case_3_revision_preserves_exact_old_version(self):
        old, formed = self.initial()
        new, changed = self.revision()
        reopened = MemoryKernel(self.path)
        self.assertEqual(reopened.get_state("s1"), old)
        self.assertEqual(reopened.get_delta("d1"), formed)
        self.assertNotEqual(new.state_id, old.state_id)
        self.assertEqual(new.lineage_id, old.lineage_id)
        self.assertEqual((changed.before_ref, changed.after_ref), ("s1", "s2"))
        self.assertEqual(reopened.lineage_history("preference-1"), [old, new])

    def test_case_4_evidence_traceability_and_transition_queries(self):
        self.initial()
        _, change = self.revision()
        self.assertEqual(self.kernel.evidence_for_state("s1"), [evidence()])
        self.assertEqual(
            [e.evidence_id for e in self.kernel.evidence_for_state("s2")], ["e1", "e2"],
        )
        self.assertEqual(
            [e.evidence_id for e in self.kernel.evidence_for_delta("d2")], ["e2"],
        )
        self.assertEqual(self.kernel.transitions_for_state("s1"), [delta(), change])
        self.assertEqual(self.kernel.transitions_for_state("s2"), [change])

    def test_case_5_all_state_columns_reject_in_place_update(self):
        old, _ = self.initial()
        for field in dataclasses.fields(State):
            with self.subTest(field=field.name), self.assertRaises(sqlite3.IntegrityError):
                self.sql(f"UPDATE memory_kernel_state SET {field.name} = {field.name}")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            old.content = "changed"
        with self.assertRaises(AttributeError):
            self.kernel.update_state("s1", content="changed")
        self.assertEqual(self.kernel.get_state("s1"), old)

    def test_case_6_missing_evidence_state_and_delta_refs(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.create_state(state(evidence_refs=("absent",)), delta())
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.create_state(state(), delta(trigger_evidence_refs=("absent",)))
        self.assertEqual(self.sql("SELECT * FROM memory_kernel_state"), [])
        with self.assertRaises(KeyError):
            self.kernel.revise_state(state("s2"), delta(before_ref="absent", after_ref="s2"))
        with self.assertRaises(ValueError):
            self.kernel.create_state(state(), delta(after_ref="absent"))
        for method in (
            self.kernel.get_state, self.kernel.get_delta, self.kernel.get_evidence,
            self.kernel.evidence_for_state, self.kernel.evidence_for_delta,
            self.kernel.transitions_for_state,
        ):
            with self.subTest(method=method.__name__), self.assertRaises(KeyError):
                method("absent")

    def test_pure_comparison_cannot_create_delta(self):
        self.initial()
        with self.assertRaisesRegex(ValueError, "semantic change"):
            self.kernel.revise_state(
                state("s2"), delta("d2", before_ref="s1", after_ref="s2"),
            )
        self.assertEqual(self.sql("SELECT count(*) FROM memory_kernel_delta"), [(1,)])

    def test_case_7_self_loop_rejected(self):
        self.initial()
        with self.assertRaises(ValueError):
            self.kernel.revise_state(state(), delta("d2", before_ref="s1"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql(
                "INSERT INTO memory_kernel_delta SELECT 'd2', target_scope, after_ref, "
                "after_ref, trigger_evidence_refs, derived_by, rationale, rationale_ref, "
                "rationale_kind, confidence, occurred_at, review_status FROM memory_kernel_delta"
            )

    def test_case_8_first_formation_requires_null_before(self):
        self.initial()
        self.assertIsNone(self.kernel.get_delta("d1").before_ref)
        with self.assertRaises(ValueError):
            self.kernel.create_state(state("s2"), delta("d2", before_ref="s1", after_ref="s2"))
        with self.assertRaises(ValueError):
            self.kernel.revise_state(state("s2"), delta("d2", after_ref="s2"))

    def test_case_9_real_delta_insert_failure_rolls_back_new_state(self):
        old, formed = self.initial()
        # Force failure at the database transition insertion, after State insertion.
        self.sql(
            "CREATE TRIGGER fixture_fail_delta BEFORE INSERT ON memory_kernel_delta "
            "BEGIN SELECT RAISE(ABORT, 'injected transition failure'); END"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
            self.revision()
        self.assertEqual(self.kernel.get_state("s1"), old)
        self.assertEqual(self.kernel.get_delta("d1"), formed)
        with self.assertRaises(KeyError):
            self.kernel.get_state("s2")
        self.assertEqual(self.sql("SELECT count(*) FROM memory_kernel_delta"), [(1,)])

    def test_case_9_state_insert_failure_never_leaves_delta(self):
        self.initial()
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.revise_state(
                state("s2", evidence_refs=("absent",)),
                delta("d2", before_ref="s1", after_ref="s2"),
            )
        with self.assertRaises(KeyError):
            self.kernel.get_delta("d2")
        self.assertEqual(self.sql("SELECT count(*) FROM memory_kernel_state"), [(1,)])

    def test_initial_delta_failure_rolls_back_formation(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.create_state(state(), delta(trigger_evidence_refs=("absent",)))
        self.assertEqual(self.kernel.lineage_history("preference-1"), [])

    def test_evidence_cannot_be_replaced_with_generated_summary(self):
        generated = evidence("summary", origin_kind="derived",
                             provenance={"processor": "fixture", "inputs": ["e1"]})
        self.kernel.create_evidence(generated)
        self.assertEqual(self.kernel.get_evidence("summary").origin_kind, "derived")
        for operation in (
            "UPDATE memory_kernel_evidence SET origin_kind = 'source' WHERE evidence_id='summary'",
            "INSERT OR REPLACE INTO memory_kernel_evidence SELECT * FROM memory_kernel_evidence",
        ):
            with self.subTest(operation=operation), self.assertRaises(sqlite3.IntegrityError):
                self.sql(operation)
        # CORE-0 has no delete API, while storage does not freeze future
        # authorized deletion governance into an impossible operation.
        self.assertFalse(hasattr(self.kernel, "delete_evidence"))
        self.sql("DELETE FROM memory_kernel_evidence WHERE evidence_id='summary'")
        with self.assertRaises(KeyError):
            self.kernel.get_evidence("summary")
        self.assertEqual(self.kernel.get_evidence("e1"), evidence())

    def test_delete_governance_is_deferred_without_a_service_surface(self):
        self.initial()
        self.assertFalse(hasattr(self.kernel, "delete_state"))
        self.assertFalse(hasattr(self.kernel, "delete_delta"))
        self.sql("DELETE FROM memory_kernel_delta WHERE delta_id='d1'")
        self.assertEqual(self.sql("SELECT count(*) FROM memory_kernel_delta"), [(0,)])

    def test_replacement_delete_and_upsert_cannot_erase_state_or_delta(self):
        self.initial()
        for table in ("memory_kernel_state", "memory_kernel_delta"):
            for operation in (
                f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}",
                f"UPDATE {table} SET rowid = rowid + 1",
            ):
                with self.subTest(sql=operation), self.assertRaises(sqlite3.IntegrityError):
                    self.sql(operation)
        # No public deletion surface is present; lifecycle authorization remains
        # deferred and is not permanently prohibited by CORE-0 storage triggers.
        self.assertFalse(hasattr(self.kernel, "delete_state"))
        self.assertFalse(hasattr(self.kernel, "delete_delta"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql(
                "INSERT INTO memory_kernel_state SELECT * FROM memory_kernel_state WHERE 1 "
                "ON CONFLICT(state_id) DO UPDATE SET content = 'new'"
            )

    def test_confidence_evidence_status_and_scope_revisions_get_new_ids(self):
        old, _ = self.initial()
        for index, change in enumerate((
            {"confidence": 0.8},
            {"epistemic_status": "contested", "status": "contested"},
            {"scope": "custom:boundary"},
            {"representation": "custom:exemplar"},
            {"subject_ref": "group:fixture"},
            {"content": None, "structured_value": {"preference": ["small gifts"]}},
        ), start=2):
            new = state(f"s{index}", **change)
            self.kernel.revise_state(
                new, delta(f"d{index}", before_ref="s1", after_ref=new.state_id,
                           target_scope=new.scope),
            )
        self.assertEqual(self.kernel.get_state("s1"), old)

    def test_lineage_mismatch_and_duplicate_formation_rejected(self):
        self.initial()
        for lineage in ("other", None):
            with self.subTest(lineage=lineage), self.assertRaises(ValueError):
                self.kernel.revise_state(
                    state("s2", lineage_id=lineage),
                    delta("d2", before_ref="s1", after_ref="s2"),
                )
        with self.assertRaises(ValueError):
            self.kernel.create_state(state("s2"), delta("d2", after_ref="s2"))

    def test_null_lineage_revision_remains_traceable(self):
        self.initial(lineage_id=None)
        _, change = self.revision(lineage_id=None)
        self.assertEqual(self.kernel.transitions_for_state("s2"), [change])
        self.assertIsNone(self.kernel.get_state("s1").lineage_id)

    def test_history_order_does_not_trust_caller_clock(self):
        old, _ = self.initial()
        new, _ = self.revision(created_at="2000-01-01T00:00:00Z")
        self.assertEqual(self.kernel.lineage_history("preference-1"), [old, new])

    def test_delta_rationale_cannot_be_promoted_to_causal_fact(self):
        explained = delta(
            rationale="可能与先前安排有关，这是一个非权威 transition note",
            rationale_ref="fixture://analysis/1",
        )
        self.kernel.create_state(state(), explained)
        stored = self.kernel.get_delta("d1")
        self.assertEqual(stored.rationale_kind, "derived")
        self.assertEqual(stored.rationale_ref, "fixture://analysis/1")
        self.assertEqual(self.kernel.evidence_for_delta("d1"), [evidence()])
        # A rationale is an annotation, never a new Evidence row or fact.
        self.assertEqual(self.sql("SELECT count(*) FROM memory_kernel_evidence"), [(1,)])
        with self.assertRaises(ValueError):
            self.kernel.create_state(
                state("s2", lineage_id="other"),
                delta("d2", after_ref="s2", rationale_kind="fact"),
            )

    def test_nested_objects_and_caller_inputs_cannot_write_through(self):
        original = evidence("nested", provenance={"details": {"who": "fixture"}})
        returned = self.kernel.create_evidence(original)
        original.provenance["details"]["who"] = "changed"
        returned.provenance["details"]["who"] = "also changed"
        self.assertEqual(self.kernel.get_evidence("nested").provenance,
                         {"details": {"who": "fixture"}})
        self.initial(content=None, structured_value={"items": ["one"]})
        self.kernel.get_state("s1").structured_value["items"].append("two")
        self.assertEqual(self.kernel.get_state("s1").structured_value, {"items": ["one"]})

    def test_validation_is_explicit_and_non_lossy(self):
        bad_states = (
            {"confidence": float("nan")}, {"confidence": float("inf")},
            {"confidence": True}, {"confidence": -0.1}, {"confidence": 1.1},
            {"evidence_refs": "e1"}, {"evidence_refs": ("e1", "e1")},
            {"scope": ""}, {"status": None}, {"created_at": "2026-09-06"},
            {"content": None}, {"structured_value": {"bad": float("nan")}},
            {"structured_value": {1: "lossy key"}}, {"structured_value": {"x": (1, 2)}},
        )
        for changes in bad_states:
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                self.kernel.create_state(state(**changes), delta())
        for changes in (
            {"origin_kind": None}, {"origin_kind": "unknown"}, {"provenance": {}},
            {"source_ref": None}, {"content": None}, {"observed_at": None},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.kernel.create_evidence(evidence("bad", **changes))

    def test_sql_references_reject_missing_after_and_before(self):
        self.initial()
        for before, after in ((None, "missing"), ("missing", "s1")):
            with self.subTest(before=before, after=after), self.assertRaises(sqlite3.IntegrityError):
                self.sql(
                    "INSERT INTO memory_kernel_delta VALUES "
                    "(?, ?, ?, ?, ?, ?, NULL, NULL, 'derived', ?, ?, ?)",
                    ("bad", "preference", before, after, '["e1"]',
                     '{"processor":"fixture"}', 0.5, NOW, "accepted"),
                )

    def test_parallel_initial_formations_cannot_split_one_lineage(self):
        def create(index):
            try:
                MemoryKernel(self.path).create_state(
                    state(f"s{index}"), delta(f"d{index}", after_ref=f"s{index}"),
                )
                return "created"
            except ValueError:
                return "rejected"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(create, (1, 2)))
        self.assertCountEqual(outcomes, ["created", "rejected"])
        self.assertEqual(self.sql("SELECT count(*) FROM memory_kernel_state"), [(1,)])
        self.assertEqual(self.sql("SELECT count(*) FROM memory_kernel_delta"), [(1,)])

    def test_migration_repeated_upgrade_preserves_kernel_and_legacy(self):
        self.initial()
        self.sql("CREATE TABLE posts (id INTEGER PRIMARY KEY, content TEXT)")
        self.sql("INSERT INTO posts VALUES (1, 'fixture legacy')")
        self.sql("PRAGMA user_version = 23")
        before = self.sql("SELECT * FROM memory_kernel_state")
        legacy_schema = self.sql("SELECT sql FROM sqlite_master WHERE name='posts'")
        self.kernel.initialize()
        self.kernel.initialize()
        self.assertEqual(before, self.sql("SELECT * FROM memory_kernel_state"))
        self.assertEqual(self.sql("SELECT * FROM posts"), [(1, "fixture legacy")])
        self.assertEqual(self.sql("SELECT sql FROM sqlite_master WHERE name='posts'"), legacy_schema)
        self.assertEqual(self.sql("PRAGMA user_version"), [(23,)])
        self.assertEqual(self.sql("PRAGMA foreign_key_check"), [])

    def test_migration_upgrade_from_legacy_only_database(self):
        path = Path(self.tmp.name) / "legacy-fixture.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE posts (content TEXT)")
            conn.execute("INSERT INTO posts VALUES ('synthetic')")
        other = MemoryKernel(path)
        other.initialize()
        with sqlite3.connect(path) as conn:
            self.assertEqual(conn.execute("SELECT * FROM posts").fetchall(), [("synthetic",)])
            names = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'memory_kernel_%'"
            ).fetchall()
            self.assertEqual(len(names), 3)

    def test_migration_rejects_active_transaction_without_committing_it(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE fixture (id INTEGER)")
            conn.execute("INSERT INTO fixture VALUES (1)")
            with self.assertRaises(ValueError):
                ensure_memory_kernel_schema(conn)
            self.assertTrue(conn.in_transaction)
            conn.rollback()
            self.assertEqual(conn.execute("SELECT * FROM fixture").fetchall(), [])

    def test_migration_failure_rolls_back_ddl(self):
        path = Path(self.tmp.name) / "failed.db"
        with sqlite3.connect(path) as conn:
            def deny_index(action, _one, _two, _db, _trigger):
                return 1 if action == 1 else 0  # SQLITE_DENY / CREATE_INDEX / OK (Python 3.10)
            conn.set_authorizer(deny_index)
            with self.assertRaises(sqlite3.DatabaseError):
                ensure_memory_kernel_schema(conn)
            conn.set_authorizer(lambda *_: 0)
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master").fetchall(), [])

    def test_database_path_required_and_initialization_explicit(self):
        for path in ("", None, ":memory:", "file:kernel?mode=memory"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                MemoryKernel(path)
        absent = Path(self.tmp.name) / "absent.db"
        MemoryKernel(absent)
        self.assertFalse(absent.exists())


if __name__ == "__main__":
    unittest.main()
