"""Unit tests for tools/workspace_jobs.py"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class WorkspaceJobsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["EXEC_CWD"] = self.tmpdir.name
        os.environ["WORKSPACE_ROOT"] = self.tmpdir.name
        os.environ["EXEC_ENABLED"] = "0"
        import importlib
        import tools.workspace_executor as ex
        import tools.workspace_jobs as wj
        import tools.workspace_agent as wa
        self.ex = importlib.reload(ex)
        self.wj = importlib.reload(wj)
        self.wa = importlib.reload(wa)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_start_disabled_when_exec_off(self):
        result = json.loads(self.wj.ws_job({"action": "start", "cmd": "echo hi"}))
        self.assertEqual(result["error"], "exec_disabled")

    def test_invalid_job_id_rejected(self):
        result = json.loads(self.wj.ws_job({"action": "status", "id": "bad"}))
        self.assertEqual(result["error"], "ValueError")

    def test_ws_job_routed_in_agent(self):
        result = json.loads(self.wa.call_tool("ws_job", {"action": "list"}))
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("jobs"), [])

    def test_drain_pending_events(self):
        self.wj.queue_event({"type": "job_finished", "meta": {"job_id": "job_abc"}})
        events = self.wj.drain_pending_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(self.wj.drain_pending_events(), [])

    def test_pending_events_persist_on_disk(self):
        self.wj.queue_event({"type": "job_finished", "meta": {"job_id": "job_disk"}})
        pending = Path(self.tmpdir.name) / ".jobs" / "events" / "pending.jsonl"
        self.assertTrue(pending.exists())
        text = pending.read_text(encoding="utf-8")
        self.assertIn("job_disk", text)
        events = self.wj.drain_pending_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(pending.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
