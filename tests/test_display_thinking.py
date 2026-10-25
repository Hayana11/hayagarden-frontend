import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chat.display_thinking import (
    AUTHORED_THINKING_INSTRUCTION,
    DISPLAY_THINKING_PROMPT_KEY,
    append_authored_thinking_instruction,
    authored_thinking_instruction_suffix,
    configured_mode_from_authored_prompt_enabled,
    display_thinking_config_state,
    filter_display_thinking_events,
    get_display_thinking_mode,
    get_display_thinking_prompt,
    get_display_thinking_snapshot,
    normalize_display_thinking_mode,
    validate_display_thinking_prompt,
    prepare_daily_display_thinking_plan,
    resolve_effective_display_thinking_mode,
)


ROOT = Path(__file__).resolve().parents[1]


def filtered(text_chunks, mode='auto', before=(), after=(), done_text=None):
    events = list(before)
    events.extend(('text', chunk) for chunk in text_chunks)
    events.extend(after)
    raw = ''.join(text_chunks) if done_text is None else done_text
    native = ''.join(
        str(payload) for event, payload in events if event == 'think'
    )
    events.append(('done', (raw, native, {'provider': 'claude_code'})))
    output = list(filter_display_thinking_events(events, mode))
    text = ''.join(str(payload) for event, payload in output if event == 'text')
    thinking = ''.join(
        str(payload) for event, payload in output if event == 'think'
    )
    self_state = ''.join(
        str(payload) for event, payload in output if event == 'self_state'
    )
    # Keep the historical helper's fourth value useful for old assertions;
    # focused tests below inspect the two event channels separately.
    display_channel = thinking or (
        self_state if mode not in ('off', 'native') else ''
    )
    done = next(payload for event, payload in output if event == 'done')
    return output, text, display_channel, done


