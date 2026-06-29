"""工作台独立服务 — port 5052
只服务 workspace.html 和 /api/workspace/* 接口
其他接口（gateway、记忆等）通过nginx代理到5050/5051
"""
import os, subprocess, pathlib, json
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder='static')

WHITELIST = ['/opt/frontend', '/etc/nginx']

def ws_allowed(path):
    p = str(pathlib.Path(path).resolve())
    return any(p == w or p.startswith(w + '/') for w in WHITELIST)

@app.route('/')
@app.route('/workspace')
def index():
    return send_from_directory('static', 'workspace.html')

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)

@app.route('/api/workspace/tree')
def ws_tree():
    root = request.args.get('dir', '/opt/frontend')
    if not ws_allowed(root):
        return jsonify({'error': 'not allowed'}), 403
    def build(path, depth=0):
        items = []
        try:
            entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name))
        except PermissionError:
            return items
        for e in entries:
            if e.name.startswith('.') and e.name not in ('.env',): continue
            if e.name in ('__pycache__', 'node_modules', '.git'): continue
            node = {'name': e.name, 'path': e.path, 'is_dir': e.is_dir()}
            if e.is_dir() and depth < 3:
                node['children'] = build(e.path, depth + 1)
            items.append(node)
        return items
    return jsonify({'tree': build(root), 'root': root})

@app.route('/api/workspace/file')
def ws_file():
    path = request.args.get('path', '')
    if not path or not ws_allowed(path):
        return jsonify({'error': 'not allowed'}), 403
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        return jsonify({'content': content, 'path': path, 'lines': content.count('\n') + 1})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/workspace/write', methods=['POST'])
def ws_write():
    data = request.get_json() or {}
    path, content = data.get('path', ''), data.get('content', '')
    if not path or not ws_allowed(path):
        return jsonify({'error': 'not allowed'}), 403
    try:
        import shutil
        try: shutil.copy2(path, path + '.wsbak')
        except: pass
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

ALLOWED_CMDS = [
    'systemctl restart frontend', 'systemctl restart frontend-gw',
    'systemctl is-active frontend', 'systemctl is-active frontend-gw',
    'systemctl status frontend', 'systemctl status frontend-gw',
    'git -C /opt/frontend status', 'git -C /opt/frontend log --oneline -10',
    'git -C /opt/frontend diff --stat',
]

@app.route('/api/workspace/exec', methods=['POST'])
def ws_exec():
    cmd = (request.get_json() or {}).get('cmd', '')
    if cmd not in ALLOWED_CMDS:
        return jsonify({'error': 'cmd not allowed'}), 403
    try:
        r = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=15)
        return jsonify({'stdout': r.stdout, 'stderr': r.stderr, 'rc': r.returncode})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/workspace/status')
def ws_status():
    svcs = {}
    for s in ['frontend', 'frontend-gw']:
        r = subprocess.run(['systemctl', 'is-active', s], capture_output=True, text=True)
        svcs[s] = r.stdout.strip()
    r2 = subprocess.run(['git', '-C', '/opt/frontend', 'log', '--oneline', '-1'], capture_output=True, text=True)
    r3 = subprocess.run(['git', '-C', '/opt/frontend', 'status', '--short'], capture_output=True, text=True)
    return jsonify({'services': svcs, 'last_commit': r2.stdout.strip(), 'git_dirty': r3.stdout.strip()})


ENV_PATH = '/opt/frontend/.env'

def _read_env():
    d = {}
    try:
        for ln in open(ENV_PATH):
            ln = ln.strip()
            if '=' in ln and not ln.startswith('#'):
                k, v = ln.split('=', 1)
                d[k] = v
    except Exception:
        pass
    return d

def _write_env_key(key, value):
    try:
        lines = open(ENV_PATH).readlines()
        found = False
        new = []
        for ln in lines:
            if ln.startswith(key + '='):
                new.append(f'{key}={value}\n'); found = True
            else:
                new.append(ln)
        if not found:
            new.append(f'{key}={value}\n')
        open(ENV_PATH, 'w').writelines(new)
        return True
    except Exception:
        return False

@app.route('/api/ws/model', methods=['GET'])
def ws_get_model():
    env = _read_env()
    model = env.get('WS_MODEL') or env.get('MODEL') or 'claude-opus-4-6'
    return jsonify({'model': model})

@app.route('/api/ws/model', methods=['POST'])
def ws_set_model():
    data = request.get_json() or {}
    new_model = (data.get('model') or '').strip()
    if not new_model:
        return jsonify({'error': 'empty'}), 400
    if _write_env_key('WS_MODEL', new_model):
        return jsonify({'ok': True, 'model': new_model})
    return jsonify({'error': 'write failed'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5053, debug=False)
