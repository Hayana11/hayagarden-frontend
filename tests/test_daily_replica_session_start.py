"""SessionStart freeze/isolation regression tests for 9A-R."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from chat.daily_replica_ab import ReplicaContractError
from chat.daily_replica_session_start import (
    CONTEXT_INJECT_WRAPPER_PREFIX,
    OMBRE_BREATH_WRAPPER_PREFIX,
    PRODUCTION_HOOK_MARKERS,
    capture_readonly_production_session_start_visible,
    prepare_replica_session_start_isolation,
    read_static_session_start_payload,
    resolve_replica_settings_path,
    validate_session_start_against_a_hash,
    validate_session_start_bundle,
)


class DailyReplicaSessionStartTests(unittest.TestCase):
    def test_settings_path_under_claude_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = prepare_replica_session_start_isolation(Path(tmp))
            expected = resolve_replica_settings_path(bundle.claude_home)
            self.assertEqual(bundle.settings_path.resolve(), expected.resolve())
            self.assertTrue(bundle.settings_path.is_file())
            settings = json.loads(bundle.settings_path.read_text(encoding='utf-8'))
            command = settings['hooks']['SessionStart'][0]['hooks'][0]['command']
            self.assertIn('static_session_start.py', command)
            proc = subprocess.run(
                command.split(),
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(proc.stdout, read_static_session_start_payload(bundle))

    def test_freeze_production_hook_visible_format_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx_file = root / 'session_context.txt'
            ctx_file.write_text('ctx-body', encoding='utf-8')

            visible = capture_readonly_production_session_start_visible(
                context_file_path=ctx_file,
                breath_runner=lambda: (
                    OMBRE_BREATH_WRAPPER_PREFIX + 'ombre-mem'
                ),
            )
            self.assertIn(OMBRE_BREATH_WRAPPER_PREFIX + 'ombre-mem', visible)
            self.assertIn(CONTEXT_INJECT_WRAPPER_PREFIX + 'ctx-body', visible)
            self.assertTrue(ctx_file.is_file())
            self.assertEqual(ctx_file.read_text(encoding='utf-8'), 'ctx-body')

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

    def test_replica_settings_do_not_reference_production_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = prepare_replica_session_start_isolation(Path(tmp))
            settings = bundle.settings_path.read_text(encoding='utf-8')
            lowered = settings.lower()
            for marker in PRODUCTION_HOOK_MARKERS:
                self.assertNotIn(marker.lower(), lowered)
            self.assertIn('static_session_start.py', settings)

    def test_validate_against_a_hash_exact_equality(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = prepare_replica_session_start_isolation(Path(tmp))
            validate_session_start_against_a_hash(
                bundle, bundle.frozen_sha256,
            )
            with self.assertRaises(ReplicaContractError) as ctx:
                validate_session_start_against_a_hash(bundle, 'wrong-hash')
            self.assertEqual(
                ctx.exception.error_code,
                'REPLICA_SESSION_START_HASH_MISMATCH',
            )

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


if __name__ == '__main__':
    unittest.main()
