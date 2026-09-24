"""P0 cold-storm fix: cold bootstrap boundedness (Fence A/B) + no-benefit
respawn loop breaker (Fence C).

Covers T1-T11 from the P0 history-rewrite cold-storm fix spec:
  T1  legacy lean OFF still gets token-budget history for cold
  T2  hot path unaffected by the cold_safe force
  T3  whole prompt <= cold_target on success
  T4  history auto-shrinks with at most one deterministic rebuild
  T5  latest user never lost even after heavy trimming
  T6  regen cold fallback overlay stays correct under token-budget history
  T7  edit cold fallback overlay stays correct under token-budget history
  T8  unshrinkable mandatory prompt fails closed before any stdin write
  T9  identical hard_context respawn after history_rewrite cold is refused
  T10 a first-ever hard_context (no baseline) is still allowed
  T11 estimator-miss fail-safe: an equal-size hard_context retry is refused
  T18 genuine hot growth hard_context still allows bounded shrink
  T20 cold overflow leaves history boundary unchanged and records overflow=True
  T23 zero remaining history budget trims to minimum via real build_messages
  T24 cold preflight serializes each message plan at most once
"""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-cold-bootstrap-budget-config.db'),
)

_OPT_FRONTEND = Path('/opt/frontend')
try:
    _OPT_FRONTEND.mkdir(parents=True, exist_ok=True)
    for _name in ('memories.db', 'commands.db'):
        sqlite3.connect(str(_OPT_FRONTEND / _name)).close()
except OSError:
    pass

from cc_resident import empty_usage
from chat.cold_bootstrap_budget import (
    ColdBootstrapOverflow,
    NoBenefitRespawnError,
    effective_history_budget,
)
from chat import rewrite_staging as rw


def _import_gateway():
    expected = Path(ROOT, 'gateway.py').resolve()
    loaded = sys.modules.get('gateway')
    if loaded is not None:
        loaded_path = Path(getattr(loaded, '__file__', '') or '').resolve()
        if loaded_path == expected:
            return loaded
        sys.modules.pop('gateway', None)
    # Some production helper modules prepend /opt/frontend to sys.path.
    # Reassert this worktree as the import authority for the test gateway.
    if ROOT in sys.path:
        sys.path.remove(ROOT)
    sys.path.insert(0, ROOT)
    stubbed = []
    if 'tools.workspace_registry' not in sys.modules:
        reg = mock.MagicMock()
        reg.TOOLS_NOTE = ''
        reg.build_resident_tool_defs.return_value = []
        reg.load_registry.return_value = []
        sys.modules['tools.workspace_registry'] = reg
        stubbed.append('tools.workspace_registry')
    if 'tools.workspace_agent' not in sys.modules:
        wa = mock.MagicMock()
        wa.get_workspace_tool_defs.return_value = []
        sys.modules['tools.workspace_agent'] = wa
        stubbed.append('tools.workspace_agent')
    import gateway
    if Path(gateway.__file__).resolve() != expected:
        raise AssertionError('test imported gateway outside the isolated worktree')
    for name in stubbed:
        sys.modules.pop(name, None)
    return gateway


def _cfg_patches(int_overrides=None, bool_overrides=None):
    int_overrides = dict(int_overrides or {})
    bool_overrides = dict(bool_overrides or {})
    return [
        mock.patch('config_store.get_int', side_effect=lambda k, d=0: int_overrides.get(k, d)),
        mock.patch('config_store.get_bool', side_effect=lambda k, d=False: bool_overrides.get(k, d)),
    ]


# ---------------------------------------------------------------------------
# T1/T2/T6/T7 — build_messages(cold_safe=...) integration against a real DB
# ---------------------------------------------------------------------------

_CHAT_SCHEMA = """
CREATE TABLE chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author TEXT,
    content TEXT,
    thinking TEXT DEFAULT '',
    tool_calls TEXT DEFAULT '',
    branches TEXT DEFAULT '',
    branch_idx INTEGER DEFAULT 0,
    image_url TEXT DEFAULT '',
    file_url TEXT DEFAULT '',
    file_name TEXT DEFAULT '',
    cache_info TEXT DEFAULT '',
    choices TEXT DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'chat',
    created_at TEXT
);
"""


def _make_chat_db(tmp_dir):
    db_path = str(Path(tmp_dir) / 'chat.db')
    conn = sqlite3.connect(db_path)
    conn.executescript(_CHAT_SCHEMA)
    rw.ensure_schema(conn)
    conn.commit()
    conn.close()
    return db_path


