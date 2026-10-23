import json
import os
import sys
import tempfile
import threading
import time
import unittest

from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from moments_auth import OwnerAuthError
from voice_call import store as call_store
from voice_call.chat_record import record_call_in_chat
from voice_call.brain import BrainError, ChatPipelineBrain, format_utterance, parse_sse_text, PHONE_STYLE
from voice_call.sentences import SentenceSplitter
from voice_call.service import MAX_CLIP_BYTES, STALE_UTTERANCE_S, CallService, format_duration
from voice_call.stt import ElevenLabsSTT, STTError
from voice_call.tts import CachedTTS, ElevenLabsTTS, TTSError, read_env_value
from voice_call_routes import create_voice_call_blueprint


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeEngine:
    voice_id = 'voice'
    model_id = 'model'

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def synthesize(self, text):
        self.calls.append(text)
        if self.fail:
            raise TTSError('boom')
        return ('mp3:' + text).encode('utf-8')


class FakeBrain:
    def __init__(self, chunks=None, error=None):
        self.chunks = chunks or ['你好。\n', '我在', '听。\n']
        self.error = error
        self.calls = []
        self.during = None

    def reply(self, text, first_turn):
        self.calls.append((text, first_turn))
        for index, chunk in enumerate(self.chunks):
            if self.during:
                self.during(index)
            yield chunk
        if self.error:
            raise self.error


def run_now(target):
    target()


class SentenceSplitterTests(unittest.TestCase):
    def test_newlines_are_boundaries_across_chunks(self):
        splitter = SentenceSplitter()
        self.assertEqual(splitter.feed('早'), [])
        self.assertEqual(splitter.feed('点睡。\n还'), ['早点睡。'])
        self.assertEqual(splitter.feed('不睡吗\n\n'), ['还不睡吗'])
        self.assertEqual(splitter.flush(), [])

    def test_long_line_is_cut_after_sentence_mark(self):
        splitter = SentenceSplitter(soft_limit=10)
        out = splitter.feed('第一句话说完了。第二句还在继续说着没有')
        self.assertEqual(out, ['第一句话说完了。'])
        self.assertEqual(splitter.flush(), ['第二句还在继续说着没有'])

    def test_long_line_without_marks_waits(self):
        splitter = SentenceSplitter(soft_limit=5)
        self.assertEqual(splitter.feed('一二三四五六七八'), [])
        self.assertEqual(splitter.flush(), ['一二三四五六七八'])


class TTSTests(unittest.TestCase):
    def test_cache_synthesizes_each_sentence_once(self):
        engine = FakeEngine()
        with tempfile.TemporaryDirectory() as tmp:
            cache = CachedTTS(engine, tmp)
            key1, audio1 = cache.get('晚安')
            key2, audio2 = cache.get('晚安')
            self.assertEqual(engine.calls, ['晚安'])
            self.assertEqual((key1, audio1), (key2, audio2))
            self.assertTrue(os.path.exists(cache.path(key1)))
            self.assertEqual([n for n in os.listdir(tmp) if n.endswith('.part')], [])

    def test_cache_key_depends_on_voice_model_and_text(self):
        engine = FakeEngine()
        cache = CachedTTS(engine, '/nonexistent')
        base = cache.key('晚安')
        self.assertNotEqual(base, cache.key('晚安。'))
        engine.voice_id = 'other'
        self.assertNotEqual(base, cache.key('晚安'))

    def test_failed_synthesis_is_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = CachedTTS(FakeEngine(fail=True), tmp)
            with self.assertRaises(TTSError):
                cache.get('晚安')
            self.assertEqual(os.listdir(tmp), [])

    def test_elevenlabs_request_shape(self):
        seen = {}

        def post(url, headers, body, timeout):
            seen.update(url=url, headers=headers, body=json.loads(body), timeout=timeout)
            return 200, b'mp3'

        tts = ElevenLabsTTS(api_key='k', post=post)
        self.assertEqual(tts.synthesize('你好'), b'mp3')
        self.assertIn('/text-to-speech/TpHHy93T39MKuyQgrXh8?', seen['url'])
        self.assertEqual(seen['headers']['xi-api-key'], 'k')
        self.assertEqual(seen['body']['model_id'], 'eleven_v3')
        self.assertEqual(seen['body']['voice_settings']['stability'], 0.5)
        self.assertEqual(seen['body']['text'], '你好')

    def test_elevenlabs_errors(self):
        with self.assertRaises(TTSError):
            ElevenLabsTTS(api_key='', post=lambda *a: (200, b'x')).synthesize('a')
        with self.assertRaises(TTSError):
            ElevenLabsTTS(api_key='k', post=lambda *a: (401, b'no')).synthesize('a')
        with self.assertRaises(TTSError):
            ElevenLabsTTS(api_key='k', post=lambda *a: (200, b'')).synthesize('a')

        def explode(*args):
            raise OSError('net down')
        with self.assertRaises(TTSError):
            ElevenLabsTTS(api_key='k', post=explode).synthesize('a')

    def test_env_key_is_read_from_file(self):
        with tempfile.NamedTemporaryFile('w', delete=False, suffix='.env') as env:
            env.write('OTHER=1\nELEVENLABS_API_KEY="sk_test"\n')
        try:
            self.assertEqual(read_env_value('ELEVENLABS_API_KEY', env.name), 'sk_test')
            self.assertEqual(read_env_value('MISSING_KEY_X', env.name), '')
        finally:
            os.unlink(env.name)


