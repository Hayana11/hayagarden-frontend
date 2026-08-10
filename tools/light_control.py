#!/usr/bin/env python3.11
"""
米家灯控底层封装。基于 MIOT 标准属性 (siid/piid)。
认证信息来自 /opt/frontend/.mijia_auth（由 mijia_login.py 生成）。

标准灯属性（多数米家灯泡/吸顶灯通用，型号不同可在 light_config.json 覆盖）：
  on/off           siid=2 piid=1  (bool)
  brightness 1-100 siid=2 piid=2  (int)
  color-temp       siid=2 piid=3  (int, 单位 Kelvin)

Status queries use per-zone ``supported_query_props`` only — control props may
exist in prop_map but are not queried unless listed for that zone.
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

DEFAULT_ZONES = {
    "main": {"supported_query_props": ["power"]},
    "bedside": {"supported_query_props": ["power"]},
}


def _load_config():
    cfg = {
        "prop_map": dict(DEFAULT_MAP),
        "zones": dict(DEFAULT_ZONES),
    }
    if os.path.exists(CONFIG_PATH):
        try:
            user = json.load(open(CONFIG_PATH))
            pm = dict(DEFAULT_MAP)
            pm.update(user.get("prop_map", {}))
            cfg["prop_map"] = pm
            zones = dict(DEFAULT_ZONES)
            for zone_name, zone_cfg in (user.get("zones") or {}).items():
                merged = dict(zones.get(zone_name) or {})
                merged.update(zone_cfg or {})
                zones[zone_name] = merged
            cfg["zones"] = zones
            for key in ("bedroom2_did", "bedroom2_bedside_did"):
                if key in user:
                    cfg[key] = user[key]
        except Exception:
            pass
    return cfg


def _supported_query_props(zone: str) -> list[str]:
    cfg = _load_config()
    zone_cfg = (cfg.get("zones") or {}).get(zone) or {}
    props = zone_cfg.get("supported_query_props")
    if isinstance(props, list) and props:
        return [str(p) for p in props]
    fallback = (DEFAULT_ZONES.get(zone) or {}).get("supported_query_props")
    return list(fallback or ["power"])


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


def light_status(did, zone: str = "main"):
    """Query only zone ``supported_query_props`` (production: power-only per zone)."""
    props = _supported_query_props(zone)
    pm = _load_config()["prop_map"]
    query = []
    names: list[str] = []
    for name in props:
        if name not in pm:
            continue
        p = pm[name]
        query.append({"did": did, "siid": p["siid"], "piid": p["piid"]})
        names.append(name)
    if not query:
        return {"unavailable": True, "error": "no_supported_query_props"}

    try:
        res = _api().get_devices_prop(query)
    except Exception as exc:
        return {"unavailable": True, "error": str(exc)}

    results = res if isinstance(res, list) else [res]
    out: dict = {}
    query_errors: list[str] = []
    for name, r in zip(names, results):
        if not isinstance(r, dict):
            out[name] = None
            query_errors.append(f'{name}:invalid_response')
            continue
        code = r.get("code")
        if code is not None and int(code) != 0:
            out[name] = None
            msg = str(r.get("message") or "unavailable")
            query_errors.append(f'{name}:{msg}')
        else:
            out[name] = r.get("value")

    if query_errors:
        out["query_errors"] = query_errors
    if all(out.get(n) is None for n in names):
        out["unavailable"] = True
    return out


if __name__ == '__main__':
    import pprint
    pprint.pprint(get_devices())
