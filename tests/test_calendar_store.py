from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import calendar_store as cs

DEVICE = "6f1c2a7e-4b7f-4c9e-9d1e-3a2b1c0d9e8f"
OTHER_DEVICE = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
NOW = dt.datetime(2026, 10, 25, 2, 0, tzinfo=dt.timezone.utc)  # 10:00 in Shanghai
ROOT = Path(__file__).resolve().parent.parent


def _ms(text: str, tz: str = "+08:00") -> int:
    return int(dt.datetime.fromisoformat(text + tz).timestamp() * 1000)


def _event(event_id, begin, end, *, title="会", own=False, all_day=False, calendar="个人"):
    return {
        "eventId": event_id,
        "beginMs": begin,
        "endMs": end,
        "allDay": all_day,
        "calendarId": "9" if own else "1",
        "calendarName": "费佳" if own else calendar,
        "own": own,
        "title": title,
    }


def _sync(path, *, events=(), results=(), write=True, read=True, device=DEVICE, now=NOW):
    return cs.sync_device(path, device, {
        "schemaVersion": 1,
        "permission": {"read": read, "write": write},
        "timezone": "Asia/Shanghai",
        "ownCalendarId": "9",
        "windowStartMs": _ms("2026-10-24T00:00:00"),
        "windowEndMs": _ms("2026-11-24T00:00:00"),
        "events": None if events is None else list(events),
        "results": list(results),
    }, now=now)


class CalendarStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "memories.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_without_any_sync_is_not_connected(self):
        result = cs.read_events(self.db, now=NOW)
        self.assertEqual(result["status"], "NOT_CONNECTED")
        self.assertEqual(result["events"], [])

    def test_read_returns_todays_events_in_phone_timezone(self):
        _sync(self.db, events=[
            _event("1", _ms("2026-10-25T14:00:00"), _ms("2026-10-25T15:30:00"), title="交稿"),
            _event("2", _ms("2026-10-26T09:00:00"), _ms("2026-10-26T10:00:00"), title="明天"),
            _event("3", _ms("2026-10-25T00:00:00", "+00:00"), _ms("2026-10-26T00:00:00", "+00:00"),
                   title="全天", all_day=True),
        ])
        result = cs.read_events(self.db, now=NOW)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["range"], {"from": "2026-10-25", "to": "2026-10-25", "timezone": "Asia/Shanghai"})
        self.assertEqual([e["title"] for e in result["events"]], ["全天", "交稿"])
        timed = result["events"][1]
        self.assertEqual((timed["start"], timed["end"]), ("2026-10-25 14:00", "2026-10-25 15:30"))
        self.assertEqual(result["events"][0]["start"], "2026-10-25")
        self.assertFalse(result["stale"])
        self.assertTrue(result["can_write"])
        two_days = cs.read_events(self.db, days=2, now=NOW)
        self.assertEqual(len(two_days["events"]), 3)

    def test_read_marks_old_snapshot_stale(self):
        _sync(self.db)
        later = NOW + dt.timedelta(hours=3)
        self.assertTrue(cs.read_events(self.db, now=later)["stale"])

    def test_snapshot_replaces_previous_and_read_revoke_clears(self):
        _sync(self.db, events=[_event("1", _ms("2026-10-25T14:00:00"), _ms("2026-10-25T15:00:00"))])
        _sync(self.db, read=False, events=None)
        result = cs.read_events(self.db, now=NOW)
        self.assertEqual(result["status"], "READ_PERMISSION_MISSING")
        self.assertEqual(result["events"], [])

    def test_write_requires_write_permission(self):
        _sync(self.db, write=False)
        result = cs.queue_write(self.db, op="create", title="吃饭", start="2026-10-25 12:00", now=NOW)
        self.assertEqual(result["status"], "WRITE_UNAVAILABLE")
        self.assertEqual(cs.get_status(self.db)["commands"]["pending"], 0)

    def test_create_defaults_and_round_trip_through_phone(self):
        _sync(self.db)
        queued = cs.queue_write(self.db, op="create", title="吃饭", start="2026-10-25 12:00", now=NOW)
        self.assertEqual(queued["status"], "QUEUED")
        self.assertEqual(queued["write"]["start"], "2026-10-25 12:00")
        self.assertEqual(queued["write"]["end"], "2026-10-25 13:00")
        self.assertTrue(cs.has_pending_commands(self.db, DEVICE))

        handed = _sync(self.db)["commands"]
        self.assertEqual(len(handed), 1)
        command = handed[0]
        self.assertEqual(command["op"], "create")
        self.assertEqual(command["startMs"], _ms("2026-10-25T12:00:00"))
        self.assertEqual(command["endMs"], _ms("2026-10-25T13:00:00"))
        self.assertIs(command["allDay"], False)
        self.assertEqual(command["reminderMinutes"], 10)
        self.assertFalse(cs.has_pending_commands(self.db, DEVICE))
        self.assertEqual(_sync(self.db)["commands"], [])  # not handed out twice

        done = _sync(self.db, results=[{"id": command["id"], "ok": True, "eventId": "77"}],
                     events=[_event("77", command["startMs"], command["endMs"], title="吃饭", own=True)])
        self.assertEqual(done["appliedResults"], 1)
        status = cs.get_status(self.db)
        self.assertEqual(status["commands"]["done"], 1)
        mine = cs.read_events(self.db, now=NOW)["events"][0]
        self.assertTrue(mine["mine"])
        self.assertEqual(mine["calendar"], "费佳")

    def test_all_day_create_uses_utc_midnight_and_inclusive_end(self):
        _sync(self.db)
        cs.queue_write(self.db, op="create", title="假期", start="2026-10-30", end="2026-10-31", now=NOW)
        command = _sync(self.db)["commands"][0]
        self.assertIs(command["allDay"], True)
        self.assertEqual(command["startMs"], _ms("2026-10-30T00:00:00", "+00:00"))
        self.assertEqual(command["endMs"], _ms("2026-11-01T00:00:00", "+00:00"))
        self.assertIsNone(command["reminderMinutes"])

    def test_update_and_delete_only_touch_own_calendar(self):
        _sync(self.db, events=[
            _event("5", _ms("2026-10-25T14:00:00"), _ms("2026-10-25T15:00:00"), own=False),
            _event("8", _ms("2026-10-25T18:00:00"), _ms("2026-10-25T19:00:00"), own=True),
        ])
        with self.assertRaisesRegex(cs.CalendarError, "own calendar"):
            cs.queue_write(self.db, op="delete", event_id="5", now=NOW)
        with self.assertRaisesRegex(cs.CalendarError, "own calendar"):
            cs.queue_write(self.db, op="update", event_id="404", title="x", now=NOW)
        with self.assertRaisesRegex(cs.CalendarError, "only event_id"):
            cs.queue_write(self.db, op="delete", event_id="8", title="x", now=NOW)
        with self.assertRaisesRegex(cs.CalendarError, "at least one field"):
            cs.queue_write(self.db, op="update", event_id="8", now=NOW)
        self.assertEqual(cs.queue_write(self.db, op="delete", event_id="8", now=NOW)["status"], "QUEUED")
        updated = cs.queue_write(self.db, op="update", event_id="8", title="晚饭", now=NOW)
        self.assertEqual(updated["write"], {"event_id": "8", "title": "晚饭"})
        commands = _sync(self.db)["commands"]
        self.assertEqual([c["op"] for c in commands], ["delete", "update"])
        self.assertNotIn("reminderMinutes", commands[1])

    def test_created_event_can_be_updated_before_next_snapshot(self):
        _sync(self.db)
        cs.queue_write(self.db, op="create", title="睡觉", start="2026-10-25 23:30", now=NOW)
        command = _sync(self.db)["commands"][0]
        _sync(self.db, results=[{"id": command["id"], "ok": True, "eventId": "90"}])
        self.assertEqual(
            cs.queue_write(self.db, op="update", event_id="90", start="2026-10-26 00:00", now=NOW)["status"],
            "QUEUED",
        )

    def test_invalid_inputs_are_rejected(self):
        _sync(self.db)
        cases = [
            dict(op="create", title="", start="2026-10-25 12:00"),
            dict(op="create", title="x"),
            dict(op="create", title="x", start="明天中午"),
            dict(op="create", title="x", start="2026-10-25 12:00", end="2026-10-25 11:00"),
            dict(op="create", title="x", start="2026-10-25 12:00", end="2026-10-26"),
            dict(op="create", title="x", start="2026-10-25", all_day=False),
            dict(op="create", title="x", start="2026-10-25 12:00", reminder_minutes=99999),
            dict(op="move", title="x"),
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(cs.CalendarError):
                cs.queue_write(self.db, now=NOW, **case)
        self.assertEqual(cs.get_status(self.db)["commands"]["pending"], 0)

    def test_failed_result_is_reported_and_unanswered_commands_are_resent_then_dropped(self):
        _sync(self.db)
        cs.queue_write(self.db, op="create", title="a", start="2026-10-25 12:00", now=NOW)
        cs.queue_write(self.db, op="create", title="b", start="2026-10-25 13:00", now=NOW)
        first, second = _sync(self.db)["commands"]
        _sync(self.db, results=[{"id": first["id"], "ok": False, "error": "calendar_missing"}])
        failed = cs.read_events(self.db, now=NOW)["recent_failed_writes"]
        self.assertEqual(failed, [{"command_id": first["id"], "op": "create", "error": "calendar_missing"}])

        when = NOW
        for _attempt in range(2):
            when += dt.timedelta(minutes=11)
            resent = _sync(self.db, now=when)["commands"]
            self.assertEqual([c["id"] for c in resent], [second["id"]])
        when += dt.timedelta(minutes=11)
        self.assertEqual(_sync(self.db, now=when)["commands"], [])
        self.assertEqual(cs.get_status(self.db)["commands"]["failed"], 2)

    def test_results_from_another_device_are_ignored(self):
        _sync(self.db)
        cs.queue_write(self.db, op="create", title="a", start="2026-10-25 12:00", now=NOW)
        command = _sync(self.db)["commands"][0]
        other = _sync(self.db, device=OTHER_DEVICE, write=False,
                      results=[{"id": command["id"], "ok": True, "eventId": "1"}])
        self.assertEqual(other["appliedResults"], 0)
        self.assertEqual(cs.get_status(self.db)["commands"]["sent"], 1)

    def test_no_commands_or_flag_without_write_permission(self):
        _sync(self.db)
        cs.queue_write(self.db, op="create", title="a", start="2026-10-25 12:00", now=NOW)
        self.assertEqual(_sync(self.db, write=False)["commands"], [])
        self.assertFalse(cs.has_pending_commands(self.db, DEVICE))

    def test_sync_rejects_bad_payloads(self):
        for payload in (
            [],
            {"schemaVersion": 2},
            {"schemaVersion": 1, "permission": {"read": True}, "events": "x"},
            {"schemaVersion": 1, "permission": {"read": True}, "events": [{"eventId": "1"}]},
            {"schemaVersion": 1, "results": [{"id": "not-a-uuid"}]},
        ):
            with self.subTest(payload=payload), self.assertRaises(cs.CalendarError):
                cs.sync_device(self.db, DEVICE, payload, now=NOW)

    def test_capability_adapter_reports_validation_errors_as_json(self):
        _sync(self.db)
        proc = subprocess.run(
            [sys.executable, "-m", "tools.calendar_capability_adapter"],
            input=json.dumps({"operation": "calendar_write", "db_path": self.db, "op": "create", "title": "x"}),
            capture_output=True, text=True, cwd=ROOT, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "INVALID_INPUT")
        proc = subprocess.run(
            [sys.executable, "-m", "tools.calendar_capability_adapter"],
            input=json.dumps({"operation": "calendar_read", "db_path": self.db, "days": 3}),
            capture_output=True, text=True, cwd=ROOT, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "OK")


class CalendarRoutesTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask

        from calendar_routes import create_calendar_blueprint
        from tools import health_store

        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "memories.db")
        self.health_db = os.path.join(self.tmp.name, "health.db")
        enrolled = health_store.enroll_device({
            "schemaVersion": health_store.SCHEMA_VERSION,
            "installId": "4b8a0b0e-2c1d-4f3a-9b8c-7d6e5f4a3b2c",
            "packageName": "xyz.lovestyle.home.canary",
            "label": "test",
        }, self.health_db)
        self.device_id = enrolled["deviceId"]
        self.credential = enrolled["credential"]
        app = Flask(__name__)
        app.register_blueprint(create_calendar_blueprint(
            db_path=self.db,
            health_db_path=self.health_db,
            owner_guard=lambda request: None,
        ))
        self.client = app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _headers(self, credential=None):
        return {
            "X-Health-Device-ID": self.device_id,
            "Authorization": "Bearer " + (credential or self.credential),
        }

    def test_sync_requires_device_credential(self):
        body = {"schemaVersion": 1, "permission": {"read": False, "write": False}}
        self.assertEqual(self.client.post("/api/calendar/mobile/sync", json=body).status_code, 401)
        bad = self.client.post("/api/calendar/mobile/sync", json=body, headers=self._headers("wrong"))
        self.assertEqual(bad.status_code, 401)
        ok = self.client.post("/api/calendar/mobile/sync", json=body, headers=self._headers())
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()["commands"], [])

    def test_sync_rejects_invalid_payload_and_status_reports(self):
        bad = self.client.post("/api/calendar/mobile/sync", json={"schemaVersion": 9}, headers=self._headers())
        self.assertEqual(bad.status_code, 400)
        self.client.post("/api/calendar/mobile/sync", headers=self._headers(), json={
            "schemaVersion": 1, "permission": {"read": True, "write": True}, "timezone": "Asia/Shanghai",
            "events": [_event("1", _ms("2026-10-25T14:00:00"), _ms("2026-10-25T15:00:00"))],
        })
        status = self.client.get("/api/calendar/mobile/status").get_json()
        self.assertTrue(status["connected"])
        self.assertTrue(status["writeGranted"])
        self.assertEqual(status["eventCount"], 1)


class CalendarPushFlagTests(unittest.TestCase):
    def test_pending_calendar_marks_long_poll_payload(self):
        import wake_push

        self.assertEqual(wake_push.with_calendar_flag(None), {"has_message": False, "calendar": True})
        self.assertEqual(
            wake_push.with_calendar_flag({"has_message": True, "id": "wake-1"}),
            {"has_message": True, "id": "wake-1", "calendar": True},
        )


if __name__ == "__main__":
    unittest.main()
