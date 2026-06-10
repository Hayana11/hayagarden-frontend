#!/usr/bin/env python3
"""
米家灯控底层封装。基于 MIOT 标准属性 (siid/piid)。
认证信息来自 /opt/frontend/.mijia_auth（由 mijia_login.py 生成）。

标准灯属性（多数米家灯泡/吸顶灯通用，型号不同可在 light_config.json 覆盖）：
  on/off           siid=2 piid=1  (bool)
  brightness 1-100 siid=2 piid=2  (int)
  color-temp       siid=2 piid=3  (int, 单位 Kelvin)
"""
import os, json
from mijiaAPI import mijiaAPI

AUTH_PATH   = '/opt/frontend/.mijia_auth'
CONFIG_PATH = '/opt/frontend/tools/light_config.json'

# 默认 MIOT 映射，可被 light_config.json 覆盖
DEFAULT_MAP = {
    "power":      {"siid": 2, "piid": 1},
    "brightness": {"siid": 2, "piid": 2},
    "color_temp": {"siid": 2, "piid": 3},
}

def _load_config():
    cfg = {"prop_map": DEFAULT_MAP}
    if os.path.exists(CONFIG_PATH):
        try:
            user = json.load(open(CONFIG_PATH))
            cfg.update(user)
            pm = dict(DEFAULT_MAP)
            pm.update(user.get("prop_map", {}))
            cfg["prop_map"] = pm
        except Exception:
            pass
    return cfg

def _api():
    if not os.path.exists(AUTH_PATH):
        raise RuntimeError("未授权：请先运行 mijia_login.py 扫码登录")
    return mijiaAPI(auth_data_path=AUTH_PATH)

def _prop(name):
    return _load_config()["prop_map"][name]

def get_devices():
    """列出账号下所有设备"""
    return _api().get_devices_list()

def light_on(did):
    p = _prop("power")
    return _api().set_devices_prop({"did": did, "siid": p["siid"], "piid": p["piid"], "value": True})

def light_off(did):
    p = _prop("power")
    return _api().set_devices_prop({"did": did, "siid": p["siid"], "piid": p["piid"], "value": False})

def set_brightness(did, value):
    value = max(1, min(100, int(value)))
    p = _prop("brightness")
    return _api().set_devices_prop({"did": did, "siid": p["siid"], "piid": p["piid"], "value": value})

def set_color_temp(did, value):
    value = int(value)
    p = _prop("color_temp")
    return _api().set_devices_prop({"did": did, "siid": p["siid"], "piid": p["piid"], "value": value})

def light_status(did):
    pm = _load_config()["prop_map"]
    query = [{"did": did, "siid": v["siid"], "piid": v["piid"]} for v in pm.values()]
    names = list(pm.keys())
    res = _api().get_devices_prop(query)
    out = {}
    for name, r in zip(names, res if isinstance(res, list) else [res]):
        out[name] = r.get("value")
    return out

if __name__ == '__main__':
    import pprint
    pprint.pprint(get_devices())
