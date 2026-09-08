import ast
import importlib.util
import os
import tempfile
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

    def test_environment_takes_precedence(self):
        read_cc_oauth_token = _cc_reader()
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_OAUTH_TOKEN': 'token-from-env'}, clear=False):
            self.assertEqual(read_cc_oauth_token('/definitely/missing'), 'token-from-env')


if __name__ == '__main__':
    unittest.main()

