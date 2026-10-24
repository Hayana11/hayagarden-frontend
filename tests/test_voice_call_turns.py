"""Call turns stay in the chat history but fold away on the chat page."""

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from voice_call import turn_messages  # noqa: E402
from voice_call.brain import ChatPipelineBrain  # noqa: E402
from voice_call.chat_record import record_call_in_chat  # noqa: E402


class FoldTests(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT,"
            " source_kind TEXT NOT NULL DEFAULT 'chat')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db)

    def add(self, author, content, kind='chat'):
        conn = sqlite3.connect(self.db)
        cur = conn.execute('INSERT INTO chat_messages (author, content, source_kind) VALUES (?, ?, ?)',
                           (author, content, kind))
        conn.commit()
        conn.close()
        return cur.lastrowid

    def her_call_turn(self, call_id, text):
        mid = self.add('hayana', '〔电话〕她说：「%s」' % text)
        turn_messages.mark_turn(self.db, call_id, mid)
        return mid

    def record(self, call_id, text='ᯅ 通话时长 00:30'):
        return record_call_in_chat(self.db, {'record': text, 'call': {'id': call_id}})

    def page(self, after=None, before=None, limit=50):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        sql, args = 'SELECT * FROM chat_messages', ()
        if after is not None:
            sql, args = sql + ' WHERE id > ?', (after,)
        elif before is not None:
            sql, args = sql + ' WHERE id < ?', (before,)
        rows = [dict(r) for r in conn.execute(sql + ' ORDER BY id LIMIT ?', args + (limit,))]
        try:
            return turn_messages.fold_call_turns(conn, rows)
        finally:
            conn.close()

    def texts(self, rows):
        return [r['content'] for r in rows]

    def test_a_call_shows_as_its_record_only(self):
        self.add('hayana', '晚上好')
        self.add('fyodor', '晚上好，小猫')
        self.her_call_turn('c1', '在吗')
        self.add('fyodor', '在。')
        self.add('fyodor', '想你了。')  # a turn can have more than one reply row
        self.her_call_turn('c1', '我也是')
        self.add('fyodor', '那就多说一会儿。')
        rec = self.record('c1')
        self.add('hayana', '刚才挂得太快了')
        rows = self.page()
        self.assertEqual(self.texts(rows), ['晚上好', '晚上好，小猫', 'ᯅ 通话时长 00:30', '刚才挂得太快了'])
        tagged = [r for r in rows if r['id'] == rec][0]
        self.assertEqual((tagged['call_id'], tagged['call_part']), ('c1', 'record'))
        self.assertNotIn('call_id', rows[0])

    def test_rows_are_unchanged_in_the_database(self):
        self.her_call_turn('c1', '在吗')
        self.add('fyodor', '在。')
        conn = sqlite3.connect(self.db)
        count = conn.execute('SELECT COUNT(*) FROM chat_messages').fetchone()[0]
        conn.close()
        self.assertEqual(count, 2, 'the model still reads every call turn')
        self.assertEqual(self.page(), [])

    def test_a_page_opening_mid_turn_still_folds_its_replies(self):
        self.add('hayana', '早')
        turn = self.her_call_turn('c1', '在吗')
        self.add('fyodor', '在。')
        self.add('fyodor', '刚醒？')
        self.record('c1')
        rows = self.page(after=turn)
        self.assertEqual(self.texts(rows), ['ᯅ 通话时长 00:30'])

    def test_a_page_opening_after_her_normal_message_shows_replies(self):
        self.her_call_turn('c1', '在吗')
        self.add('fyodor', '在。')
        self.record('c1')
        normal = self.add('hayana', '打字回你')
        self.add('fyodor', '看到了')
        rows = self.page(after=normal)
        self.assertEqual(self.texts(rows), ['看到了'])

    def test_her_typed_message_ends_a_turn(self):
        self.her_call_turn('c1', '在吗')
        self.add('fyodor', '在。')
        self.add('hayana', '（打字）信号不好')
        self.add('fyodor', '那挂了打字说')
        self.assertEqual(self.texts(self.page()), ['（打字）信号不好', '那挂了打字说'])

    def test_wake_messages_are_never_folded(self):
        self.her_call_turn('c1', '在吗')
        self.add('fyodor', '在。')
        self.add('fyodor', '（Wake）睡了吗', kind='wake')
        self.add('fyodor', '这条不该被藏')
        self.assertEqual(self.texts(self.page()), ['（Wake）睡了吗', '这条不该被藏'])

    def test_his_message_right_after_the_record_is_shown(self):
        self.her_call_turn('c1', '在吗')
        self.add('fyodor', '在。')
        self.record('c1')
        self.add('fyodor', '挂了电话还想跟你说一句')
        self.assertEqual(self.texts(self.page()), ['ᯅ 通话时长 00:30', '挂了电话还想跟你说一句'])

    def test_two_calls_fold_independently(self):
        self.her_call_turn('c1', '一')
        self.add('fyodor', '一回')
        self.record('c1', 'ᯅ 通话时长 00:10')
        self.add('hayana', '中间')
        self.her_call_turn('c2', '二')
        self.add('fyodor', '二回')
        self.record('c2', 'ᯅ 通话时长 00:20')
        rows = self.page()
        self.assertEqual(self.texts(rows), ['ᯅ 通话时长 00:10', '中间', 'ᯅ 通话时长 00:20'])
        self.assertEqual([r.get('call_id') for r in rows], ['c1', None, 'c2'])

    def test_the_record_says_who_placed_the_call(self):
        from voice_call import store as call_store
        store = call_store.CallStore(self.db, lambda: 1_800_000_000.0)
        store.create_if_none_open('mine')
        store.transition('mine', call_store.CANCELED)
        store.create_if_none_open('his', caller='fyodor', opening='在吗')
        self.record('mine', 'ᯅ 对方已取消')
        self.record('his', 'ᯅ 通话时长 00:05')
        self.record('unknown', 'ᯅ 未接听')
        self.assertEqual([r['call_caller'] for r in self.page()], ['her', 'fyodor', 'her'])

    def test_no_call_tables_leaves_rows_alone(self):
        self.add('hayana', 'a')
        self.add('fyodor', 'b')
        self.assertEqual(self.texts(self.page()), ['a', 'b'])

    def test_mark_turn_is_idempotent_and_ignores_missing_ids(self):
        mid = self.add('hayana', 'x')
        turn_messages.mark_turn(self.db, 'c1', mid)
        turn_messages.mark_turn(self.db, 'c1', mid)
        turn_messages.mark_turn(self.db, 'c1', None)
        turn_messages.mark_turn(self.db, '', mid)
        conn = sqlite3.connect(self.db)
        rows = conn.execute('SELECT * FROM %s' % turn_messages.TABLE).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)


