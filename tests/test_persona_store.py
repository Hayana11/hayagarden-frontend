"""Persona runtime authority — store, API, system_builder, backup, Git isolation."""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat import persona_store
from chat import system_builder


class PersonaStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.runtime = base / 'runtime' / 'persona.md'
        self.repo = base / 'repo' / 'prompts' / 'persona.md'
        self.repo.parent.mkdir(parents=True)
        self.runtime.parent.mkdir(parents=True)
        self.patches = [
            mock.patch.object(persona_store, 'RUNTIME_PERSONA_PATH', str(self.runtime)),
            mock.patch.object(
                persona_store, 'REPO_PERSONA_FALLBACK_PATH', str(self.repo),
            ),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_T1_first_bootstrap_from_repo(self):
        self.repo.write_text('PERSONA_A', encoding='utf-8')
        self.assertFalse(self.runtime.exists())
        text = persona_store.read_persona()
        self.assertEqual(text, 'PERSONA_A')
        self.assertTrue(self.runtime.exists())
        self.assertEqual(self.runtime.read_text(encoding='utf-8'), 'PERSONA_A')

    def test_T2_runtime_wins_over_repo(self):
        self.runtime.write_text('PERSONA_USER_EDIT', encoding='utf-8')
        self.repo.write_text('PERSONA_REPO_NEW', encoding='utf-8')
        self.assertEqual(persona_store.read_persona(), 'PERSONA_USER_EDIT')

    def test_T3_existing_runtime_not_overwritten_by_bootstrap(self):
        self.runtime.write_text('PERSONA_USER_EDIT', encoding='utf-8')
        before = hashlib.sha256(self.runtime.read_bytes()).hexdigest()
        self.repo.write_text('PERSONA_REPO_CHANGED', encoding='utf-8')
        persona_store.ensure_runtime_persona()
        after = hashlib.sha256(self.runtime.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(persona_store.read_persona(), 'PERSONA_USER_EDIT')

    def test_T4_atomic_write_leaves_no_tmp_and_skips_repo(self):
        self.repo.write_text('PERSONA_REPO', encoding='utf-8')
        persona_store.write_persona('PERSONA_B')
        self.assertEqual(self.runtime.read_text(encoding='utf-8'), 'PERSONA_B')
        leftovers = list(self.runtime.parent.glob('.persona.*.tmp'))
        self.assertEqual(leftovers, [])
        self.assertEqual(self.repo.read_text(encoding='utf-8'), 'PERSONA_REPO')

    def test_T7_empty_write_rejected(self):
        self.runtime.write_text('KEEP_ME', encoding='utf-8')
        with self.assertRaises(persona_store.PersonaStoreError):
            persona_store.write_persona('   ')
        self.assertEqual(self.runtime.read_text(encoding='utf-8'), 'KEEP_ME')

    def test_T8_empty_runtime_fail_closed_no_repo_fallback(self):
        self.runtime.write_text('   \n', encoding='utf-8')
        self.repo.write_text('PERSONA_REPO_SHOULD_NOT_LEAK', encoding='utf-8')
        with self.assertRaises(persona_store.PersonaStoreError):
            persona_store.read_persona()
        self.assertEqual(
            self.runtime.read_text(encoding='utf-8'), '   \n',
        )

    def test_T9_system_builder_uses_runtime(self):
        self.runtime.write_text('RUNTIME_FOR_BUILDER', encoding='utf-8')
        self.repo.write_text('REPO_SHOULD_NOT_APPEAR', encoding='utf-8')
        with mock.patch.object(system_builder, 'build_stable_note', return_value='NOTE'):
            parts = system_builder.build_cc_static_parts()
            daily = system_builder.build_cc_daily_static_parts()
        self.assertEqual(parts['persona'], 'RUNTIME_FOR_BUILDER')
        self.assertIn('RUNTIME_FOR_BUILDER', parts['full_system'])
        self.assertNotIn('REPO_SHOULD_NOT_APPEAR', parts['full_system'])
        self.assertEqual(daily['persona'], 'RUNTIME_FOR_BUILDER')
        self.assertIn('RUNTIME_FOR_BUILDER', daily['full_system'])

    def test_M15_git_checkout_isolation(self):
        """Repo seed can change via checkout simulation; runtime must stay."""
        seed_dir = Path(self.tmp.name) / 'gitseed'
        seed_dir.mkdir()
        seed = seed_dir / 'persona.md'
        seed.write_text('A', encoding='utf-8')
        self.repo.write_text('A', encoding='utf-8')
        persona_store.ensure_runtime_persona()
        persona_store.write_persona('USER_EDIT_B')
        # Simulate git checkout replacing the tracked seed with C.
        self.repo.write_text('C', encoding='utf-8')
        seed.write_text('C', encoding='utf-8')
        self.assertEqual(persona_store.read_persona(), 'USER_EDIT_B')
        self.assertEqual(self.repo.read_text(encoding='utf-8'), 'C')


class PersonaApiTests(unittest.TestCase):
    """Exercise the same GET/POST contract as app.py /api/persona handlers."""

    def setUp(self) -> None:
        from flask import Flask, jsonify, request
        import subprocess

        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.runtime = base / 'persona.md'
        self.repo = base / 'seed.md'
        self.repo.write_text('REPO_SEED', encoding='utf-8')
        self.patches = [
            mock.patch.object(persona_store, 'RUNTIME_PERSONA_PATH', str(self.runtime)),
            mock.patch.object(
                persona_store, 'REPO_PERSONA_FALLBACK_PATH', str(self.repo),
            ),
        ]
        for p in self.patches:
            p.start()

        app = Flask(__name__)

        @app.route('/api/persona', methods=['GET'])
        def get_persona():
            try:
                text = persona_store.read_persona()
                return jsonify({"ok": True, "content": text})
            except persona_store.PersonaStoreError as e:
                return jsonify({"ok": False, "error": str(e)}), 500

        @app.route('/api/persona', methods=['POST'])
        def save_persona():
            data = request.get_json(silent=True) or {}
            content = data.get('content', '')
            if not str(content).strip():
                return jsonify({
                    "ok": False,
                    "error": "persona content must be non-empty",
                }), 400
            try:
                persona_store.write_persona(content)
                subprocess.Popen(['systemctl', 'restart', 'frontend-gw'])
                return jsonify({"ok": True})
            except persona_store.PersonaStoreError as e:
                return jsonify({"ok": False, "error": str(e)}), 400

        self.subprocess = subprocess
        self.client = app.test_client()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_T5_get_returns_runtime(self):
        self.runtime.write_text('RUNTIME_ONLY', encoding='utf-8')
        self.repo.write_text('REPO_SEED', encoding='utf-8')
        resp = self.client.get('/api/persona')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['content'], 'RUNTIME_ONLY')

    def test_T6_post_writes_runtime_not_repo(self):
        self.runtime.write_text('OLD', encoding='utf-8')
        with mock.patch.object(self.subprocess, 'Popen') as popen:
            resp = self.client.post(
                '/api/persona',
                json={'content': 'NEW_RUNTIME_BODY'},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['ok'])
        self.assertEqual(self.runtime.read_text(encoding='utf-8'), 'NEW_RUNTIME_BODY')
        self.assertEqual(self.repo.read_text(encoding='utf-8'), 'REPO_SEED')
        popen.assert_called_once()
        self.assertEqual(popen.call_args[0][0], ['systemctl', 'restart', 'frontend-gw'])

    def test_T7_post_empty_rejected(self):
        self.runtime.write_text('KEEP', encoding='utf-8')
        with mock.patch.object(self.subprocess, 'Popen') as popen:
            resp = self.client.post('/api/persona', json={'content': '  '})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.runtime.read_text(encoding='utf-8'), 'KEEP')
        popen.assert_not_called()

    def test_app_py_no_longer_writes_tracked_persona(self):
        src = (ROOT / 'app.py').read_text(encoding='utf-8')
        self.assertNotIn(
            "open('/opt/frontend/prompts/persona.md', 'w')",
            src,
        )
        self.assertIn('from chat.persona_store import', src)
        self.assertIn("write_persona as _write_runtime_persona", src)


class PersonaBackupContractTests(unittest.TestCase):
    def test_T10_backup_script_includes_runtime_persona(self):
        script = (ROOT / 'tools' / 'backup.sh').read_text(encoding='utf-8')
        self.assertIn('/var/lib/hayagarden/persona.md', script)
        self.assertIn('runtime/persona.md', script)
        self.assertIn('mkdir -p "$TMP/runtime"', script)


if __name__ == '__main__':
    unittest.main()
