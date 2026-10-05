from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import cc_resident

SID = 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff'


def _row(kind: str, *, sid: str = SID, **extra) -> bytes:
    obj = {'type': kind, 'sessionId': sid, **extra}
    return (json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')


class _AliveResident:
    _session_id = SID
    _proc = None

    def _alive(self):
        return True


class StagedStartupObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='staged-observation-')
        self.path = Path(self.tmp.name) / 'candidate.jsonl'
        self.prefix = (json.dumps({
            'type': 'assistant',
            'uuid': 'a-1',
            'sessionId': SID,
            'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'carry'}]},
        }, ensure_ascii=False) + '\n').encode('utf-8')
        self.sha = hashlib.sha256(self.prefix).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, suffix: bytes = b''):
        self.path.write_bytes(self.prefix + suffix)

    def test_safe_uuidless_startup_rows_are_allowed_when_opted_in(self):
        self._write(
            _row('atis-latch', atis={'enabled': True})
            + _row('mode', mode='default')
            + _row('cost-state', totalCostUSD=0)
        )
        fake = _AliveResident()
        self.assertTrue(cc_resident.ResidentSession.wait_staged_health(
            fake,
            health_ms=0,
            jsonl_path=self.path,
            expected_sha256=self.sha,
            expected_size=len(self.prefix),
            expected_session_id=SID,
            allow_startup_observation_append=True,
        ))

    def test_strict_mode_still_rejects_any_append(self):
        self._write(_row('mode', mode='default'))
        fake = _AliveResident()
        with self.assertRaises(cc_resident.ResidentError):
            cc_resident.ResidentSession.wait_staged_health(
                fake,
                health_ms=0,
                jsonl_path=self.path,
                expected_sha256=self.sha,
            )

    def test_conversation_or_wrong_session_append_is_rejected(self):
        unsafe = (
            json.dumps({
                'type': 'user', 'uuid': 'u-1', 'parentUuid': 'a-1',
                'sessionId': SID, 'message': {'role': 'user', 'content': 'hello'},
            }, ensure_ascii=False) + '\n'
        ).encode('utf-8')
        self._write(unsafe)
        self.assertFalse(cc_resident.staged_jsonl_has_only_safe_startup_observations(
            self.path,
            frozen_size=len(self.prefix),
            frozen_sha256=self.sha,
            expected_session_id=SID,
        ))
        self._write(_row('mode', sid='different-session', mode='default'))
        self.assertFalse(cc_resident.staged_jsonl_has_only_safe_startup_observations(
            self.path,
            frozen_size=len(self.prefix),
            frozen_sha256=self.sha,
            expected_session_id=SID,
        ))

    def test_first_turn_growth_probe_ignores_only_safe_startup_rows(self):
        import gateway

        safe = _row('mode', mode='default') + _row('cost-state', totalCostUSD=0)
        self._write(safe)
        session = SimpleNamespace(
            jsonl_path=self.path,
            start_offset=len(self.prefix),
            staged=SimpleNamespace(_session_id=SID),
        )
        self.assertFalse(gateway._gw_first_turn_jsonl_grew(session))

        with self.path.open('ab') as handle:
            handle.write((json.dumps({
                'type': 'user', 'uuid': 'u-2', 'parentUuid': 'a-1',
                'sessionId': SID, 'message': {'role': 'user', 'content': 'sent'},
            }) + '\n').encode('utf-8'))
        self.assertTrue(gateway._gw_first_turn_jsonl_grew(session))


if __name__ == '__main__':
    unittest.main()
