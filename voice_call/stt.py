"""ElevenLabs speech-to-text (Scribe) for one finished utterance.

The phone decides when a sentence is over (on-device VAD) and uploads that one
clip; this turns it into text for CallService.say. Same key as the voice
(ELEVENLABS_API_KEY), read through tts.read_env_value so it is never logged.
"""
import json
import re
import uuid

from voice_call.tts import _urllib_post, read_env_value

DEFAULT_MODEL_ID = 'scribe_v2'
DEFAULT_LANGUAGE = 'zho'
LANGUAGE_CODE = re.compile(r'^[a-z]{3}$')  # Scribe takes ISO 639-3 codes
AUTO = 'auto'  # no language_code: Scribe detects the language itself
AUDIO_TYPES = {
    'audio/wav': 'clip.wav',
    'audio/x-wav': 'clip.wav',
    'audio/wave': 'clip.wav',
    'audio/webm': 'clip.webm',
    'audio/ogg': 'clip.ogg',
    'audio/mpeg': 'clip.mp3',
    'audio/mp4': 'clip.m4a',
}


class STTError(Exception):
    pass


def _multipart(fields, file_name, mime, data):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields:
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                      % (boundary, name, value)).encode('utf-8'))
    parts.append(('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                  'Content-Type: %s\r\n\r\n' % (boundary, file_name, mime)).encode('utf-8'))
    parts.append(data)
    parts.append(('\r\n--%s--\r\n' % boundary).encode('utf-8'))
    return boundary, b''.join(parts)


class ElevenLabsSTT:
    URL = 'https://api.elevenlabs.io/v1/speech-to-text'

    def __init__(self, model_id=DEFAULT_MODEL_ID, api_key=None, post=_urllib_post, timeout=30):
        self.model_id = model_id
        self._api_key = api_key
        self._post = post
        self._timeout = timeout

    def api_key(self):
        return self._api_key if self._api_key is not None else read_env_value('ELEVENLABS_API_KEY')

    def transcribe(self, audio, mime='audio/wav', language=DEFAULT_LANGUAGE):
        key = self.api_key()
        if not key:
            raise STTError('ELEVENLABS_API_KEY is not configured')
        if mime not in AUDIO_TYPES:
            raise STTError('unsupported audio type')
        fields = [('model_id', self.model_id), ('tag_audio_events', 'false')]
        if language != AUTO:
            if not isinstance(language, str) or not LANGUAGE_CODE.match(language):
                language = DEFAULT_LANGUAGE
            fields.insert(1, ('language_code', language))
        boundary, body = _multipart(fields, AUDIO_TYPES[mime], mime, audio)
        headers = {'xi-api-key': key, 'content-type': 'multipart/form-data; boundary=' + boundary}
        try:
            status, data = self._post(self.URL, headers, body, self._timeout)
        except Exception as exc:
            raise STTError('stt request failed: %s' % type(exc).__name__) from exc
        if status < 200 or status >= 300:
            raise STTError('stt http %s' % status)
        try:
            text = json.loads(data.decode('utf-8')).get('text')
        except (ValueError, AttributeError) as exc:
            raise STTError('stt returned no json') from exc
        return (text or '').strip()
