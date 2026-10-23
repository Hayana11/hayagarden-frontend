"""ElevenLabs text-to-speech with a per-sentence content cache.

One request per sentence so the first line can play while the rest are still
being generated. Synthesis is streamed: the page starts playing at the first
bytes, and a line is started as soon as he says it (CachedTTS.warm), so the
page usually joins a synthesis already under way. The cache key covers voice,
model and text: a phone that reconnects and asks for the same line again is
served from disk instead of being billed twice.
"""
import hashlib
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request

STREAM_CHUNK = 4096

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
        body = json.dumps({
            'text': text,
            'model_id': model_id or self.model_id,
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
    def __init__(self, engine, cache_dir, spawn=_spawn_thread):
        self._engine = engine
        self._cache_dir = cache_dir
        self._spawn = spawn
        self._lock = threading.Lock()
        self._running = {}  # key -> _Synthesis, while it is being synthesized

    def key(self, text, voice_id=None, model_id=None):
        raw = '\0'.join((voice_id or self._engine.voice_id, model_id or self._engine.model_id, text))
        return hashlib.sha1(raw.encode('utf-8')).hexdigest()

    def path(self, key):
        return os.path.join(self._cache_dir, key + '.mp3')

    def stream(self, text, voice_id=None):
        """Return (key, mp3 chunks): from disk, from the synthesis already under way, or a new one.

        A line is synthesized at most once at a time however many ask for it.
        """
        model_id = self._engine.model_id  # read once: the model can change live between key and request
        key = self.key(text, voice_id, model_id)
        path = self.path(key)
        started = False
        with self._lock:
            job = self._running.get(key)
            if job is None:
                try:
                    with open(path, 'rb') as cached:
                        return key, iter([cached.read()])
                except OSError:
                    pass
                job = _Synthesis()
                self._running[key] = job
                started = True
        if started:  # outside the lock: _synthesize takes it again when it ends
            self._spawn(lambda: self._synthesize(key, path, job, text, voice_id, model_id))
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

    def _synthesize(self, key, path, job, text, voice_id, model_id):
        tmp = None
        try:
            stream = getattr(self._engine, 'stream', None)
            if callable(stream):
                chunks = stream(text, voice_id, model_id)
            else:
                chunks = [self._engine.synthesize(text, voice_id, model_id)]
            os.makedirs(self._cache_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._cache_dir, suffix='.part')
            got = False
            with os.fdopen(fd, 'wb') as out:
                for chunk in chunks:
                    if chunk:
                        out.write(chunk)
                        job.push(chunk)
                        got = True
            if not got:
                raise TTSError('tts returned no audio')
            os.replace(tmp, path)  # only a complete line is ever cached
            tmp = None
            job.finish()
        except Exception as exc:
            job.finish(exc if isinstance(exc, TTSError) else TTSError('tts failed: %s' % type(exc).__name__))
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            with self._lock:
                self._running.pop(key, None)
