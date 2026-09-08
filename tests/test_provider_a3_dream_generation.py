import ast
import contextlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__('sys').path:
    __import__('sys').path.insert(0, str(ROOT))


def _cc_reader():
    spec = importlib.util.spec_from_file_location('a3_cc_auth', ROOT / 'chat' / 'cc_auth.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.read_cc_oauth_token


class DreamAuthoritySourceContractTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / 'tools' / 'dream_generator.py').read_text(encoding='utf-8')
        self.tree = ast.parse(self.source)

    def test_captures_authority_once_in_task_entry(self):
        fn = next(n for n in self.tree.body if isinstance(n, ast.FunctionDef) and n.name == 'generate_dream')
        captures = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'capture_generation_authority'
        ]
        self.assertEqual(len(captures), 1)

    def test_background_adapter_owns_model_call_and_request_contract(self):
        self.assertIn("prompt_text='[做梦]'", self.source)
        self.assertIn('max_tokens_hint=4096', self.source)
        self.assertIn('timeout_sec=90', self.source)
        self.assertIn("task_kind='dream'", self.source)
        self.assertIn('generate_background(', self.source)
        self.assertNotIn('urlopen(', self.source)
        self.assertNotIn("resolve_provider('background')", self.source)
        self.assertNotIn("select_wake_provider('dream')", self.source)

    def test_parser_gates_content_before_sanitization(self):
        helper = next(n for n in self.tree.body if isinstance(n, ast.FunctionDef) and n.name == '_generate_dream_model')
        text = ast.get_source_segment(self.source, helper)
        self.assertIn('parse_response(result.text or \'\')', text)
        self.assertIn("if action != 'message' or not content.strip()", text)
        self.assertIn('return content, executor, True', text)

    def test_metadata_excludes_primer_and_raw_response(self):
        fn = next(n for n in self.tree.body if isinstance(n, ast.FunctionDef) and n.name == 'generate_dream')
        metadata = next(
            n.value for n in ast.walk(fn)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == 'metadata' for t in n.targets)
        )
        keys = {k.value for k in metadata.keys if isinstance(k, ast.Constant)}
        self.assertTrue({
            'generation_provider', 'generation_model_identity', 'generation_executor',
            'generation_attempts', 'model_generation_succeeded',
        }.issubset(keys))
        self.assertNotIn('primer', keys)
        self.assertNotIn('raw_response', keys)


class DreamLegacyWakeTests(unittest.TestCase):
    def test_dream_is_not_a_background_wake_mode(self):
        source = (ROOT / 'wake' / 'runners.py').read_text(encoding='utf-8')
        self.assertIn("BACKGROUND_WAKE_MODES = frozenset(('summarize',))", source)
        self.assertIn("if mode == 'dream':", source)
        self.assertIn('surface-owned Background Generation Adapter', source)


class CcTokenReaderTests(unittest.TestCase):
    def test_reads_deployment_compatible_env_without_environment_leak(self):
        read_cc_oauth_token = _cc_reader()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text('OTHER=x\nCLAUDE_CODE_OAUTH_TOKEN="token-from-file"\n', encoding='utf-8')
            with mock.patch.dict(os.environ, {'CLAUDE_CODE_OAUTH_TOKEN': ''}, clear=False):
                self.assertEqual(read_cc_oauth_token(path), 'token-from-file')

    def test_ambient_environment_does_not_override_deployment_file(self):
        read_cc_oauth_token = _cc_reader()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text('CLAUDE_CODE_OAUTH_TOKEN=token-from-file\n', encoding='utf-8')
            with mock.patch.dict(os.environ, {'CLAUDE_CODE_OAUTH_TOKEN': 'token-from-env'}, clear=False):
                self.assertEqual(read_cc_oauth_token(path), 'token-from-file')