class FakeSTT:
    def __init__(self, text='你在干嘛', error=None):
        self.text = text
        self.error = error
        self.calls = []

    def transcribe(self, audio, mime, language):
        self.calls.append((len(audio), mime, language))
        if self.error:
            raise self.error
        return self.text


class GatedSTT:
    """Each transcription blocks until the test releases that clip."""

    def __init__(self):
        self.gates = {}
        self.texts = {}
        self.errors = {}
        self.calls = []

    def expect(self, clip, text, error=None):
        self.gates[clip] = threading.Event()
        self.texts[clip] = text
        self.errors[clip] = error

    def release(self, clip):
        self.gates[clip].set()

    def transcribe(self, audio, mime, language):
        self.calls.append(audio)
        self.gates[audio].wait(5)
        if self.errors[audio]:
            raise self.errors[audio]
        return self.texts[audio]


class UtteranceOrderTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.brain = FakeBrain()
        self.stt = GatedSTT()
        self.service = CallService(
            call_store.CallStore(self.path, self.clock),
            CachedTTS(FakeEngine(), self.tmp.name),
            self.brain, clock=self.clock, spawn=run_now, stt=self.stt,
        )
        call = self.service.ring()
        self.service.answer(call['id'])
        self.call_id = call['id']
        self.results = {}

    def tearDown(self):
        os.unlink(self.path)
        self.tmp.cleanup()

    def hear_async(self, clip, utterance):
        def run():
            try:
                self.results[clip] = self.service.hear(self.call_id, clip, 'audio/wav', None, utterance)
            except Exception as exc:  # noqa: BLE001 - recorded for the assertion
                self.results[clip] = exc
        thread = threading.Thread(target=run)
        thread.start()
        return thread

    def her_lines(self):
        lines, _ = self.service.lines_after(self.call_id, 0)
        return [line['text'] for line in lines if line['speaker'] == 'her']

    def wait_started(self, number):
        for _ in range(500):
            queue = self.service._utterances.get(self.call_id)
            if queue and queue.started >= number:
                return
            time.sleep(0.01)
        self.fail('utterance %d never started' % number)

    def test_older_sentence_finishing_after_she_spoke_again_does_not_revive_a_reply(self):
        self.stt.expect(b'A', '你在吗')
        thread_a = self.hear_async(b'A', 1)
        self.wait_started(1)
        self.service.interrupt(self.call_id, utterance=2)  # B begins while A is in Scribe
        self.stt.release(b'A')
        thread_a.join(5)
        self.assertIsNone(self.results[b'A']['line'])
        self.assertEqual(self.her_lines(), [])
        self.assertEqual(self.brain.calls, [], 'A must not start a reply of its own')

        self.stt.expect(b'B', '我刚才在想你')
        self.stt.release(b'B')
        result = self.service.hear(self.call_id, b'B', 'audio/wav', None, 2)
        self.assertEqual(self.her_lines(), ['你在吗 我刚才在想你'])
        self.assertEqual(result['line']['text'], '你在吗 我刚才在想你')
        self.assertEqual([text for text, _ in self.brain.calls], ['你在吗 我刚才在想你'])

    def test_concurrent_sentences_keep_spoken_order_when_the_later_one_transcribes_first(self):
        self.stt.expect(b'A', '第一句')
        self.stt.expect(b'B', '第二句')
        thread_a = self.hear_async(b'A', 1)
        thread_b = self.hear_async(b'B', 2)
        self.wait_started(2)
        self.stt.release(b'B')
        thread_b.join(5)
        self.assertIsNone(self.results[b'B']['line'], 'B waits for A')
        self.assertEqual(self.her_lines(), [])
        self.stt.release(b'A')
        thread_a.join(5)
        self.assertEqual(self.her_lines(), ['第一句 第二句'])
        self.assertEqual([text for text, _ in self.brain.calls], ['第一句 第二句'])

    def test_failed_or_discarded_utterance_does_not_block_the_queue(self):
        self.stt.expect(b'A', '', error=STTError('stt http 500'))
        self.stt.expect(b'B', '还在吗')
        thread_a = self.hear_async(b'A', 1)
        thread_b = self.hear_async(b'B', 2)
        self.wait_started(2)
        self.stt.release(b'B')
        thread_b.join(5)
        self.stt.release(b'A')
        thread_a.join(5)
        self.assertIsInstance(self.results[b'A'], STTError)
        self.assertEqual(self.her_lines(), ['还在吗'])

        self.service.interrupt(self.call_id, utterance=3)
        self.assertEqual(self.service.hear(self.call_id, None, None, utterance=3)['text'], '')
        self.stt.expect(b'D', '好')
        self.stt.release(b'D')
        self.service.hear(self.call_id, b'D', 'audio/wav', None, 4)
        self.assertEqual(self.her_lines(), ['还在吗', '好'])

    def test_lost_utterance_stops_holding_the_queue_after_the_stale_window(self):
        self.service.interrupt(self.call_id, utterance=1)  # began, upload never arrives
        self.stt.expect(b'B', '喂')
        self.stt.release(b'B')
        self.assertIsNone(self.service.hear(self.call_id, b'B', 'audio/wav', None, 2)['line'])
        self.clock.now += STALE_UTTERANCE_S + 1
        self.stt.expect(b'C', '听得到吗')
        self.stt.release(b'C')
        self.service.hear(self.call_id, b'C', 'audio/wav', None, 3)
        self.assertEqual(self.her_lines(), ['喂 听得到吗'])

    def test_repeated_utterance_numbers_replay_idempotently(self):
        self.stt.expect(b'A', '嗯')
        self.stt.release(b'A')
        self.service.hear(self.call_id, b'A', 'audio/wav', None, 1)
        duplicate = self.service.hear(self.call_id, b'A', 'audio/wav', None, 1)
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(self.stt.calls, [b'A'])
        self.assertEqual(self.her_lines(), ['嗯'])

    def test_duplicate_in_flight_waits_and_replays_without_second_stt(self):
        self.stt.expect(b'A', '只处理一次')
        returned = []
        errors = []

        def hear_once():
            try:
                returned.append(self.service.hear(self.call_id, b'A', 'audio/wav', None, 1))
            except Exception as exc:  # noqa: BLE001 - assertion below
                errors.append(exc)

        first = threading.Thread(target=hear_once)
        duplicate = threading.Thread(target=hear_once)
        first.start()
        self.wait_started(1)
        duplicate.start()
        time.sleep(0.05)
        self.assertTrue(duplicate.is_alive(), 'duplicate must wait for the in-flight Scribe call')
        self.stt.release(b'A')
        first.join(5)
        duplicate.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(self.stt.calls, [b'A'])
        self.assertEqual(len(returned), 2)
        self.assertEqual(self.her_lines(), ['只处理一次'])
        self.assertTrue(any(result.get('duplicate') for result in returned))

    def test_without_numbers_arrival_order_is_used(self):
        self.stt.expect(b'A', '一')
        self.stt.release(b'A')
        self.stt.expect(b'B', '二')
        self.stt.release(b'B')
        self.assertEqual(self.service.hear(self.call_id, b'A', 'audio/wav')['utterance'], 1)
        self.assertEqual(self.service.hear(self.call_id, b'B', 'audio/wav')['utterance'], 2)
        self.assertEqual(self.her_lines(), ['一', '二'])


