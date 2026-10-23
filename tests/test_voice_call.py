import json
import os
import sys
import sqlite3
import tempfile
import threading
import time
import unittest

from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from moments_auth import OwnerAuthError
from voice_call import languages as call_languages
from voice_call import store as call_store
from voice_call.chat_record import record_call_in_chat
from voice_call.brain import (
    BrainError,
    CancellableReply,
    ChatPipelineBrain,
    format_utterance,
    parse_sse_text,
    PHONE_STYLE,
)
from voice_call.sentences import SentenceSplitter
from voice_call.service import MAX_CLIP_BYTES, STALE_UTTERANCE_S, CallService, format_duration
from voice_call.stt import ElevenLabsSTT, STTError
from voice_call.tts import CachedTTS, ElevenLabsTTS, TTSError, read_env_value
from voice_call_routes import create_voice_call_blueprint, create_voice_call_internal_blueprint


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
        self.voices = []
        self.fail = fail

    def synthesize(self, text, voice_id=None, model_id=None):
        self.calls.append(text)
        self.voices.append(voice_id)
        if self.fail:
            raise TTSError('boom')
        return ('mp3:' + text).encode('utf-8')


class FakeBrain:
    def __init__(self, chunks=None, error=None):
        self.chunks = chunks or ['你好。\n', '我在', '听。\n']
        self.error = error
        self.calls = []
        self.languages = []
        self.openings = []
        self.during = None

    def reply(self, text, first_turn, language=None, opening=None):
        self.calls.append((text, first_turn))
        self.languages.append(language)
        self.openings.append(opening)
        for index, chunk in enumerate(self.chunks):
            if self.during:
                self.during(index)
            yield chunk
        if self.error:
            raise self.error


def run_now(target):
    target()


class CancellableHandle:
    def __init__(self, chunks=(), wait_for_cancel=False, late_chunks=()):
        self.chunks = list(chunks)
        self.wait_for_cancel = wait_for_cancel
        self.late_chunks = list(late_chunks)
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.cancel_calls = 0

    def __iter__(self):
        self.started.set()
        if self.wait_for_cancel:
            self.cancelled.wait(5)
            yield from self.late_chunks
        else:
            yield from self.chunks

    def cancel(self):
        self.cancel_calls += 1
        self.cancelled.set()


class CancellableBrain:
    def __init__(self):
        self.calls = []
        self.handles = []

    def start_reply(self, text, first_turn):
        self.calls.append((text, first_turn))
        handle = CancellableHandle(
            wait_for_cancel=not self.handles,
            late_chunks=['旧回复。\n'],
            chunks=['新回复。\n'],
        )
        self.handles.append(handle)
        return handle