class DreamBehaviorTests(unittest.TestCase):
    """Execute the Dream seam with fake providers and temporary SQLite only."""

    def _load_generator(self):
        spec = importlib.util.spec_from_file_location(
            'a3_dream_generator_behavior', ROOT / 'tools' / 'dream_generator.py',
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @contextlib.contextmanager
    def _fake_runtime(self, authority, responses=(), *, capture_error=None,
                      persona='PERSONA', context_error=None):
        calls = []
        response_iter = iter(responses)

        class FakeRequest:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeBackgroundError(RuntimeError):
            pass

        def generate_background(request, received_authority, **kwargs):
            calls.append((request, received_authority))
            next_response = next(response_iter)
            if isinstance(next_response, BaseException):
                raise next_response
            return types.SimpleNamespace(
                text=next_response,
                actual_executor='fake-background',
            )

        bg = types.ModuleType('chat.background_generation')
        bg.BackgroundGenerationRequest = FakeRequest
        bg.BackgroundGenerationError = FakeBackgroundError
        bg.generate_background = generate_background

        system_builder = types.ModuleType('chat.system_builder')
        system_builder.read_persona = (
            mock.Mock(side_effect=context_error) if context_error
            else mock.Mock(return_value=persona)
        )
        builder = types.ModuleType('wake.builder')
        builder.build_prompt_suffix = mock.Mock(return_value='SUFFIX')

        parser = types.ModuleType('wake.parser')
        def parse_response(text):
            raw = str(text or '')
            marker = 'CONTENT:'
            content = raw.split(marker, 1)[1].strip() if marker in raw else raw
            return 'thoughts', 'message', content
        parser.parse_response = parse_response

        auth = types.ModuleType('chat.cc_auth')
        auth.read_cc_oauth_token = mock.Mock(return_value='test-token')

        provider = types.ModuleType('chat.provider_router')
        provider.GenerationAuthoritySnapshot = lambda p, m: types.SimpleNamespace(
            provider=p, model_identity=m,
        )
        provider.capture_generation_authority = mock.Mock(
            side_effect=capture_error if capture_error else lambda: authority,
        )

        meta = types.ModuleType('tools.dream_meta')
        meta.sanitize_dream_content = lambda text: str(text or '').strip()
        latents = types.ModuleType('dream_latents')
        latents.MOTIF_EXTRACTION_WEIGHT = {'normal_dream': 1.0, 'fallback_dream': 0.2}
        latents.ensure_table = lambda conn: None
        latents.get_fallback_latents = lambda conn, limit=5: []
        latents.mark_used = lambda conn, ids: None
        latents.extract_dream_latents = lambda text, max_items=3: []
        latents.insert_latents = lambda *args, **kwargs: 0
        memory = types.ModuleType('memory_tool')
        memory.save_memory = mock.Mock()

        modules = {
            'chat.background_generation': bg,
            'chat.system_builder': system_builder,
            'wake.builder': builder,
            'wake.parser': parser,
            'chat.cc_auth': auth,
            'chat.provider_router': provider,
            'tools.dream_meta': meta,
            'dream_latents': latents,
            'memory_tool': memory,
        }
        with mock.patch.dict(sys.modules, modules):
            yield calls, provider.capture_generation_authority, memory.save_memory, builder

    def _prepare_generator(self, gen, db_path):
        def db():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            return conn

        gen._db = db
        gen._gather_materials = lambda conn: {
            'fragments': [], 'sensory': [], 'source_mix': {},
            'avg_v': 0.5, 'avg_a': 0.4,
        }
        gen._enrich_with_latents = lambda conn, materials: {
            'places': [], 'objects': [], 'actions': [], 'shards': [],
            'used_latent_ids': [],
        }
        gen._infer_tone = lambda avg_v, avg_a: 'drifting'
        gen._pick_traits = lambda: ['unresolved_ending']
        gen._build_primer = lambda *args, **kwargs: 'PRIMER'
        gen._now = lambda: __import__('datetime').datetime(2026, 9, 8, 12, 34)
        gen._log = lambda *args, **kwargs: None
        gen._fallback_dream = lambda tone, conn=None: 'LOCAL FALLBACK DREAM'

    def _new_db(self, tmp):
        path = str(Path(tmp) / 'dream.db')
        conn = sqlite3.connect(path)
        conn.execute(
            'CREATE TABLE dream_pool ('
            'id INTEGER PRIMARY KEY, content TEXT, valence REAL, arousal REAL, '
            'tone TEXT, created_at TEXT, metadata TEXT)'
        )
        conn.commit()
        conn.close()
        return path

    def _stored_row(self, path):
        conn = sqlite3.connect(path)
        row = conn.execute(
            'SELECT content, metadata FROM dream_pool ORDER BY id DESC LIMIT 1'
        ).fetchone()
        conn.close()
        return row[0], json.loads(row[1])

    def test_dream_newline_semantics_and_retry_context(self):
        gen = self._load_generator()
        authority = types.SimpleNamespace(provider='claude_code', model_identity='explicit:model-A')
        with self._fake_runtime(authority, ['THOUGHTS: hidden\nACTION: send\nCONTENT: MODEL CONTENT']) as (calls, _, _, builder):
            request = gen._dream_background_request('drifting', 'PRIMER', extra='HINT')
            content, executor, ok = gen._generate_dream_model(
                'drifting', 'PRIMER', authority, 1, extra='HINT',
            )
        self.assertEqual(request.system_text, 'PERSONA\n\nSUFFIX')
        self.assertEqual(calls[0][0].system_text, 'PERSONA\n\nSUFFIX')
        self.assertEqual(builder.build_prompt_suffix.call_args.args[1]['dream_primer'], 'PRIMER\n\nHINT')
        self.assertNotIn('\\n', calls[0][0].system_text)
        self.assertNotIn('\\n', gen._dream_background_request.__code__.co_consts)

    def test_behavior_1_cc_follows_primary(self):
        gen = self._load_generator()
        authority = types.SimpleNamespace(provider='claude_code', model_identity='explicit:model-A')
        with self._fake_runtime(authority, ['THOUGHTS: x\nACTION: message\nCONTENT: ok']) as (calls, *_):
            gen._generate_dream_model('drifting', 'PRIMER', authority, 1)
        self.assertEqual(calls[0][1], authority)

    def test_behavior_2_relay_follows_primary(self):
        gen = self._load_generator()
        authority = types.SimpleNamespace(provider='api_relay', model_identity='model-R')
        with self._fake_runtime(authority, ['THOUGHTS: x\nACTION: message\nCONTENT: ok']) as (calls, *_):
            gen._generate_dream_model('drifting', 'PRIMER', authority, 1)
        self.assertEqual(calls[0][1], authority)

    def test_behavior_3_background_provider_is_irrelevant_to_primary_snapshot(self):
        for background_provider in ('api_relay', 'claude_code'):
            gen = self._load_generator()
            authority = types.SimpleNamespace(provider='claude_code', model_identity='explicit:model-A')
            values = {'CHAT_PROVIDER': 'claude_code', 'BACKGROUND_PROVIDER': background_provider}
            with self.subTest(background_provider=background_provider), \
                    self._fake_runtime(authority, ['THOUGHTS: x\nACTION: message\nCONTENT: ok']) as (calls, *_), \
                    mock.patch.dict(os.environ, values, clear=False):
                gen._generate_dream_model('drifting', 'PRIMER', authority, 1)
            self.assertEqual(calls[0][1], authority)

    def test_behavior_4_severe_retry_freezes_snapshot(self):
        gen = self._load_generator()
        authority = types.SimpleNamespace(provider='api_relay', model_identity='model-R')
        severe = 'x' * 160 + '我终于明白了'
        valid = 'VALID DREAM ' + ('y' * 160)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._new_db(tmp)
            self._prepare_generator(gen, db_path)
            with self._fake_runtime(authority, [
                'THOUGHTS: x\nACTION: message\nCONTENT: ' + severe,
                'THOUGHTS: x\nACTION: message\nCONTENT: ' + valid,
            ]) as (calls, capture, save_memory, _):
                self.assertTrue(gen.generate_dream())
            self.assertEqual(capture.call_count, 1)
            self.assertEqual([item[1] for item in calls], [authority, authority])
            content, metadata = self._stored_row(db_path)
            self.assertEqual(content, valid)
            self.assertTrue(metadata['regenerated'])
            self.assertFalse(metadata['fallback'])
            self.assertEqual(metadata['generation_attempts'], 2)
            self.assertEqual(save_memory.call_args.args[0], valid)

    def test_behavior_5_provider_failure_uses_local_fallback_without_cross_provider(self):
        for provider in ('claude_code', 'api_relay'):
            gen = self._load_generator()
            authority = types.SimpleNamespace(provider=provider, model_identity='model-X')
            with tempfile.TemporaryDirectory() as tmp:
                db_path = self._new_db(tmp)
                self._prepare_generator(gen, db_path)
                with self._fake_runtime(authority, [RuntimeError('selected provider down')]) as (calls, *_):
                    self.assertTrue(gen.generate_dream())
                content, metadata = self._stored_row(db_path)
                self.assertEqual(content, 'LOCAL FALLBACK DREAM')
                self.assertTrue(metadata['fallback'])
                self.assertFalse(metadata['model_generation_succeeded'])
                self.assertEqual(len(calls), 1)

    def test_behavior_6_authority_capture_failure_skips_model_and_persists_fallback(self):
        gen = self._load_generator()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._new_db(tmp)
            self._prepare_generator(gen, db_path)
            error = ValueError('invalid provider config')
            with self._fake_runtime(None, [], capture_error=error) as (calls, capture, *_):
                self.assertTrue(gen.generate_dream())
            content, metadata = self._stored_row(db_path)
            self.assertEqual(content, 'LOCAL FALLBACK DREAM')
            self.assertEqual(capture.call_count, 1)
            self.assertEqual(calls, [])
            self.assertEqual(metadata['generation_provider'], 'unavailable')
            self.assertEqual(metadata['generation_model_identity'], 'unknown')
            self.assertEqual(metadata['generation_attempts'], 0)
            self.assertFalse(metadata['model_generation_succeeded'])

    def test_behavior_7_context_build_failure_skips_adapter_and_persists_fallback(self):
        gen = self._load_generator()
        authority = types.SimpleNamespace(provider='claude_code', model_identity='explicit:model-A')
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._new_db(tmp)
            self._prepare_generator(gen, db_path)
            with self._fake_runtime(
                authority, [], context_error=RuntimeError('persona unavailable'),
            ) as (calls, *_):
                self.assertTrue(gen.generate_dream())
            content, metadata = self._stored_row(db_path)
            self.assertEqual(content, 'LOCAL FALLBACK DREAM')
            self.assertTrue(metadata['fallback'])
            self.assertEqual(calls, [])

    def test_behavior_8_severe_regen_persists_content_only(self):
        gen = self._load_generator()
        authority = types.SimpleNamespace(provider='api_relay', model_identity='model-R')
        severe = 'z' * 160 + '我终于明白了'
        valid = 'SECOND MODEL DREAM ' + ('q' * 160)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._new_db(tmp)
            self._prepare_generator(gen, db_path)
            with self._fake_runtime(authority, [
                'THOUGHTS: hidden\nACTION: send\nCONTENT: ' + severe,
                'THOUGHTS: hidden\nACTION: send\nCONTENT: ' + valid,
            ]) as (calls, capture, save_memory, _):
                self.assertTrue(gen.generate_dream())
            content, metadata = self._stored_row(db_path)
            self.assertEqual(capture.call_count, 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(content, valid)
            self.assertEqual(save_memory.call_args.args[0], valid)
            self.assertTrue(metadata['regenerated'])

    def test_behavior_9_parser_passes_content_only_to_persistence(self):
        gen = self._load_generator()
        authority = types.SimpleNamespace(provider='claude_code', model_identity='explicit:model-A')
        raw = 'THOUGHTS: do not persist\nACTION: send\nCONTENT: only this text'
        with self._fake_runtime(authority, [raw]):
            content, _executor, ok = gen._generate_dream_model(
                'drifting', 'PRIMER', authority, 1,
            )
        self.assertTrue(ok)
        self.assertEqual(content, 'only this text')
        self.assertNotIn('THOUGHTS:', content)
        self.assertNotIn('ACTION:', content)


if __name__ == '__main__':
    unittest.main()