class BrainHandleTests(unittest.TestCase):
    def test_the_reply_handle_carries_her_message_id(self):
        brain = ChatPipelineBrain(send_message=lambda content: 42, stream_reply=lambda mid: iter(['好']))
        handle = brain.start_reply('在吗', False)
        self.assertEqual(handle.message_id, 42)
        self.assertEqual(list(handle), ['好'])


class ServiceTurnTests(unittest.TestCase):
    """Each call turn's chat message is handed to on_turn; a failure there never hurts the call."""

    def make(self, on_turn):
        from tests.test_voice_call import FakeEngine
        from voice_call import store as call_store
        from voice_call.brain import CancellableReply
        from voice_call.service import CallService
        from voice_call.tts import CachedTTS

        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(os.unlink, path)
        self.addCleanup(tmp.cleanup)
        ids = iter(range(100, 200))

        class Brain:
            def start_reply(self, text, first_turn, **extra):
                return CancellableReply(iter(['嗯。\n']), message_id=next(ids))

        service = CallService(call_store.CallStore(path, time.time), CachedTTS(FakeEngine(), tmp.name),
                              Brain(), on_turn=on_turn)
        call = service.ring()
        service.answer(call['id'])
        self.addCleanup(service.hangup, call['id'])
        return service, call['id']

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail('condition did not become true')

    def test_each_turn_is_reported_with_its_message_id(self):
        seen = []
        service, call_id = self.make(lambda cid, mid: seen.append((cid, mid)))
        service.say(call_id, '在吗')
        self.wait_for(lambda: len(seen) == 1)
        service.say(call_id, '还在吗')
        self.wait_for(lambda: len(seen) == 2)
        self.assertEqual(seen, [(call_id, 100), (call_id, 101)])

    def test_a_failing_on_turn_does_not_stop_the_reply(self):
        def boom(cid, mid):
            raise sqlite3.OperationalError('database is locked')
        service, call_id = self.make(boom)
        service.say(call_id, '在吗')
        self.wait_for(lambda: any(l['text'] == '嗯。' for l in service.lines_after(call_id, 0)[0]))


class PhoneStyleTests(unittest.TestCase):
    def test_runtime_config_replaces_the_call_prompt(self):
        from unittest import mock
        from voice_call import brain
        with mock.patch('config_store.get', return_value='  打电话时就像在耳边说话。  '):
            first = brain.format_utterance('喂', True)
        self.assertTrue(first.startswith('打电话时就像在耳边说话。'))
        self.assertNotIn(brain.PHONE_STYLE, first)
        self.assertIn('〔电话〕她刚在电话里说：「喂」', first)

    def test_empty_or_unreadable_config_falls_back_to_the_default(self):
        from unittest import mock
        from voice_call import brain
        with mock.patch('config_store.get', return_value=''):
            self.assertTrue(brain.format_utterance('喂', True).startswith(brain.PHONE_STYLE))
        with mock.patch('config_store.get', side_effect=RuntimeError('db locked')):
            self.assertTrue(brain.format_utterance('喂', True).startswith(brain.PHONE_STYLE))

    def test_the_default_still_forbids_what_tts_would_read_aloud(self):
        from voice_call import brain
        for word in ('括号', '表情', '列表', 'Markdown', '两三句'):
            self.assertIn(word, brain.PHONE_STYLE)


class ChatRouteSourceTests(unittest.TestCase):
    def test_route_folds_and_never_fails_because_of_it(self):
        src = Path(ROOT, 'app.py').read_text(encoding='utf-8')
        start = src.index("@app.route('/api/chat/messages', methods=['GET'])")
        body = src[start:src.index('@app.route', start + 10)]
        self.assertIn('fold_call_turns(conn, rows)', body)
        fold = body.index('fold_call_turns(conn, rows)')
        self.assertIn('except Exception', body[fold:])
        self.assertIn('on_turn=lambda call_id, message_id: mark_turn(DB_PATH, call_id, message_id)', src)


if __name__ == '__main__':
    unittest.main()