class STTTests(unittest.TestCase):
    def test_scribe_request_shape(self):
        seen = {}

        def post(url, headers, body, timeout):
            seen.update(url=url, headers=headers, body=body)
            return 200, json.dumps({'text': ' 小猫今天有没有好好吃饭？ ', 'language_code': 'zho'}).encode()

        stt = ElevenLabsSTT(api_key='k', post=post)
        self.assertEqual(stt.transcribe(b'RIFFdata', 'audio/wav', 'zho'), '小猫今天有没有好好吃饭？')
        self.assertEqual(seen['url'], 'https://api.elevenlabs.io/v1/speech-to-text')
        self.assertEqual(seen['headers']['xi-api-key'], 'k')
        self.assertTrue(seen['headers']['content-type'].startswith('multipart/form-data; boundary='))
        for part in (b'name="model_id"\r\n\r\nscribe_v2', b'name="language_code"\r\n\r\nzho',
                     b'name="tag_audio_events"\r\n\r\nfalse', b'filename="clip.wav"', b'RIFFdata'):
            self.assertIn(part, seen['body'])

    def test_unknown_language_falls_back_to_chinese(self):
        seen = {}

        def post(url, headers, body, timeout):
            seen['body'] = body
            return 200, b'{"text": "hi"}'

        ElevenLabsSTT(api_key='k', post=post).transcribe(b'x', 'audio/wav', 'klingon')
        self.assertIn(b'name="language_code"\r\n\r\nzho', seen['body'])

    def test_scribe_errors(self):
        with self.assertRaises(STTError):
            ElevenLabsSTT(api_key='', post=lambda *a: (200, b'{}')).transcribe(b'x')
        with self.assertRaises(STTError):
            ElevenLabsSTT(api_key='k', post=lambda *a: (200, b'{}')).transcribe(b'x', 'video/avi')
        with self.assertRaises(STTError):
            ElevenLabsSTT(api_key='k', post=lambda *a: (401, b'no')).transcribe(b'x')
        with self.assertRaises(STTError):
            ElevenLabsSTT(api_key='k', post=lambda *a: (200, b'<html>')).transcribe(b'x')

        def explode(*args):
            raise OSError('net down')
        with self.assertRaises(STTError):
            ElevenLabsSTT(api_key='k', post=explode).transcribe(b'x')


class StoreTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.clock = Clock()
        self.store = call_store.CallStore(self.path, self.clock)

    def tearDown(self):
        os.unlink(self.path)

    def test_ring_answer_end(self):
        self.store.create_if_none_open('c1')
        self.assertEqual(self.store.transition('c1', call_store.ACTIVE)['state'], 'active')
        self.assertEqual(self.store.transition('c1', call_store.ENDED)['state'], 'ended')

    def test_illegal_transitions_are_refused(self):
        self.store.create_if_none_open('c1')
        with self.assertRaises(call_store.CallStateError):
            self.store.transition('c1', call_store.ENDED)
        with self.assertRaises(call_store.CallStateError) as ctx:
            self.store.transition('nope', call_store.ACTIVE)
        self.assertEqual(ctx.exception.status, 404)

    def test_unanswered_call_becomes_missed_after_timeout(self):
        self.store.create_if_none_open('c1')
        self.clock.now += 59
        self.assertEqual(self.store.get('c1')['state'], 'ringing')
        self.clock.now += 2
        call = self.store.get('c1')
        self.assertEqual(call['state'], 'missed')
        self.assertEqual(call['ended_at'], 1060.0)
        self.assertIsNone(self.store.latest_open())
        with self.assertRaises(call_store.CallStateError):
            self.store.transition('c1', call_store.ACTIVE)

    def test_concurrent_rings_create_at_most_one_call(self):
        import sqlite3
        import threading
        for round_no in range(20):
            fd, path = tempfile.mkstemp(suffix='.db')
            os.close(fd)
            try:
                # Separate store objects = separate in-process locks, like separate workers.
                stores = [call_store.CallStore(path, self.clock) for _ in range(8)]
                barrier = threading.Barrier(len(stores))
                created, refused, errors = [], [], []

                def ring(store, n):
                    try:
                        barrier.wait()
                        created.append(store.create_if_none_open('c%d' % n))
                    except call_store.CallStateError:
                        refused.append(n)
                    except Exception as exc:  # pragma: no cover - reported below
                        errors.append(repr(exc))

                threads = [threading.Thread(target=ring, args=(st, i)) for i, st in enumerate(stores)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(30)
                self.assertEqual(errors, [])
                self.assertEqual(len(created), 1, 'round %d created %d calls' % (round_no, len(created)))
                self.assertEqual(len(refused), len(stores) - 1)
                conn = sqlite3.connect(path)
                open_rows = conn.execute(
                    "SELECT COUNT(*) FROM voice_calls WHERE state IN ('ringing','active')").fetchone()[0]
                conn.close()
                self.assertEqual(open_rows, 1)
            finally:
                os.unlink(path)

    def test_database_refuses_a_second_open_call_even_bypassing_the_store(self):
        import sqlite3
        self.store.create_if_none_open('c1')
        conn = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO voice_calls (id, state, created_at) VALUES ('c2', 'ringing', 0)")
        finally:
            conn.close()

    def test_new_call_allowed_once_the_open_one_expired(self):
        self.store.create_if_none_open('c1')
        with self.assertRaises(call_store.CallStateError):
            self.store.create_if_none_open('c2')
        self.clock.now += call_store.RING_TIMEOUT_S + 1
        self.assertEqual(self.store.create_if_none_open('c2')['state'], 'ringing')

    def test_lines_are_sequenced_per_call(self):
        self.store.create_if_none_open('c1')
        self.store.add_line('c1', 'her', 'a')
        self.store.add_line('c1', 'him', 'b')
        self.assertEqual([l['seq'] for l in self.store.lines_after('c1', 0)], [1, 2])
        self.assertEqual([l['text'] for l in self.store.lines_after('c1', 1)], ['b'])


class BrainTests(unittest.TestCase):
    def test_utterance_carries_phone_tag_and_style_on_first_turn(self):
        first = format_utterance(' 今天好累啊 ', True)
        self.assertTrue(first.startswith(PHONE_STYLE))
        self.assertIn('〔电话〕她刚在电话里说：「今天好累啊」', first)
        self.assertNotIn(PHONE_STYLE, format_utterance('嗯', False))

    def test_sse_parsing(self):
        body = [
            b'data: {"t":"ping"}\n', b'\n',
            b'data: {"t":"think","d":"hmm"}\n',
            b'data: {"t":"text","d":"\\u4f60"}\n',
            'data: {"t":"text","d":"好"}\n',
            b'data: {"t":"done","ok":true}\n',
            b'data: {"t":"text","d":"after done"}\n',
        ]
        self.assertEqual(''.join(parse_sse_text(body)), '你好')
        with self.assertRaises(BrainError):
            list(parse_sse_text([b'data: {"t":"err","d":"busy"}\n']))

    def test_pipeline_brain_sends_then_streams(self):
        sent = []
        brain = ChatPipelineBrain(
            send_message=lambda content: sent.append(content) or 42,
            stream_reply=lambda message_id: iter(['id=%s' % message_id]),
        )
        self.assertEqual(list(brain.reply('喂', False)), ['id=42'])
        self.assertEqual(sent, ['〔电话〕她刚在电话里说：「喂」'])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.engine = FakeEngine()
        self.brain = FakeBrain()
        self.ended = []
        self.service = CallService(
            call_store.CallStore(self.path, self.clock),
            CachedTTS(self.engine, self.tmp.name),
            self.brain, clock=self.clock, spawn=run_now, on_end=self.ended.append,
        )

    def tearDown(self):
        os.unlink(self.path)
        self.tmp.cleanup()

    def active_call(self):
        call = self.service.ring()
        self.service.answer(call['id'])
        return call['id']

    def test_full_call(self):
        call_id = self.active_call()
        self.service.say(call_id, '今天好累啊')
        lines, call = self.service.lines_after(call_id, 0)
        self.assertEqual([(l['speaker'], l['text']) for l in lines],
                         [('her', '今天好累啊'), ('him', '你好。'), ('him', '我在听。')])
        self.assertEqual(self.brain.calls, [('今天好累啊', True)])
        self.service.say(call_id, '嗯')
        self.assertEqual(self.brain.calls[-1], ('嗯', False))

        self.assertEqual(self.service.audio(call_id, 2), 'mp3:你好。'.encode('utf-8'))
        self.service.audio(call_id, 2)
        self.assertEqual(self.engine.calls, ['你好。'])
        with self.assertRaises(call_store.CallStateError):
            self.service.audio(call_id, 1)  # her own line has no audio

        self.clock.now += 201
        summary = self.service.hangup(call_id)
        self.assertEqual(summary['record'], 'ᯅ 通话时长 03:21')
        self.assertEqual(self.ended, [summary])
        self.assertEqual(self.service.hangup(call_id)['record'], 'ᯅ 通话时长 03:21')
        self.assertEqual(len(self.ended), 1)

    def test_only_one_open_call(self):
        self.service.ring()
        with self.assertRaises(call_store.CallStateError):
            self.service.ring()

    def test_say_requires_active_call(self):
        call = self.service.ring()
        with self.assertRaises(call_store.CallStateError):
            self.service.say(call['id'], '喂')
        call_id = call['id']
        self.service.answer(call_id)
        with self.assertRaises(call_store.CallStateError):
            self.service.say(call_id, '   ')

    def test_interrupt_drops_rest_of_answer(self):
        call_id = self.active_call()
        self.brain.chunks = ['第一句。\n', '第二句。\n', '第三句。\n']
        self.brain.during = lambda index: self.service.interrupt(call_id) if index == 1 else None
        self.service.say(call_id, '等一下')
        texts = [l['text'] for l in self.service.lines_after(call_id, 0)[0]]
        self.assertEqual(texts, ['等一下', '第一句。'])

    def test_brain_failure_is_spoken_as_one_line(self):
        call_id = self.active_call()
        self.brain.chunks = []
        self.brain.error = BrainError('busy')
        self.service.say(call_id, '喂')
        texts = [l['text'] for l in self.service.lines_after(call_id, 0)[0]]
        self.assertEqual(texts, ['喂', '（信号不太好，刚才那句我没接住。）'])

    def test_unanswered_records(self):
        call = self.service.ring()
        self.assertEqual(self.service.decline(call['id'])['record'], 'ᯅ 已拒绝')
        call = self.service.ring()
        self.assertEqual(self.service.hangup(call['id'])['record'], 'ᯅ 已取消')
        call = self.service.ring()
        self.clock.now += 61
        self.assertEqual(self.service.get(call['id'])['state'], 'missed')

    def test_format_duration(self):
        self.assertEqual(format_duration(0), '00:00')
        self.assertEqual(format_duration(201.4), '03:21')
        self.assertEqual(format_duration(-3), '00:00')


def sqlite3_error(message):
    return __import__('sqlite3').OperationalError(message)


class ChatRecordTests(unittest.TestCase):
    def make_db(self, with_source_kind):
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.addCleanup(os.unlink, path)
        conn = __import__('sqlite3').connect(path)
        extra = ", source_kind TEXT DEFAULT 'chat'" if with_source_kind else ''
        conn.execute('CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT%s)' % extra)
        conn.commit()
        conn.close()
        return path

    def rows(self, path):
        conn = __import__('sqlite3').connect(path)
        try:
            return conn.execute('SELECT * FROM chat_messages').fetchall()
        finally:
            conn.close()

    def test_record_is_fyodors_call_line(self):
        path = self.make_db(True)
        self.assertEqual(record_call_in_chat(path, {'record': 'ᯅ 通话时长 03:21', 'call': {'id': 'c1'}}), 1)
        self.assertEqual(self.rows(path), [(1, 'fyodor', 'ᯅ 通话时长 03:21', 'call')])

    def test_old_schema_without_source_kind(self):
        path = self.make_db(False)
        record_call_in_chat(path, {'record': 'ᯅ 未接听', 'call': {'id': 'c1'}})
        self.assertEqual(self.rows(path), [(1, 'fyodor', 'ᯅ 未接听')])

    def test_empty_record_writes_nothing(self):
        path = self.make_db(True)
        self.assertIsNone(record_call_in_chat(path, {'record': ''}))
        self.assertEqual(self.rows(path), [])

    def test_same_call_id_is_written_once(self):
        path = self.make_db(True)
        summary = {'record': 'ᯅ 通话时长 00:05', 'call': {'id': 'c1'}}
        first = record_call_in_chat(path, summary)
        second = record_call_in_chat(path, summary)
        self.assertEqual(first, second)
        self.assertEqual(len(self.rows(path)), 1)
        with self.assertRaises(ValueError):
            record_call_in_chat(path, {'record': 'ᯅ 未接听', 'call': {}})

    def service_with(self, db, chat_db, clock, on_end):
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, tmp)
        return CallService(call_store.CallStore(db, clock), CachedTTS(FakeEngine(), tmp), FakeBrain(),
                           clock=clock, spawn=run_now, on_end=on_end)

    def new_db(self):
        fd, db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.addCleanup(os.unlink, db)
        return db

    def test_failed_record_is_retried_until_it_succeeds(self):
        db, chat_db, clock = self.new_db(), self.make_db(True), Clock()
        attempts = []

        def flaky(summary):
            attempts.append(summary['call']['id'])
            if len(attempts) == 1:
                raise sqlite3_error('database is locked')
            return record_call_in_chat(chat_db, summary)

        service = self.service_with(db, chat_db, clock, flaky)
        call = service.ring()
        service.answer(call['id'])
        clock.now += 30
        service.hangup(call['id'])          # first attempt fails inside hangup
        self.assertEqual(self.rows(chat_db), [])
        self.assertEqual(service._store.unrecorded_final()[0]['id'], call['id'])
        service.flush_records()             # the sweep retries and succeeds
        self.assertEqual([r[2] for r in self.rows(chat_db)], ['ᯅ 通话时长 00:30'])
        self.assertEqual(service._store.unrecorded_final(), [])
        service.flush_records()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(self.rows(chat_db)), 1)

    def test_written_record_is_not_repeated_after_a_crash_before_marking(self):
        db, chat_db, clock = self.new_db(), self.make_db(True), Clock()
        first = self.service_with(db, chat_db, clock, None)  # no on_end: nothing gets marked
        call = first.ring()
        first.answer(call['id'])
        clock.now += 12
        first.hangup(call['id'])
        # The chat line was written, then the process died before mark_recorded.
        record_call_in_chat(chat_db, first._summary(first.get(call['id'])))
        restarted = self.service_with(db, chat_db, clock, lambda s: record_call_in_chat(chat_db, s))
        restarted.flush_records()
        self.assertEqual([r[2] for r in self.rows(chat_db)], ['ᯅ 通话时长 00:12'])
        self.assertEqual(restarted._store.unrecorded_final(), [])

    def test_two_sweepers_on_one_call_write_one_line(self):
        import threading
        db, chat_db, clock = self.new_db(), self.make_db(True), Clock()
        a = self.service_with(db, chat_db, clock, lambda s: record_call_in_chat(chat_db, s))
        b = self.service_with(db, chat_db, clock, lambda s: record_call_in_chat(chat_db, s))
        a._on_end, saved = None, a._on_end
        call = a.ring()
        a.decline(call['id'])
        a._on_end = saved
        barrier = threading.Barrier(2)

        def sweep(service):
            barrier.wait()
            service.flush_records()

        threads = [threading.Thread(target=sweep, args=(svc,)) for svc in (a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual([r[2] for r in self.rows(chat_db)], ['ᯅ 已拒绝'])

    def test_every_outcome_reaches_on_end_once(self):
        fd, db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.addCleanup(os.unlink, db)
        chat_db = self.make_db(True)
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            service = CallService(call_store.CallStore(db, clock), CachedTTS(FakeEngine(), tmp), FakeBrain(),
                                  clock=clock, spawn=run_now,
                                  on_end=lambda summary: record_call_in_chat(chat_db, summary))
            first = service.ring()
            service.answer(first['id'])
            clock.now += 65
            service.hangup(first['id'])
            service.hangup(first['id'])
            second = service.ring()
            service.decline(second['id'])
        self.assertEqual([row[2] for row in self.rows(chat_db)], ['ᯅ 通话时长 01:05', 'ᯅ 已拒绝'])


class MissedCallRecordTests(unittest.TestCase):
    def test_missed_call_is_recorded_once_by_the_sweep(self):
        fd, db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.addCleanup(os.unlink, db)
        clock = Clock()
        ended = []
        with tempfile.TemporaryDirectory() as tmp:
            store = call_store.CallStore(db, clock)
            service = CallService(store, CachedTTS(FakeEngine(), tmp), FakeBrain(),
                                  clock=clock, spawn=run_now, on_end=ended.append)
            service.ring()
            service.flush_records()
            self.assertEqual(ended, [])
            clock.now += 61
            service.flush_records()
            service.flush_records()
            self.assertEqual([s['record'] for s in ended], ['ᯅ 未接听'])
            # A restarted service over the same DB does not record it again.
            again = []
            CallService(call_store.CallStore(db, clock), CachedTTS(FakeEngine(), tmp), FakeBrain(),
                        clock=clock, spawn=run_now, on_end=again.append).flush_records()
            self.assertEqual(again, [])


class ThreadedServiceTests(unittest.TestCase):
    def test_waiting_listener_wakes_when_reply_arrives(self):
        import threading
        import time

        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        release = threading.Event()

        class SlowBrain:
            def reply(self, text, first_turn):
                release.wait(5)
                yield '晚安。\n'

        try:
            with tempfile.TemporaryDirectory() as tmp:
                service = CallService(call_store.CallStore(path, time.time),
                                      CachedTTS(FakeEngine(), tmp), SlowBrain())
                call_id = service.ring()['id']
                service.answer(call_id)
                service.say(call_id, '睡了')
                threading.Timer(0.2, release.set).start()
                started = time.monotonic()
                lines, _call = service.lines_after(call_id, 1, wait_s=5)
                self.assertEqual([l['text'] for l in lines], ['晚安。'])
                self.assertLess(time.monotonic() - started, 2)
                lines, _call = service.lines_after(call_id, 2, wait_s=0.3)
                self.assertEqual(lines, [])
        finally:
            os.unlink(path)


class RouteTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.engine = FakeEngine()
        self.allowed = True
        self.stt = FakeSTT()
        service = CallService(call_store.CallStore(self.path, self.clock),
                              CachedTTS(self.engine, self.tmp.name),
                              FakeBrain(), clock=self.clock, spawn=run_now, stt=self.stt)

        def guard(request):
            if not self.allowed:
                raise OwnerAuthError('owner login required', 401)

        app = Flask(__name__)
        app.register_blueprint(create_voice_call_blueprint(service, owner_guard=guard, stream_window_s=0))
        self.client = app.test_client()

    def tearDown(self):
        os.unlink(self.path)
        self.tmp.cleanup()

    def test_owner_required(self):
        self.allowed = False
        resp = self.client.post('/api/call/ring')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.engine.calls, [])

    def test_call_over_http(self):
        call_id = self.client.post('/api/call/ring').get_json()['call']['id']
        self.assertEqual(self.client.get('/api/call/current').get_json()['call']['id'], call_id)
        self.assertEqual(self.client.post('/api/call/%s/say' % call_id, json={'text': '喂'}).status_code, 409)
        self.client.post('/api/call/%s/answer' % call_id)
        self.assertEqual(self.client.post('/api/call/%s/say' % call_id, json={'text': '喂'}).status_code, 200)

        body = self.client.get('/api/call/%s/stream?after=1' % call_id).get_data(as_text=True)
        self.assertIn('event: line', body)
        self.assertIn('"text":"你好。"', body)
        self.assertIn('id: 3', body)
        self.assertIn('"after":3', body)

        audio = self.client.get('/api/call/%s/audio/2' % call_id)
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.mimetype, 'audio/mpeg')

        resp = self.client.post('/api/call/%s/hangup' % call_id)
        self.assertEqual(resp.get_json()['record'], 'ᯅ 通话时长 00:00')
        self.assertEqual(self.client.get('/api/call/nope').status_code, 200)
        self.assertEqual(self.client.post('/api/call/nope/answer').status_code, 404)

    def test_stream_resume_prefers_newest_of_query_and_last_event_id(self):
        call_id = self.client.post('/api/call/ring').get_json()['call']['id']
        self.client.post('/api/call/%s/answer' % call_id)
        self.client.post('/api/call/%s/say' % call_id, json={'text': '喂'})
        body = self.client.get('/api/call/%s/stream?after=0' % call_id,
                               headers={'Last-Event-ID': '2'}).get_data(as_text=True)
        self.assertNotIn('"seq":1', body)
        self.assertNotIn('"seq":2', body)
        self.assertIn('"seq":3', body)

    def test_tts_failure_is_503_not_another_voice(self):
        self.engine.fail = True
        call_id = self.client.post('/api/call/ring').get_json()['call']['id']
        self.client.post('/api/call/%s/answer' % call_id)
        self.client.post('/api/call/%s/say' % call_id, json={'text': '喂'})
        self.assertEqual(self.client.get('/api/call/%s/audio/2' % call_id).status_code, 503)

    def hear(self, call_id, body=b'RIFF....WAVE', mime='audio/wav', query=''):
        return self.client.post('/api/call/%s/hear%s' % (call_id, query), data=body,
                                headers={'Content-Type': mime})

    def test_hear_transcribes_then_says(self):
        call_id = self.client.post('/api/call/ring').get_json()['call']['id']
        self.assertEqual(self.hear(call_id).status_code, 409)
        self.assertEqual(self.stt.calls, [], 'a clip for a call that is not active is never billed')
        self.client.post('/api/call/%s/answer' % call_id)

        resp = self.hear(call_id, query='?lang=eng')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body['text'], '你在干嘛')
        self.assertEqual(body['line']['speaker'], 'her')
        self.assertEqual(self.stt.calls, [(12, 'audio/wav', 'eng')])
        stream = self.client.get('/api/call/%s/stream?after=0' % call_id).get_data(as_text=True)
        self.assertIn('"text":"你在干嘛"', stream)
        self.assertIn('"speaker":"him"', stream)

    def test_hear_silence_says_nothing(self):
        call_id = self.client.post('/api/call/ring').get_json()['call']['id']
        self.client.post('/api/call/%s/answer' % call_id)
        self.stt.text = ''
        body = self.hear(call_id).get_json()
        self.assertEqual((body['text'], body['line']), ('', None))
        stream = self.client.get('/api/call/%s/stream?after=0' % call_id).get_data(as_text=True)
        self.assertNotIn('event: line', stream)

    def test_hear_rejects_empty_oversized_and_failed_clips(self):
        call_id = self.client.post('/api/call/ring').get_json()['call']['id']
        self.client.post('/api/call/%s/answer' % call_id)
        self.assertEqual(self.hear(call_id, body=b'').status_code, 413)
        self.assertEqual(self.hear(call_id, body=b'0' * (MAX_CLIP_BYTES + 1)).status_code, 413)
        self.assertEqual(self.hear(call_id, mime='video/avi').status_code, 415)
        self.assertEqual(self.hear(call_id, query='?utt=abc').status_code, 400)
        self.assertEqual(self.hear(call_id, query='?utt=0').status_code, 400)
        self.assertEqual(self.stt.calls, [])
        self.stt.error = STTError('stt http 401')
        resp = self.hear(call_id)
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.get_json()['error'], 'stt http 401')

    def test_hear_discard_and_numbered_interrupt(self):
        call_id = self.client.post('/api/call/ring').get_json()['call']['id']
        self.client.post('/api/call/%s/answer' % call_id)
        self.assertEqual(self.client.post('/api/call/%s/interrupt?utt=1' % call_id).status_code, 200)
        body = self.client.post('/api/call/%s/hear?utt=1&discard=1' % call_id).get_json()
        self.assertEqual((body['utterance'], body['text'], body['line']), (1, '', None))
        self.assertEqual(self.stt.calls, [], 'a discarded utterance is never transcribed')
        self.assertEqual(self.hear(call_id, query='?utt=2').get_json()['line']['text'], '你在干嘛')

    def test_hear_requires_owner(self):
        self.allowed = False
        self.assertEqual(self.hear('anything').status_code, 401)
        self.assertEqual(self.stt.calls, [])


if __name__ == '__main__':
    unittest.main()
