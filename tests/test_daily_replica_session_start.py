"""SessionStart freeze/isolation regression tests for 9A-R."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from chat.daily_replica_ab import ReplicaContractError
from chat.daily_replica_session_start import (
    PRODUCTION_HOOK_MARKERS,
    prepare_replica_session_start_isolation,
    read_static_session_start_payload,
    validate_session_start_bundle,
)


class DailyReplicaSessionStartTests(unittest.TestCase):
    def test_dynamic_capture_once_static_hook_stable_for_ab(self):
        calls = {'n': 0}

        def dynamic_capturer() -> str:
            calls['n'] += 1
            return 'dynamic-%d' % calls['n']

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = prepare_replica_session_start_isolation(
                root,
                material_capturer=dynamic_capturer,
            )
            validate_session_start_bundle(bundle)
            self.assertEqual(calls['n'], 1)
            a_payload = read_static_session_start_payload(bundle)
            b_payload = read_static_session_start_payload(bundle)
            self.assertEqual(a_payload, 'dynamic-1')
            self.assertEqual(a_payload, b_payload)
            self.assertEqual(bundle.frozen_sha256, bundle.frozen_sha256)

    def test_fake_consumable_source_not_deleted_by_static_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            consumable = root / 'consumable.txt'
            consumable.write_text('consume-me', encoding='utf-8')

            def capturer() -> str:
                return consumable.read_text(encoding='utf-8')

            bundle = prepare_replica_session_start_isolation(
                root,
                material_capturer=capturer,
            )
            read_static_session_start_payload(bundle)
            read_static_session_start_payload(bundle)
            self.assertTrue(consumable.is_file())
            self.assertEqual(consumable.read_text(encoding='utf-8'), 'consume-me')

    def test_replica_settings_do_not_reference_production_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = prepare_replica_session_start_isolation(Path(tmp))
            settings = bundle.settings_path.read_text(encoding='utf-8')
            lowered = settings.lower()
            for marker in PRODUCTION_HOOK_MARKERS:
                self.assertNotIn(marker.lower(), lowered)
            self.assertIn('static_session_start.py', settings)

    def test_hash_mismatch_fails_closed_before_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = prepare_replica_session_start_isolation(Path(tmp))
            bundle.frozen_path.write_text('mutated', encoding='utf-8')
            with self.assertRaises(ReplicaContractError) as ctx:
                validate_session_start_bundle(bundle)
            self.assertEqual(
                ctx.exception.error_code,
                'REPLICA_SESSION_START_HASH_MISMATCH',
            )

    def test_static_hook_emits_frozen_payload_via_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = prepare_replica_session_start_isolation(
                Path(tmp),
                material_capturer=lambda: 'frozen-visible',
            )
            proc = subprocess.run(
                ['python3', str(bundle.static_hook_path)],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(proc.stdout, 'frozen-visible')


if __name__ == '__main__':
    unittest.main()