def _insert_rows(db_path, rows):
    conn = sqlite3.connect(db_path)
    base = dt.datetime.now() + dt.timedelta(hours=8)
    ids = []
    for i, (author, content) in enumerate(rows):
        ts = (base + dt.timedelta(seconds=i)).strftime('%Y-%m-%d %H:%M:%S')
        cur = conn.execute(
            'INSERT INTO chat_messages (author, content, created_at) VALUES (?, ?, ?)',
            (author, content, ts),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    conn.close()
    return ids


class BuildMessagesColdSafeTests(unittest.TestCase):
    def setUp(self):
        self.gateway = _import_gateway()
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = _make_chat_db(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _get_db(self):
        def get_db():
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn
        return get_db

    def test_t1_legacy_lean_off_cold_uses_token_budget_history(self):
        """CONTEXT_LEAN_HISTORY_ENABLED=0 must not degrade cold safety."""
        rows = []
        for i in range(35):
            rows.append(('hayana', 'U%d ' % i + 'x' * 200))
            rows.append(('assistant', 'A%d ' % i + 'y' * 200))
        _insert_rows(self.db_path, rows)  # 70 rows total; legacy window would keep all 70

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.gateway, 'get_db', self._get_db()))
            for p in _cfg_patches(
                int_overrides={'HISTORY_TOKEN_BUDGET': 300},
                bool_overrides={'CONTEXT_LEAN_HISTORY_ENABLED': False},
            ):
                stack.enter_context(p)
            hist_stats = {}
            msgs = self.gateway.build_messages(
                for_cc=True, cold_safe=True, history_stats_out=hist_stats,
            )
        self.assertLess(len(msgs), 70)
        self.assertTrue(hist_stats.get('conversation_content_trimmed'))

    def test_t2_hot_path_unaffected_by_cold_safe_force(self):
        """cold_safe=False (hot turn) must keep the exact legacy behavior."""
        rows = []
        for i in range(35):
            rows.append(('hayana', 'U%d' % i))
            rows.append(('assistant', 'A%d' % i))
        _insert_rows(self.db_path, rows)

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.gateway, 'get_db', self._get_db()))
            for p in _cfg_patches(
                int_overrides={'HISTORY_TOKEN_BUDGET': 300},
                bool_overrides={'CONTEXT_LEAN_HISTORY_ENABLED': False},
            ):
                stack.enter_context(p)
            hist_stats = {}
            msgs = self.gateway.build_messages(
                for_cc=True, cold_safe=False, history_stats_out=hist_stats,
            )
        # Legacy count-only window: 70 <= legacy_block_limit(70) == 70 -> all kept.
        self.assertEqual(len(msgs), 70)
        self.assertFalse(hist_stats.get('conversation_content_trimmed'))

    def test_t6_regen_overlay_correct_under_token_budget(self):
        """regen cold fallback: history up to U1 (no A1), U1 appears once."""
        ids = _insert_rows(self.db_path, [
            ('hayana', 'U1'),
            ('assistant', 'A1'),
        ])
        a1_id = ids[1]
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        prep = rw.prepare_regen(conn, source_assistant_id=a1_id)
        conn.commit()
        conn.close()

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.gateway, 'get_db', self._get_db()))
            for p in _cfg_patches(
                int_overrides={'HISTORY_TOKEN_BUDGET': 5000},
                bool_overrides={'CONTEXT_LEAN_HISTORY_ENABLED': False},
            ):
                stack.enter_context(p)
            msgs = self.gateway.build_messages(
                for_cc=True, cold_safe=True, rewrite_id=prep['rewrite_id'],
            )
        contents = [m['content'] for m in msgs]
        self.assertEqual(contents.count('U1'), 1)
        self.assertNotIn('A1', contents)

    def test_t7_edit_overlay_correct_under_token_budget(self):
        """edit cold fallback: U1 replaced by U1', old A1 / descendants gone."""
        ids = _insert_rows(self.db_path, [
            ('hayana', 'U1'),
            ('assistant', 'A1'),
        ])
        u1_id = ids[0]
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        prep = rw.prepare_edit(conn, source_message_id=u1_id, edited_content="U1'")
        conn.commit()
        conn.close()

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.gateway, 'get_db', self._get_db()))
            for p in _cfg_patches(
                int_overrides={'HISTORY_TOKEN_BUDGET': 5000},
                bool_overrides={'CONTEXT_LEAN_HISTORY_ENABLED': False},
            ):
                stack.enter_context(p)
            msgs = self.gateway.build_messages(
                for_cc=True, cold_safe=True, rewrite_id=prep['rewrite_id'],
            )
        contents = [m['content'] for m in msgs]
        self.assertEqual(contents, ["U1'"])
        self.assertNotIn('U1', contents)
        self.assertNotIn('A1', contents)


