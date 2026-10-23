"""Hand one utterance to the normal chat pipeline and stream the reply back.

The call is not a second AI: her words are stored as an ordinary chat message
(tagged so the model knows it is a phone call) and the reply comes from the
same /chat/stream turn as any typed message, so it is remembered afterwards.
"""
import json
import urllib.request

APP_BASE = 'http://127.0.0.1:5050'
GATEWAY_BASE = 'http://127.0.0.1:5051'

PHONE_STYLE = (
    '〔电话接通〕现在是在打电话，不是打字聊天。\n'
    '用说话的口气，一轮两三句短句，说完就停下等她。\n'
    '一句一行；不要用列表、括号动作描写、表情符号或 Markdown，它们会被念出来。'
)


class BrainError(Exception):
    pass


def format_utterance(text, first_turn):
    line = '〔电话〕她刚在电话里说：「%s」' % text.strip()
    if first_turn:
        return PHONE_STYLE + '\n' + line
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


def _http_stream_reply(message_id, timeout=320):
    body = json.dumps({'user_message_id': message_id}).encode('utf-8')
    req = urllib.request.Request(GATEWAY_BASE + '/chat/stream', data=body, method='POST',
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        yield from parse_sse_text(resp)


class ChatPipelineBrain:
    def __init__(self, send_message=_http_send_message, stream_reply=_http_stream_reply):
        self._send_message = send_message
        self._stream_reply = stream_reply

    def reply(self, text, first_turn):
        message_id = self._send_message(format_utterance(text, first_turn))
        yield from self._stream_reply(message_id)