class SentenceSplitterTests(unittest.TestCase):
    def test_a_long_first_line_is_cut_at_its_first_pause(self):
        splitter = SentenceSplitter()
        self.assertEqual(splitter.feed('嗯，'), [])  # short so far: wait for more
        self.assertEqual(splitter.feed('我在想你刚才说的那句话是什么意思'), [],
                         'a pause before FIRST_MIN is too early to cut at')
        splitter = SentenceSplitter()
        self.assertEqual(splitter.feed('宝宝你刚才说的那件事，我想了很久才明白'), ['宝宝你刚才说的那件事，'])
        self.assertEqual(splitter.feed('。\n第二句，也很长很长很长很长很长很长'), ['我想了很久才明白。'],
                         'only the first line is cut early')
        self.assertEqual(splitter.flush(), ['第二句，也很长很长很长很长很长很长'])

    def test_a_short_first_line_is_left_whole(self):
        splitter = SentenceSplitter()
        self.assertEqual(splitter.feed('嗯，我在。\n'), ['嗯，我在。'])

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
        self.assertEqual(seen['body']['model_id'], 'eleven_v4_turbo')
        self.assertEqual(seen['body']['voice_settings']['stability'], 0.5)
        self.assertNotIn('style', seen['body']['voice_settings'], 'v4 does not take style')
        self.assertEqual(seen['body']['text'], '你好')

    def test_voice_override_and_live_model(self):
        seen = []
        model = ['eleven_v4']
        tts = ElevenLabsTTS(api_key='k', model_id=lambda: model[0],
                            post=lambda url, headers, body, timeout: seen.append((url, json.loads(body))) or (200, b'mp3'))
        tts.synthesize('a', 'K7JxslCTelez3qaKj5SJ')
        model[0] = ''  # cleared in runtime config: back to the default model
        tts.synthesize('b')
        self.assertIn('/text-to-speech/K7JxslCTelez3qaKj5SJ?', seen[0][0])
        self.assertEqual(seen[0][1]['model_id'], 'eleven_v4')
        self.assertIn('/text-to-speech/TpHHy93T39MKuyQgrXh8?', seen[1][0])
        self.assertEqual(seen[1][1]['model_id'], 'eleven_v4_turbo')

    def test_one_lookup_uses_one_model_for_key_and_request(self):
        models = iter(['eleven_v4', 'eleven_v4_turbo'])  # the config flips between two reads
        seen = []
        tts = ElevenLabsTTS(api_key='k', model_id=lambda: next(models),
                            open_stream=lambda url, headers, body, timeout: (
                                seen.append(json.loads(body)) or (200, iter([b'mp3']))))
        with tempfile.TemporaryDirectory() as tmp:
            cache = CachedTTS(tts, tmp)
            key, _ = cache.get('はい')
            self.assertEqual(key, cache.key('はい', None, 'eleven_v4'))
        self.assertEqual([body['model_id'] for body in seen], ['eleven_v4'])

    def test_stream_uses_the_stream_endpoint_and_yields_as_it_arrives(self):
        seen = {}

        def open_stream(url, headers, body, timeout):
            seen['url'] = url
            return 200, iter([b'ID3', b'', b'more'])

        tts = ElevenLabsTTS(api_key='k', open_stream=open_stream)
        self.assertEqual(list(tts.stream('你好', 'K7JxslCTelez3qaKj5SJ')), [b'ID3', b'more'])
        self.assertIn('/text-to-speech/K7JxslCTelez3qaKj5SJ/stream?output_format=mp3_44100_128', seen['url'])
        bad = ElevenLabsTTS(api_key='k', open_stream=lambda *a: (401, iter([b'no'])))
        with self.assertRaises(TTSError):
            list(bad.stream('你好'))

    def test_readers_share_one_synthesis_and_only_a_whole_line_is_cached(self):
        gate = threading.Event()
        calls = []

        class SlowEngine(FakeEngine):
            def stream(self, text, voice_id=None, model_id=None):
                calls.append(text)
                yield b'ab'
                gate.wait(5)
                yield b'cd'

        with tempfile.TemporaryDirectory() as tmp:
            cache = CachedTTS(SlowEngine(), tmp)
            key, first = cache.stream('晚安')
            self.assertEqual(next(first), b'ab', 'the page plays before the line is finished')
            cache.warm('晚安')
            _, second = cache.stream('晚安')
            self.assertFalse(os.path.exists(cache.path(key)), 'not cached while unfinished')
            gate.set()
            self.assertEqual(b'ab' + b''.join(first), b'abcd')
            self.assertEqual(b''.join(second), b'abcd')
            self.assertEqual(calls, ['晚安'], 'synthesized once however many ask')
            with open(cache.path(key), 'rb') as cached:
                self.assertEqual(cached.read(), b'abcd')

    def test_two_workers_share_one_synthesis_and_both_stream(self):
        """Two CachedTTS sharing cache_dir stand in for two Gunicorn workers."""
        gate = threading.Event()
        started = []

        class SlowEngine(FakeEngine):
            def stream(self, text, voice_id=None, model_id=None):
                started.append(text)
                yield b'ab'
                gate.wait(5)
                yield b'cd'

        with tempfile.TemporaryDirectory() as tmp:
            worker_a = CachedTTS(SlowEngine(), tmp, poll_s=0.005)
            worker_b = CachedTTS(SlowEngine(), tmp, poll_s=0.005)
            key_a, from_a = worker_a.stream('晚安')  # _emit's warm, on worker A
            self.assertEqual(next(from_a), b'ab')
            key_b, from_b = worker_b.stream('晚安')  # the page's request lands on worker B
            self.assertEqual(key_a, key_b)
            self.assertEqual(next(from_b), b'ab', 'B plays the bytes A has so far, before the line is done')
            gate.set()
            self.assertEqual(b'ab' + b''.join(from_b), b'abcd')
            self.assertEqual(b'ab' + b''.join(from_a), b'abcd')
            self.assertEqual(started, ['晚安'], 'ElevenLabs is asked once across both workers')
            self.assertEqual(worker_b.get('晚安')[1], b'abcd')
            self.assertEqual(sorted(os.listdir(tmp)), [key_a + '.mp3'], 'no part or lock left behind')

    def test_a_worker_following_a_failed_owner_stops_with_an_error(self):
        gate = threading.Event()

        class FailingEngine(FakeEngine):
            def stream(self, text, voice_id=None, model_id=None):
                yield b'ab'
                gate.wait(5)
                raise TTSError('tts stream broke: ConnectionResetError')

        with tempfile.TemporaryDirectory() as tmp:
            worker_a = CachedTTS(FailingEngine(), tmp, poll_s=0.005)
            worker_b = CachedTTS(FailingEngine(), tmp, poll_s=0.005)
            _, from_a = worker_a.stream('晚安')
            self.assertEqual(next(from_a), b'ab')
            _, from_b = worker_b.stream('晚安')
            self.assertEqual(next(from_b), b'ab')
            gate.set()
            with self.assertRaises(TTSError):
                b''.join(from_b)
            with self.assertRaises(TTSError):
                b''.join(from_a)
            self.assertEqual(os.listdir(tmp), [], 'nothing cached, nothing left to block a retry')

    def test_a_stream_that_breaks_is_not_cached(self):
        class BrokenEngine(FakeEngine):
            def stream(self, text, voice_id=None, model_id=None):
                yield b'ab'
                raise TTSError('tts stream broke: ConnectionResetError')

        with tempfile.TemporaryDirectory() as tmp:
            cache = CachedTTS(BrokenEngine(), tmp, spawn=run_now)
            _, chunks = cache.stream('晚安')
            with self.assertRaises(TTSError):
                b''.join(chunks)
            self.assertEqual(os.listdir(tmp), [])

    def test_cache_keeps_each_voice_apart(self):
        engine = FakeEngine()
        with tempfile.TemporaryDirectory() as tmp:
            cache = CachedTTS(engine, tmp)
            ja_key, _ = cache.get('はい', 'jaVoice0001')
            en_key, _ = cache.get('はい', 'enVoice0001')
            cache.get('はい', 'jaVoice0001')
        self.assertNotEqual(ja_key, en_key)
        self.assertEqual(engine.voices, ['jaVoice0001', 'enVoice0001'], 'one synthesis per voice')

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


class FakeScheduler:
    """Stands in for threading.Timer: the test advances the clock and fires due timers."""

    def __init__(self):
        self.pending = []

    def __call__(self, delay_s, callback):
        timer = FakeTimer(delay_s, callback)
        self.pending.append(timer)
        return timer

    def run_due(self):
        due, self.pending = [t for t in self.pending if not t.cancelled], []
        for timer in due:
            timer.callback()
        return len(due)


