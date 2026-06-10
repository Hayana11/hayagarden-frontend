#!/usr/bin/env python3
"""
次卧灯控守护进程，HTTP 接口 localhost:5052。
只操作 light_config.json 里 bedroom2_did 指定的那一盏灯。
  POST /light/on
  POST /light/off
  POST /light/brightness  {"value": 50}
  POST /light/color_temp  {"value": 4000}
  GET  /light/status
"""
import json, sys
sys.path.insert(0, '/opt/frontend/tools')
from flask import Flask, request, jsonify
import light_control as lc

CONFIG_PATH = '/opt/frontend/tools/light_config.json'
app = Flask(__name__)

def bedroom_did():
    cfg = json.load(open(CONFIG_PATH))
    did = cfg.get("bedroom2_did", "").strip()
    if not did:
        raise RuntimeError("次卧灯 did 未配置：请在 light_config.json 填入 bedroom2_did")
    return did

def _wrap(fn):
    try:
        return jsonify({"ok": True, "result": fn()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/light/on', methods=['POST'])
def on():
    return _wrap(lambda: lc.light_on(bedroom_did()))

@app.route('/light/off', methods=['POST'])
def off():
    return _wrap(lambda: lc.light_off(bedroom_did()))

@app.route('/light/brightness', methods=['POST'])
def brightness():
    v = (request.get_json() or {}).get("value", 50)
    return _wrap(lambda: lc.set_brightness(bedroom_did(), v))

@app.route('/light/color_temp', methods=['POST'])
def color_temp():
    v = (request.get_json() or {}).get("value", 4000)
    return _wrap(lambda: lc.set_color_temp(bedroom_did(), v))

@app.route('/light/status', methods=['GET'])
def status():
    return _wrap(lambda: lc.light_status(bedroom_did()))

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5052, debug=False)
