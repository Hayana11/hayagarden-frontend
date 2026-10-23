"""HTTP surface for wake notification delivery (protocols: see wake_push.py)."""
import time
import uuid

from flask import Blueprint, jsonify, request

import wake_push
from tools import health_store


def _unauthorized():
    response = jsonify({'ok': False, 'error': 'unauthorized'})
    response.status_code = 401
    response.headers['WWW-Authenticate'] = 'Bearer'
    return response


def create_wake_push_blueprint(get_db, fetch_row, soft_window_enabled, health_db_path,
                               clock=time.time, incoming_call=None):
    """``incoming_call()`` returns the id of a call he placed that is ringing now, or None."""
    bp = Blueprint('wake_push', __name__)

    def device_id_from_request():
        """Health Bridge device credential; None if absent or wrong. Never echoes it."""
        device_id = (request.headers.get('X-Health-Device-ID') or '').strip()
        authorization = request.headers.get('Authorization', '')
        credential = authorization[7:].strip() if authorization.startswith('Bearer ') else ''
        if not device_id or not credential:
            return None
        if not health_store.authenticate_device(health_db_path, device_id, credential):
            return None
        return str(uuid.UUID(device_id))

    @bp.get('/api/wake_log/pending_notification')
    def pending_notification():
        """供 app 拉取费奥多尔自主发出、还没送达的消息。
        只支持 protocol=ack：需要 Health Bridge 设备凭证，只发绑定该设备的租约，
        客户端展示后 POST /api/wake_log/ack 才算送达。
        旧的“领取即消费”协议已停用（#625），不带 protocol 的请求返回 410、不动任何状态。
        带 ?wait=N（最多 25 秒）时挂住请求直到有可投递内容或超时。"""
        if request.args.get('protocol') != wake_push.ACK_PROTOCOL:
            return jsonify({
                'ok': False,
                'error': 'legacy consume-on-read protocol retired; use protocol=ack with a device credential',
            }), 410
        device_id = device_id_from_request()
        if device_id is None:
            return _unauthorized()
        # ?since= from older Canary builds and ?commands= (phone screenshots, retired)
        # are accepted and ignored (see wake_push).
        # ?known_call= is the call the phone shows as ringing. The response carries a
        # call delta beside (never instead of) the message: the message lease/ack is
        # unchanged and the call is neither leased nor acked. The poll returns when a
        # message can be leased or the ringing call differs from known_call.
        known_call = wake_push.parse_known_call(request.args.get('known_call'))

        def take(conn):
            payload = wake_push.lease_pending_payload(
                conn, fetch_row, soft_window_enabled(), clock(), device_id,
            )
            if incoming_call is None:
                return payload
            delta = wake_push.call_delta(incoming_call(), known_call)
            return wake_push.with_call_delta(payload, delta)

        return jsonify(wake_push.poll(get_db, take, wake_push.parse_wait(request.args.get('wait'))))

    @bp.post('/api/wake_log/ack')
    def ack_notification():
        """设备确认已展示/已接收；幂等。body: {"ids": ["wake-12"]}
        只能确认本设备领取过的 id；任何一个不是，就整批 403、不改任何状态。"""
        device_id = device_id_from_request()
        if device_id is None:
            return _unauthorized()
        data = request.get_json(silent=True) or {}
        ids = data.get('ids')
        if ids is None and data.get('id'):
            ids = [data.get('id')]
        if not isinstance(ids, list) or not ids or len(ids) > 50:
            return jsonify({'ok': False, 'error': 'ids must be a non-empty list'}), 400
        conn = get_db()
        try:
            results = wake_push.ack(conn, ids, soft_window_enabled(), device_id, clock())
        except wake_push.AckRefused:
            return jsonify({'ok': False, 'error': 'not delivered to this device'}), 403
        finally:
            conn.close()
        return jsonify({'ok': True, 'results': results})

    return bp