class FakeTimer:
    def __init__(self, delay_s, callback):
        self.delay_s = delay_s
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class UtteranceOrderTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.brain = FakeBrain()
        self.stt = GatedSTT()
        self.scheduler = FakeScheduler()
        self.service = CallService(
            call_store.CallStore(self.path, self.clock),
            CachedTTS(FakeEngine(), self.tmp.name),
            self.brain, clock=self.clock, spawn=run_now, stt=self.stt, schedule=self.scheduler,
        )
        call = self.service.ring()
        self.service.answer(call['id'])
        self.call_id = call['id']
        self.results = {}

    def tearDown(self):
        os.unlink(self.path)
        self.tmp.cleanup()

    def hear_async(self, clip, utterance, prev=None):
        def run():
            try:
                self.results[clip] = self.service.hear(self.call_id, clip, 'audio/wav', None, utterance, prev)
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
        self.assertEqual(self.results[b'A']['line']['text'], '你在吗')
        self.assertEqual(self.her_lines(), ['你在吗'])
        self.assertEqual(self.brain.calls, [], 'A is visible immediately but must not start a reply of its own')

        self.stt.expect(b'B', '我刚才在想你')
        self.stt.release(b'B')
        result = self.service.hear(self.call_id, b'B', 'audio/wav', None, 2)
        self.assertEqual(self.her_lines(), ['你在吗', '我刚才在想你'])
        self.assertEqual(result['line']['text'], '我刚才在想你')
        self.assertEqual([text for text, _ in self.brain.calls], ['你在吗 我刚才在想你'])

    def test_committed_transcript_is_visible_before_the_speech_burst_flushes(self):
        self.stt.expect(b'A', '先说这一句')
        thread_a = self.hear_async(b'A', 1)
        self.wait_started(1)
        self.service.interrupt(self.call_id, utterance=2)  # B began; its audio is still pending.
        self.stt.release(b'A')
        thread_a.join(5)

        self.assertEqual(self.results[b'A']['line']['text'], '先说这一句')
        self.assertEqual(self.her_lines(), ['先说这一句'])
        self.assertEqual(self.brain.calls, [], 'visibility must not force an early brain reply')

        self.stt.expect(b'B', '再补一句')
        self.stt.release(b'B')
        result_b = self.service.hear(self.call_id, b'B', 'audio/wav', None, 2)
        self.assertEqual(result_b['line']['text'], '再补一句')
        self.assertEqual(self.her_lines(), ['先说这一句', '再补一句'])
        self.assertEqual([text for text, _ in self.brain.calls], ['先说这一句 再补一句'])

    def test_visible_transcript_does_not_wait_for_a_lost_successor_timeout(self):
        self.stt.expect(b'A', '我先说完了')
        thread_a = self.hear_async(b'A', 1)
        self.wait_started(1)
        self.service.interrupt(self.call_id, utterance=2)  # B starts, but its upload is lost.
        self.stt.release(b'A')
        thread_a.join(5)

        self.assertEqual(self.her_lines(), ['我先说完了'])
        self.assertEqual(self.brain.calls, [], 'B still holds only the brain flush, not transcript visibility')
        self.assertEqual([t.delay_s for t in self.scheduler.pending], [STALE_UTTERANCE_S])

        self.clock.now += STALE_UTTERANCE_S
        self.assertEqual(self.scheduler.run_due(), 1)
        self.assertEqual(self.her_lines(), ['我先说完了'])
        self.assertEqual([text for text, _ in self.brain.calls], ['我先说完了'])

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
        self.assertEqual(self.her_lines(), ['第一句', '第二句'])
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
        self.assertEqual(self.her_lines(), ['喂', '听得到吗'])

    def test_last_sentence_behind_a_lost_one_is_handed_on_by_the_stale_timer(self):
        self.service.interrupt(self.call_id, utterance=1)  # A began; its upload is lost
        self.stt.expect(b'B', '晚安')
        self.stt.release(b'B')
        self.assertIsNone(self.service.hear(self.call_id, b'B', 'audio/wav', None, 2)['line'])
        self.assertEqual(self.her_lines(), [])
        self.assertEqual([t.delay_s for t in self.scheduler.pending], [STALE_UTTERANCE_S])
        # B is her last sentence: nothing else arrives, only time passes.
        self.clock.now += STALE_UTTERANCE_S
        self.assertEqual(self.scheduler.run_due(), 1)
        self.assertEqual(self.her_lines(), ['晚安'])
        self.assertEqual([text for text, _ in self.brain.calls], ['晚安'])
        self.assertEqual(self.scheduler.pending, [])

    def test_utterance_in_scribe_never_goes_stale(self):
        self.stt.expect(b'A', '慢慢说')
        first = self.hear_async(b'A', 1)
        self.wait_started(1)
        self.stt.expect(b'B', '我在')
        self.stt.release(b'B')
        self.service.hear(self.call_id, b'B', 'audio/wav', None, 2)
        self.clock.now += STALE_UTTERANCE_S * 2
        self.scheduler.run_due()
        self.assertEqual(self.her_lines(), [], 'A is still in Scribe: B keeps waiting for it')
        self.stt.release(b'A')
        first.join(5)
        self.assertEqual(self.her_lines(), ['慢慢说', '我在'])
        queue = self.service._utterances[self.call_id]
        self.assertEqual(queue.results, {}, 'no late transcript is left behind')

    def test_hanging_up_cancels_the_stale_timer(self):
        self.service.interrupt(self.call_id, utterance=1)
        self.stt.expect(b'B', '喂')
        self.stt.release(b'B')
        self.service.hear(self.call_id, b'B', 'audio/wav', None, 2)
        timer = self.scheduler.pending[0]
        self.service.hangup(self.call_id)
        self.assertTrue(timer.cancelled)
        self.assertNotIn(self.call_id, self.service._utterances)

    def test_interrupt_rejects_non_active_and_late_calls_without_recreating_queue(self):
        self.service.hangup(self.call_id)
        with self.assertRaises(call_store.CallStateError):
            self.service.interrupt(self.call_id, utterance=3)
        with self.assertRaises(call_store.CallStateError):
            self.service.interrupt(self.call_id)
        self.assertNotIn(self.call_id, self.service._utterances)

        ringing = self.service.ring()
        with self.assertRaises(call_store.CallStateError):
            self.service.interrupt(ringing['id'], utterance=1)
        self.assertNotIn(ringing['id'], self.service._utterances)

        with self.assertRaises(call_store.CallStateError) as ctx:
            self.service.interrupt('missing-call', utterance=1)
        self.assertEqual(ctx.exception.status, 404)
        self.assertNotIn('missing-call', self.service._utterances)

    def test_call_opening_at_a_large_process_wide_seq_skips_the_gap_in_one_step(self):
        self.stt.expect(b'A', '我回来了')
        self.stt.release(b'A')
        result = self.service.hear(self.call_id, b'A', 'audio/wav', None, 50001)
        self.assertEqual(result['line']['text'], '我回来了')
        queue = self.service._utterances[self.call_id]
        self.assertEqual(queue.committed, 50001)
        self.assertEqual(queue.stale, set(), 'no O(N) stale slots for numbers this call never saw')
        self.assertTrue(self.service.hear(self.call_id, b'old', 'audio/wav', None, 3)['stale'])
        self.stt.expect(b'B', '接着说')
        self.stt.release(b'B')
        self.service.hear(self.call_id, b'B', 'audio/wav', None, 50002)
        self.assertEqual(self.her_lines(), ['我回来了', '接着说'])
        self.assertEqual(self.stt.calls, [b'A', b'B'])

    # ── prev: the predecessor chain (the shell's seq is process-wide) ──
    def say_clip(self, clip, text, utterance, prev):
        self.stt.expect(clip, text)
        self.stt.release(clip)
        return self.service.hear(self.call_id, clip, 'audio/wav', None, utterance, prev)

    def test_call_root_with_prev_zero_skips_the_history_gap_in_one_step(self):
        result = self.say_clip(b'A', '我回来了', 50001, 0)
        self.assertEqual(result['line']['text'], '我回来了')
        queue = self.service._utterances[self.call_id]
        self.assertEqual((queue.committed, queue.stale), (50001, set()))

    def test_successor_arriving_first_cannot_skip_its_predecessor(self):
        n = 50000
        self.assertIsNone(self.say_clip(b'B', '第二句', n + 1, n)['line'])
        self.service.interrupt(self.call_id, utterance=n, prev=0)  # late speech_start(N)
        self.say_clip(b'A', '第一句', n, 0)
        self.assertEqual(self.her_lines(), ['第一句', '第二句'])
        self.assertEqual([text for text, _ in self.brain.calls], ['第一句 第二句'])
        self.assertEqual(self.service._utterances[self.call_id].stale, set())

    def test_three_deep_reversed_chain_keeps_spoken_order(self):
        n = 50000
        self.assertIsNone(self.say_clip(b'C', '三', n + 2, n + 1)['line'])
        self.assertIsNone(self.say_clip(b'B', '二', n + 1, n)['line'])
        self.say_clip(b'A', '一', n, 0)
        self.assertEqual(self.her_lines(), ['一', '二', '三'])
        self.assertEqual(self.service._utterances[self.call_id].stale, set())

    def test_lost_predecessor_is_released_by_the_stale_timer(self):
        n = 50000
        self.assertIsNone(self.say_clip(b'B', '晚安', n + 1, n)['line'])
        self.assertEqual([t.delay_s for t in self.scheduler.pending], [STALE_UTTERANCE_S])
        self.clock.now += STALE_UTTERANCE_S
        self.assertEqual(self.scheduler.run_due(), 1)
        self.assertEqual(self.her_lines(), ['晚安'])
        self.assertEqual(self.service._utterances[self.call_id].stale, {n}, 'only the lost one, no O(N) slots')

    def test_predecessor_in_scribe_is_never_skipped(self):
        n = 50000
        self.stt.expect(b'A', '慢慢说')
        first = self.hear_async(b'A', n, 0)
        self.wait_started(n)
        self.say_clip(b'B', '我在', n + 1, n)
        self.clock.now += STALE_UTTERANCE_S * 2
        self.scheduler.run_due()
        self.assertEqual(self.her_lines(), [], 'N is in Scribe: N+1 keeps waiting for it')
        self.stt.release(b'A')
        first.join(5)
        self.assertEqual(self.her_lines(), ['慢慢说', '我在'])

    def test_same_prev_replays_and_a_conflicting_prev_is_refused(self):
        n = 50000
        self.say_clip(b'A', '嗯', n, 0)
        self.assertTrue(self.service.hear(self.call_id, b'A', 'audio/wav', None, n, 0)['duplicate'])
        self.assertEqual(self.stt.calls, [b'A'])
        for call in (lambda: self.service.hear(self.call_id, b'A', 'audio/wav', None, n, 7),
                     lambda: self.service.interrupt(self.call_id, utterance=n, prev=3)):
            with self.assertRaises(call_store.CallStateError) as ctx:
                call()
            self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.stt.calls, [b'A'])
        self.assertEqual(self.her_lines(), ['嗯'])

    def test_discard_closes_a_predecessor_slot(self):
        n = 50000
        self.assertIsNone(self.say_clip(b'B', '二', n + 1, n)['line'])
        self.service.hear(self.call_id, None, None, utterance=n, prev=0)
        self.assertEqual(self.her_lines(), ['二'])
        self.assertEqual(self.stt.calls, [b'B'], 'a discarded utterance is never transcribed')

    def test_prev_must_be_zero_or_an_earlier_seq(self):
        for utterance, prev in ((5, 5), (5, 6), (5, -1), (None, 1)):
            with self.assertRaises(call_store.CallStateError) as ctx:
                self.service.hear(self.call_id, b'X', 'audio/wav', None, utterance, prev)
            self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(call_store.CallStateError) as ctx:
            self.service.interrupt(self.call_id, prev=1)
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.stt.calls, [])

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