class DisplayThinkingStreamTests(unittest.TestCase):
    def test_t1_single_chunk(self):
        _, text, thinking, done = filtered(['[[自我状态]]A[[/自我状态]]B'])
        self.assertEqual('A', thinking)
        self.assertEqual('B', text)
        self.assertEqual(('B', ''), done[:2])

    def test_t2_opening_tag_split(self):
        _, text, thinking, _ = filtered(['[[自我', '状态]]A[[/自我状态]]B'])
        self.assertEqual(('A', 'B'), (thinking, text))

    def test_t3_closing_tag_split(self):
        _, text, thinking, _ = filtered(['[[自我状态]]A[[/自我', '状态]]B'])
        self.assertEqual(('A', 'B'), (thinking, text))

    def test_t4_both_tags_split(self):
        _, text, thinking, _ = filtered([
            '[[自我', '状态]]\n我在想', '什么', '[[/自我', '状态]]\n正式回复',
        ])
        self.assertEqual('\n我在想什么', thinking)
        self.assertEqual('\n正式回复', text)

    def test_t5_plain_chinese_text_is_preserved(self):
        _, text, thinking, _ = filtered(['今天思绪很多。'])
        self.assertEqual('今天思绪很多。', text)
        self.assertEqual('', thinking)

    def test_t6_native_and_self_state_are_both_retained(self):
        output, text, _display_channel, done = filtered(
            ['[[自我状态]]A[[/自我状态]]B'],
            before=[('think', 'N')],
        )
        self.assertEqual('N', ''.join(
            str(payload) for event, payload in output if event == 'think'
        ))
        self.assertEqual('A', ''.join(
            str(payload) for event, payload in output if event == 'self_state'
        ))
        self.assertEqual('B', text)
        self.assertEqual(('B', 'N'), done[:2])

    def test_t7_self_state_does_not_enter_native_thinking(self):
        output, text, _display_channel, done = filtered(
            ['[[自我状态]]A[[/自我状态]]B'],
        )
        self.assertEqual('', ''.join(
            str(payload) for event, payload in output if event == 'think'
        ))
        self.assertEqual('A', ''.join(
            str(payload) for event, payload in output if event == 'self_state'
        ))
        self.assertEqual('B', text)
        self.assertEqual(('B', ''), done[:2])

    def test_t8_native_and_self_state_do_not_compete(self):
        events = [
            ('text', '[[自我状态]]A'),
            ('think', 'LATE'),
            ('text', '[[/自我状态]]B'),
            ('done', ('[[自我状态]]A[[/自我状态]]B', 'LATE', {})),
        ]
        output = list(filter_display_thinking_events(events, 'auto'))
        self.assertEqual(
            'LATE',
            ''.join(p for event, p in output if event == 'think'),
        )
        self.assertEqual(
            'A',
            ''.join(p for event, p in output if event == 'self_state'),
        )
        self.assertEqual(
            'B',
            ''.join(p for event, p in output if event == 'text'),
        )

    def test_self_state_and_native_channels_are_distinct(self):
        events = [
            ('think', 'native thinking'),
            ('text', '[[自我状态]]A[[/自我状态]]B'),
            ('done', ('[[自我状态]]A[[/自我状态]]B', 'native thinking', {})),
        ]
        output = list(filter_display_thinking_events(events, 'auto'))
        self.assertEqual(
            ['native thinking'],
            [p for event, p in output if event == 'think'],
        )
        self.assertEqual(
            ['A'],
            [p for event, p in output if event == 'self_state'],
        )
        self.assertEqual(
            ['B'],
            [p for event, p in output if event == 'text'],
        )
        done = next(payload for event, payload in output if event == 'done')
        self.assertEqual(('B', 'native thinking'), done[:2])

    def test_legacy_self_state_protocol_is_read_compatibly(self):
        output = list(filter_display_thinking_events([
            ('text', '<思绪>旧内容</思绪>正文'),
            ('done', ('<思绪>旧内容</思绪>正文', '', {})),
        ], 'auto'))
        self.assertEqual(
            '旧内容',
            ''.join(p for event, p in output if event == 'self_state'),
        )
        self.assertEqual(
            '正文',
            ''.join(p for event, p in output if event == 'text'),
        )
        self.assertNotIn('<思绪>', repr(output))
        self.assertNotIn('</思绪>', repr(output))

    def test_t9_no_tag_streams_text_unchanged(self):
        _, text, thinking, done = filtered(['普', '通', '正文'])
        self.assertEqual('普通正文', text)
        self.assertEqual('', thinking)
        self.assertEqual('普通正文', done[0])

    def test_t10_unclosed_self_state_never_becomes_content(self):
        output, text, thinking, done = filtered(['[[自我状态]]A', '还在想'])
        self.assertEqual('', text)
        self.assertEqual('A还在想', thinking)
        self.assertEqual(('', ''), done[:2])
        self.assertEqual('A还在想', ''.join(
            p for event, p in output if event == 'self_state'
        ))
        self.assertNotIn('[[自我状态]]', repr(output))

    def test_t10_partial_opening_at_eof_is_suppressed(self):
        output, text, thinking, done = filtered(['正文', '<思'])
        self.assertEqual('正文', text)
        self.assertEqual('', thinking)
        self.assertEqual('正文', done[0])
        self.assertNotIn('<思', repr(output))

    def test_t11_text_after_close_keeps_streaming(self):
        _, text, thinking, _ = filtered([
            '[[自我状态]]A[[/自我状态]]', '正', '式', '回复',
        ])
        self.assertEqual('A', thinking)
        self.assertEqual('正式回复', text)

    def test_r1_repeated_authored_tags_have_one_owner(self):
        _, text, thinking, _ = filtered(
            ['[[自我状态]]A[[/自我状态]]B[[自我状态]]C[[/自我状态]]D'],
        )
        self.assertEqual('BD', text)
        self.assertEqual('AC', thinking)
        self.assertNotIn('[[自我状态]]', text)
        self.assertNotIn('[[/自我状态]]', text)

    def test_r2_repeated_tags_split_across_chunks(self):
        _, text, thinking, _ = filtered([
            '[[自我状态]]A[[/自我状态]]B<思', '绪>C</思', '绪>D',
        ])
        self.assertEqual('BD', text)
        self.assertEqual('AC', thinking)
        self.assertNotIn('[[自我状态]]', text)
        self.assertNotIn('[[/自我状态]]', text)

    def test_r3_native_does_not_discard_repeated_self_state_blocks(self):
        output, text, _display_channel, _ = filtered(
            ['[[自我状态]]A[[/自我状态]]B[[自我状态]]C[[/自我状态]]D'],
            before=[('think', 'N')],
        )
        self.assertEqual('BD', text)
        self.assertEqual('N', ''.join(
            str(payload) for event, payload in output if event == 'think'
        ))
        self.assertEqual('AC', ''.join(
            str(payload) for event, payload in output if event == 'self_state'
        ))
        self.assertNotIn('[[自我状态]]', repr(output))
        self.assertNotIn('[[/自我状态]]', repr(output))

    def test_m1_stray_close_in_text_is_discarded(self):
        output, text, thinking, done = filtered(['正文[[/自我状态]]继续'])
        self.assertEqual('正文继续', text)
        self.assertEqual('', thinking)
        self.assertEqual(('正文继续', ''), done[:2])
        self.assertNotIn('[[自我状态]]', repr(output))
        self.assertNotIn('[[/自我状态]]', repr(output))

    def test_m2_stray_close_split_across_chunks_is_discarded(self):
        output, text, thinking, _ = filtered(['正文</思', '绪>继续'])
        self.assertEqual(('正文继续', ''), (text, thinking))
        self.assertNotIn('[[自我状态]]', repr(output))
        self.assertNotIn('[[/自我状态]]', repr(output))

    def test_m3_nested_open_in_thinking_is_discarded(self):
        output, text, thinking, _ = filtered(['[[自我状态]]A[[自我状态]]B[[/自我状态]]C'])
        self.assertEqual(('C', 'AB'), (text, thinking))
        self.assertNotIn('[[自我状态]]', repr(output))
        self.assertNotIn('[[/自我状态]]', repr(output))

    def test_m4_nested_open_split_across_chunks_is_discarded(self):
        output, text, thinking, _ = filtered([
            '[[自我状态]]A<思', '绪>B[[/自我状态]]C',
        ])
        self.assertEqual(('C', 'AB'), (text, thinking))
        self.assertNotIn('[[自我状态]]', repr(output))
        self.assertNotIn('[[/自我状态]]', repr(output))

    def test_m5_native_keeps_malformed_self_state_parser_behavior(self):
        output, text, _display_channel, _ = filtered(
            ['[[自我状态]]A[[自我状态]]B[[/自我状态]]C[[/自我状态]]D'],
            before=[('think', 'native')],
        )
        self.assertEqual('CD', text)
        self.assertEqual('native', ''.join(
            str(payload) for event, payload in output if event == 'think'
        ))
        self.assertEqual('AB', ''.join(
            str(payload) for event, payload in output if event == 'self_state'
        ))
        self.assertNotIn('[[自我状态]]', repr(output))
        self.assertNotIn('[[/自我状态]]', repr(output))

    def test_m6_partial_stray_close_at_eof_is_suppressed(self):
        output, text, thinking, done = filtered(['正文', '</思'])
        self.assertEqual(('正文', ''), (text, thinking))
        self.assertEqual(('正文', ''), done[:2])
        self.assertNotIn('</思', repr(output))

    def test_t12_off_does_not_prompt_but_keeps_parser_channels(self):
        original = 'hello'
        self.assertIs(
            original,
            append_authored_thinking_instruction(original, 'off'),
        )
        output, text, _display_channel, done = filtered(
            ['[[自我状态]]A[[/自我状态]]B'], mode='off',
        )
        self.assertEqual('', ''.join(
            str(payload) for event, payload in output if event == 'think'
        ))
        self.assertEqual('A', ''.join(
            str(payload) for event, payload in output if event == 'self_state'
        ))
        self.assertEqual('B', text)
        self.assertEqual(('B', ''), done[:2])

    def test_t13_native_does_not_prompt_but_keeps_both_channels(self):
        original = ['hello']
        prepared = append_authored_thinking_instruction(original, 'native')
        self.assertIs(original, prepared)
        output, text, _display_channel, done = filtered(
            ['[[自我状态]]A[[/自我状态]]B'],
            mode='native',
            before=[('think', 'N')],
        )
        self.assertEqual('N', ''.join(
            str(payload) for event, payload in output if event == 'think'
        ))
        self.assertEqual('A', ''.join(
            str(payload) for event, payload in output if event == 'self_state'
        ))
        self.assertEqual('B', text)
        self.assertEqual(('B', 'N'), done[:2])

    def test_t14_authored_mode_keeps_native_and_self_state_independent(self):
        prepared = append_authored_thinking_instruction('hello', 'authored')
        self.assertIn(AUTHORED_THINKING_INSTRUCTION, prepared)
        output, text, _display_channel, done = filtered(
            ['[[自我状态]]A[[/自我状态]]B'],
            mode='authored',
            before=[('think', 'N')],
        )
        self.assertEqual('N', ''.join(
            str(payload) for event, payload in output if event == 'think'
        ))
        self.assertEqual('A', ''.join(
            str(payload) for event, payload in output if event == 'self_state'
        ))
        self.assertEqual('B', text)
        self.assertEqual(('B', 'N'), done[:2])

    def test_t15_auto_prompts_and_prefers_early_native(self):
        original = [{'type': 'text', 'text': 'hello'}]
        prepared = append_authored_thinking_instruction(original, 'auto')
        self.assertIsNot(original, prepared)
        self.assertEqual(2, len(prepared))
        _, text, thinking, _ = filtered(
            ['[[自我状态]]A[[/自我状态]]B'],
            mode='auto',
            before=[('think', 'N')],
        )
        self.assertEqual(('B', 'N'), (text, thinking))

    def test_unknown_mode_normalizes_to_auto_without_error(self):
        self.assertEqual('auto', normalize_display_thinking_mode('bad'))
        self.assertEqual(
            'auto',
            get_display_thinking_mode(lambda _key, _default: 'bad'),
        )
        self.assertEqual(
            'auto',
            get_display_thinking_mode(
                lambda _key, _default: (_ for _ in ()).throw(RuntimeError())
            ),
        )

    def test_empty_authored_and_one_character_chunks(self):
        chunks = list('[[自我状态]][[/自我状态]]正文')
        _, text, thinking, _ = filtered(chunks)
        self.assertEqual('正文', text)
        self.assertEqual('', thinking)

    def test_normal_wake_filter_removes_authored_tags_and_preserves_tools(self):
        events = [
            ('tool_use', {'name': 'mcp__home__get_todos'}),
            ('text', '[[自我状态]]先看一眼[[/自我状态]]正式回复'),
            ('tool_result', {'result': 'ok'}),
            ('done', ('[[自我状态]]先看一眼[[/自我状态]]正式回复', '', {})),
        ]
        output = list(filter_display_thinking_events(events, 'auto'))
        self.assertEqual(
            '正式回复',
            ''.join(p for event, p in output if event == 'text'),
        )
        self.assertEqual(
            '先看一眼',
            ''.join(p for event, p in output if event == 'self_state'),
        )
        self.assertIn(('tool_use', {'name': 'mcp__home__get_todos'}), output)
        self.assertIn(('tool_result', {'result': 'ok'}), output)
        done = next(p for event, p in output if event == 'done')
        self.assertEqual(('正式回复', ''), done[:2])
        self.assertNotIn('[[自我状态]]', repr(output))
        self.assertNotIn('[[/自我状态]]', repr(output))

    def test_tool_events_are_unchanged(self):
        events = [
            ('tool_use', {'name': 'x'}),
            ('text', '[[自我状态]]A[[/自我状态]]B'),
            ('tool_result', {'result': 'ok'}),
            ('done', ('[[自我状态]]A[[/自我状态]]B', '', {})),
        ]
        output = list(filter_display_thinking_events(events, 'auto'))
        self.assertIn(('tool_use', {'name': 'x'}), output)
        self.assertIn(('tool_result', {'result': 'ok'}), output)

    def test_existing_response_parser_contract_is_unchanged(self):
        from chat.response_parser import extract_text, extract_thinking

        blocks = [
            {'type': 'thinking', 'thinking': 'native'},
            {'type': 'text', 'text': 'formal'},
        ]
        self.assertEqual('formal', extract_text(blocks))
        self.assertEqual('native', extract_thinking(blocks))

    def test_t8_opus55_auto_keeps_self_state_suffix(self):
        mode = resolve_effective_display_thinking_mode(
            'auto', 'explicit:claude-opus-5-5',
        )
        self.assertEqual('auto', mode)
        self.assertIn(AUTHORED_THINKING_INSTRUCTION, authored_thinking_instruction_suffix(mode))
        plan = SimpleNamespace()
        prepare_daily_display_thinking_plan(plan, mode)
        self.assertEqual('auto', plan.provider_display_thinking_mode)
        self.assertIn(AUTHORED_THINKING_INSTRUCTION, plan.provider_display_thinking_suffix)

    def test_t9_explicit_authored_and_older_auto_behavior_are_preserved(self):
        self.assertEqual(
            'authored',
            resolve_effective_display_thinking_mode(
                'authored', 'explicit:claude-opus-5-5',
            ),
        )
        self.assertEqual(
            'auto',
            resolve_effective_display_thinking_mode(
                'auto', 'explicit:claude-sonnet-4-6',
            ),
        )
        self.assertTrue(append_authored_thinking_instruction(
            'hi', resolve_effective_display_thinking_mode(
                'auto', 'explicit:claude-sonnet-4-6',
            ),
        ).endswith(AUTHORED_THINKING_INSTRUCTION))
        self.assertTrue(append_authored_thinking_instruction(
            'hi', 'authored',
        ).endswith(AUTHORED_THINKING_INSTRUCTION))

    def test_mode_resolver_is_lazy_until_provider_text_arrives(self):
        current = {'identity': None}
        calls = []
        def source():
            yield ('heartbeat', None)
            current['identity'] = 'explicit:claude-opus-5-5'
            yield ('text', '[[自我状态]]A[[/自我状态]]B')
            yield ('done', ('[[自我状态]]A[[/自我状态]]B', '', {}))
        def mode():
            calls.append(current['identity'])
            return resolve_effective_display_thinking_mode(
                'auto', current['identity'],
            )
        output = list(filter_display_thinking_events(source(), mode))
        self.assertEqual(['explicit:claude-opus-5-5'], calls)
        self.assertEqual('B', ''.join(p for e, p in output if e == 'text'))
        self.assertEqual('', ''.join(p for e, p in output if e == 'think'))

    def test_t16_prompt_and_filter_are_solo_chat_scoped(self):
        source = (ROOT / 'gateway.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        owners = {'append_authored_self_state_instruction': set(),
                  'filter_display_thinking_events': set()}

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.stack = []

            def visit_FunctionDef(self, node):
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id in owners:
                    owners[node.func.id].add(self.stack[-1] if self.stack else '')
                self.generic_visit(node)

        Visitor().visit(tree)
        self.assertEqual(
            {
                '_cc_resident_stream_gen',
                '_messages_with_display_instruction',
            },
            owners['append_authored_self_state_instruction'],
        )
        self.assertEqual(
            {
                '_run_unified_normal_main_chat_turn',
                '_stream_cc_daily_soft_window',
                'gen_cc',
            },
            owners['filter_display_thinking_events'],
        )
        self.assertIn(
            "'DISPLAY_THINKING_MODE': 'auto'",
            (ROOT / 'config_store.py').read_text(encoding='utf-8'),
        )


    def test_daily_image_only_preserves_vision_bridge_and_adds_instruction(self):
        from chat.daily_runtime import format_resident_turn_content
        from chat import cc_vision_bridge as vb

        def provider_content(mode):
            plan = SimpleNamespace(
                user_content='[image]',
                user_image_url='/static/uploads/daily.png',
                assembly={'state': '', 'current_day_history': []},
            )
            prepare_daily_display_thinking_plan(plan, mode)
            with mock.patch.object(
                vb, 'resolve_image_bytes',
                return_value=(b'png-bytes', 'image/png'),
            ):
                return plan, format_resident_turn_content(
                    assembly=plan.assembly,
                    user_content=plan.user_content,
                    is_cold=False,
                    is_respawn=False,
                    user_image_url=plan.user_image_url,
                    provider_display_thinking_suffix=(
                        plan.provider_display_thinking_suffix
                    ),
                )

        for mode in ('auto', 'authored'):
            plan, content = provider_content(mode)
            self.assertEqual('[image]', plan.user_content)
            self.assertEqual('', plan.assembly['state'])
            self.assertIsInstance(content, list)
            text = ''.join(
                block.get('text', '') for block in content
                if block.get('type') == 'text'
            )
            self.assertIn(AUTHORED_THINKING_INSTRUCTION, text)
            self.assertNotIn('[image]', text)
            self.assertTrue(any(block.get('type') == 'image' for block in content))

        for mode in ('off', 'native'):
            plan, content = provider_content(mode)
            self.assertEqual('[image]', plan.user_content)
            self.assertEqual('', plan.assembly['state'])
            self.assertEqual(1, sum(
                block.get('type') == 'image' for block in content
            ))


    def test_cold_fence_rebuild_preserves_ephemeral_suffix_contract(self):
        from chat import cc_vision_bridge as vb
        from chat import daily_runtime as dr

        def run_case(mode, user_content, image_url=''):
            plan = SimpleNamespace(
                user_content=user_content,
                user_image_url=image_url,
                user_attachments=[],
                provider_display_thinking_suffix='',
                assembly={
                    'state': 'ORIGINAL_STATE',
                    'current_day_history': [],
                    'manifest': {'cold_history_budget': 100},
                },
                manifest={},
            )
            prepare_daily_display_thinking_plan(plan, mode)
            with mock.patch.object(
                vb, 'resolve_image_bytes',
                return_value=(b'png-bytes', 'image/png'),
            ):
                initial = dr.format_resident_turn_content(
                    assembly=plan.assembly,
                    user_content=plan.user_content,
                    is_cold=True,
                    is_respawn=False,
                    user_image_url=plan.user_image_url,
                    provider_display_thinking_suffix=(
                        plan.provider_display_thinking_suffix
                    ),
                )
                rebuilt = {
                    'state': 'REBUILT_STATE',
                    'current_day_history': [],
                    'manifest': {},
                }
                resident = SimpleNamespace(
                    pending_respawn_reason=None,
                    note_cold_bootstrap_estimate=lambda _estimate: None,
                )
                with mock.patch.object(
                    dr, '_rebuild_daily_assembly_with_history_budget',
                    return_value=rebuilt,
                ) as rebuild, mock.patch(
                    'chat.cold_bootstrap_budget.estimate_whole_prompt',
                    side_effect=[100000, 1000],
                ), mock.patch(
                    'chat.cold_bootstrap_budget.cold_prompt_target',
                    return_value=5000,
                ), mock.patch(
                    'chat.cold_bootstrap_budget.effective_history_budget',
                    return_value=1,
                ):
                    final = dr._apply_daily_cold_prompt_fence(
                        plan,
                        resident=resident,
                        static_system='STATIC',
                        content=initial,
                        is_cold=True,
                        is_respawn=False,
                    )
            rebuild.assert_called_once()
            self.assertIs(plan.assembly, rebuilt)
            self.assertEqual('REBUILT_STATE', plan.assembly['state'])
            self.assertNotIn(AUTHORED_THINKING_INSTRUCTION, plan.assembly['state'])
            self.assertNotIn(AUTHORED_THINKING_INSTRUCTION, repr(plan.manifest))
            return plan, final

        def content_text(content):
            if isinstance(content, str):
                return content
            return ''.join(
                block.get('text', '') for block in content
                if block.get('type') == 'text'
            )

        for mode in ('auto', 'authored'):
            plan, content = run_case(mode, '[image]', '/static/uploads/cold.png')
            text = content_text(content)
            self.assertEqual(1, text.count(AUTHORED_THINKING_INSTRUCTION))
            self.assertNotIn('[image]', text)
            self.assertTrue(any(block.get('type') == 'image' for block in content))
            self.assertEqual('[image]', plan.user_content)

            _plan, text_content = run_case(mode, 'CURRENT')
            text = content_text(text_content)
            self.assertIn('CURRENT', text)
            self.assertEqual(1, text.count(AUTHORED_THINKING_INSTRUCTION))

        for mode in ('off', 'native'):
            plan, content = run_case(mode, '[image]', '/static/uploads/cold.png')
            text = content_text(content)
            self.assertEqual(0, text.count(AUTHORED_THINKING_INSTRUCTION))
            self.assertNotIn('[image]', text)
            self.assertTrue(any(block.get('type') == 'image' for block in content))
            self.assertEqual('[image]', plan.user_content)

            _plan, text_content = run_case(mode, 'CURRENT')
            text = content_text(text_content)
            self.assertIn('CURRENT', text)
            self.assertEqual(0, text.count(AUTHORED_THINKING_INSTRUCTION))

    def test_r17_runtime_prompt_fallback_save_and_turn_snapshot(self):
        custom = '[[自我状态]]custom[[/自我状态]] 请保持简短。'
        state = {'DISPLAY_THINKING_MODE': 'auto', DISPLAY_THINKING_PROMPT_KEY: ''}

        def getter(key, default):
            return state.get(key, default)

        self.assertEqual(AUTHORED_THINKING_INSTRUCTION, get_display_thinking_prompt(getter))
        state[DISPLAY_THINKING_PROMPT_KEY] = custom
        self.assertEqual(custom, get_display_thinking_prompt(getter))

        frozen = get_display_thinking_snapshot(getter)
        state[DISPLAY_THINKING_PROMPT_KEY] = '[[自我状态]]new[[/自我状态]]'
        self.assertEqual(('auto', custom), frozen)
        self.assertEqual('[[自我状态]]new[[/自我状态]]', get_display_thinking_prompt(getter))

    def test_r18_prompt_validation_and_self_state_contract(self):
        valid = '[[自我状态]]custom[[/自我状态]] 请保持简短。'
        self.assertEqual(valid, validate_display_thinking_prompt(valid))
        self.assertIn('[[自我状态]]', AUTHORED_THINKING_INSTRUCTION)
        self.assertNotIn('<思绪>', AUTHORED_THINKING_INSTRUCTION)
        self.assertEqual(
            valid,
            validate_display_thinking_prompt('<思绪>custom</思绪> 请保持简短。'),
        )
        for invalid in ('', '没有标签', '[[/自我状态]][[自我状态]]反序'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    validate_display_thinking_prompt(invalid)
        self.assertEqual('hello', append_authored_thinking_instruction(
            'hello', 'off', valid,
        ))
        prepared = append_authored_thinking_instruction(
            'hello', 'auto', valid,
        )
        self.assertEqual(1, prepared.count(valid))
        self.assertNotIn('[image]', prepared)

    def test_r19_daily_plan_keeps_one_frozen_prompt_for_rebuild(self):
        valid = '[[自我状态]]daily[[/自我状态]] 请保持简短。'
        plan = SimpleNamespace()
        prepare_daily_display_thinking_plan(plan, 'authored', valid)
        self.assertEqual(valid, plan.provider_display_thinking_prompt)
        self.assertEqual(1, plan.provider_display_thinking_suffix.count(valid))

    def test_r20_profile_route_does_not_touch_persona(self):
        source = (ROOT / 'app.py').read_text(encoding='utf-8')
        start = source.index("@app.route('/api/profile/display-thinking-prompt'")
        end = source.index("# ── User Profile", start)
        route = source[start:end]
        self.assertIn("config_store.set(DISPLAY_THINKING_PROMPT_KEY", route)
        self.assertIn("config_store.delete(DISPLAY_THINKING_PROMPT_KEY)", route)
        self.assertNotIn('persona.md', route)
        gateway = (ROOT / 'gateway.py').read_text(encoding='utf-8')
        self.assertIn('get_display_thinking_snapshot()', gateway)
        self.assertIn('display_thinking_prompt=display_thinking_prompt', gateway)

    def test_r21_runtime_config_set_and_reset_falls_back_to_source_default(self):
        import config_store
        from tempfile import TemporaryDirectory

        custom = '[[自我状态]]stored[[/自我状态]] 请保持简短。'
        original_db_path = config_store.DB_PATH
        with TemporaryDirectory() as tmp:
            try:
                config_store.DB_PATH = str(Path(tmp) / 'runtime.sqlite3')
                config_store._init_table()
                config_store.set(DISPLAY_THINKING_PROMPT_KEY, custom)
                self.assertEqual(custom, get_display_thinking_prompt())
                config_store.delete(DISPLAY_THINKING_PROMPT_KEY)
                self.assertEqual(AUTHORED_THINKING_INSTRUCTION, get_display_thinking_prompt())
            finally:
                config_store.DB_PATH = original_db_path


class DisplayThinkingConfigSurfaceTests(unittest.TestCase):
    def test_opus55_auto_config_state_keeps_self_state_suffix(self):
        state = display_thinking_config_state(
            'auto', 'explicit:claude-opus-5-5',
        )
        self.assertEqual('auto', state['configured_mode'])
        self.assertEqual('auto', state['effective_mode'])
        self.assertEqual('explicit:claude-opus-5-5', state['model_identity'])
        self.assertTrue(state['authored_prompt_effective'])
        self.assertIn(
            AUTHORED_THINKING_INSTRUCTION,
            authored_thinking_instruction_suffix(state['effective_mode']),
        )

    def test_opus55_native_config_state_omits_authored_suffix(self):
        state = display_thinking_config_state(
            'native', 'explicit:claude-opus-5-5',
        )
        self.assertEqual('native', state['configured_mode'])
        self.assertEqual('native', state['effective_mode'])
        self.assertFalse(state['authored_prompt_effective'])
        self.assertEqual(
            '',
            authored_thinking_instruction_suffix(state['effective_mode']),
        )

    def test_sonnet_auto_config_state_keeps_authored_prompt(self):
        state = display_thinking_config_state(
            'auto', 'explicit:claude-sonnet-5',
        )
        self.assertEqual('auto', state['configured_mode'])
        self.assertEqual('auto', state['effective_mode'])
        self.assertTrue(state['authored_prompt_effective'])
        self.assertIn(
            AUTHORED_THINKING_INSTRUCTION,
            authored_thinking_instruction_suffix(state['effective_mode']),
        )

    def test_sonnet_native_config_state_turns_prompt_off(self):
        state = display_thinking_config_state(
            'native', 'explicit:claude-sonnet-5',
        )
        self.assertEqual('native', state['effective_mode'])
        self.assertFalse(state['authored_prompt_effective'])
        self.assertEqual(
            '',
            authored_thinking_instruction_suffix(state['effective_mode']),
        )

    def test_authored_prompt_toggle_maps_only_auto_and_native(self):
        self.assertEqual(
            'auto', configured_mode_from_authored_prompt_enabled(True),
        )
        self.assertEqual(
            'native', configured_mode_from_authored_prompt_enabled(False),
        )
        for invalid in ('auto', 'native', 'off', 1, 0, None, 'true'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    configured_mode_from_authored_prompt_enabled(invalid)

    def test_ui_toggle_keeps_opus55_native_and_self_state_independent(self):
        enabled_state = display_thinking_config_state(
            configured_mode_from_authored_prompt_enabled(True),
            'explicit:claude-opus-5-5',
        )
        self.assertEqual('auto', enabled_state['effective_mode'])
        self.assertTrue(enabled_state['authored_prompt_effective'])
        self.assertIn(
            AUTHORED_THINKING_INSTRUCTION,
            authored_thinking_instruction_suffix(enabled_state['effective_mode']),
        )

        disabled_state = display_thinking_config_state(
            configured_mode_from_authored_prompt_enabled(False),
            'explicit:claude-opus-5-5',
        )
        self.assertEqual('native', disabled_state['effective_mode'])
        self.assertFalse(disabled_state['authored_prompt_effective'])

    def test_config_routes_reuse_resolver_and_reject_mode_strings(self):
        source = (ROOT / 'app.py').read_text(encoding='utf-8')
        start = source.index('def _display_thinking_config_payload(')
        end = source.index('# ── User Profile', start)
        route = source[start:end]
        self.assertIn('display_thinking_config_state', route)
        self.assertIn('configured_mode_from_authored_prompt_enabled', route)
        self.assertIn('capture_generation_authority', route)
        self.assertIn("config_store.set('DISPLAY_THINKING_MODE', mode)", route)
        self.assertIn('authored_prompt_enabled', route)
        self.assertNotIn("data.get('mode')", route)
        self.assertNotIn('--thinking', route)
        self.assertNotIn('thinking_enabled', route)
        self.assertNotIn('rollback', route)
        prompt_start = source.index(
            "@app.route('/api/profile/display-thinking-prompt'",
        )
        self.assertLess(prompt_start, start)
        module = (ROOT / 'chat' / 'display_thinking.py').read_text(
            encoding='utf-8',
        )
        self.assertEqual(1, module.count('def resolve_effective_display_thinking_mode('))
        self.assertEqual(1, module.count('def authored_self_state_instruction_suffix('))
        self.assertNotIn('--thinking-enabled', module)
        self.assertNotIn('--thinking ', module)
        self.assertNotIn('thinking_owner', module)


if __name__ == '__main__':
    unittest.main()