# ---------------------------------------------------------------------------
# T3/T4/T5/T8/T9/T10/T11 — the preflight fence itself, isolated from the real
# history DB so the rebuild / overflow / no-benefit paths are deterministic.
# ---------------------------------------------------------------------------

class _FenceFakeResident:
    def __init__(
        self,
        *,
        last_cold_bootstrap_estimate=0,
        last_cold_bootstrap_generation=0,
        generation=1,
        hard_context_pre_spawn_turns=None,
    ):
        self.last_state_snapshot = {}
        self.last_state_send_snapshot = {}
        self.last_group_message_id = 0
        self.group_cursor_initialized = True
        self.generation = generation
        self.tool_surface_snapshot = {}
        self.committed_file_hashes = set()
        self.resident_pid = 4242
        self.session_id = 'sess-1'
        self.mcp_config_path = '/tmp/cc-tools.json'
        self.allowed_tools = ''
        self.keepwarm_lease_expires_at = None
        self.last_cold_bootstrap_estimate = last_cold_bootstrap_estimate
        self.last_cold_bootstrap_generation = last_cold_bootstrap_generation
        self.hard_context_pre_spawn_turns = hard_context_pre_spawn_turns
        self.send_turn_calls = []

    def peek_idle_seconds(self):
        return None

    def note_cold_bootstrap_estimate(self, estimate):
        self.last_cold_bootstrap_estimate = int(estimate or 0)
        self.last_cold_bootstrap_generation = int(self.generation or 0)

    def clear_hard_context_pre_spawn_turns(self):
        self.hard_context_pre_spawn_turns = None

    def send_turn(self, content, commit_meta=None):
        self.send_turn_calls.append(content)
        yield ('done', ('ok', '', empty_usage(), {}))


def _history_messages(n_pairs, row_chars, last_user_text):
    msgs = []
    for i in range(n_pairs):
        msgs.append({'role': 'user', 'content': 'U%d ' % i + 'x' * row_chars})
        msgs.append({'role': 'assistant', 'content': 'A%d ' % i + 'y' * row_chars})
    msgs.append({'role': 'user', 'content': last_user_text})
    return msgs


