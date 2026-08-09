"""Persona runtime authority — store, API, system_builder, backup, Git isolation."""
from __future__ import annotations

import hashlib
import os
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

    def test_T14_bootstrap_race_create_once(self):
        """A reads seed; B creates USER_EDIT; A must not overwrite with seed."""
        self.repo.write_bytes(b'SEED')
        self.assertFalse(self.runtime.exists())
        real_read_bytes = Path.read_bytes
        raced = {'n': 0}

        def read_bytes_then_race(path_self):
            data = real_read_bytes(path_self)
            if (
                path_self.resolve() == self.repo.resolve()
                and raced['n'] == 0
            ):
                raced['n'] += 1
                persona_store.write_persona('USER_EDIT')
            return data

        with mock.patch.object(Path, 'read_bytes', read_bytes_then_race):
            persona_store.ensure_runtime_persona()

        self.assertEqual(self.runtime.read_text(encoding='utf-8'), 'USER_EDIT')
        self.assertNotEqual(self.runtime.read_bytes(), b'SEED')

    def test_T15_bootstrap_byte_exact_crlf(self):
        seed = b'LINE1\r\nLINE2\r\n'
        self.repo.write_bytes(seed)
        persona_store.ensure_runtime_persona()
        runtime_bytes = self.runtime.read_bytes()
        self.assertEqual(runtime_bytes, seed)
        self.assertEqual(
            hashlib.sha256(seed).hexdigest(),
            hashlib.sha256(runtime_bytes).hexdigest(),
        )

    def test_T19_bootstrap_failure_after_concurrent_save_keeps_runtime(self):
        """Concurrent USER_EDIT must survive bootstrap post-publish failure."""
        self.repo.write_bytes(b'SEED')
        real_link = os.link

        def link_then_concurrent_save_then_fail(src, dst):
            real_link(src, dst)
            persona_store.write_persona('USER_EDIT')
            raise OSError('simulated bootstrap post-write failure')

        with mock.patch.object(os, 'link', side_effect=link_then_concurrent_save_then_fail):
            with self.assertRaises(OSError):
                persona_store.ensure_runtime_persona()

        self.assertTrue(self.runtime.exists())
        self.assertEqual(self.runtime.read_text(encoding='utf-8'), 'USER_EDIT')

    def test_T20_incomplete_bootstrap_does_not_expose_runtime(self):
        """Runtime path stays absent until full tmp write + fsync + link."""
        seed = b'FULL_SEED_BYTES'
        self.repo.write_bytes(seed)
        real_fsync = os.fsync
        real_link = os.link
        observed = {'runtime_during_tmp_fsync': None, 'linked': False}

        def fsync_assert_absent(fd):
            # Capture only the first fsync (tmp file) before publish.
            if observed['runtime_during_tmp_fsync'] is None:
                observed['runtime_during_tmp_fsync'] = self.runtime.exists()
            return real_fsync(fd)

        def link_assert_ready(src, dst):
            self.assertFalse(self.runtime.exists())
            self.assertEqual(Path(src).read_bytes(), seed)
            observed['linked'] = True
            return real_link(src, dst)

        with mock.patch.object(os, 'fsync', side_effect=fsync_assert_absent), \
             mock.patch.object(os, 'link', side_effect=link_assert_ready):
            persona_store.ensure_runtime_persona()

        self.assertIs(observed['runtime_during_tmp_fsync'], False)
        self.assertTrue(observed['linked'])
        self.assertEqual(self.runtime.read_bytes(), seed)

    def test_bootstrap_never_unlinks_runtime(self):
        src = Path(persona_store.__file__).read_text(encoding='utf-8')
        # Final authority must never be deleted by bootstrap cleanup.
        self.assertNotIn('runtime.unlink', src)
        self.assertNotIn('runtime_path.unlink', src)


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


class PersonaActiveReaderFailClosedTests(unittest.TestCase):
    def test_T16_chat_reply_persona_failure_no_provider(self):
        """Mirror app.py chat_reply fail-closed contract without importing app."""
        from flask import Flask, jsonify

        app = Flask(__name__)
        calls = {'n': 0}

        @app.route('/api/chat/reply', methods=['POST'])
        def chat_reply():
            try:
                persona_store.read_persona()
            except persona_store.PersonaStoreError as e:
                return jsonify({"ok": False, "error": str(e)}), 500
            calls['n'] += 1
            return jsonify({"ok": True})

        src = (ROOT / 'app.py').read_text(encoding='utf-8')
        self.assertIn('except PersonaStoreError as e:', src)
        self.assertNotIn(
            "persona = '你是费奥多尔，一个渊博冷静却深情的人。'",
            src,
        )

        with mock.patch.object(
            persona_store,
            'read_persona',
            side_effect=persona_store.PersonaStoreError('runtime empty'),
        ):
            resp = app.test_client().post('/api/chat/reply')
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(resp.get_json().get('ok'))
        self.assertEqual(calls['n'], 0)

    def test_T17_auto_diary_fail_closed(self):
        import auto_diary

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = Path(tmp.name) / 'persona.md'
        runtime.write_text('   \n', encoding='utf-8')
        repo = Path(tmp.name) / 'seed.md'
        repo.write_text('SEED', encoding='utf-8')

        with mock.patch.object(persona_store, 'RUNTIME_PERSONA_PATH', str(runtime)), \
             mock.patch.object(persona_store, 'REPO_PERSONA_FALLBACK_PATH', str(repo)), \
             mock.patch.object(auto_diary, 'today_diary_exists', return_value=False), \
             mock.patch.object(
                 auto_diary, 'fetch_today_messages',
                 return_value=[{'author': 'hayana', 'content': 'hi'}],
             ), \
             mock.patch.object(auto_diary, 'call_api') as call_api, \
             mock.patch.object(auto_diary, 'load_key', return_value='k'):
            with self.assertRaises(persona_store.PersonaStoreError):
                auto_diary.generate()
            call_api.assert_not_called()

    def test_T18_thought_gen_fail_closed(self):
        import importlib.util

        path = ROOT / 'tools' / 'thought_gen.py'
        spec = importlib.util.spec_from_file_location('thought_gen_under_test', path)
        assert spec and spec.loader
        thought_gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(thought_gen)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = Path(tmp.name) / 'persona.md'
        runtime.write_text('   \n', encoding='utf-8')
        repo = Path(tmp.name) / 'seed.md'
        repo.write_text('SEED', encoding='utf-8')

        with mock.patch.object(persona_store, 'RUNTIME_PERSONA_PATH', str(runtime)), \
             mock.patch.object(persona_store, 'REPO_PERSONA_FALLBACK_PATH', str(repo)), \
             mock.patch.object(thought_gen, 'today_exists', return_value=False), \
             mock.patch.object(thought_gen, 'get_unsaid_thoughts', return_value=['x']), \
             mock.patch.object(thought_gen, 'get_emotional_buckets', return_value=[]), \
             mock.patch.object(thought_gen, 'get_last_messages', return_value=[]), \
             mock.patch.object(thought_gen.subprocess, 'run') as run:
            with self.assertRaises(persona_store.PersonaStoreError):
                thought_gen.generate()
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
