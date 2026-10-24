"""Call languages: which voice speaks, how her speech is transcribed, how he answers.

Kept as JSON in runtime_config under VOICE_CALL_LANGUAGES, so a language (a
Chinese voice once one is trained, say) is added without a deploy:

    {"ja": {"label": "日本語", "voice_id": "0zi0ae2RvBmXaahlPt2n", "stt": "jpn", "speech": "ja-JP"},
     "en": {"label": "English", "voice_id": "K7JxslCTelez3qaKj5SJ", "stt": "eng", "speech": "en-US"}}

voice_id (ElevenLabs) and stt (Scribe ISO 639-3, or "auto" to let Scribe detect
whatever she speaks) are required; label defaults to the code; speech (the
Android fallback recognizer's locale) and fallback (what he says when his reply
fails) are optional. A malformed entry is skipped, never fatal. No entries
means today's single Chinese call with the default voice.

A bilingual call ("English, Chinese subtitles") adds subtitle: "zh". He then
writes every sentence as a pair of marked lines, the spoken line and its
gloss (see voice_call.pairs); only the spoken line is voiced, the gloss is only
shown. marker is the spoken line's mark (default: the code upper-cased, EN /
JA / RU); the gloss mark is the subtitle code upper-cased (ZH).

    {"en": {"label": "English", "voice_id": "...", "stt": "auto", "subtitle": "zh"}}

A call keeps a snapshot of its entry from ring (see CallService.ring), so an edit
here changes the next call, never one already ringing or talking.
"""
import json
import re

CONFIG_KEY = 'VOICE_CALL_LANGUAGES'

_CODE = re.compile(r'^[a-z][a-z0-9-]{0,15}$')
_VOICE = re.compile(r'^[A-Za-z0-9]{8,64}$')
_STT = re.compile(r'^([a-z]{3}|auto)$')
_SUBTITLE = re.compile(r'^[a-z]{2,3}$')
_MARKER = re.compile(r'^[A-Z]{2,3}$')
_SPEECH = re.compile(r'^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$')


def _config_get(key):
    import config_store  # imported lazily: it opens /opt/frontend/memories.db on import
    return config_store.get(key, '')


def load(get=None):
    """Return the valid configured languages as {code: entry}, in configured order."""
    raw = (get or _config_get)(CONFIG_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    languages = {}
    for code, entry in data.items():
        if not isinstance(code, str) or not _CODE.match(code) or not isinstance(entry, dict):
            continue
        voice_id, stt = entry.get('voice_id'), entry.get('stt')
        if not (isinstance(voice_id, str) and _VOICE.match(voice_id)
                and isinstance(stt, str) and _STT.match(stt)):
            continue
        label = entry.get('label')
        speech = entry.get('speech')
        fallback = entry.get('fallback')
        subtitle = entry.get('subtitle')
        subtitle = subtitle if isinstance(subtitle, str) and _SUBTITLE.match(subtitle) else None
        marker = entry.get('marker')
        if not (isinstance(marker, str) and _MARKER.match(marker)):
            marker = code.split('-')[0].upper()
        if subtitle and (not _MARKER.match(marker) or marker == subtitle.upper()):
            subtitle = None  # the two marks must differ, or the pair cannot be told apart
        languages[code] = {
            'code': code,
            'label': label.strip() if isinstance(label, str) and label.strip() else code,
            'voice_id': voice_id,
            'stt': stt,
            'speech': speech if isinstance(speech, str) and _SPEECH.match(speech) else None,
            'fallback': fallback.strip() if isinstance(fallback, str) and 0 < len(fallback.strip()) <= 200 else None,
            'subtitle': subtitle,
            'marker': marker if subtitle else None,
        }
    return languages


SNAPSHOT_KEYS = ('label', 'voice_id', 'stt', 'speech', 'fallback', 'subtitle', 'marker')


def snapshot(entry):
    """The part of an entry a call freezes at ring (it keeps it for its whole life)."""
    return {key: entry.get(key) for key in SNAPSHOT_KEYS}


def public(languages):
    """What the page needs to offer a choice: no voice ids."""
    return [{'code': e['code'], 'label': e['label'], 'speech': e['speech']} for e in languages.values()]
