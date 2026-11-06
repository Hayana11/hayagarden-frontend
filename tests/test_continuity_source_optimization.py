"""CanonicalTurn golden identity and per-turn computation reuse regression."""
from __future__ import annotations

import json
import unittest
from collections import Counter
from unittest import mock

import continuity.sources as sources


def row(mid: int, author: str, content: str, **extra):
    base = {
        'id': mid,
        'author': author,
        'content': content,
        'thinking': '',
        'source_kind': 'chat',
        'tool_calls': '',
        'branches': '',
        'branch_idx': 0,
        'cache_info': '',
        'attachments': '[]',
        'image_url': '',
        'file_url': '',
        'file_name': '',
        'created_at': f'2026-11-01 10:{mid:02d}:00',
    }
    base.update(extra)
    return base


def optimization_rows():
    branches = json.dumps([
        {'content': '旧分支', 'thinking': '旧私密思绪', 'tool_calls': []},
        {'content': '当前分支', 'thinking': '当前私密思绪', 'tool_calls': [
            {'name': 'branch.tool', 'args': {'v': 1}, 'result': 'branch result', 'success': True},
        ]},
    ], ensure_ascii=False)
    tools = json.dumps([
        {'name': 'memory.search', 'args': {'query': '青鸟', 'limit': 2}, 'result': ['一', '二'], 'success': True, 'artifact': {'id': 'm-1'}, 'diff': None},
        {'name': 'notes.write', 'args': {'title': '记下'}, 'result': 'saved', 'success': True, 'artifact': None, 'diff': {'added': 1}},
    ], ensure_ascii=False)
    wake_cache = json.dumps({
        'wake_mode': 'normal',
        'canonical_chat_history': True,
        'unified_chat_resident': True,
        'b3_authority': True,
        'source': 'wake',
        'provider': 'claude_code',
    })
    return [
        row(1, 'hayana', '请看这个附件', attachments=json.dumps([
            {'type': 'file', 'url': '/static/uploads/files/aaaaaaaa_a.txt', 'name': 'a.txt'},
        ]), file_url='/static/uploads/files/aaaaaaaa_a.txt', file_name='a.txt'),
        row(2, 'assistant', 'Wake 插入两条消息之间', source_kind='wake', cache_info=wake_cache),
        row(3, 'assistant', '带工具和分支的回复', tool_calls=tools, branches=branches, branch_idx=1),
        row(4, 'hayana', '这一轮中断了'),
        row(5, 'assistant', '只恢复了一部分', cache_info=json.dumps({'partial_rescue': True})),
        row(6, 'hayana', '普通文本问题'),
        row(7, 'assistant', '普通文本回答'),
        row(8, 'hayana', '中A😀　日本語한글' * 256),
        row(9, 'assistant', '大段回答x甲！' * 384),
    ]


def ref_signature(ref):
    return (ref.source_ref, ref.source_revision, ref.content_hash, ref.logical_size)


def turn_signature(turn):
    return (
        turn.turn_id,
        turn.branch_id,
        ref_signature(turn.user_input_ref),
        tuple(ref_signature(ref) for ref in turn.assistant_committed_output_refs),
        tuple(ref_signature(ref) for ref in turn.tool_outcome_refs),
        turn.source_revision,
        turn.started_at,
        turn.committed_at,
        turn.finality_status,
    )


