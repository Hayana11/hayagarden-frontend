"""Task Timer runtime DB path and lifecycle hygiene tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.cc_capability_adapter import (
    build_uh_a0_mcp_config,
    _resolve_task_timer_commands_db_path,
)
from tools.task_timer_db import (
    DEFAULT_TASK_TIMER_COMMANDS_DB_PATH,
    resolve_task_timer_commands_db_path,
)

ROOT = Path(__file__).resolve().parents[1]


class TaskTimerRuntimeDbHygieneTests(unittest.TestCase):
    def _python(self, code, *, cwd, env):
        child_env = os.environ.copy()
        child_env.update(env)
        child_env["PYTHONPATH"] = str(ROOT)
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(cwd),
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def test_explicit_path_is_shared_by_resolvers_and_proxy_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "commands.db")
            configured = {"TASK_TIMER_COMMANDS_DB_PATH": db_path}
            self.assertEqual(
                resolve_task_timer_commands_db_path(env=configured),
                db_path,
            )
            self.assertEqual(
                _resolve_task_timer_commands_db_path(configured),
                db_path,
            )
            config = build_uh_a0_mcp_config(env=configured)
            self.assertEqual(
                config["mcpServers"]["capability"]["env"]["TASK_TIMER_COMMANDS_DB_PATH"],
                db_path,
            )
            result = self._python(
                (
                    "import json, command_store; "
                    "from tools.task_timer_capability_adapter import start_task_timer; "
                    "created = start_task_timer(None, title='shared path', countdown_seconds=30); "
                    "print(json.dumps({'db': command_store.DB_PATH, 'created': created}))"
                ),
                cwd=Path(tmp),
                env=configured,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["db"], db_path)
            self.assertEqual(payload["created"]["status"], "CREATED")
            self.assertTrue(Path(db_path).is_file())

    def test_default_is_external_and_never_repo_commands_db(self):
        self.assertEqual(
            resolve_task_timer_commands_db_path(env={}),
            DEFAULT_TASK_TIMER_COMMANDS_DB_PATH,
        )
        self.assertNotEqual(
            DEFAULT_TASK_TIMER_COMMANDS_DB_PATH,
            str(ROOT / "commands.db"),
        )

    def test_import_is_side_effect_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime_db = Path(tmp) / "runtime" / "commands.db"
            result = self._python(
                (
                    "import json, os; "
                    "from pathlib import Path; "
                    "target = Path(os.environ['TASK_TIMER_COMMANDS_DB_PATH']); "
                    "before = target.exists(); "
                    "import command_store; "
                    "after = target.exists(); "
                    "print(json.dumps({'before': before, 'after': after, "
                    "'db': command_store.DB_PATH}))"
                ),
                cwd=Path(tmp),
                env={"TASK_TIMER_COMMANDS_DB_PATH": str(runtime_db)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["before"])
            self.assertFalse(payload["after"])
            self.assertEqual(payload["db"], str(runtime_db))

    def test_isolated_full_lifecycle_uses_only_runtime_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime_db = Path(tmp) / "commands.db"
            result = self._python(
                (
                    "import json, command_store; "
                    "assert not __import__('pathlib').Path(command_store.DB_PATH).exists(); "
                    "cid = command_store.issue('lifecycle', 60); "
                    "pending = command_store.list_pending(); "
                    "command_store.mark_started(cid); "
                    "done = command_store.mark_done(cid); "
                    "feedback, ids = command_store.peek_feedback(); "
                    "consumed = command_store.consume_feedback(ids); "
                    "remaining, remaining_ids = command_store.peek_feedback(); "
                    "print(json.dumps({'cid': cid, 'pending': pending, 'done': done, "
                    "'feedback': feedback, 'ids': ids, 'consumed': consumed, "
                    "'remaining': remaining, 'remaining_ids': remaining_ids}))"
                ),
                cwd=Path(tmp),
                env={"TASK_TIMER_COMMANDS_DB_PATH": str(runtime_db)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIsInstance(payload["cid"], int)
            self.assertEqual(len(payload["pending"]), 1)
            self.assertEqual(payload["pending"][0]["title"], "lifecycle")
            self.assertIsInstance(payload["done"]["duration_ms"], int)
            self.assertEqual(payload["feedback"], ["「lifecycle」用时 0秒（比预设快 60 秒）"])
            self.assertEqual(payload["consumed"], 1)
            self.assertEqual(payload["remaining"], [])
            self.assertEqual(payload["remaining_ids"], [])
            self.assertTrue(runtime_db.is_file())


if __name__ == "__main__":
    unittest.main()
