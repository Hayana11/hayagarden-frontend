"""HTTP surface for voice calls under /api/call. Owner-only: audio costs money."""
import json

from flask import Blueprint, Response, jsonify, request, stream_with_context

from moments_auth import OwnerAuthError, require_owner
from voice_call.store import CallStateError
from voice_call.service import MAX_CLIP_BYTES
from voice_call.stt import AUDIO_TYPES, STTError
from voice_call.tts import TTSError

STREAM_WINDOW_S = 25


def _sse(event, data, event_id=None):
    lines = []
    if event_id is not None:
        lines.append('id: %s' % event_id)
    lines.append('event: %s' % event)
    lines.append('data: ' + json.dumps(data, ensure_ascii=False, separators=(',', ':')))
    return '\n'.join(lines) + '\n\n'


def _utterance_arg():
    seq = request.args.get('seq')
    utt = request.args.get('utt')
    if seq is not None and utt is not None and seq != utt:
        raise CallStateError('seq and utt disagree', 400)
    raw = seq if seq is not None else utt
    if raw is None:
        return None
    if not raw.isdigit() or not 0 < int(raw) < 10 ** 9:
        raise CallStateError('seq must be a positive integer', 400)
    return int(raw)


def _prev_arg():
    """prev=<n>: the seq she started just before this one in this call (0 for the first)."""
    raw = request.args.get('prev')
    if raw is None:
        return None
    if not raw.isdigit() or not int(raw) < 10 ** 9:
        raise CallStateError('prev must be a non-negative integer', 400)
    return int(raw)


def create_voice_call_blueprint(service, owner_guard=None, stream_window_s=STREAM_WINDOW_S):
    bp = Blueprint('voice_call', __name__, url_prefix='/api/call')
    guard = owner_guard or require_owner

    @bp.before_request
    def require_call_owner():
        try:
            guard(request)
        except OwnerAuthError as exc:
            response = jsonify({'ok': False, 'code': 'OWNER_AUTH_REQUIRED', 'detail': exc.message})
            response.status_code = exc.status_code
            return response
        return None

    @bp.errorhandler(CallStateError)
    def call_state_error(exc):
        return jsonify({'ok': False, 'error': str(exc)}), exc.status

    @bp.post('/ring')
    def ring():
        """Body (optional): {"language": "<code from GET /languages>"}."""
        data = request.get_json(silent=True) or {}
        language = None
        if isinstance(data, dict) and 'language' in data:
            language = data['language']
            if not isinstance(language, str) or not language:
                raise CallStateError('language must be a configured code', 400)
        return jsonify({'ok': True, 'call': service.ring(language)}), 201

    @bp.get('/languages')
    def languages():
        return jsonify({'ok': True, 'languages': service.languages()})

    @bp.get('/current')
    def current():
        return jsonify({'ok': True, 'call': service.current()})

    @bp.get('/<call_id>')
    def get_call(call_id):
        lines, call = service.lines_after(call_id, 0)
        return jsonify({'ok': True, 'call': call, 'lines': lines})

    @bp.post('/<call_id>/answer')
    def answer(call_id):
        return jsonify({'ok': True, 'call': service.answer(call_id)})

    @bp.post('/<call_id>/decline')
    def decline(call_id):
        return jsonify({'ok': True, **service.decline(call_id)})

    @bp.post('/<call_id>/cancel')
    def cancel(call_id):
        return jsonify({'ok': True, **service.cancel(call_id)})

    @bp.post('/<call_id>/hangup')
    def hangup(call_id):
        # Also the sendBeacon target, so the body (if any) is ignored.
        return jsonify({'ok': True, **service.hangup(call_id)})

    @bp.post('/<call_id>/interrupt')
    def interrupt(call_id):
        service.interrupt(call_id, _utterance_arg(), _prev_arg())
        return jsonify({'ok': True})

    @bp.post('/<call_id>/say')
    def say(call_id):
        data = request.get_json(silent=True) or {}
        return jsonify({'ok': True, 'line': service.say(call_id, data.get('text'))})

    @bp.post('/<call_id>/hear')
    def hear(call_id):
        """Body: one utterance (audio/wav PCM16 from the shell).

        Query: seq=<n> (with legacy utt=<n> accepted as an alias), prev=<n> (see
        CallService.hear), lang=zho|eng, discard=1 (the shell threw this utterance
        away: no body).
        """
        utterance = _utterance_arg()
        prev = _prev_arg()
        length = request.content_length
        if length is not None and length > MAX_CLIP_BYTES:
            return jsonify({'ok': False, 'error': 'clip too large'}), 413
        if request.args.get('discard') == '1':
            discarded_body = request.stream.read(MAX_CLIP_BYTES + 1)
            if len(discarded_body) > MAX_CLIP_BYTES:
                return jsonify({'ok': False, 'error': 'clip too large'}), 413
            return jsonify({'ok': True, **service.hear(call_id, None, None, utterance=utterance, prev=prev)})
        mime = (request.mimetype or '').lower()
        if mime not in AUDIO_TYPES:
            return jsonify({'ok': False, 'error': 'unsupported audio type'}), 415
        # Never buffer more than the cap, even without (or with a lying) Content-Length.
        audio = request.stream.read(MAX_CLIP_BYTES + 1)
        if len(audio) > MAX_CLIP_BYTES:
            return jsonify({'ok': False, 'error': 'clip too large'}), 413
        try:
            result = service.hear(call_id, audio, mime, request.args.get('lang'), utterance, prev)
        except STTError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 502
        return jsonify({'ok': True, **result})

    @bp.get('/<call_id>/stream')
    def stream(call_id):
        service.get(call_id)
        # EventSource reconnects keep the original ?after= but send Last-Event-ID.
        after = 0
        for raw in (request.args.get('after'), request.headers.get('Last-Event-ID')):
            try:
                after = max(after, int(raw or 0))
            except ValueError:
                pass

        def generate():
            last = after
            lines, call = service.lines_after(call_id, last, wait_s=stream_window_s)
            for line in lines:
                last = line['seq']
                yield _sse('line', line, event_id=last)
            yield _sse('state', {'state': call['state'] if call else 'gone', 'after': last})

        return Response(stream_with_context(generate()), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @bp.get('/<call_id>/audio/<int:seq>')
    def audio(call_id, seq):
        try:
            data = service.audio(call_id, seq)
        except TTSError as exc:
            # Never fall back to another voice: the page shows the text only.
            return jsonify({'ok': False, 'error': str(exc)}), 503
        return Response(data, mimetype='audio/mpeg',
                        headers={'Cache-Control': 'private, max-age=86400'})

    return bp
