"""The call state machine: ring, answer, talk, hang up.

Her lines and his lines share one sequence per call. The page asks for lines
after the last seq it has, so a reconnect replays only what it missed. When she
starts a new utterance while he is still answering, the rest of his answer is
dropped from the call (it stays in chat history, it is just not spoken).
"""
import threading
import time
import uuid

from voice_call import store as call_store
from voice_call.brain import BrainError
from voice_call.sentences import SentenceSplitter
from voice_call.stt import STTError
from voice_call.tts import TTSError

HER = 'her'
HIM = 'him'
# 16 kHz mono PCM16 is 32 KB/s: 4 MB holds a ~2 minute clip, far above a VAD-cut sentence.
MAX_CLIP_BYTES = 4 * 1024 * 1024
DEFAULT_STT_LANGUAGE = 'zho'
# An utterance that was started but never heard (lost upload) stops holding the
# queue after this long, so the sentences behind it are not stuck forever.
STALE_UTTERANCE_S = 45


class _Utterances:
    """Per-call order of her spoken utterances (see CallService.hear)."""

    def __init__(self):
        self.started = 0      # highest utterance number known to have begun
        self.committed = 0    # every utterance <= this has been handed on (or dropped)
        self.started_at = {}  # utterance -> clock time it began
        self.results = {}     # utterance -> transcript, waiting for its turn
        self.carry = []       # committed text held back because she was already talking again
        self.in_flight = set() # utterances currently in Scribe; closes duplicate-upload races
        self.responses = {}    # completed request payloads for idempotent retries
        self.errors = {}       # completed STT failures; retries do not call Scribe again
        self.stale = set()       # sequence numbers skipped after a lost/expired upload
        self.timer = None        # wakes the queue when a lost utterance goes stale

    def begin(self, number, now):
        self.started = max(self.started, number)
        self.started_at.setdefault(number, now)


