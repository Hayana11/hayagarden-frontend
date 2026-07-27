#!/usr/bin/env python3.11
"""
米家灯控底层封装。基于 MIOT 标准属性 (siid/piid)。
认证信息来自 /opt/frontend/.mijia_auth（由 mijia_login.py 生成）。

查询能力由 light_config.json 声明：
  zones.<name>.supported_query_props — 该设备允许查询的属性名列表
  prop_map — 属性名到 MIOT siid/piid 的映射（仅声明过的属性可被查询）

brightness / color_temp 等若未列入 supported_query_props，则不会发起查询，
也不会在 status 结果中伪造 unknown。
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping

AUTH_PATH = '/opt/frontend/.mijia_auth'
CONFIG_PATH = '/opt/frontend/tools/light_config.json'

# 完整 MIOT 映射参考；仅当 prop_map 显式声明时才参与查询。
DEFAULT_PROP_MAP = {
    'power': {'siid': 2, 'piid': 1},
    'brightness': {'siid': 2, 'piid': 2},
    'color_temp': {'siid': 2, 'piid': 3},
}

DEFAULT_ZONE_QUERY_PROPS = ('power',)


def _load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {
        'prop_map': dict(DEFAULT_PROP_MAP),
        'zones': {},
    }
    if not os.path.exists(CONFIG_PATH):
        return cfg
    try:
        user = json.load(open(CONFIG_PATH, encoding='utf-8'))
    except Exception:
        return cfg
    if not isinstance(user, dict):
        return cfg
    cfg.update({k: v for k, v in user.items() if k not in ('prop_map', 'zones')})
    user_pm = user.get('prop_map')
    pm = dict(DEFAULT_PROP_MAP)
    if isinstance(user_pm, dict):
        pm.update({str(k): v for k, v in user_pm.items()})
    cfg['prop_map'] = pm
    zones = user.get('zones')
    if isinstance(zones, dict):
        cfg['zones'] = zones
    return cfg


def get_prop_map() -> dict[str, dict[str, int]]:
    return dict(_load_config().get('prop_map') or {})


def supported_query_props(zone: str | None = None) -> list[str]:
    """Return queryable property names for a zone (or global default)."""
    cfg = _load_config()
    zones = cfg.get('zones') or {}
    if zone and isinstance(zones.get(zone), dict):
        declared = zones[zone].get('supported_query_props')
        if isinstance(declared, list) and declared:
            return [str(p) for p in declared]
    # Backward compatible: default to power-only when zone has no declaration.
    return list(DEFAULT_ZONE_QUERY_PROPS)


def _api():
    if not os.path.exists(AUTH_PATH):
        raise RuntimeError('未授权：请先运行 mijia_login.py 扫码登录')
    from mijiaAPI import mijiaAPI
    return mijiaAPI(auth_data_path=AUTH_PATH)


def _prop(name: str) -> dict[str, int]:
    pm = get_prop_map()
    if name not in pm:
        raise KeyError('unsupported property: %s' % name)
    return pm[name]


def get_devices():
    """列出账号下所有设备"""
    return _api().get_devices_list()


def light_on(did):
    p = _prop('power')
    return _api().set_devices_prop({'did': did, 'siid': p['siid'], 'piid': p['piid'], 'value': True})


def light_off(did):
    p = _prop('power')
    return _api().set_devices_prop({'did': did, 'siid': p['siid'], 'piid': p['piid'], 'value': False})


def set_brightness(did, value):
    value = max(1, min(100, int(value)))
    p = _prop('brightness')
    return _api().set_devices_prop({'did': did, 'siid': p['siid'], 'piid': p['piid'], 'value': value})


def set_color_temp(did, value):
    value = int(value)
    p = _prop('color_temp')
    return _api().set_devices_prop({'did': did, 'siid': p['siid'], 'piid': p['piid'], 'value': value})


def _parse_prop_row(name: str, row: Any) -> Any:
    """Validate one MIOT property row; raise on incomplete or error responses."""
    if not isinstance(row, dict):
        raise RuntimeError('invalid MIOT row for %s' % name)
    if row.get('code', 0) != 0:
        raise RuntimeError('MIOT error for %s: code=%s' % (name, row.get('code')))
    if 'value' not in row:
        raise RuntimeError('MIOT row missing value for %s' % name)
    value = row['value']
    if name == 'power':
        if isinstance(value, bool):
            return value
        if value in (0, 1):
            return bool(value)
        raise RuntimeError('invalid power value for %s: %r' % (name, value))
    return value


def light_status(did, *, supported_props: list[str] | None = None) -> dict[str, Any]:
    """Query only supported properties for one device.

    Returns {"available": True, "values": {<prop>: <value>}} on success.
  Raises on transport/auth failure (caller marks zone unavailable).
    """
    props = list(supported_props or supported_query_props())
    pm = get_prop_map()
    query = []
    names: list[str] = []
    for name in props:
        if name not in pm:
            continue
        spec = pm[name]
        query.append({'did': did, 'siid': spec['siid'], 'piid': spec['piid']})
        names.append(name)
    if not query:
        return {'available': True, 'values': {}}
    res = _api().get_devices_prop(query)
    rows = res if isinstance(res, list) else [res]
    if len(rows) != len(names):
        raise RuntimeError(
            'MIOT response row count mismatch: expected %d got %d' % (len(names), len(rows))
        )
    values: dict[str, Any] = {}
    for name, row in zip(names, rows):
        values[name] = _parse_prop_row(name, row)
    return {'available': True, 'values': values}


def zone_status(zone: str, did: str) -> dict[str, Any]:
    """Per-zone status with partial-failure isolation."""
    try:
        payload = light_status(did, supported_props=supported_query_props(zone))
        return {
            'available': True,
            'values': dict(payload.get('values') or {}),
        }
    except Exception as exc:
        return {
            'available': False,
            'error': str(exc),
        }


def format_power_value(values: Mapping[str, Any] | None) -> str:
    """Format known power reading for lean state (on/off only)."""
    if not values or 'power' not in values:
        return ''
    return 'on' if values.get('power') else 'off'


if __name__ == '__main__':
    import pprint
    pprint.pprint(get_devices())
