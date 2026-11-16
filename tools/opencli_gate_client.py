"""VPS 端小红书门卫客户端：经 Tailscale 调用哈娅电脑上的 opencli-gate（只读）。"""
import json
import os
import socket
import urllib.error
import urllib.request

ENV_PATH = '/opt/frontend/.env'
MAX_CHARS = 30000
STATUS_TIMEOUT = 25
XHS_TIMEOUT = 120

OFFLINE_MSG = 'computer_not_connected：哈娅电脑上的小红书门卫不在线（电脑关机、睡眠或 Tailscale 未连接）。'
MISCONFIGURED_MSG = ('opencli_gate_misconfigured：未配置 OPENCLI_GATE_URL / OPENCLI_GATE_TOKEN'
                     '（/opt/frontend/.env 或环境变量）')
AUTH_FAILED_MSG = 'opencli_gate_auth_failed：门卫拒绝了 token（OPENCLI_GATE_TOKEN 与电脑上的配置不一致）'

_ERROR_TEXT = {
    'auth_required': '小红书账号需要重新登录（请哈娅在电脑的 OpenCLI Chrome 里重新登录）。',
    'bridge_down': '浏览器扩展没连上（哈娅电脑上的 OpenCLI Chrome 扩展未运行）。',
    'busy': '门卫正在执行上一条命令，稍后重试。',
    'empty': '没有找到内容（结果为空）。',
    'timeout': '小红书命令超时。',
    'bad_output': '门卫返回了无法解析的输出。',
    'opencli_failed': 'OpenCLI 执行失败。',
}


def _read_env_value(key):
    try:
        with open(ENV_PATH, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + '='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ''


def _config(key):
    return (os.environ.get(key) or '').strip() or _read_env_value(key)


def _truncate(text, limit=MAX_CHARS):
    if len(text) <= limit:
        return text
    return text[:limit] + '\n…（已截断，共 %d 字符）' % len(text)


def _request(method, path, body, timeout):
    """返回 (payload_dict, error_str)。"""
    url = _config('OPENCLI_GATE_URL').rstrip('/')
    token = _config('OPENCLI_GATE_TOKEN')
    if not url or not token:
        return None, MISCONFIGURED_MSG
    data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method)
    req.add_header('Authorization', 'Bearer ' + token)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        raw = ''
        try:
            raw = e.read().decode('utf-8', 'replace')
        except Exception:
            pass
        if e.code == 401:
            return None, AUTH_FAILED_MSG
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                return payload, None
        except Exception:
            pass
        return None, 'opencli_gate_http_%d：%s' % (e.code, raw[:120])
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError):
        return None, OFFLINE_MSG
    try:
        payload = json.loads(raw)
    except Exception:
        return None, 'opencli_gate_error：门卫返回了非 JSON 内容'
    if not isinstance(payload, dict):
        return None, 'opencli_gate_error：门卫返回格式异常'
    return payload, None


def _format_failure(payload):
    code = str(payload.get('error') or 'opencli_failed')
    text = _ERROR_TEXT.get(code)
    if text is None:
        text = '失败：' + code[:200]
    return '%s：%s' % (code, text)


def status():
    payload, err = _request('GET', '/status', None, STATUS_TIMEOUT)
    if err:
        return err
    if not payload.get('ok'):
        return 'opencli_gate_unhealthy：门卫在线，但 OpenCLI 自检未通过。\n' + _truncate(str(payload.get('doctor') or ''), 2000)
    return '小红书门卫在线。\n' + _truncate(str(payload.get('doctor') or ''), 2000)


def xhs(command, **params):
    body = {'command': command}
    body.update({k: v for k, v in params.items() if v is not None})
    payload, err = _request('POST', '/xhs', body, XHS_TIMEOUT)
    if err:
        return err
    if payload.get('ok'):
        return _truncate(json.dumps(payload.get('data'), ensure_ascii=False, separators=(',', ':')))
    return _format_failure(payload)
