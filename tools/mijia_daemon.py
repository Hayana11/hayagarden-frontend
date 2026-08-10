#!/usr/bin/env python3
"""
次卧灯控守护进程，HTTP 接口 localhost:5052。
操作 light_config.json 里 bedroom2_did（主灯）和 bedroom2_bedside_did（床头灯）。

  POST /light/on            — 开主灯（向后兼容）
  POST /light/off           — 关主灯（向后兼容）
  POST /light/brightness    {"value": 50}  — 主灯亮度
  POST /light/color_temp    {"value": 4000}  — 主灯色温
  GET  /light/status        — 两个灯的状态

  POST /light/main/on       — 开主灯
  POST /light/main/off      — 关主灯
  POST /light/bedside/on    — 开床头灯
  POST /light/bedside/off   — 关床头灯
  POST /light/all/on        — 两个一起开
  POST /light/all/off       — 两个一起关
"""
import json, sys
sys.path.insert(0, '/opt/frontend/tools')
from flask import Flask, request, jsonify
import light_control as lc

CONFIG_PATH = '/opt/frontend/tools/light_config.json'
app = Flask(__name__)

def _load_dids():
    cfg = json.load(open(CONFIG_PATH))
    main = cfg.get("bedroom2_did", "").strip()
    bedside = cfg.get("bedroom2_bedside_did", "").strip()
    if not main:
        raise RuntimeError("主灯 did 未配置：请在 light_config.json 填入 bedroom2_did")
    if not bedside:
        raise RuntimeError("床头灯 did 未配置：请在 light_config.json 填入 bedroom2_bedside_did")
    return main, bedside

def main_did():
    return _load_dids()[0]

def bedside_did():
    return _load_dids()[1]

def _wrap(fn):
    try:
        return jsonify({"ok": True, "result": fn()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── 向后兼容：/light/on /light/off 控制主灯 ──────────────────────────────────

@app.route('/light/on', methods=['POST'])
def on():
    return _wrap(lambda: lc.light_on(main_did()))

@app.route('/light/off', methods=['POST'])
def off():
    return _wrap(lambda: lc.light_off(main_did()))

@app.route('/light/brightness', methods=['POST'])
def brightness():
    v = (request.get_json() or {}).get("value", 50)
    return _wrap(lambda: lc.set_brightness(main_did(), v))

@app.route('/light/color_temp', methods=['POST'])
def color_temp():
    v = (request.get_json() or {}).get("value", 4000)
    return _wrap(lambda: lc.set_color_temp(main_did(), v))

# ── 主灯 ─────────────────────────────────────────────────────────────────────

@app.route('/light/main/on', methods=['POST'])
def main_on():
    return _wrap(lambda: lc.light_on(main_did()))

@app.route('/light/main/off', methods=['POST'])
def main_off():
    return _wrap(lambda: lc.light_off(main_did()))

# ── 床头灯 ───────────────────────────────────────────────────────────────────

@app.route('/light/bedside/on', methods=['POST'])
def bedside_on():
    return _wrap(lambda: lc.light_on(bedside_did()))

@app.route('/light/bedside/off', methods=['POST'])
def bedside_off():
    return _wrap(lambda: lc.light_off(bedside_did()))
# ── 暖灯模式（开关两次触发暖色） ──────────────────────────────────────────────

@app.route('/light/bedside/warm', methods=['POST'])
def bedside_warm():
    def _fn():
        import time
        b = bedside_did()
        lc.light_off(b);  time.sleep(0.8)
        lc.light_on(b);   time.sleep(0.8)
        lc.light_off(b);  time.sleep(0.8)
        return lc.light_on(b)
    return _wrap(_fn)

# ── 中性光模式（开关三次触发中性色） ────────────────────────────────────────────

@app.route('/light/bedside/neutral', methods=['POST'])
def bedside_neutral():
    def _fn():
        import time
        b = bedside_did()
        lc.light_off(b);  time.sleep(0.5)
        lc.light_on(b);   time.sleep(0.5)
        lc.light_off(b);  time.sleep(0.5)
        lc.light_on(b);   time.sleep(0.5)
        lc.light_off(b);  time.sleep(0.5)
        return lc.light_on(b)
    return _wrap(_fn)

# ── 全部 ─────────────────────────────────────────────────────────────────────

@app.route('/light/all/on', methods=['POST'])
def all_on():
    def _fn():
        m, b = _load_dids()
        r1 = lc.light_on(m)
        r2 = lc.light_on(b)
        return {"main": r1, "bedside": r2}
    return _wrap(_fn)

@app.route('/light/all/off', methods=['POST'])
def all_off():
    def _fn():
        m, b = _load_dids()
        r1 = lc.light_off(m)
        r2 = lc.light_off(b)
        return {"main": r1, "bedside": r2}
    return _wrap(_fn)

# ── 状态（两个灯）────────────────────────────────────────────────────────────

@app.route('/light/status', methods=['GET'])
def status():
    def _fn():
        m, b = _load_dids()
        return {
            "main":    lc.light_status(m, zone="main"),
            "bedside": lc.light_status(b, zone="bedside"),
        }
    return _wrap(_fn)

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5052, debug=False)