JA_EN = json.dumps({
    'ja': {'label': '日本語', 'voice_id': '0zi0ae2RvBmXaahlPt2n', 'stt': 'jpn', 'speech': 'ja-JP',
           'fallback': 'ごめん、今の一言をうまく聞き取れなかった。'},
    'en': {'label': 'English', 'voice_id': 'K7JxslCTelez3qaKj5SJ', 'stt': 'eng', 'speech': 'en-US'},
})


class LanguageConfigTests(unittest.TestCase):
    def test_valid_entries_in_order_and_public_view_hides_voices(self):
        langs = call_languages.load(lambda key: JA_EN)
        self.assertEqual(list(langs), ['ja', 'en'])
        self.assertEqual(langs['ja']['stt'], 'jpn')
        self.assertEqual(call_languages.public(langs), [
            {'code': 'ja', 'label': '日本語', 'speech': 'ja-JP'},
            {'code': 'en', 'label': 'English', 'speech': 'en-US'},
        ])

    def test_malformed_config_or_entries_are_skipped(self):
        for raw in ('', 'not json', '[]', '"ja"', None):
            self.assertEqual(call_languages.load(lambda key, raw=raw: raw), {})
        raw = json.dumps({
            'ok': {'voice_id': 'K7JxslCTelez3qaKj5SJ', 'stt': 'eng', 'speech': 'not a locale!'},
            'nostt': {'voice_id': 'K7JxslCTelez3qaKj5SJ'},
            'badstt': {'voice_id': 'K7JxslCTelez3qaKj5SJ', 'stt': 'english'},
            'badvoice': {'voice_id': '../../x', 'stt': 'eng'},
            'Bad Code': {'voice_id': 'K7JxslCTelez3qaKj5SJ', 'stt': 'eng'},
            'notdict': 'eng',
        })
        langs = call_languages.load(lambda key: raw)
        self.assertEqual(list(langs), ['ok'])
        self.assertEqual((langs['ok']['label'], langs['ok']['speech']), ('ok', None))


class CallLanguageTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.engine = FakeEngine()
        self.brain = FakeBrain()
        self.stt = FakeSTT(text='もしもし')
        self.config = JA_EN
        self.service = CallService(
            call_store.CallStore(self.path, self.clock), CachedTTS(self.engine, self.tmp.name, spawn=run_now),
            self.brain, clock=self.clock, spawn=run_now, stt=self.stt,
            languages=lambda: call_languages.load(lambda key: self.config),
        )

    def tearDown(self):
        os.unlink(self.path)
        self.tmp.cleanup()

    def active_call(self, language=None):
        call = self.service.ring(language)
        self.service.answer(call['id'])
        return call

    def test_the_call_language_picks_voice_transcription_and_reply_language(self):
        call = self.active_call('en')
        self.assertEqual(call['language'], 'en')
        result = self.service.hear(call['id'], b'RIFF', 'audio/wav', 'zho', 1)  # the page still says zho
        self.assertEqual(self.stt.calls[-1][2], 'eng', 'the call decides, not the page')
        self.assertEqual(result['line']['text'], 'もしもし')
        self.assertEqual(self.brain.languages, ['English'])
        lines, _ = self.service.lines_after(call['id'], 0)
        self.service.audio(call['id'], [l for l in lines if l['speaker'] == 'him'][0]['seq'])
        self.assertEqual(set(self.engine.voices), {'K7JxslCTelez3qaKj5SJ'}, 'every line in the call voice')

    def test_default_call_keeps_todays_voice_and_language(self):
        call = self.active_call()
        self.assertIsNone(call['language'])
        self.service.hear(call['id'], b'RIFF', 'audio/wav', 'zho', 1)
        self.assertEqual(self.stt.calls[-1][2], 'zho')
        self.assertEqual(self.brain.languages, [None])
        lines, _ = self.service.lines_after(call['id'], 0)
        self.service.audio(call['id'], [l for l in lines if l['speaker'] == 'him'][0]['seq'])
        self.assertEqual(set(self.engine.voices), {None})

    def test_unknown_language_is_refused(self):
        with self.assertRaises(call_store.CallStateError) as ctx:
            self.service.ring('fr')
        self.assertEqual(ctx.exception.status, 400)
        self.assertIsNone(self.service.current())

    def test_a_config_edit_only_changes_the_next_call(self):
        call = self.active_call('ja')
        self.config = json.dumps({'ja': {'label': 'にほんご', 'voice_id': 'NEWVOICE0001', 'stt': 'eng'}})
        self.service.hear(call['id'], b'RIFF', 'audio/wav', 'zho', 1)
        self.assertEqual(self.stt.calls[-1][2], 'jpn')
        self.assertEqual(self.brain.languages, ['日本語'])
        self.config = ''  # removed entirely: still the same Japanese call
        self.service.hear(call['id'], b'RIFF', 'audio/wav', 'zho', 2)
        self.assertEqual(self.stt.calls[-1][2], 'jpn')
        lines, _ = self.service.lines_after(call['id'], 0)
        self.service.audio(call['id'], [l for l in lines if l['speaker'] == 'him'][-1]['seq'])
        self.assertEqual(set(self.engine.voices), {'0zi0ae2RvBmXaahlPt2n'})
        self.service.hangup(call['id'])
        with self.assertRaises(call_store.CallStateError):
            self.service.ring('ja')  # the next call sees the edit

    def test_the_snapshot_survives_a_restart(self):
        call = self.active_call('ja')
        self.config = ''
        restarted = CallService(
            call_store.CallStore(self.path, self.clock), CachedTTS(self.engine, self.tmp.name),
            self.brain, clock=self.clock, spawn=run_now, stt=self.stt,
            languages=lambda: call_languages.load(lambda key: self.config),
        )
        restarted.hear(call['id'], b'RIFF', 'audio/wav', 'zho', 1)
        self.assertEqual(self.stt.calls[-1][2], 'jpn')
        self.assertEqual(self.brain.languages, ['日本語'])

    def test_the_call_shows_its_speech_locale_but_never_its_voice(self):
        call = self.service.ring('ja')
        self.assertEqual((call['language'], call['speech']), ('ja', 'ja-JP'))
        self.assertNotIn('voice_id', json.dumps(call))
        self.assertNotIn('language_spec', call)
        self.assertEqual(self.service.current()['speech'], 'ja-JP')

    def test_a_failed_reply_falls_back_in_the_call_language(self):
        self.brain.chunks = ['']
        self.brain.error = BrainError('down')
        call = self.active_call('ja')
        self.service.hear(call['id'], b'RIFF', 'audio/wav', None, 1)
        lines, _ = self.service.lines_after(call['id'], 0)
        self.assertEqual([l['text'] for l in lines if l['speaker'] == 'him'],
                         ['ごめん、今の一言をうまく聞き取れなかった。'])
        self.brain.languages.clear()
        self.service.hangup(call['id'])
        call = self.active_call('en')  # no fallback configured: today's sentence
        self.service.hear(call['id'], b'RIFF', 'audio/wav', None, 1)
        lines, _ = self.service.lines_after(call['id'], 0)
        self.assertEqual([l['text'] for l in lines if l['speaker'] == 'him'], ['（信号不太好，刚才那句我没接住。）'])

    def test_reply_instruction_names_the_language_every_turn(self):
        first = format_utterance('もしもし', True, '日本語')
        self.assertIn('这通电话用日本語', first)
        self.assertIn('（用日本語回答）', first)
        self.assertIn('（用日本語回答）', format_utterance('うん', False, '日本語'))
        self.assertNotIn('回答）', format_utterance('喂', False))

    def test_routes_offer_languages_and_ring_with_one(self):
        app = Flask(__name__)
        app.register_blueprint(create_voice_call_blueprint(self.service, owner_guard=lambda r: None,
                                                           stream_window_s=0))
        client = app.test_client()
        body = client.get('/api/call/languages').get_json()
        self.assertEqual([l['code'] for l in body['languages']], ['ja', 'en'])
        self.assertNotIn('voice_id', json.dumps(body))
        self.assertEqual(client.post('/api/call/ring', json={'language': 'fr'}).status_code, 400)
        self.assertEqual(client.post('/api/call/ring', json={'language': 7}).status_code, 400)
        self.assertEqual(client.post('/api/call/ring', json={'language': ''}).status_code, 400)
        self.assertEqual(client.post('/api/call/ring', json={'language': None}).status_code, 400)
        resp = client.post('/api/call/ring', json={'language': 'ja'})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()['call']['language'], 'ja')

    def test_old_database_gains_the_language_column(self):
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE voice_calls (id TEXT PRIMARY KEY, state TEXT NOT NULL, "
                         "created_at REAL NOT NULL, answered_at REAL, ended_at REAL)")
            conn.execute("INSERT INTO voice_calls VALUES ('old', 'ended', 1, 2, 3)")
            conn.commit()
            conn.close()
            store = call_store.CallStore(path, self.clock)
            self.assertIsNone(store.get('old')['language'])
            store.create_if_none_open('new', 'en')
            self.assertEqual(store.get('new')['language'], 'en')
        finally:
            os.unlink(path)


