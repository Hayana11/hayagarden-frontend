import http.client
import urllib.parse

from flask import Response, jsonify, request, stream_with_context

from tools import workspace_apps
from tools.workspace_apps import WorkspaceAppError, proxy_target, verified_proxy_upstream


_WS_PROXY_HOP_BY_HOP = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailer', 'transfer-encoding', 'upgrade',
}
_WS_PROXY_REQ_DENY = _WS_PROXY_HOP_BY_HOP | {'host', 'content-length'}
_WS_PROXY_RESP_DENY = _WS_PROXY_HOP_BY_HOP | {'content-length'}


def _workspace_proxy_request_headers():
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _WS_PROXY_REQ_DENY
    }


def _workspace_proxy_response_headers(headers, *, app_id: str, upstream: str) -> dict[str, str]:
    out: dict[str, str] = {}
    proxy_base = f"/api/gw/workspace/apps/{app_id}/proxy"
    for key, value in headers:
        lower = key.lower()
        if lower in _WS_PROXY_RESP_DENY:
            continue
        if lower == 'location':
            if value.startswith(upstream):
                value = proxy_base + value[len(upstream):]
            elif value.startswith('/'):
                value = proxy_base + value
        out[key] = value
    return out


def workspace_apps_list():
    return jsonify({'apps': workspace_apps.list_apps()})


def workspace_app_detail(app_id):
    try:
        return jsonify(workspace_apps.app_status(app_id))
    except WorkspaceAppError as exc:
        return jsonify({'error': exc.code, 'detail': exc.detail}), exc.status_code


def workspace_app_proxy(app_id, path):
    """Reverse proxy to loopback workspace app (127.0.0.1 only)."""
    try:
        upstream = verified_proxy_upstream(app_id)
    except WorkspaceAppError as exc:
        return jsonify({'error': exc.code, 'detail': exc.detail}), exc.status_code

    query = request.query_string.decode('utf-8', errors='replace')
    target = proxy_target(upstream, path, query)
    parsed = urllib.parse.urlparse(target)
    body = request.get_data()
    req_headers = _workspace_proxy_request_headers()
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=60)
    try:
        req_path = parsed.path or '/'
        if parsed.query:
            req_path += '?' + parsed.query
        conn.request(request.method, req_path, body=body, headers=req_headers)
        upstream_resp = conn.getresponse()
        resp_headers = _workspace_proxy_response_headers(
            upstream_resp.getheaders(), app_id=app_id, upstream=upstream,
        )

        def generate():
            try:
                while True:
                    chunk = upstream_resp.read(8192)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    upstream_resp.close()
                finally:
                    conn.close()

        return Response(
            stream_with_context(generate()),
            status=upstream_resp.status,
            headers=resp_headers,
        )
    except Exception as exc:
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({
            'error': 'workspace_app_upstream_unavailable',
            'detail': str(exc)[:240],
        }), 502