_GOLDEN = (
    (
        'turn:1:3', 'message:3:branch:1',
        ('message:1', '5fc26e497ea4083f5bc54337605c4e29f5bdebb0b2f3d3a13b499390e26ed162', '5fc26e497ea4083f5bc54337605c4e29f5bdebb0b2f3d3a13b499390e26ed162', 68),
        (('message:3', 'cd5972ff16615d03fcc318b4e92539b717c72de5217acc7596ba0bb97d023b45', 'cd5972ff16615d03fcc318b4e92539b717c72de5217acc7596ba0bb97d023b45', 159),),
        (
            ('message:3:tool_outcome:0', 'afa19a3d1d4cd319b6593cdf7000ad219d71b5de634ab65d132bfb2b4c80bc3c', 'afa19a3d1d4cd319b6593cdf7000ad219d71b5de634ab65d132bfb2b4c80bc3c', 35),
            ('message:3:tool_outcome:1', 'b121f4f7ad6ba31bb19b575a90e1eb2bc22dee405a5163e9b8cca31fd34afb43', 'b121f4f7ad6ba31bb19b575a90e1eb2bc22dee405a5163e9b8cca31fd34afb43', 30),
        ),
        '6b2e9acc9f7cd3db6cbce4c9ff0930d82bb0ca7cb3ac28adeacfa02cbf9235c7',
        '2026-11-01 10:01:00', '2026-11-01 10:03:00', 'completed',
    ),
    (
        'turn:6:7', 'active-transcript',
        ('message:6', 'e37c69fb44194ba78e1a57bc568217511cec36c486a0c1639e33f76953bb31c5', 'e37c69fb44194ba78e1a57bc568217511cec36c486a0c1639e33f76953bb31c5', 50),
        (('message:7', '00f116ffde72c1e71be9cd17973d0a61afe758b8160ca976de044c366c7aa0e8', '00f116ffde72c1e71be9cd17973d0a61afe758b8160ca976de044c366c7aa0e8', 50),),
        (),
        '1e1b5fc1c3d92d402e5496fc9f75aeef32b30dab6a8e5e858bbc3129d818e8fd',
        '2026-11-01 10:06:00', '2026-11-01 10:07:00', 'completed',
    ),
    (
        'turn:8:9', 'active-transcript',
        ('message:8', 'f015dad4332061ca9d02226e25fcc76eddcd4b7947ce1f4de72ee2b61dbaa1f3', 'f015dad4332061ca9d02226e25fcc76eddcd4b7947ce1f4de72ee2b61dbaa1f3', 1964),
        (('message:9', '25ee8c752af3b8ffba496eba1ab4a02b92655c723a7873b65ba0425cc5dcb94e', '25ee8c752af3b8ffba496eba1ab4a02b92655c723a7873b65ba0425cc5dcb94e', 2444),),
        (),
        '294c70e9db64b4bc15a3b2dccca60e23ea0c04b35c3f2ad3ded28dcdbfd1ea32',
        '2026-11-01 10:08:00', '2026-11-01 10:09:00', 'completed',
    ),
)


class CanonicalTurnOptimizationTests(unittest.TestCase):
    def test_turn_fields_match_pre_optimization_golden(self):
        turns = sources.derive_completed_turns(optimization_rows())
        self.assertEqual(tuple(turn_signature(turn) for turn in turns), _GOLDEN)
        self.assertEqual([event.event_id for event in sources.derive_autonomous_events(optimization_rows())], ['wake:2'])

    def test_flush_reuses_evidence_branch_and_tool_results_once(self):
        rows = optimization_rows()
        with (
            mock.patch.object(sources, 'evidence_ref', wraps=sources.evidence_ref) as evidence,
            mock.patch.object(sources, 'row_revision', wraps=sources.row_revision) as row_revision,
            mock.patch.object(sources, '_tool_outcome_refs', wraps=sources._tool_outcome_refs) as tool_refs,
            mock.patch.object(sources, '_active_branch_identity', wraps=sources._active_branch_identity) as branch_ids,
        ):
            turns = sources.derive_completed_turns(rows)
        self.assertEqual(len(turns), 3)
        self.assertEqual(
            Counter(int(call.args[0]['id']) for call in evidence.call_args_list),
            Counter({1: 1, 3: 1, 6: 1, 7: 1, 8: 1, 9: 1}),
        )
        self.assertEqual(row_revision.call_count, 0)
        self.assertEqual(tool_refs.call_count, 3)
        self.assertEqual(branch_ids.call_count, 3)


if __name__ == '__main__':
    unittest.main()
