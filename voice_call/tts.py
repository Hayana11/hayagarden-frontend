"""ElevenLabs text-to-speech with a per-sentence content cache.

One request per sentence so the first line can play while the rest are still
being generated. The cache key covers voice, model and text: a phone that
reconnects and asks for the same line again is served from disk instead of
being billed twice.
"""
import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request

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


class ElevenLabsTTS:
    URL = 'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128'

    def __init__(self, voice_id=DEFAULT_VOICE_ID, model_id=DEFAULT_MODEL_ID,
                 api_key=None, post=_urllib_post, timeout=30):
        """``model_id`` is a model name, or a callable read on every request so a
        runtime-config change takes effect without a restart."""
        self.voice_id = voice_id
        self._model_id = model_id
        self._api_key = api_key
        self._post = post
        self._timeout = timeout

    @property
    def model_id(self):
        model = self._model_id() if callable(self._model_id) else self._model_id
        return model or DEFAULT_MODEL_ID

    def api_key(self):
        return self._api_key if self._api_key is not None else read_env_value('ELEVENLABS_API_KEY')

    def synthesize(self, text, voice_id=None):
        """Speak ``text`` with ``voice_id`` (the call language's voice) or the default voice."""
        key = self.api_key()
        if not key:
            raise TTSError('ELEVENLABS_API_KEY is not configured')
        body = json.dumps({
            'text': text,
            'model_id': self.model_id,
            'voice_settings': {  # no 'style': v4 does not take it
                'stability': 0.5,
                'similarity_boost': 0.75,
                'use_speaker_boost': True,
            },
        }).encode('utf-8')
        headers = {'xi-api-key': key, 'content-type': 'application/json', 'accept': 'audio/mpeg'}
        try:
            status, data = self._post(self.URL.format(voice_id=voice_id or self.voice_id), headers, body, self._timeout)
        except Exception as exc:
            raise TTSError('tts request failed: %s' % type(exc).__name__) from exc
        if status < 200 or status >= 300:
            raise TTSError('tts http %s' % status)
        if not data:
            raise TTSError('tts returned no audio')
        return data


class CachedTTS:
    def __init__(self, engine, cache_dir):
        self._engine = engine
        self._cache_dir = cache_dir

    def key(self, text, voice_id=None):
        raw = '\0'.join((voice_id or self._engine.voice_id, self._engine.model_id, text))
        return hashlib.sha1(raw.encode('utf-8')).hexdigest()

    def path(self, key):
        return os.path.join(self._cache_dir, key + '.mp3')

    def get(self, text, voice_id=None):
        """Return (key, mp3 bytes), synthesizing only on a cache miss."""
        key = self.key(text, voice_id)
        path = self.path(key)
        try:
            with open(path, 'rb') as cached:
                return key, cached.read()
        except OSError:
            pass
        audio = self._engine.synthesize(text, voice_id)
        os.makedirs(self._cache_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._cache_dir, suffix='.part')
        with os.fdopen(fd, 'wb') as out:
            out.write(audio)
        os.replace(tmp, path)
        return key, audio