class ColdPreflightFenceTests(unittest.TestCase):
    def setUp(self):
        self.gateway = _import_gateway()

    def _fence_patches(self, resident, *, int_overrides, bool_overrides=None):
        bool_overrides = bool_overrides or {}
        return [
            mock.patch.object(self.gateway, '_CC_RESIDENT', resident),
            mock.patch.object(self.gateway, 'CC_TOKEN', 'tok'),
            mock.patch.object(self.gateway, 'CC_CWD', tempfile.mkdtemp()),
            mock.patch.object(self.gateway, '_recall_memories', return_value=('', [])),
            mock.patch.object(self.gateway, '_fetch_group_chat_rows', return_value=([], None)),
            mock.patch('chat.system_builder.build_cc_state', return_value={}),
            mock.patch('chat.system_builder.build_cc_one_shot', return_value={
                'wake_nonmessage_background': '', 'wake_message_background': '',
                'wake_reply_bridge': '', 'wake_ids': [], 'wake_items': [],
                'task_feedback': '', 'dream_flash': '',
                'feedback_ids': [], 'dream_id': None,
            }),
            mock.patch('chat.system_builder.build_cc_cold_once', return_value={}),
            mock.patch('chat.system_builder.build_cc_static_parts', return_value={
                'persona': 'STATIC', 'stable_note': '', 'save_instr': '', 'full_system': 'STATIC',
            }),
            *_cfg_patches(int_overrides=int_overrides, bool_overrides=bool_overrides),
        ]

    def _run(self, resident, messages, *, int_overrides, rebuild_messages_fn=None,
              pending_respawn_reason=None, history_stats=None):
        with contextlib.ExitStack() as stack:
            for p in self._fence_patches(resident, int_overrides=int_overrides):
                stack.enter_context(p)
            events = list(self.gateway._cc_resident_stream_gen(
                messages,
                user_turn=True,
                is_cold=True,
                history_stats=history_stats if history_stats is not None else {},
                rebuild_messages_fn=rebuild_messages_fn,
                pending_respawn_reason=pending_respawn_reason,
            ))
        return events

    def test_t3_whole_prompt_within_target_on_success(self):
        resident = _FenceFakeResident()
        messages = _history_messages(2, row_chars=10, last_user_text='hi')
        events = self._run(
            resident, messages,
            int_overrides={'CC_CONTEXT_HARD_LIMIT': 1000, 'CC_CONTEXT_SOFT_LIMIT': 700,
                            'CC_COLD_BOOTSTRAP_SAFETY_MARGIN': 100},
        )
        self.assertEqual(len(resident.send_turn_calls), 1)
        done = [p for e, p in events if e == 'done'][0]
        usage = done[2]
        breakdown = usage['context_breakdown']
        self.assertEqual(breakdown['cold_budget_mode'], 'token_budget')
        self.assertFalse(breakdown['cold_budget_overflow'])
        self.assertLessEqual(breakdown['cold_prompt_estimate'], breakdown['cold_prompt_target'])
        self.assertEqual(breakdown['cold_prompt_target'], 700)

    def test_t4_history_auto_shrinks_with_one_deterministic_rebuild(self):
        resident = _FenceFakeResident()
        big_messages = _history_messages(40, row_chars=100, last_user_text='hi')
        small_messages = _history_messages(2, row_chars=10, last_user_text='hi')
        rebuild_calls = []

        def _rebuild(new_budget):
            rebuild_calls.append(new_budget)
            return small_messages, {'conversation_content_trimmed': True}

        events = self._run(
            resident, big_messages,
            int_overrides={'CC_CONTEXT_HARD_LIMIT': 1000, 'CC_CONTEXT_SOFT_LIMIT': 700,
                            'CC_COLD_BOOTSTRAP_SAFETY_MARGIN': 100,
                            'HISTORY_TOKEN_BUDGET': 5000},
            rebuild_messages_fn=_rebuild,
        )
        self.assertEqual(len(rebuild_calls), 1)
        self.assertEqual(len(resident.send_turn_calls), 1)
        done = [p for e, p in events if e == 'done'][0]
        breakdown = done[2]['context_breakdown']
        self.assertLessEqual(breakdown['cold_prompt_estimate'], breakdown['cold_prompt_target'])
        self.assertTrue(breakdown['cold_history_trimmed'])

    def test_classic_cold_display_suffix_is_fixed_budget_overhead(self):
        big_messages = _history_messages(40, row_chars=100, last_user_text='hi')
        small_messages = _history_messages(2, row_chars=10, last_user_text='hi')
        suffix_marker = '正式回复不能为空。'
        suffix_start = '正式回复之前，先写一小段只用于界面展示的内心独白，并严格包在：'

        def run_case(mode, model_identity=None):
            from chat.display_thinking import resolve_effective_display_thinking_mode
            resident = _FenceFakeResident()
            resident._model_identity = model_identity
            effective_mode = resolve_effective_display_thinking_mode(mode, model_identity)
            rebuild = mock.Mock(return_value=(
                small_messages, {'conversation_content_trimmed': True},
            ))
            budget_calls = []
            estimate_calls = []
            token_calls = []

            def fake_text_tokens(value):
                token_calls.append(value)
                return 20 if str(value).strip().startswith(suffix_start) else 80

            def fake_effective_budget(*, default_history_budget, non_history_estimate, cold_target):
                budget_calls.append(non_history_estimate)
                if effective_mode in ('auto', 'authored'):
                    return 1 if non_history_estimate >= 50 else 999
                return 1

            def fake_whole_prompt(_system, _content):
                estimate_calls.append(_content)
                return 110 if len(estimate_calls) == 1 else (
                    95 if effective_mode in ('off', 'native') or (
                        budget_calls and budget_calls[-1] >= 50
                    ) else 105
                )

            with contextlib.ExitStack() as stack:
                for p in self._fence_patches(
                    resident,
                    int_overrides={'CC_CONTEXT_HARD_LIMIT': 1000,
                                   'CC_CONTEXT_SOFT_LIMIT': 700,
                                   'CC_COLD_BOOTSTRAP_SAFETY_MARGIN': 100,
                                   'HISTORY_TOKEN_BUDGET': 5000},
                ):
                    stack.enter_context(p)
                stack.enter_context(mock.patch(
                    'chat.cold_bootstrap_budget.cold_prompt_target',
                    return_value=100,
                ))
                stack.enter_context(mock.patch(
                    'chat.cold_bootstrap_budget.estimate_text_tokens',
                    side_effect=fake_text_tokens,
                ))
                stack.enter_context(mock.patch(
                    'chat.cold_bootstrap_budget.effective_history_budget',
                    side_effect=fake_effective_budget,
                ))
                stack.enter_context(mock.patch(
                    'chat.cold_bootstrap_budget.estimate_whole_prompt',
                    side_effect=fake_whole_prompt,
                ))
                messages_to_text_mock = stack.enter_context(mock.patch.object(
                    self.gateway, 'messages_to_text',
                    wraps=self.gateway.messages_to_text,
                ))
                events = list(self.gateway._cc_resident_stream_gen(
                    big_messages,
                    user_turn=True,
                    is_cold=True,
                    history_stats={},
                    rebuild_messages_fn=rebuild,
                    display_thinking_mode=mode,
                ))

            rebuild.assert_called_once()
            expected_non_history = 50 if effective_mode in ('auto', 'authored') else 30
            self.assertEqual(budget_calls, [expected_non_history])
            self.assertEqual(len(estimate_calls), 2)
            self.assertEqual(messages_to_text_mock.call_count, 2)
            self.assertEqual(len(resident.send_turn_calls), 1)
            sent = resident.send_turn_calls[0]
            expected_suffix_count = 1 if effective_mode in ('auto', 'authored') else 0
            self.assertEqual(sent.count(suffix_marker), expected_suffix_count)
            self.assertTrue(any(event == 'done' for event, _ in events))

        for mode in ('auto', 'authored', 'off', 'native'):
            with self.subTest(mode=mode):
                run_case(mode)
        with self.subTest(mode='auto', model='explicit:claude-opus-5-5'):
            run_case('auto', 'explicit:claude-opus-5-5')

    def test_t5_latest_user_survives_rebuild(self):
        resident = _FenceFakeResident()
        big_messages = _history_messages(40, row_chars=100, last_user_text='LATEST_USER_MESSAGE')
        # Deliberately-small rebuild result still preserves the exact latest user text.
        small_messages = [
            {'role': 'user', 'content': 'LATEST_USER_MESSAGE'},
        ]
        rebuild_calls = []

        def _rebuild(new_budget):
            rebuild_calls.append(new_budget)
            return small_messages, {'conversation_content_trimmed': True}

        self._run(
            resident, big_messages,
            int_overrides={'CC_CONTEXT_HARD_LIMIT': 1000, 'CC_CONTEXT_SOFT_LIMIT': 700,
                            'CC_COLD_BOOTSTRAP_SAFETY_MARGIN': 100,
                            'HISTORY_TOKEN_BUDGET': 5000},
            rebuild_messages_fn=_rebuild,
        )
        self.assertEqual(len(resident.send_turn_calls), 1)
        sent_content = resident.send_turn_calls[0]
        self.assertIn('LATEST_USER_MESSAGE', sent_content)

    def test_t8_unshrinkable_prompt_fails_closed_before_stdin(self):
        resident = _FenceFakeResident()
        messages = _history_messages(1, row_chars=5, last_user_text='hi')
        # Mandatory non-history component (system) alone already exceeds target:
        # 400 ascii chars ~= 100 tokens, target = min(80, 100-10) = 70.
        rebuild_calls = []

        def _rebuild(new_budget):
            rebuild_calls.append(new_budget)
            return messages, {'conversation_content_trimmed': True}

        with contextlib.ExitStack() as stack:
            for p in self._fence_patches(
                resident,
                int_overrides={'CC_CONTEXT_HARD_LIMIT': 100, 'CC_CONTEXT_SOFT_LIMIT': 80,
                                'CC_COLD_BOOTSTRAP_SAFETY_MARGIN': 10,
                                'HISTORY_TOKEN_BUDGET': 5000},
            ):
                stack.enter_context(p)
            # Override static parts *after* the generic fence patches so the
            # oversized system text wins.
            stack.enter_context(mock.patch('chat.system_builder.build_cc_static_parts', return_value={
                'persona': 'STATIC', 'stable_note': '', 'save_instr': '',
                'full_system': 'S' * 400,
            }))
            with self.assertRaises(ColdBootstrapOverflow):
                list(self.gateway._cc_resident_stream_gen(
                    messages, user_turn=True, is_cold=True,
                    rebuild_messages_fn=_rebuild,
                ))
        self.assertEqual(rebuild_calls, [1])
        self.assertEqual(resident.send_turn_calls, [])

    def test_t9_identical_hard_context_respawn_after_history_rewrite_refused(self):
        resident = _FenceFakeResident()
        messages = _history_messages(2, row_chars=10, last_user_text='hi')
        int_overrides = {
            'CC_CONTEXT_HARD_LIMIT': 1000, 'CC_CONTEXT_SOFT_LIMIT': 700,
            'CC_COLD_BOOTSTRAP_SAFETY_MARGIN': 100,
        }
        # First cold: triggered by history_rewrite -> always allowed, records baseline.
        self._run(
            resident, messages, int_overrides=int_overrides,
            pending_respawn_reason='history_rewrite',
        )
        self.assertEqual(len(resident.send_turn_calls), 1)
        baseline = resident.last_cold_bootstrap_estimate
        self.assertGreater(baseline, 0)

        # Simulate the hard_context spawn bumping generation and capturing storm
        # window metadata from the immediately preceding cold.
        resident.generation = 2
        resident.hard_context_pre_spawn_turns = 1

        # Nothing changed: an immediate hard_context respawn with an identical
        # (non-shrinking) cold bootstrap must be refused, not repeated forever.
        with self.assertRaises(NoBenefitRespawnError):
            self._run(
                resident, messages, int_overrides=int_overrides,
                pending_respawn_reason='hard_context',
            )
        self.assertEqual(len(resident.send_turn_calls), 1)  # unchanged: no 2nd send

    def test_t10_first_ever_hard_context_without_baseline_is_allowed(self):
        resident = _FenceFakeResident(last_cold_bootstrap_estimate=0)
        messages = _history_messages(2, row_chars=10, last_user_text='hi')
        events = self._run(
            resident, messages,
            int_overrides={'CC_CONTEXT_HARD_LIMIT': 1000, 'CC_CONTEXT_SOFT_LIMIT': 700,
                            'CC_COLD_BOOTSTRAP_SAFETY_MARGIN': 100},
            pending_respawn_reason='hard_context',
        )
        self.assertEqual(len(resident.send_turn_calls), 1)
        self.assertTrue(any(e == 'done' for e, _ in events))
        self.assertGreater(resident.last_cold_bootstrap_estimate, 0)

    def test_t11_estimator_miss_equal_size_hard_context_retry_refused(self):
        """Preflight estimate was within target and got sent; provider's real
        context still tripped hard_context. Refuse the identical retry."""
        resident = _FenceFakeResident()
        messages = _history_messages(2, row_chars=10, last_user_text='hi')
        int_overrides = {
            'CC_CONTEXT_HARD_LIMIT': 1000, 'CC_CONTEXT_SOFT_LIMIT': 700,
            'CC_COLD_BOOTSTRAP_SAFETY_MARGIN': 100,
        }
        self._run(resident, messages, int_overrides=int_overrides, pending_respawn_reason=None)
        self.assertEqual(len(resident.send_turn_calls), 1)

        resident.generation = 2
        resident.hard_context_pre_spawn_turns = 1

        with self.assertRaises(NoBenefitRespawnError):
            self._run(
                resident, messages, int_overrides=int_overrides,
                pending_respawn_reason='hard_context',
            )
        self.assertEqual(len(resident.send_turn_calls), 1)


    def test_t18_genuine_hot_growth_hard_context_allows_bounded_shrink(self):
        """70k cold → many hot turns → 120k hard → 80k bounded cold must be allowed."""
        resident = _FenceFakeResident(
            last_cold_bootstrap_estimate=70_000,
            last_cold_bootstrap_generation=1,
            generation=12,
            hard_context_pre_spawn_turns=15,
        )
        messages = _history_messages(2, row_chars=10, last_user_text='hi')
        int_overrides = {
            'CC_CONTEXT_HARD_LIMIT': 120_000, 'CC_CONTEXT_SOFT_LIMIT': 90_000,
            'CC_COLD_BOOTSTRAP_SAFETY_MARGIN': 8000,
        }
        with contextlib.ExitStack() as stack:
            for p in self._fence_patches(resident, int_overrides=int_overrides):
                stack.enter_context(p)
            stack.enter_context(mock.patch(
                'chat.cold_bootstrap_budget.estimate_whole_prompt',
                return_value=80_000,
            ))
            events = list(self.gateway._cc_resident_stream_gen(
                messages,
                user_turn=True,
                is_cold=True,
                history_stats={},
                pending_respawn_reason='hard_context',
            ))
        self.assertEqual(len(resident.send_turn_calls), 1)
        self.assertTrue(any(e == 'done' for e, _ in events))

    def test_t20_cold_overflow_leaves_boundary_unchanged_and_records_overflow(self):
        resident = _FenceFakeResident()
        messages = _history_messages(2, row_chars=10, last_user_text='hi')
        rebuild_calls = []

        def _rebuild(new_budget):
            rebuild_calls.append(new_budget)
            return messages, {'conversation_content_trimmed': True}

        import config_store
        config_store.set('HISTORY_TRIMMED_UP_TO_ID', '11')
        config_store.set('HISTORY_OLDEST_RETAINED_ID', '22')
        before = {
            'trimmed_up_to_id': config_store.get_int('HISTORY_TRIMMED_UP_TO_ID', 0),
            'oldest_retained_message_id': config_store.get_int('HISTORY_OLDEST_RETAINED_ID', 0),
        }

        with contextlib.ExitStack() as stack:
            for p in self._fence_patches(
                resident,
                int_overrides={'CC_CONTEXT_HARD_LIMIT': 100, 'CC_CONTEXT_SOFT_LIMIT': 80,
                                'CC_COLD_BOOTSTRAP_SAFETY_MARGIN': 10,
                                'HISTORY_TOKEN_BUDGET': 5000},
            ):
                stack.enter_context(p)
            stack.enter_context(mock.patch('chat.system_builder.build_cc_static_parts', return_value={
                'persona': 'STATIC', 'stable_note': '', 'save_instr': '',
                'full_system': 'S' * 400,
            }))
            with self.assertRaises(ColdBootstrapOverflow) as ctx:
                list(self.gateway._cc_resident_stream_gen(
                    messages, user_turn=True, is_cold=True,
                    rebuild_messages_fn=_rebuild,
                ))
        after = {
            'trimmed_up_to_id': config_store.get_int('HISTORY_TRIMMED_UP_TO_ID', 0),
            'oldest_retained_message_id': config_store.get_int('HISTORY_OLDEST_RETAINED_ID', 0),
        }
        self.assertEqual(after, before)
        self.assertEqual(ctx.exception.error_code, 'cold_bootstrap_overflow')
        self.assertTrue(ctx.exception.usage.get('cold_budget_overflow'))
        self.assertNotIn('cold_rebuild_guard_triggered', ctx.exception.usage)
        self.assertNotIn('cold_rebuild_guard_overflow', ctx.exception.usage)
        self.assertNotIn('cold_rebuild_guard', ctx.exception.usage)
        self.assertEqual(resident.send_turn_calls, [])

    def test_effective_history_budget_zero_remaining_is_minimum_safe(self):
        self.assertEqual(
            effective_history_budget(
                default_history_budget=24_000,
                non_history_estimate=90_000,
                cold_target=70_000,
            ),
            1,
        )

    def test_t24_each_message_plan_serialized_once(self):
        resident = _FenceFakeResident()
        big_messages = _history_messages(40, row_chars=100, last_user_text='hi')
        small_messages = _history_messages(2, row_chars=10, last_user_text='hi')
        serialize_calls = {'original': 0, 'rebuilt': 0}
        real_mtt = self.gateway.messages_to_text

        def _rebuild(new_budget):
            return small_messages, {'conversation_content_trimmed': True}

        def _tracking_mtt(msgs):
            text = real_mtt(msgs)
            if len(msgs) > 10:
                serialize_calls['original'] += 1
            else:
                serialize_calls['rebuilt'] += 1
            return text

        with contextlib.ExitStack() as stack:
            for p in self._fence_patches(
                resident,
                int_overrides={'CC_CONTEXT_HARD_LIMIT': 1000, 'CC_CONTEXT_SOFT_LIMIT': 700,
                                'CC_COLD_BOOTSTRAP_SAFETY_MARGIN': 100,
                                'HISTORY_TOKEN_BUDGET': 5000},
            ):
                stack.enter_context(p)
            stack.enter_context(
                mock.patch.object(self.gateway, 'messages_to_text', side_effect=_tracking_mtt),
            )
            self._run(
                resident, big_messages,
                int_overrides={'CC_CONTEXT_HARD_LIMIT': 1000, 'CC_CONTEXT_SOFT_LIMIT': 700,
                                'CC_COLD_BOOTSTRAP_SAFETY_MARGIN': 100,
                                'HISTORY_TOKEN_BUDGET': 5000},
                rebuild_messages_fn=_rebuild,
            )
        self.assertEqual(serialize_calls['original'], 1)
        self.assertEqual(serialize_calls['rebuilt'], 1)