class ProactiveCallTests(unittest.TestCase):
    """He calls her: caller, cooldown, opening said exactly once on answer."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.engine = FakeEngine()
        self.brain = FakeBrain()
        self.service = CallService(
            call_store.CallStore(self.path, self.clock), CachedTTS(self.engine, self.tmp.name, spawn=run_now),
            self.brain, clock=self.clock, spawn=run_now,
        )

    def tearDown(self):
        os.unlink(self.path)
        self.tmp.cleanup()

    def him_lines(self, call_id):
        lines, _ = self.service.lines_after(call_id, 0)
        return [l['text'] for l in lines if l['speaker'] == 'him']

    def test_his_call_rings_as_incoming_without_showing_the_opening(self):
        call = self.service.ring_from_him('哈娅，在做什么？')
        self.assertEqual((call['caller'], call['state']), ('fyodor', 'ringing'))
        self.assertNotIn('opening', call)
        self.assertEqual(self.service.incoming_ringing_id(), call['id'])
        self.assertEqual(self.him_lines(call['id']), [], 'not a line while it is ringing')
        self.assertEqual(self.engine.calls, [], 'not voiced before she answers')

    def test_answer_says_the_opening_exactly_once(self):
        call = self.service.ring_from_him('哈娅，在做什么？')
        self.service.answer(call['id'])
        self.assertEqual(self.him_lines(call['id']), ['哈娅，在做什么？'])
        self.assertEqual(self.engine.calls, ['哈娅，在做什么？'], 'voiced as soon as she picks up')
        with self.assertRaises(call_store.CallStateError):
            self.service.answer(call['id'])  # a retried answer
        self.assertEqual(self.him_lines(call['id']), ['哈娅，在做什么？'])
        self.assertIsNone(self.service.incoming_ringing_id())

    def test_her_first_words_tell_the_brain_he_called_and_what_he_said(self):
        call = self.service.ring_from_him('哈娅，在做什么？')
        self.service.answer(call['id'])
        self.service.say(call['id'], '在想你')
        self.assertEqual(self.brain.openings, ['哈娅，在做什么？'])
        self.service.say(call['id'], '真的')
        self.assertEqual(self.brain.openings[-1], None, 'only on the first turn')
        first = format_utterance('在想你', True, None, '哈娅，在做什么？')
        self.assertIn(PHONE_STYLE, first)
        self.assertIn('这通电话是你主动打给她的；她接起来时你先说了：「哈娅，在做什么？」', first)

    def test_her_own_dialing_is_never_an_incoming_call(self):
        mine = self.service.ring()  # between ring() and answer() when she dials
        self.assertEqual(mine['caller'], 'her')
        self.assertIsNone(self.service.incoming_ringing_id())
        self.service.answer(mine['id'])
        self.assertEqual(self.him_lines(mine['id']), [])

    def test_no_second_call_while_one_is_open_or_within_three_hours(self):
        self.service.ring()
        with self.assertRaises(call_store.CallStateError) as ctx:
            self.service.ring_from_him('在吗')
        self.assertEqual(ctx.exception.status, 409)
        self.service.hangup(self.service.current()['id'])
        call = self.service.ring_from_him('在吗')
        self.service.decline(call['id'])  # declined still counts as a call he placed
        self.clock.now += 3 * 3600 - 1
        with self.assertRaises(call_store.CallStateError) as ctx:
            self.service.ring_from_him('在吗')
        self.assertEqual(ctx.exception.status, 429)
        self.clock.now += 1
        self.assertEqual(self.service.ring_from_him('在吗')['caller'], 'fyodor')

    def test_a_missed_call_still_counts_for_the_cooldown(self):
        self.service.ring_from_him('在吗')
        self.clock.now += call_store.RING_TIMEOUT_S + 1
        self.assertIsNone(self.service.incoming_ringing_id(), 'unanswered: missed')
        with self.assertRaises(call_store.CallStateError) as ctx:
            self.service.ring_from_him('在吗')
        self.assertEqual(ctx.exception.status, 429)

    def test_two_concurrent_rings_cannot_both_pass(self):
        results = []

        def ring():
            try:
                results.append(self.service.ring_from_him('在吗')['id'])
            except call_store.CallStateError as exc:
                results.append(exc.status)

        threads = [threading.Thread(target=ring) for _ in range(4)]
        [t.start() for t in threads]
        [t.join(5) for t in threads]
        self.assertEqual(sum(1 for r in results if isinstance(r, str)), 1)

    def test_opening_is_required_and_bounded(self):
        for opening in ('', '   ', 'x' * 201):
            with self.assertRaises(call_store.CallStateError) as ctx:
                self.service.ring_from_him(opening)
            self.assertEqual(ctx.exception.status, 400)

    def internal_client(self, token='s3cret'):
        app = Flask(__name__)
        app.register_blueprint(create_voice_call_internal_blueprint(self.service, token_fn=lambda: token))
        client = app.test_client()
        client.environ_base['HTTP_HOST'] = '127.0.0.1:5050'  # how the gateway calls it
        return client

    def test_internal_ring_needs_its_token_and_refuses_proxied_requests(self):
        client = self.internal_client()
        body = {'opening': '在吗'}
        self.assertEqual(client.post('/internal/voice_call/ring', json=body).status_code, 401)
        self.assertEqual(client.post('/internal/voice_call/ring', json=body,
                                     headers={'X-Voice-Call-Internal-Token': 'wrong'}).status_code, 401)
        for proxy in ('X-Forwarded-For', 'X-Real-IP', 'Forwarded'):
            resp = client.post('/internal/voice_call/ring', json=body,
                               headers={'X-Voice-Call-Internal-Token': 's3cret', proxy: '1.2.3.4'})
            self.assertEqual(resp.status_code, 403, proxy)
        resp = client.post('/internal/voice_call/ring', json=body, base_url='https://love-style.xyz',
                           headers={'X-Voice-Call-Internal-Token': 's3cret'})
        self.assertEqual(resp.status_code, 403, 'the public host, as nginx forwards it')
        self.assertIsNone(self.service.current(), 'nothing rang')
        unconfigured = self.internal_client(token='')
        self.assertEqual(unconfigured.post('/internal/voice_call/ring', json=body,
                                           headers={'X-Voice-Call-Internal-Token': ''}).status_code, 503)

    def test_internal_ring_rings_and_reports_conflicts(self):
        client = self.internal_client()
        auth = {'X-Voice-Call-Internal-Token': 's3cret'}
        resp = client.post('/internal/voice_call/ring', json={'opening': '在吗'}, headers=auth)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()['call']['caller'], 'fyodor')
        self.assertEqual(client.post('/internal/voice_call/ring', json={'opening': '在吗'},
                                     headers=auth).status_code, 409)
        self.assertEqual(client.post('/internal/voice_call/ring', json={'opening': 7},
                                     headers=auth).status_code, 400)
        self.assertEqual(client.post('/internal/voice_call/ring', json={'opening': '在吗', 'language': ''},
                                     headers=auth).status_code, 400)

    def test_old_database_gains_caller_and_opening(self):
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE voice_calls (id TEXT PRIMARY KEY, state TEXT NOT NULL, "
                         "created_at REAL NOT NULL, answered_at REAL, ended_at REAL)")
            conn.execute("INSERT INTO voice_calls VALUES ('old', 'ended', 1, 2, 3)")
            conn.commit()
            conn.close()
            store = call_store.CallStore(path, self.clock)
            self.assertEqual(store.get('old')['caller'], 'her')
            self.assertIsNone(store.opening('old'))
        finally:
            os.unlink(path)


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



    def test_start_reply_can_close_only_its_underlying_stream(self):
        class CloseableStream:
            def __init__(self):
                self.closed = False
                self.started = threading.Event()
                self.released = threading.Event()

            def __iter__(self):
                self.started.set()
                self.released.wait(5)
                return
                yield  # keep this a generator

            def close(self):
                self.closed = True
                self.released.set()

        stream = CloseableStream()
        brain = ChatPipelineBrain(
            send_message=lambda _content: 42,
            stream_reply=lambda _message_id: stream,
        )
        handle = brain.start_reply('喂', False)
        thread = threading.Thread(target=lambda: list(handle))
        thread.start()
        self.assertTrue(stream.started.wait(5))
        handle.cancel()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(stream.closed)


class BargeInCancellationTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.tmp = tempfile.TemporaryDirectory()
        self.brain = CancellableBrain()
        self.service = CallService(
            call_store.CallStore(self.path, time.time),
            CachedTTS(FakeEngine(), self.tmp.name),
            self.brain,
        )
        call = self.service.ring()
        self.service.answer(call['id'])
        self.call_id = call['id']

    def tearDown(self):
        self.service.hangup(self.call_id)
        os.unlink(self.path)
        self.tmp.cleanup()

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail('condition did not become true')

    def test_interrupt_closes_old_stream_and_next_turn_gets_a_clean_generation(self):
        self.service.say(self.call_id, '第一句')
        self.wait_until(lambda: len(self.brain.handles) == 1)
        first = self.brain.handles[0]
        self.assertTrue(first.started.wait(5))

        # A non-call stream registered under another owner is not touched by
        # call-scoped cancellation (ordinary Chat/Wake never enters this map).
        ordinary_stream = CancellableHandle(wait_for_cancel=True)
        with self.service._changed:
            self.service._brain_turns['ordinary-chat'] = (1, ordinary_stream)
        self.service.interrupt(self.call_id, utterance=7, prev=0)
        self.assertEqual(first.cancel_calls, 1)
        self.assertEqual(ordinary_stream.cancel_calls, 0)

        self.service.say(self.call_id, '第二句')
        self.wait_until(lambda: len(self.brain.handles) == 2)
        self.wait_until(lambda: any(
            line['text'] == '新回复。'
            for line in self.service.lines_after(self.call_id, 0)[0]
        ))

        texts = [line['text'] for line in self.service.lines_after(self.call_id, 0)[0]]
        self.assertIn('第一句', texts)
        self.assertIn('第二句', texts)
        self.assertIn('新回复。', texts)
        self.assertNotIn('旧回复。', texts)
        self.assertNotIn('（信号不太好，刚才那句我没接住。）', texts)
        self.wait_until(lambda: self.call_id not in self.service._brain_turns)
        self.assertNotIn(self.call_id, self.service._brain_turns)

    def test_hangup_closes_the_active_call_reply(self):
        self.service.say(self.call_id, '挂断前')
        self.wait_until(lambda: len(self.brain.handles) == 1)
        handle = self.brain.handles[0]
        self.assertTrue(handle.started.wait(5))
        self.service.hangup(self.call_id)
        self.assertEqual(handle.cancel_calls, 1)
        self.assertNotIn(self.call_id, self.service._brain_turns)


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
            CachedTTS(self.engine, self.tmp.name, spawn=run_now),
            self.brain, clock=self.clock, spawn=run_now, on_end=self.ended.append,
        )

    def tearDown(self):
        os.unlink(self.path)
        self.tmp.cleanup()

    def active_call(self):
        call = self.service.ring()
        self.service.answer(call['id'])
        return call['id']

    def test_his_lines_are_voiced_before_the_page_asks(self):
        call_id = self.active_call()
        self.service.say(call_id, '在吗')
        self.assertEqual(self.engine.calls, ['你好。', '我在听。'])
        self.assertEqual(self.service.audio(call_id, 2), 'mp3:你好。'.encode('utf-8'))
        self.assertEqual(self.engine.calls, ['你好。', '我在听。'], 'served from what was already made')

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
        self.assertEqual(self.engine.calls, ['你好。', '我在听。'],
                         'each of his lines is voiced once, as soon as he says it')
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

    def test_audio_is_streamed_and_never_kept_by_the_browser(self):
        call_id = self.client.post('/api/call/ring').get_json()['call']['id']
        self.client.post('/api/call/%s/answer' % call_id)
        self.client.post('/api/call/%s/say' % call_id, json={'text': '喂'})
        resp = self.client.get('/api/call/%s/audio/2' % call_id)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.is_streamed)
        self.assertEqual(resp.headers['Cache-Control'], 'no-store')
        self.assertEqual(resp.mimetype, 'audio/mpeg')
        self.assertEqual(resp.get_data(), 'mp3:你好。'.encode('utf-8'))

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
        self.assertEqual(self.hear(call_id, body=b'0' * (MAX_CLIP_BYTES + 1),
                                   query='?seq=1&discard=1').status_code, 413)
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

    def test_hear_and_interrupt_carry_prev(self):
        call_id = self.client.post('/api/call/ring').get_json()['call']['id']
        self.client.post('/api/call/%s/answer' % call_id)
        self.assertEqual(self.client.post('/api/call/%s/interrupt?seq=7&prev=x' % call_id).status_code, 400)
        self.assertEqual(self.hear(call_id, query='?seq=7&prev=7').status_code, 400)
        self.assertEqual(self.client.post('/api/call/%s/interrupt?seq=7&prev=0' % call_id).status_code, 200)
        self.assertEqual(self.hear(call_id, query='?seq=7&prev=3').status_code, 409)
        body = self.client.post('/api/call/%s/hear?seq=7&prev=0&discard=1' % call_id).get_json()
        self.assertEqual((body['utterance'], body['line']), (7, None))
        self.assertEqual(self.hear(call_id, query='?seq=9&prev=7').get_json()['line']['text'], '你在干嘛')
        self.assertEqual(len(self.stt.calls), 1, 'the 409 and the discard never reach Scribe')

    def test_hear_requires_owner(self):
        self.allowed = False
        self.assertEqual(self.hear('anything').status_code, 401)
        self.assertEqual(self.stt.calls, [])


if __name__ == '__main__':
    unittest.main()
