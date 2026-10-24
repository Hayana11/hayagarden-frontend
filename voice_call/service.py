"""The call state machine: ring, answer, talk, hang up.

Her lines and his lines share one sequence per call. The page asks for lines
after the last seq it has, so a reconnect replays only what it missed. When she
starts a new utterance while he is still answering, the rest of his answer is
dropped from the call (it stays in chat history, it is just not spoken).
"""
import http.client
import itertools
import logging
import threading
import time
import uuid

from voice_call import languages as call_languages
from voice_call import store as call_store
from voice_call.brain import BrainError
from voice_call.sentences import SentenceSplitter
from voice_call.stt import STTError
from voice_call.tts import TTSError

_LOG = logging.getLogger('voice_call')

HER = 'her'
HIM = 'him'
PROACTIVE_COOLDOWN_S = 3 * 3600  # between calls he places, answered or not
MAX_OPENING_CHARS = 200
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
        self.prev = {}           # utterance -> its predecessor in this call (0: the call's first)

    def begin(self, number, now):
        self.started = max(self.started, number)
        self.started_at.setdefault(number, now)

    def link(self, number, prev, now):
        """Record ``prev`` as the utterance she started just before ``number`` (caller holds the lock).

        The edge is fixed once known: a retry naming another predecessor is refused.
        A predecessor not yet here is held as begun from now, so it is waited for
        (and goes stale like any begun utterance) instead of being skipped as a gap.
        """
        if prev is None:
            return
        if prev < 0 or (prev != 0 and prev >= number):
            raise call_store.CallStateError('prev must be 0 or an earlier seq', 400)
        known = self.prev.setdefault(number, prev)
        if known != prev:
            raise call_store.CallStateError('seq %d already follows %d' % (number, known), 409)
        if prev > self.committed:
            self.begin(prev, now)

    def gap_end(self, number, now):
        """``(end, wait_s)`` for the run of never-begun numbers starting at ``number``.

        ``end`` is the next number that did begin, minus one: skip to it now.
        Without predecessors (old shells) any such run is skipped. Once she
        sends them, a run is skipped only when an edge proves it holds none of
        her utterances; a run below an utterance whose predecessor is not known
        yet may hide that predecessor, so it waits (``wait_s``) until that
        utterance goes stale. ``(None, None)`` when nothing lies ahead.
        """
        ahead = [n for n in itertools.chain(self.started_at, self.results, self.in_flight) if n > number]
        if not ahead:
            return None, None
        nearest = min(ahead)
        if not self.prev or self.prev.get(nearest, nearest) < number:
            return nearest - 1, None
        wait_s = self.started_at.get(nearest, now) + STALE_UTTERANCE_S - now
        return (nearest - 1, None) if wait_s <= 0 else (None, wait_s)


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
                 schedule=_schedule, languages=None, on_turn=None):
        self._store = store
        self._on_turn = on_turn  # (call_id, chat message id of her turn): lets the chat page fold it
        self._languages = languages or call_languages.load  # read per call: config edits apply live
        self._tts = tts
        self._stt = stt
        self._brain = brain
        self._clock = clock
        self._spawn = spawn
        self._on_end = on_end
        self._changed = threading.Condition()
        self._turns = {}  # call_id -> generation counter of the current reply
        self._brain_turns = {}  # call_id -> (generation, cancellable reply handle)
        self._replied = set()  # call_ids that already had a first turn
        self._record_lock = threading.Lock()
        self._utter_lock = threading.Lock()
        self._utter_done = threading.Condition(self._utter_lock)
        self._utterances = {}  # call_id -> _Utterances
        self._schedule = schedule

    # ── lifecycle ────────────────────────────────────────────
    def ring(self, language=None):
        """Open a call; ``language`` is a configured code (see voice_call.languages) or None.

        The language's entry is frozen with the call: a config edit only changes the next one.
        """
        spec = None
        if language is not None:
            entry = self._languages().get(language)
            if not entry:
                raise call_store.CallStateError('unknown call language', 400)
            spec = {key: entry[key] for key in ('label', 'voice_id', 'stt', 'speech', 'fallback')}
        call = self._store.create_if_none_open(uuid.uuid4().hex, language, spec)
        self._notify()
        return call

    def current(self):
        return self._store.latest_open()

    def get(self, call_id):
        call = self._store.get(call_id)
        if not call:
            raise call_store.CallStateError('no such call', 404)
        return call

    def languages(self):
        return call_languages.public(self._languages())

    def _language(self, call_id):
        """The call's language as frozen at ring, or None for the default call."""
        return self._store.language_spec(call_id)

    def ring_from_him(self, opening, language=None, cooldown_s=PROACTIVE_COOLDOWN_S):
        """He calls her (a Wake's call action). ``opening`` is what he says once she answers.

        The store re-checks, in the transaction that creates the call, that no call is
        open (409) and that his last call was placed at least ``cooldown_s`` ago (429).
        """
        opening = (opening or '').strip()
        if not opening or len(opening) > MAX_OPENING_CHARS:
            raise call_store.CallStateError('opening must be 1 to %d characters' % MAX_OPENING_CHARS, 400)
        spec = None
        if language is not None:
            entry = self._languages().get(language)
            if not entry:
                raise call_store.CallStateError('unknown call language', 400)
            spec = {key: entry[key] for key in ('label', 'voice_id', 'stt', 'speech', 'fallback')}
        call = self._store.create_if_none_open(
            uuid.uuid4().hex, language, spec, caller=call_store.FYODOR, opening=opening,
            caller_cooldown_s=cooldown_s,
        )
        self._notify()
        return call

    def incoming_ringing_id(self):
        return self._store.incoming_ringing_id()

    def answer(self, call_id):
        call = self._change(call_id, call_store.ACTIVE)
        if call.get('caller') == call_store.FYODOR:
            opening = self._store.opening(call_id)
            if opening:
                self._warm(call_id, opening)
        return call

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
        line = self._commit_her_line(call_id, text)
        self._start_reply(call_id, text)
        return line

    def _commit_her_line(self, call_id, text):
        """Persist one ordered transcript immediately, independently of brain batching."""
        line = self._store.add_line(call_id, HER, text)
        self._notify()
        return line

    def _start_reply(self, call_id, text):
        """Start one brain turn for an already-visible HER utterance or speech burst."""
        with self._changed:
            previous = self._brain_turns.pop(call_id, None)
            generation = self._turns.get(call_id, 0) + 1
            self._turns[call_id] = generation
            first_turn = call_id not in self._replied
            self._replied.add(call_id)
            self._changed.notify_all()
        self._cancel_brain_handle(previous)
        self._spawn(lambda: self._run_reply(call_id, text, first_turn, generation))

    def hear(self, call_id, audio, mime, language=None, utterance=None, prev=None):
        """Transcribe one finished VAD utterance with strict, idempotent ordering.

        The client number is allocated at speech_start. Scribe may finish out
        of order, but only ordered transcripts are committed. Each committed
        transcript becomes a visible HER line immediately; consecutive speech
        is still batched into one later brain turn. A duplicate upload waits
        for an in-flight request or returns its saved result, so a retry never
        calls Scribe or creates another her line.

        A number that began (speech_start / interrupt) but whose audio never
        arrives stops holding the queue after STALE_UTTERANCE_S; a call-scoped
        timer re-drains the queue then, so a last sentence behind it is handed on
        even if nothing else arrives. A number whose audio is in Scribe never goes
        stale: it hands itself on when Scribe returns or fails.

        ``prev`` is the number she started just before this one in this call (0
        for the first). Numbers are process-wide on the shell, so only these
        edges tell a gap that holds no utterance of hers (skipped at once) from
        one still on its way (waited for); see _Utterances.link.
        """
        if self._stt is None:
            raise call_store.CallStateError('speech-to-text is not configured', 503)
        if audio is not None and (not audio or len(audio) > MAX_CLIP_BYTES):
            raise call_store.CallStateError('clip must be 1 byte to %d bytes' % MAX_CLIP_BYTES, 413)
        call = self.get(call_id)
        if call['state'] != call_store.ACTIVE:
            raise call_store.CallStateError('call is not active')
        entry = self._language(call_id)
        if entry:
            language = entry['stt']  # the call decides, not the page
        try:
            requested = int(utterance) if utterance is not None else None
        except (TypeError, ValueError):
            raise call_store.CallStateError('utterance must be a positive integer', 400)
        if prev is not None and requested is None:
            raise call_store.CallStateError('prev needs a seq', 400)
        with self._utter_lock:
            queue = self._utterances.setdefault(call_id, _Utterances())
            number = queue.started + 1 if requested is None else requested
            if number <= 0:
                raise call_store.CallStateError('utterance must be a positive integer', 400)
            queue.link(number, prev, self._clock())
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
                committed_lines = self._hand_on(call_id, queue)
                line = committed_lines.get(number)
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
        """Commit ordered transcripts now; batch only the later brain turn (caller holds _utter_lock)."""
        committed_lines = {}
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
                if began is None:
                    # Never begun on this call (the shell's seq is process-wide, so a
                    # call may open at N): jump the whole run in one step.
                    end, wait_s = queue.gap_end(number, now)
                    if wait_s is not None:
                        self._wake_when_stale(call_id, queue, wait_s)
                        break
                    if end is not None:
                        queue.committed = end
                        continue
                queue.results[number] = ''  # never begun here, or lost: skip it
                queue.stale.add(number)
            text = queue.results.pop(number)
            queue.started_at.pop(number, None)
            queue.committed = number
            active = self.get(call_id)['state'] == call_store.ACTIVE
            if text and active:
                # Visibility follows spoken-order commit, not brain flush.  A user
                # should see what Scribe heard even while a later utterance is open.
                line = self._commit_her_line(call_id, text)
                committed_lines[number] = line
                previous_response = queue.responses.get(number)
                if previous_response is not None:
                    previous_response['line'] = line
                queue.carry.append(text)
            if queue.carry and queue.started <= number:
                joined = ' '.join(queue.carry)
                queue.carry = []
                if active:
                    # One brain turn for the complete speech burst; HER lines above
                    # are already durable, so do not add a second joined HER line.
                    self._start_reply(call_id, joined)
        return committed_lines

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

    def interrupt(self, call_id, utterance=None, prev=None):
        """She started talking: stop adding the rest of his current answer.

        With ``utterance`` (the number of the utterance she just began), any older
        utterance still being transcribed is held instead of starting a reply.
        """
        # A speech_start can arrive after hangup. Hold the same lock used by
        # _finish() while checking state and touching the queue, so a hangup
        # that has already transitioned the call cannot be followed by a
        # setdefault() that recreates the removed queue.
        with self._utter_lock:
            if self.get(call_id)['state'] != call_store.ACTIVE:
                raise call_store.CallStateError('call is not active')
            if prev is not None and utterance is None:
                raise call_store.CallStateError('prev needs a seq', 400)
            if utterance is not None:
                queue = self._utterances.setdefault(call_id, _Utterances())
                queue.link(int(utterance), prev, self._clock())
                queue.begin(int(utterance), self._clock())
        # Invalidate the old generation before closing its stream. The close
        # then propagates through the client's HTTP response and lets gateway
        # cleanup release the generation lease naturally.
        with self._changed:
            previous = self._brain_turns.pop(call_id, None)
            self._turns[call_id] = self._turns.get(call_id, 0) + 1
            self._changed.notify_all()
        self._cancel_brain_handle(previous)

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

    def audio_stream(self, call_id, seq):
        """His line's mp3 as chunks, joining the synthesis _emit already started."""
        line = self._store.line(call_id, seq)
        if not line or line['speaker'] != HIM:
            raise call_store.CallStateError('no such spoken line', 404)
        entry = self._language(call_id)
        key, chunks = self._tts.stream(line['text'], entry['voice_id'] if entry else None)
        if line.get('audio_key') != key:
            self._store.set_audio_key(call_id, seq, key)
        return chunks

    def audio(self, call_id, seq):
        return b''.join(self.audio_stream(call_id, seq))

    # ── internals ────────────────────────────────────────────
    @staticmethod
    def _cancel_brain_handle(entry):
        if not entry:
            return
        handle = entry[1] if isinstance(entry, tuple) else entry
        cancel = getattr(handle, 'cancel', None)
        if not callable(cancel):
            cancel = getattr(handle, 'close', None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass

    def _register_brain_handle(self, call_id, generation, handle):
        with self._changed:
            if self._turns.get(call_id, 0) == generation:
                self._brain_turns[call_id] = (generation, handle)
                return True
        # interrupt() or hangup() won the race while start_reply() was opening
        # the response; close it immediately instead of letting a stale turn
        # acquire the gateway lease.
        self._cancel_brain_handle(handle)
        return False

    def _unregister_brain_handle(self, call_id, generation, handle):
        with self._changed:
            current = self._brain_turns.get(call_id)
            if current and current[0] == generation and current[1] is handle:
                self._brain_turns.pop(call_id, None)

    def _current_generation(self, call_id):
        with self._changed:
            return self._turns.get(call_id, 0)

    def _run_reply(self, call_id, text, first_turn, generation):
        splitter = SentenceSplitter()
        handle = None
        entry = self._language(call_id)
        extra = {'language': entry['label']} if entry else {}
        if first_turn:
            call = self._store.get(call_id)
            if call and call.get('caller') == call_store.FYODOR:
                opening = self._store.opening(call_id)
                if opening:
                    extra['opening'] = opening  # the brain must know he placed this call and what he said
        fallback = (entry and entry.get('fallback')) or '（信号不太好，刚才那句我没接住。）'
        try:
            start_reply = getattr(self._brain, 'start_reply', None)
            if callable(start_reply):
                handle = start_reply(text, first_turn, **extra)
                self._note_turn(call_id, getattr(handle, 'message_id', None))
                if not self._register_brain_handle(call_id, generation, handle):
                    return
                stream = handle
            else:
                # Keep the old injected-brain contract for unit tests and
                # non-HTTP implementations; only ChatPipelineBrain exposes a
                # cancellable handle.
                stream = self._brain.reply(text, first_turn, **extra)
            for chunk in stream:
                if not self._emit(call_id, generation, splitter.feed(chunk)):
                    return
            self._emit(call_id, generation, splitter.flush())
        except (BrainError, OSError, ValueError, EOFError, http.client.HTTPException):
            # A cancelled stale stream is intentionally silent: its generation
            # has already been superseded, so it must not revive a fallback.
            if self._current_generation(call_id) == generation:
                self._emit(call_id, generation, [fallback])
        finally:
            if handle is not None:
                self._unregister_brain_handle(call_id, generation, handle)

    def _note_turn(self, call_id, message_id):
        """Best effort: a failure only leaves this turn visible on the chat page."""
        if self._on_turn is None or message_id is None:
            return
        try:
            self._on_turn(call_id, message_id)
        except Exception:
            _LOG.warning('voice call turn %s not mapped to call %s', message_id, call_id, exc_info=True)

    def _emit(self, call_id, generation, sentences):
        for sentence in sentences:
            if self._current_generation(call_id) != generation:
                return False
            call = self._store.get(call_id)
            if not call or call['state'] != call_store.ACTIVE:
                return False
            self._store.add_line(call_id, HIM, sentence)
            self._notify()
            self._warm(call_id, sentence)
        return True

    def _warm(self, call_id, text):
        """Start his line's voice now, so the page joins a synthesis already under way."""
        warm = getattr(self._tts, 'warm', None)
        if callable(warm):
            entry = self._language(call_id)
            warm(text, entry['voice_id'] if entry else None)

    def _change(self, call_id, state):
        call = self._store.transition(call_id, state)
        self._notify()
        return call

    def _finish(self, call_id, state):
        call = self._change(call_id, state)
        with self._changed:
            previous = self._brain_turns.pop(call_id, None)
            self._turns[call_id] = self._turns.get(call_id, 0) + 1
            self._changed.notify_all()
        self._cancel_brain_handle(previous)
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