def format_duration(seconds):
    seconds = max(0, int(round(seconds)))
    return '%02d:%02d' % (seconds // 60, seconds % 60)


def _schedule(delay_s, callback):
    timer = threading.Timer(max(0.0, delay_s), callback)
    timer.daemon = True
    timer.start()
    return timer


def _spawn(target):
    thread = threading.Thread(target=target, daemon=True, name='voice-call-reply')
    thread.start()
    return thread


class CallService:
    def __init__(self, store, tts, brain, clock=time.time, spawn=_spawn, on_end=None, stt=None,
                 schedule=_schedule):
        self._store = store
        self._tts = tts
        self._stt = stt
        self._brain = brain
        self._clock = clock
        self._spawn = spawn
        self._on_end = on_end
        self._changed = threading.Condition()
        self._turns = {}  # call_id -> generation counter of the current reply
        self._replied = set()  # call_ids that already had a first turn
        self._record_lock = threading.Lock()
        self._utter_lock = threading.Lock()
        self._utter_done = threading.Condition(self._utter_lock)
        self._utterances = {}  # call_id -> _Utterances
        self._schedule = schedule

    # ── lifecycle ────────────────────────────────────────────
    def ring(self):
        call = self._store.create_if_none_open(uuid.uuid4().hex)
        self._notify()
        return call

    def current(self):
        return self._store.latest_open()

    def get(self, call_id):
        call = self._store.get(call_id)
        if not call:
            raise call_store.CallStateError('no such call', 404)
        return call

    def answer(self, call_id):
        return self._change(call_id, call_store.ACTIVE)

    def decline(self, call_id):
        return self._finish(call_id, call_store.DECLINED)

    def cancel(self, call_id):
        return self._finish(call_id, call_store.CANCELED)

    def hangup(self, call_id):
        call = self.get(call_id)
        if call['state'] in call_store.FINAL_STATES:
            return self._summary(call)
        if call['state'] == call_store.RINGING:
            return self._finish(call_id, call_store.CANCELED)
        return self._finish(call_id, call_store.ENDED)

    # ── talking ──────────────────────────────────────────────
    def say(self, call_id, text):
        text = (text or '').strip()
        if not text:
            raise call_store.CallStateError('empty utterance', 400)
        if self.get(call_id)['state'] != call_store.ACTIVE:
            raise call_store.CallStateError('call is not active')
        line = self._store.add_line(call_id, HER, text)
        with self._changed:
            generation = self._turns.get(call_id, 0) + 1
            self._turns[call_id] = generation
            first_turn = call_id not in self._replied
            self._replied.add(call_id)
            self._changed.notify_all()
        self._spawn(lambda: self._run_reply(call_id, text, first_turn, generation))
        return line

    def hear(self, call_id, audio, mime, language=None, utterance=None):
        """Transcribe one finished VAD utterance with strict, idempotent ordering.

        The client number is allocated at speech_start. Scribe may finish out
        of order, but only the next number is handed to say(). A duplicate
        upload waits for an in-flight request or returns its saved result, so
        a retry never calls Scribe or creates another her line.

        A number that began (speech_start / interrupt) but whose audio never
        arrives stops holding the queue after STALE_UTTERANCE_S; a call-scoped
        timer re-drains the queue then, so a last sentence behind it is handed on
        even if nothing else arrives. A number whose audio is in Scribe never goes
        stale: it hands itself on when Scribe returns or fails.
        """
        if self._stt is None:
            raise call_store.CallStateError('speech-to-text is not configured', 503)
        if audio is not None and (not audio or len(audio) > MAX_CLIP_BYTES):
            raise call_store.CallStateError('clip must be 1 byte to %d bytes' % MAX_CLIP_BYTES, 413)
        if self.get(call_id)['state'] != call_store.ACTIVE:
            raise call_store.CallStateError('call is not active')
        try:
            requested = int(utterance) if utterance is not None else None
        except (TypeError, ValueError):
            raise call_store.CallStateError('utterance must be a positive integer', 400)
        with self._utter_lock:
            queue = self._utterances.setdefault(call_id, _Utterances())
            number = queue.started + 1 if requested is None else requested
            if number <= 0:
                raise call_store.CallStateError('utterance must be a positive integer', 400)
            while number in queue.in_flight:
                self._utter_done.wait()
            if number in queue.errors:
                raise STTError(queue.errors[number])
            if number in queue.responses:
                result = dict(queue.responses[number])
                result['duplicate'] = True
                return result
            if number in queue.stale or number <= queue.committed:
                return {
                    'utterance': number,
                    'text': '',
                    'line': None,
                    'stale': True,
                    'duplicate': True,
                }
            queue.begin(number, self._clock())
            queue.in_flight.add(number)

        text = ''
        stt_error = None
        try:
            if audio is not None:
                text = self._stt.transcribe(audio, mime, language or DEFAULT_STT_LANGUAGE)
        except STTError as exc:
            stt_error = exc
        finally:
            with self._utter_lock:
                queue.in_flight.discard(number)
                queue.results[number] = text
                line = self._hand_on(call_id, queue)
                response = {
                    'utterance': number,
                    'text': text,
                    'line': line,
                    'stale': False,
                    'discarded': audio is None,
                }
                queue.responses[number] = response
                if stt_error is not None:
                    queue.errors[number] = str(stt_error)
                self._utter_done.notify_all()
        if stt_error is not None:
            raise stt_error
        return response

    def _hand_on(self, call_id, queue):
        """Hand on every transcript whose turn has come (caller holds _utter_lock)."""
        said = None
        now = self._clock()
        while True:
            number = queue.committed + 1
            if number > queue.started:
                break
            if number not in queue.results:
                if number in queue.in_flight:
                    break  # in Scribe: it hands itself on when Scribe returns or fails
                began = queue.started_at.get(number)
                if began is not None and now - began < STALE_UTTERANCE_S:
                    # Begun, audio not here yet: everything after it waits, and a timer
                    # makes sure the wait ends even if nothing else ever arrives.
                    self._wake_when_stale(call_id, queue, began + STALE_UTTERANCE_S - now)
                    break
                queue.results[number] = ''  # never begun here, or lost: skip it
                queue.stale.add(number)
            text = queue.results.pop(number)
            queue.started_at.pop(number, None)
            queue.committed = number
            if text:
                queue.carry.append(text)
            if queue.carry and queue.started <= number:
                joined = ' '.join(queue.carry)
                queue.carry = []
                if self.get(call_id)['state'] == call_store.ACTIVE:
                    said = self.say(call_id, joined)
        return said

    def _wake_when_stale(self, call_id, queue, delay_s):
        """Re-run _hand_on once the waited-for utterance goes stale (caller holds _utter_lock).

        Call-scoped: at most one timer per call, only while something is queued
        behind the gap, cancelled when the call ends.
        """
        if queue.timer is not None or not (queue.results or queue.carry):
            return

        def wake():
            with self._utter_lock:
                queue.timer = None
                if self._utterances.get(call_id) is not queue:
                    return
                try:
                    self._hand_on(call_id, queue)
                except call_store.CallStateError:
                    pass  # the call ended meanwhile; nothing left to say

        queue.timer = self._schedule(delay_s, wake)

    def interrupt(self, call_id, utterance=None):
        """She started talking: stop adding the rest of his current answer.

        With ``utterance`` (the number of the utterance she just began), any older
        utterance still being transcribed is held instead of starting a reply.
        """
        if utterance is not None:
            with self._utter_lock:
                self._utterances.setdefault(call_id, _Utterances()).begin(int(utterance), self._clock())
        with self._changed:
            self._turns[call_id] = self._turns.get(call_id, 0) + 1
            self._changed.notify_all()

    def lines_after(self, call_id, after_seq, wait_s=0):
        deadline = self._clock() + wait_s
        with self._changed:
            while True:
                lines = self._store.lines_after(call_id, after_seq)
                call = self._store.get(call_id)
                if lines or not call or call['state'] in call_store.FINAL_STATES:
                    return lines, call
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return lines, call
                self._changed.wait(min(remaining, 1.0))

    def audio(self, call_id, seq):
        line = self._store.line(call_id, seq)
        if not line or line['speaker'] != HIM:
            raise call_store.CallStateError('no such spoken line', 404)
        key, data = self._tts.get(line['text'])
        if line.get('audio_key') != key:
            self._store.set_audio_key(call_id, seq, key)
        return data

    # ── internals ────────────────────────────────────────────
    def _current_generation(self, call_id):
        with self._changed:
            return self._turns.get(call_id, 0)

    def _run_reply(self, call_id, text, first_turn, generation):
        splitter = SentenceSplitter()
        try:
            for chunk in self._brain.reply(text, first_turn):
                if not self._emit(call_id, generation, splitter.feed(chunk)):
                    return
            self._emit(call_id, generation, splitter.flush())
        except (BrainError, OSError, ValueError):
            self._emit(call_id, generation, ['（信号不太好，刚才那句我没接住。）'])

    def _emit(self, call_id, generation, sentences):
        for sentence in sentences:
            if self._current_generation(call_id) != generation:
                return False
            call = self._store.get(call_id)
            if not call or call['state'] != call_store.ACTIVE:
                return False
            self._store.add_line(call_id, HIM, sentence)
            self._notify()
        return True

    def _change(self, call_id, state):
        call = self._store.transition(call_id, state)
        self._notify()
        return call

    def _finish(self, call_id, state):
        call = self._change(call_id, state)
        with self._utter_lock:
            queue = self._utterances.pop(call_id, None)
            if queue is not None and queue.timer is not None:
                queue.timer.cancel()
        self.flush_records()
        return self._summary(call)

    def flush_records(self):
        """Write each finished call's chat record; retry failures on the next run.

        on_end must be idempotent per call id (record_call_in_chat is). A call is
        marked recorded only after on_end returns, so a failure is retried and a
        crash between the chat write and the mark cannot duplicate the line.
        Calls that end by ring timeout have no request to hook, so this also
        runs from a background sweep.
        """
        if not self._on_end:
            return
        with self._record_lock:
            for call in self._store.unrecorded_final():
                try:
                    self._on_end(self._summary(call))
                except Exception:
                    continue  # stays unrecorded; the next sweep retries
                self._store.mark_recorded(call['id'])

    def start_record_sweeper(self, interval_s=15):
        def sweep():
            while True:
                time.sleep(interval_s)
                try:
                    self.flush_records()
                except Exception:
                    pass
        thread = threading.Thread(target=sweep, daemon=True, name='voice-call-records')
        thread.start()
        return thread

    def _summary(self, call):
        summary = {'call': call}
        if call['state'] == call_store.ENDED and call.get('answered_at'):
            duration = (call.get('ended_at') or self._clock()) - call['answered_at']
            summary['duration'] = format_duration(duration)
            summary['record'] = 'ᯅ 通话时长 ' + summary['duration']
        else:
            summary['record'] = {
                call_store.MISSED: 'ᯅ 未接听',
                call_store.DECLINED: 'ᯅ 已拒绝',
                call_store.CANCELED: 'ᯅ 已取消',
            }.get(call['state'], '')
        return summary

    def _notify(self):
        with self._changed:
            self._changed.notify_all()


__all__ = ['CallService', 'TTSError', 'format_duration', 'HER', 'HIM']
