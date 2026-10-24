"""ElevenLabs text-to-speech with a per-sentence content cache.

One request per sentence so the first line can play while the rest are still
being generated. Synthesis is streamed: the page starts playing at the first
bytes, and a line is started as soon as he says it (CachedTTS.warm), so the
page usually joins a synthesis already under way. The cache key covers voice,
model and text: a phone that reconnects and asks for the same line again is
served from disk instead of being billed twice.
"""
import fcntl
import hashlib
import json
import os
import threading
import time
import uuid
import urllib.error
import urllib.request

from voice_call.tags import speakable

STREAM_CHUNK = 4096
FOLLOW_POLL_S = 0.02  # how often another worker's reader checks for new bytes

DEFAULT_VOICE_ID = 'TpHHy93T39MKuyQgrXh8'
DEFAULT_MODEL_ID = 'eleven_v4_turbo'
MODEL_CONFIG_KEY = 'ELEVENLABS_TTS_MODEL'
ENV_PATH = '/opt/frontend/.env'


class TTSError(Exception):
    pass


def read_env_value(name, env_path=ENV_PATH):
    """Read one key from the .env file on every call, so a rotated key needs no restart."""
    value = os.environ.get(name, '')
    try:
        with open(env_path, encoding='utf-8') as env_file:
            for line in env_file:
                key, _, raw = line.partition('=')
                if key.strip() == name:
                    value = raw.strip().strip('"').strip("'")
    except OSError:
        pass
    return value