class ZeroBudgetRebuildTests(unittest.TestCase):
    def setUp(self):
        self.gateway = _import_gateway()
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = _make_chat_db(self.tmp.name)
        rows = []
        for i in range(25):
            rows.append(('hayana', 'U%d ' % i + 'x' * 400))
            rows.append(('assistant', 'A%d ' % i + 'y' * 400))
        self.ids = _insert_rows(self.db_path, rows)
        self.latest_user = 'LATEST_USER_UNIQUE'
        _insert_rows(self.db_path, [('hayana', self.latest_user)])

    def tearDown(self):
        self.tmp.cleanup()

    def _get_db(self):
        def get_db():
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn
        return get_db

    def test_t23_zero_remaining_budget_trims_via_real_build_messages(self):
        resident = _FenceFakeResident()
        rebuild_budgets = []
        rebuilt_msg_counts = []
        latest_user_hits = []

        gateway = self.gateway

        def _real_rebuild(new_budget):
            rebuild_budgets.append(new_budget)
            rebuilt_stats = {}
            rebuilt_msgs = gateway.build_messages(
                for_cc=True,
                cold_safe=True,
                history_stats_out=rebuilt_stats,
                history_token_budget_override=new_budget,
                commit_history_boundary=False,
            )
            rebuilt_msg_counts.append(len(rebuilt_msgs))
            latest_user_hits.append(
                sum(
                    1 for m in rebuilt_msgs
                    if m.get('role') == 'user' and self.latest_user in str(m.get('content') or '')
                )
            )
            return rebuilt_msgs, rebuilt_stats

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(gateway, 'get_db', self._get_db()))
            stack.enter_context(mock.patch.object(gateway, '_CC_RESIDENT', resident))
            stack.enter_context(mock.patch.object(gateway, 'CC_TOKEN', 'tok'))
            stack.enter_context(mock.patch.object(gateway, 'CC_CWD', tempfile.mkdtemp()))
            stack.enter_context(mock.patch.object(gateway, '_recall_memories', return_value=('', [])))
            stack.enter_context(mock.patch.object(gateway, '_fetch_group_chat_rows', return_value=([], None)))
            stack.enter_context(mock.patch('chat.system_builder.build_cc_state', return_value={}))
            stack.enter_context(mock.patch('chat.system_builder.build_cc_one_shot', return_value={
                'wake_nonmessage_background': '', 'wake_message_background': '',
                'wake_reply_bridge': '', 'wake_ids': [], 'wake_items': [],
                'task_feedback': '', 'dream_flash': '',
                'feedback_ids': [], 'dream_id': None,
            }))
            stack.enter_context(mock.patch('chat.system_builder.build_cc_cold_once', return_value={}))
            for p in _cfg_patches(
                int_overrides={
                    'CC_CONTEXT_HARD_LIMIT': 100,
                    'CC_CONTEXT_SOFT_LIMIT': 80,
                    'CC_COLD_BOOTSTRAP_SAFETY_MARGIN': 10,
                    'HISTORY_TOKEN_BUDGET': 24_000,
                },
                bool_overrides={'CONTEXT_LEAN_HISTORY_ENABLED': False},
            ):
                stack.enter_context(p)
            stack.enter_context(mock.patch('chat.system_builder.build_cc_static_parts', return_value={
                'persona': 'STATIC', 'stable_note': '', 'save_instr': '',
                'full_system': 'S' * 400,
            }))
            history_stats = {}
            messages = gateway.build_messages(
                for_cc=True,
                cold_safe=True,
                history_stats_out=history_stats,
                commit_history_boundary=False,
            )
            with self.assertRaises(ColdBootstrapOverflow):
                list(gateway._cc_resident_stream_gen(
                    messages,
                    user_turn=True,
                    is_cold=True,
                    history_stats=history_stats,
                    rebuild_messages_fn=_real_rebuild,
                ))

        self.assertEqual(rebuild_budgets, [1])
        self.assertEqual(len(rebuilt_msg_counts), 1)
        self.assertLess(rebuilt_msg_counts[0], len(messages))
        self.assertEqual(latest_user_hits, [1])
        self.assertEqual(resident.send_turn_calls, [])


if __name__ == '__main__':
    unittest.main()
