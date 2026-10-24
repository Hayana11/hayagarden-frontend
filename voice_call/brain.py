"""Hand one utterance to the normal chat pipeline and stream the reply back.

The call is not a second AI: her words are stored as an ordinary chat message
(tagged so the model knows it is a phone call) and the reply comes from the
same /chat/stream turn as any typed message, so it is remembered afterwards.
"""
import json
import threading
import urllib.request

from voice_call.tags import PROMPT_HINT

APP_BASE = 'http://127.0.0.1:5050'
GATEWAY_BASE = 'http://127.0.0.1:5051'

# The model reads this at the start of each call (the chat page never shows
# call turns). Runtime config VOICE_CALL_PHONE_STYLE replaces it; empty = this.
PHONE_STYLE_CONFIG_KEY = 'VOICE_CALL_PHONE_STYLE'
PHONE_STYLE = (
    '〔电话〕你们正在打电话，她听得见你的声音。\n'
    '像平常跟她说话那样开口：句子短，一次两三句，说完就停下来听她说。\n'
    + PROMPT_HINT + '停顿用省略号……\n'
    '别写圆括号里的动作描写、表情符号、列表或 Markdown，它们会被原样念出来。'
)


def phone_style():
    """The configured call prompt, or PHONE_STYLE (also when config cannot be read)."""
    try:
        import config_store
        configured = str(config_store.get(PHONE_STYLE_CONFIG_KEY, '') or '').strip()
    except Exception:
        configured = ''
    return configured or PHONE_STYLE


class BrainError(Exception):
    pass


LANGUAGE_STYLE = '这通电话用{0}：她说{0}，你也只用{0}说话。'


CALLER_STYLE = '这通电话是你主动打给她的；她接起来时你先说了：「{0}」'

# A bilingual call (see voice_call.pairs): the spoken line and its gloss, as marked pairs.
PAIR_STYLE = (
    '这通电话有中文字幕：她听你说{label}，屏幕上看中文。每一句写成两行，一句一组：\n'
    '{m}: 你要说出口的{label}（语气标签只放在这一行）\n'
    '{g}: 这句的中文意思（只给她看，不会念出来）\n'
    '比如：\n{m}: [softly] Take your time.\n{g}: 慢慢来。\n'
    '不要把两种语言写在同一行，也不要写别的格式。她说什么语言都行，你都用{label}回答。'
)
PAIR_REMINDER = '（用{label}回答，每句写 {m}: 和 {g}: 两行）'


def format_utterance(text, first_turn, language=None, opening=None, pairs=None):
    """``language`` is the call language's label (日本語, English…); None is the default call.
    ``opening`` is his first line when he placed the call (given on the first turn only).
    ``pairs`` is (spoken mark, gloss mark) in a bilingual call, e.g. ('EN', 'ZH')."""
    line = '〔电话〕她刚在电话里说：「%s」' % text.strip()
    if language and pairs:
        line += PAIR_REMINDER.format(label=language, m=pairs[0], g=pairs[1])
    elif language:
        line += '（用%s回答）' % language  # every turn: the chat history around it is Chinese
    if first_turn:
        if language and pairs:
            style = phone_style() + '\n' + PAIR_STYLE.format(label=language, m=pairs[0], g=pairs[1])
        else:
            style = phone_style() + ('\n' + LANGUAGE_STYLE.format(language) if language else '')
        if opening:
            style += '\n' + CALLER_STYLE.format(opening.strip())
        return style + '\n' + line
    return line


def parse_sse_text(lines):
    """Yield text deltas from a /chat/stream SSE body given as an iterable of lines."""
    for raw in lines:
        line = raw.decode('utf-8') if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line.startswith('data:'):
            continue
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            continue
        kind = event.get('t')
        if kind == 'text':
            yield event.get('d') or ''
        elif kind == 'err':
            raise BrainError(str(event.get('d') or 'stream error'))
        elif kind == 'done':
            return


def _http_send_message(content, timeout=15):
    body = json.dumps({'author': 'hayana', 'content': content}).encode('utf-8')
    req = urllib.request.Request(APP_BASE + '/api/chat/send', data=body, method='POST',
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode('utf-8'))
    if not payload.get('ok'):
        raise BrainError('chat send failed: %s' % payload.get('error'))
    return payload.get('message_id')


class _HTTPStream:
    """Own the HTTP response so a caller can abort one chat turn only."""

    def __init__(self, request, timeout):
        self._lock = threading.Lock()
        self._response = urllib.request.urlopen(request, timeout=timeout)
        self._closed = False

    def __iter__(self):
        with self._lock:
            response = self._response
        try:
            yield from parse_sse_text(response)
        finally:
            self.close()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            response = self._response
        try:
            response.close()
        except Exception:
            pass


class CancellableReply:
    """An iterable reply whose close aborts only its underlying stream."""

    def __init__(self, stream, message_id=None):
        self._stream = stream
        self.message_id = message_id  # her chat message this reply answers
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._closed = False

    def __iter__(self):
        try:
            for chunk in self._stream:
                if self._cancelled.is_set():
                    return
                yield chunk
        except Exception:
            if self._cancelled.is_set():
                return
            raise
        finally:
            self.cancel()

    def cancel(self):
        self._cancelled.set()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            stream = self._stream
        close = getattr(stream, 'close', None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _http_stream_reply(message_id, timeout=320):
    body = json.dumps({'user_message_id': message_id}).encode('utf-8')
    req = urllib.request.Request(GATEWAY_BASE + '/chat/stream', data=body, method='POST',
                                 headers={'Content-Type': 'application/json'})
    return _HTTPStream(req, timeout)


class ChatPipelineBrain:
    def __init__(self, send_message=_http_send_message, stream_reply=_http_stream_reply):
        self._send_message = send_message
        self._stream_reply = stream_reply

    def start_reply(self, text, first_turn, language=None, opening=None, pairs=None):
        message_id = self._send_message(format_utterance(text, first_turn, language, opening, pairs))
        return CancellableReply(self._stream_reply(message_id), message_id=message_id)

    def reply(self, text, first_turn, language=None, opening=None, pairs=None):
        handle = self.start_reply(text, first_turn, language, opening, pairs)
        try:
            yield from handle
        finally:
            handle.cancel()
