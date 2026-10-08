"""Storage-only contracts for the Hidden Flow runtime tables."""

from __future__ import annotations

import math
import os
import tempfile
import unittest

from chat.hidden_flow import runtime_store
from chat.hidden_flow.types import HIDDEN_FLOW_CONTROL_ACTIONS, FlowState


def _config(enabled: bool = True) -> dict:
    return {
        "schemaVersion": 1,
        "flowId": "store-flow",
        "enabled": enabled,
        "initialStage": "s1",
        "stages": [
            {
                "id": "s1",
                "enabled": True,
                "minTurns": 1,
                "repeatMinTurns": 1,
                "terminal": True,
            }
        ],
        "cues": [],
        "pools": [],
    }


def _snapshot(aid: int, version: int = 0) -> dict:
    return {
        "assistant_message_id": aid,
        "user_message_id": aid - 1,
        "chat_id": "default",
        "flow_id": "store-flow",
        "config_version": 1,
        "runtime_version_before": version,
        "state_before": FlowState.inactive().to_dict(),
        "state_after": FlowState.inactive().to_dict(),
        "control": None,
        "next_guide": None,
        "applied_guide": None,
    }


class HiddenFlowStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self) -> None:
        os.unlink(self.db_path)

    def test_config_versions_are_monotonic_and_disabled_rows_are_not_loaded(self) -> None:
        self.assertEqual(
            runtime_store.upsert_flow_config(_config(), db_path=self.db_path),
            1,
        )
        self.assertEqual(
            runtime_store.upsert_flow_config(_config(), db_path=self.db_path),
            2,
        )
        self.assertEqual(runtime_store.load_configs(self.db_path)[0][1], 2)
        self.assertEqual(
            runtime_store.upsert_flow_config(
                _config(False), db_path=self.db_path
            ),
            3,
        )
        self.assertEqual(runtime_store.load_configs(self.db_path), ())

    def test_pending_snapshot_commit_and_runtime_cas(self) -> None:
        runtime_store.upsert_flow_config(_config(), db_path=self.db_path)
        conn = runtime_store._connect(self.db_path)
        runtime_store.ensure_hidden_flow_schema(conn=conn)
        runtime_store.stage_snapshot(conn, _snapshot(10))
        runtime_store.finalize_snapshot(conn, 10)
        conn.commit()
        with self.assertRaises(runtime_store.HiddenFlowConflict):
            runtime_store.stage_snapshot(conn, _snapshot(11, version=0))
            runtime_store.finalize_snapshot(conn, 11)
        conn.rollback()
        conn.close()
        self.assertEqual(
            runtime_store.load_runtime("default", db_path=self.db_path)["version"],
            1,
        )

    def test_all_shared_control_actions_are_accepted_by_snapshot_validation(self) -> None:
        conn = runtime_store._connect(self.db_path)
        runtime_store.ensure_hidden_flow_schema(conn=conn)
        for index, action in enumerate(sorted(HIDDEN_FLOW_CONTROL_ACTIONS), start=100):
            runtime_store.stage_snapshot(
                conn,
                {
                    **_snapshot(index),
                    "control": {
                        "flowId": "store-flow",
                        "action": action,
                        "keys": [],
                    },
                },
            )
            conn.execute(
                "DELETE FROM hidden_flow_message_snapshots WHERE assistant_message_id=?",
                (index,),
            )
        conn.close()

    def test_non_json_safe_values_are_rejected_before_database_write(self) -> None:
        with self.assertRaises(ValueError):
            runtime_store._json({"bad": math.nan})
        conn = runtime_store._connect(self.db_path)
        runtime_store.ensure_hidden_flow_schema(conn=conn)
        with self.assertRaises(ValueError):
            runtime_store.stage_snapshot(
                conn,
                {
                    **_snapshot(1),
                    "control": {
                        "flowId": "store-flow",
                        "action": "hold",
                        "keys": ["ok", object()],
                    },
                },
            )
        conn.close()

    def test_config_version_cas_rejects_mid_turn_mutation(self) -> None:
        runtime_store.upsert_flow_config(_config(), db_path=self.db_path)
        conn = runtime_store._connect(self.db_path)
        runtime_store.ensure_hidden_flow_schema(conn=conn)
        runtime_store.stage_snapshot(conn, _snapshot(20))
        conn.commit()
        conn.close()

        self.assertEqual(
            runtime_store.upsert_flow_config(_config(), db_path=self.db_path),
            2,
        )
        conn = runtime_store._connect(self.db_path)
        with self.assertRaises(runtime_store.HiddenFlowConflict):
            runtime_store.finalize_snapshot(conn, 20)
        conn.rollback()
        status = conn.execute(
            "SELECT status FROM hidden_flow_message_snapshots "
            "WHERE assistant_message_id=?",
            (20,),
        ).fetchone()[0]
        conn.close()
        self.assertEqual(status, runtime_store.SNAPSHOT_PENDING)
        self.assertEqual(
            runtime_store.load_runtime("default", db_path=self.db_path)["version"],
            0,
        )



if __name__ == "__main__":
    unittest.main()
