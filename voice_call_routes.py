"""HTTP surface for voice calls under /api/call. Owner-only: audio costs money."""
import json

from flask import Blueprint, Response, jsonify, request, stream_with_context

from moments_auth import OwnerAuthError, require_owner
from voice_call.store import CallStateError
from voice_call.tts import TTSError

STREAM_WINDOW_S = 25


def _sse(event, data, event_id=None):
    lines = []
    if event_id is not None:
        lines.append('id: %s' % event_id)
    lines.append('event: %s' % event)
    lines.append('data: ' + json.dumps(data, ensure_ascii=False, separators=(',', ':')))
    return '\n'.join(lines) + '\n\n'


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
        return jsonify({'ok': True, 'call': service.ring()}), 201

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
        service.interrupt(call_id)
        return jsonify({'ok': True})

    @bp.post('/<call_id>/say')
    def say(call_id):
        data = request.get_json(silent=True) or {}
        return jsonify({'ok': True, 'line': service.say(call_id, data.get('text'))})

    @bp.get('/<call_id>/stream')
    def stream(call_id):
        service.get(call_id)
        try:
            after = int(request.args.get('after') or request.headers.get('Last-Event-ID') or 0)
        except ValueError:
            after = 0

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