def _urllib_post(url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _urllib_open_stream(url, headers, body, timeout):
    """Return (status, chunks); chunks yields the body as it arrives."""
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return exc.code, iter([exc.read()])

    def chunks():
        with resp:
            while True:
                chunk = resp.read(STREAM_CHUNK)
                if not chunk:
                    return
                yield chunk
    return resp.status, chunks()


class ElevenLabsTTS:
    URL = 'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128'
    STREAM_URL = 'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?output_format=mp3_44100_128'

    def __init__(self, voice_id=DEFAULT_VOICE_ID, model_id=DEFAULT_MODEL_ID,
                 api_key=None, post=_urllib_post, timeout=30, open_stream=_urllib_open_stream):
        """``model_id`` is a model name, or a callable read on every request so a
        runtime-config change takes effect without a restart."""
        self.voice_id = voice_id
        self._model_id = model_id
        self._api_key = api_key
        self._post = post
        self._open_stream = open_stream
        self._timeout = timeout

    @property
    def model_id(self):
        model = self._model_id() if callable(self._model_id) else self._model_id
        return model or DEFAULT_MODEL_ID

    def api_key(self):
        return self._api_key if self._api_key is not None else read_env_value('ELEVENLABS_API_KEY')

    def _request(self, text, model_id):
        key = self.api_key()
        if not key:
            raise TTSError('ELEVENLABS_API_KEY is not configured')
        model = model_id or self.model_id
        body = json.dumps({
            'text': speakable(text, model),  # his audio tags, filtered for this model
            'model_id': model,
            'voice_settings': {  # no 'style': v4 does not take it
                'stability': 0.5,
                'similarity_boost': 0.75,
                'use_speaker_boost': True,
            },
        }).encode('utf-8')
        headers = {'xi-api-key': key, 'content-type': 'application/json', 'accept': 'audio/mpeg'}
        return headers, body

    def synthesize(self, text, voice_id=None, model_id=None):
        """Speak ``text`` with ``voice_id`` (the call language's voice) or the default voice,
        in ``model_id`` (the caller's snapshot) or the configured model."""
        headers, body = self._request(text, model_id)
        try:
            status, data = self._post(self.URL.format(voice_id=voice_id or self.voice_id), headers, body, self._timeout)
        except Exception as exc:
            raise TTSError('tts request failed: %s' % type(exc).__name__) from exc
        if status < 200 or status >= 300:
            raise TTSError('tts http %s' % status)
        if not data:
            raise TTSError('tts returned no audio')
        return data

    def stream(self, text, voice_id=None, model_id=None):
        """Like synthesize, but yield the mp3 as ElevenLabs produces it."""
        headers, body = self._request(text, model_id)
        try:
            status, chunks = self._open_stream(
                self.STREAM_URL.format(voice_id=voice_id or self.voice_id), headers, body, self._timeout)
        except Exception as exc:
            raise TTSError('tts request failed: %s' % type(exc).__name__) from exc
        if status < 200 or status >= 300:
            raise TTSError('tts http %s' % status)
        try:
            for chunk in chunks:
                if chunk:
                    yield chunk
        except Exception as exc:
            raise TTSError('tts stream broke: %s' % type(exc).__name__) from exc


class _Synthesis:
    """One synthesis under way: its chunks arrive once, any number of readers follow along."""

    def __init__(self):
        self._chunks = []
        self._done = False
        self._error = None
        self._cond = threading.Condition()

    def push(self, chunk):
        with self._cond:
            self._chunks.append(chunk)
            self._cond.notify_all()

    def finish(self, error=None):
        with self._cond:
            self._done = True
            self._error = error
            self._cond.notify_all()

    def read(self):
        index = 0
        while True:
            with self._cond:
                while index >= len(self._chunks) and not self._done:
                    self._cond.wait()
                if index < len(self._chunks):
                    chunk = self._chunks[index]
                    index += 1
                elif self._error is not None:
                    raise self._error
                else:
                    return
            yield chunk


def _spawn_thread(target):
    threading.Thread(target=target, daemon=True, name='voice-call-tts').start()


class CachedTTS:
    """Per-line mp3 cache whose lines are synthesized at most once at a time,
    across every Gunicorn worker sharing ``cache_dir``.

    The owner of a line holds ``<key>.lock`` (flock) while it streams the line
    into its own ``<key>.<attempt>.part`` (named inside the lock file) and,
    once complete, renames it to ``<key>.mp3``. A reader in the same process
    follows the owner's chunks in memory; one in another worker tails the
    part file, so it too plays from the first bytes. An owner that dies drops
    its flock, and its readers stop with a TTSError instead of waiting forever.
    """

    def __init__(self, engine, cache_dir, spawn=_spawn_thread, poll_s=FOLLOW_POLL_S):
        self._engine = engine
        self._cache_dir = cache_dir
        self._spawn = spawn
        self._poll_s = poll_s
        self._lock = threading.Lock()
        self._running = {}  # key -> _Synthesis this process owns

    def key(self, text, voice_id=None, model_id=None):
        raw = '\0'.join((voice_id or self._engine.voice_id, model_id or self._engine.model_id, text))
        return hashlib.sha1(raw.encode('utf-8')).hexdigest()

    def path(self, key):
        return os.path.join(self._cache_dir, key + '.mp3')

    def _lock_path(self, key):
        return os.path.join(self._cache_dir, key + '.lock')

    @staticmethod
    def _read(path):
        try:
            with open(path, 'rb') as cached:
                return cached.read()
        except OSError:
            return None

    def stream(self, text, voice_id=None):
        """Return (key, mp3 chunks): from disk, from the synthesis already under way
        (in this worker or another), or a new one this worker owns."""
        model_id = self._engine.model_id  # read once: the model can change live between key and request
        key = self.key(text, voice_id, model_id)
        path = self.path(key)
        with self._lock:
            job = self._running.get(key)
            if job is not None:
                return key, job.read()
            cached = self._read(path)
            if cached is not None:
                return key, iter([cached])
            owner = self._try_own(key)
            if owner is None:
                return key, self._follow(key, path)
            cached = self._read(path)  # finished between our check and taking the lock
            if cached is not None:
                self._release(key, owner)
                return key, iter([cached])
            job = _Synthesis()
            self._running[key] = job
        self._spawn(lambda: self._synthesize(key, path, job, owner, text, voice_id, model_id))
        return key, job.read()

    def warm(self, text, voice_id=None):
        """Start synthesizing a line before anyone asks for it (never raises)."""
        try:
            self.stream(text, voice_id)
        except Exception:
            pass

    def get(self, text, voice_id=None):
        """Return (key, mp3 bytes), synthesizing only on a cache miss."""
        key, chunks = self.stream(text, voice_id)
        return key, b''.join(chunks)

    def _try_own(self, key):
        """The line's lock fd if this worker now owns it, else None (someone else does)."""
        os.makedirs(self._cache_dir, exist_ok=True)
        lock_path = self._lock_path(key)
        while True:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return None
            try:
                same = os.fstat(fd).st_ino == os.stat(lock_path).st_ino
            except FileNotFoundError:
                same = False
            if same:
                return fd
            os.close(fd)  # locked a file its owner already unlinked: take the current one

    def _release(self, key, fd):
        try:
            os.unlink(self._lock_path(key))  # unlinked while still held, so no one locks a stale file
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def _owner_alive(self, key):
        try:
            fd = os.open(self._lock_path(key), os.O_RDWR)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)  # closing drops a lock we may have just taken
        return False

    def _part_name(self, key):
        try:
            with open(self._lock_path(key), 'rb') as lock_file:
                name = lock_file.read().decode('ascii', 'ignore').strip()
        except OSError:
            return None
        if name.startswith(key + '.') and name.endswith('.part') and '/' not in name:
            return name
        return None

    def _follow(self, key, path):
        """Tail another worker's synthesis of this line from its part file."""
        part = None
        offset = 0
        while True:
            if part is None:
                name = self._part_name(key)
                part = os.path.join(self._cache_dir, name) if name else None
            data = b''
            if part is not None:
                try:
                    with open(part, 'rb') as growing:
                        growing.seek(offset)
                        data = growing.read()
                except FileNotFoundError:
                    pass
            if data:
                offset += len(data)
                yield data
                continue
            done = self._read(path)
            if done is not None:  # the part became the cache: same bytes, read the rest
                if len(done) > offset:
                    yield done[offset:]
                return
            if not self._owner_alive(key):
                done = self._read(path)
                if done is not None:
                    if len(done) > offset:
                        yield done[offset:]
                    return
                raise TTSError('tts synthesis ended without audio')
            time.sleep(self._poll_s)

    def _synthesize(self, key, path, job, owner, text, voice_id, model_id):
        part = '%s.%d-%s.part' % (path[:-len('.mp3')], os.getpid(), uuid.uuid4().hex[:8])
        error = None
        try:
            with open(part, 'wb') as out:
                os.ftruncate(owner, 0)
                os.pwrite(owner, os.path.basename(part).encode('ascii'), 0)
                stream = getattr(self._engine, 'stream', None)
                if callable(stream):
                    chunks = stream(text, voice_id, model_id)
                else:
                    chunks = [self._engine.synthesize(text, voice_id, model_id)]
                got = False
                for chunk in chunks:
                    if chunk:
                        out.write(chunk)
                        out.flush()  # other workers tail this file
                        job.push(chunk)
                        got = True
            if not got:
                raise TTSError('tts returned no audio')
            os.replace(part, path)  # only a complete line is ever cached
        except Exception as exc:
            error = exc if isinstance(exc, TTSError) else TTSError('tts failed: %s' % type(exc).__name__)
            try:
                os.unlink(part)
            except OSError:
                pass
        # Tidy up before anyone hears the outcome, so a retry finds a clean slate.
        with self._lock:
            self._running.pop(key, None)
            self._release(key, owner)
        job.finish(error)
