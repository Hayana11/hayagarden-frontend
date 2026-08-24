"""Tests for history assembly, rolling summary gating, and tool budgets."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from chat.context_continuity import format_tool_history
from chat.history_assembly import (
    apply_history_tool_budget,
    assemble_history_from_rows,
    collect_committed_full_file_refs,
    inject_rolling_summary_and_enforce_budget,
    strip_internal_metadata,
    trim_messages_to_text_budget,
)
from chat.history_legacy import assemble_legacy_history


def _row(author, content, **kwargs):
    base = {
        'id': kwargs.pop('id', 1),
        'author': author,
        'content': content,
        'image_url': '',
        'created_at': '2026-07-26 12:00:00',
        'tool_calls': '',
        'file_url': '',
        'file_name': '',
        'attachments': '[]',
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _estimate(text):
    return len(text or '')


class HistoryAssemblyTests(unittest.TestCase):
    def test_no_trim_no_rolling_flag_for_small_history(self):
        rows = [_row('hayana' if i % 2 == 0 else 'assistant', f'msg {i}', id=i + 1) for i in range(10)]
        msgs, stats = assemble_history_from_rows(
            rows,
            available_count=10,
            history_mode='legacy_block',
            static_dir='/tmp',
            read_file_fn=lambda *_a, **_k: None,
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
        )
        self.assertGreaterEqual(len(msgs), 5)
        self.assertFalse(stats.conversation_content_trimmed)

    def test_token_budget_trims_old_rows(self):
        rows = [_row('hayana' if i % 2 == 0 else 'assistant', 'x' * 500, id=i + 1) for i in range(80)]
        msgs, stats = assemble_history_from_rows(
            rows,
            available_count=80,
            history_token_budget=3000,
            history_mode='cc_token_budget',
            static_dir='/tmp',
            read_file_fn=lambda *_a, **_k: None,
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
        )
        self.assertTrue(stats.conversation_content_trimmed)
        self.assertGreater(stats.trimmed_up_to_id, 0)

    def test_contiguous_suffix_trim_no_timeline_holes(self):
        msgs = [
            {'role': 'user', 'content': 'A'},
            {'role': 'assistant', 'content': 'B'},
            {'role': 'user', 'content': 'C' * 500},
            {'role': 'assistant', 'content': 'D'},
            {'role': 'user', 'content': 'E'},
        ]
        out, trimmed, overflow, ov_tok = trim_messages_to_text_budget(
            msgs, budget=503, estimate_tokens=_estimate,
        )
        joined = json.dumps(out, ensure_ascii=False)
        self.assertTrue(trimmed)
        self.assertIn('E', joined)
        self.assertIn('D', joined)
        self.assertNotIn('"A"', joined)
        self.assertIn('"B"', joined)
        self.assertIn('"E"', joined)

    def test_budget_overflow_when_latest_user_exceeds_budget(self):
        msgs = [{'role': 'user', 'content': 'E' * 100}]
        out, trimmed, overflow, ov_tok = trim_messages_to_text_budget(
            msgs, budget=10, estimate_tokens=_estimate,
        )
        self.assertTrue(overflow)
        self.assertGreater(ov_tok, 0)
        self.assertEqual(len(out), 1)

    def test_real_file_wrapper_commits_ref_without_url_in_text(self):
        body = 'hello file'
        from chat.context_budget import file_content_sha256, file_ref_key
        url = '/static/a.txt'
        ref = file_ref_key(url, file_content_sha256(body))
        rows = [_row('hayana', 'see file', id=5, file_url=url, file_name='a.txt')]
        msgs, stats = assemble_history_from_rows(
            rows,
            available_count=1,
            history_mode='legacy_block',
            static_dir='/tmp',
            read_file_fn=lambda _static, u: body if u == url else None,
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
        )
        from chat.history_assembly import flatten_message_content
        visible = flatten_message_content(msgs[0]['content'])
        self.assertIn('[用户发来文件: a.txt]', visible)
        self.assertNotIn('/static/a.txt', visible)
        self.assertIn(ref, stats.committed_full_file_refs)

    def test_multi_attachments_inject_text_only_and_never_read_word_pdf(self):
        text_url = '/static/uploads/files/abcd1234_readme.md'
        pdf_url = '/static/uploads/files/abcd1234_plan.pdf'
        docx_url = '/static/uploads/files/abcd1234_notes.docx'
        rows = [_row(
            'hayana',
            '[附件: readme.md、plan.pdf、notes.docx]',
            id=5,
            attachments=json.dumps([
                {'type': 'file', 'url': text_url, 'name': 'readme.md'},
                {'type': 'file', 'url': pdf_url, 'name': 'plan.pdf'},
                {'type': 'file', 'url': docx_url, 'name': 'notes.docx'},
            ]),
        )]
        read_urls = []
        def read_file(_static, url):
            read_urls.append(url)
            return {'%s' % text_url: 'SAFE TEXT', '%s' % pdf_url: 'PDF BYTES', '%s' % docx_url: 'DOCX BYTES'}[url]

        msgs, stats = assemble_history_from_rows(
            rows,
            available_count=1,
            history_mode='legacy_block',
            static_dir='/tmp',
            read_file_fn=read_file,
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
        )
        visible = json.dumps(msgs, ensure_ascii=False)
        self.assertEqual(read_urls, [text_url])
        self.assertIn('SAFE TEXT', visible)
        self.assertNotIn('PDF BYTES', visible)
        self.assertNotIn('DOCX BYTES', visible)
        self.assertEqual([item['url'] for item in stats.file_injections], [text_url])

    def test_legacy_multi_attachments_inject_text_only(self):
        text_url = '/static/uploads/files/abcd1234_readme.md'
        pdf_url = '/static/uploads/files/abcd1234_plan.pdf'
        rows = [_row(
            'hayana',
            'files',
            id=5,
            attachments=json.dumps([
                {'type': 'file', 'url': text_url, 'name': 'readme.md'},
                {'type': 'file', 'url': pdf_url, 'name': 'plan.pdf'},
            ]),
        )]
        read_urls = []
        with mock.patch('chat.history_legacy.persist_history_boundary'):
            msgs, _stats = assemble_legacy_history(
                rows,
                available_count=1,
                static_dir='/tmp',
                read_file_fn=lambda _static, url: read_urls.append(url) or 'SAFE TEXT',
                img_block_fn=lambda *_a, **_k: None,
                format_tool_history_fn=lambda _raw: '',
                is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
            )
        visible = json.dumps(msgs, ensure_ascii=False)
        self.assertEqual(read_urls, [text_url])
        self.assertIn('SAFE TEXT', visible)
        self.assertNotIn('plan.pdf', visible)

    def test_trimmed_file_block_not_committed(self):
        body = 'x' * 800
        from chat.context_budget import file_content_sha256, file_ref_key
        url = '/static/a.txt'
        ref = file_ref_key(url, file_content_sha256(body))
        rows = [
            _row('hayana', 'old', id=1),
            _row('assistant', 'mid', id=2),
            _row('hayana', 'latest', id=3, file_url=url, file_name='a.txt'),
        ]
        msgs, stats = assemble_history_from_rows(
            rows,
            available_count=3,
            history_token_budget=200,
            history_mode='cc_token_budget',
            static_dir='/tmp',
            read_file_fn=lambda _static, u: body if u == url else None,
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
            estimate_tokens=_estimate,
        )
        if ref in stats.committed_full_file_refs:
            self.assertIn('latest', json.dumps(msgs, ensure_ascii=False))
        else:
            self.assertNotIn(ref, stats.committed_full_file_refs)

    def test_resident_summary_does_not_commit_full_ref(self):
        body = 'full file body'
        from chat.context_budget import file_content_sha256, file_ref_key
        url = '/static/a.txt'
        ref = file_ref_key(url, file_content_sha256(body))
        rows = [_row('hayana', 'see file', id=1, file_url=url, file_name='a.txt')]
        msgs, stats = assemble_history_from_rows(
            rows,
            available_count=1,
            history_mode='legacy_block',
            static_dir='/tmp',
            read_file_fn=lambda _static, u: body if u == url else None,
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
            resident_file_hashes={ref},
        )
        self.assertNotIn(ref, stats.committed_full_file_refs)
        from chat.history_assembly import flatten_message_content
        visible = flatten_message_content(msgs[0]['content'])
        self.assertIn('此前 resident 已全文注入', visible)
        self.assertNotIn('[文件: a.txt]', visible)

    def test_old_file_keeps_marker_without_full_body(self):
        url = '/static/uploads/files/abc_design.md'
        rows = [_row('hayana', '爸爸看看这个', id=1, file_url=url, file_name='design.md')]
        rows.extend(_row('assistant' if i % 2 else 'hayana', f'later {i}', id=i + 2) for i in range(7))
        msgs, stats = assemble_history_from_rows(
            rows,
            available_count=len(rows),
            history_mode='legacy_block',
            static_dir='/tmp',
            read_file_fn=lambda _static, u: 'SECRET BODY' if u == url else None,
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
        )
        from chat.history_assembly import flatten_message_content
        visible = '\n'.join(flatten_message_content(m['content']) for m in msgs)
        self.assertIn('爸爸看看这个', visible)
        self.assertIn('[文件: design.md]', visible)
        self.assertNotIn('SECRET BODY', visible)
        self.assertIn('marker_only', [item['mode'] for item in stats.file_injections])

    def test_file_only_existing_marker_not_duplicated(self):
        url = '/static/uploads/files/abc_design.md'
        rows = [_row('hayana', '[文件:design.md]', id=1, file_url=url, file_name='design.md')]
        rows.extend(_row('assistant' if i % 2 else 'hayana', f'later {i}', id=i + 2) for i in range(7))
        msgs, _stats = assemble_history_from_rows(
            rows,
            available_count=len(rows),
            history_mode='legacy_block',
            static_dir='/tmp',
            read_file_fn=lambda *_a, **_k: 'SECRET BODY',
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
        )
        from chat.history_assembly import flatten_message_content
        visible = '\n'.join(flatten_message_content(m['content']) for m in msgs)
        self.assertEqual(visible.count('[文件:design.md]'), 1)
        self.assertNotIn('[文件: design.md]', visible)

    def test_legacy_old_file_keeps_same_marker_semantics(self):
        url = '/static/uploads/files/abc_design.md'
        rows = [_row('hayana', '爸爸看看这个', id=1, file_url=url, file_name='design.md')]
        rows.extend(_row('assistant' if i % 2 else 'hayana', f'later {i}', id=i + 2) for i in range(7))
        with mock.patch('chat.history_legacy.persist_history_boundary'):
            msgs, _stats = assemble_legacy_history(
                rows,
                available_count=len(rows),
                static_dir='/tmp',
                read_file_fn=lambda _static, u: 'SECRET BODY' if u == url else None,
                img_block_fn=lambda *_a, **_k: None,
                format_tool_history_fn=lambda _raw: '',
                is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
            )
        visible = json.dumps(msgs, ensure_ascii=False)
        self.assertIn('[文件: design.md]', visible)
        self.assertNotIn('SECRET BODY', visible)

    def test_strip_internal_metadata_before_provider(self):
        msgs = [{
            'role': 'user',
            'content': [{
                'type': 'text',
                'text': '[用户发来文件: a.txt]\n```\nx\n```',
                '_hg_meta': {'kind': 'file', 'mode': 'full', 'ref_key': 'k'},
            }],
            '_hg_row_ids': [1],
        }]
        clean = strip_internal_metadata(msgs)
        self.assertNotIn('_hg_meta', json.dumps(clean))
        self.assertNotIn('_hg_row_ids', json.dumps(clean))
        self.assertEqual(collect_committed_full_file_refs(msgs), {'k'})

    def test_rolling_summary_injected_and_within_budget(self):
        msgs = [
            {'role': 'user', 'content': 'old ' + 'x' * 2000, '_hg_row_ids': [1]},
            {'role': 'assistant', 'content': 'reply ' + 'y' * 2000, '_hg_row_ids': [2]},
            {'role': 'user', 'content': 'latest user', '_hg_row_ids': [3]},
        ]
        summary = 'summary ' + 'z' * 500
        out, tokens, _, overflow, _ = inject_rolling_summary_and_enforce_budget(
            msgs, rolling_summary=summary, budget=3500, estimate_tokens=_estimate,
        )
        self.assertLessEqual(tokens, 3500)
        self.assertIn('latest user', json.dumps(out, ensure_ascii=False))

    def test_tool_only_trim_sets_tool_history_flag_in_assembly(self):
        calls = json.dumps([
            {'name': 'web_search', 'args': {}, 'result': 'a' * 20000, 'success': True},
            {'name': 'web_search', 'args': {}, 'result': 'b' * 20000, 'success': True},
        ])
        rows = [
            _row('assistant', 'reply', id=1, tool_calls=calls),
            _row('hayana', 'latest user', id=2),
        ]
        with mock.patch('chat.history_assembly._tool_caps', return_value=(2000, 8000, 12000, 3000)):
            msgs, stats = assemble_history_from_rows(
                rows,
                available_count=2,
                history_mode='legacy_block',
                static_dir='/tmp',
                read_file_fn=lambda *_a, **_k: None,
                img_block_fn=lambda *_a, **_k: None,
                is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
                apply_tool_budget=True,
            )
        self.assertTrue(stats.tool_history_trimmed)
        self.assertFalse(stats.conversation_content_trimmed)


if __name__ == '__main__':
    unittest.main()
