"""
geo_utils.py — 坐标转换 + 高德逆地理 + 更贴近人的地点标签。

常见问题：
- Android 可能上报 WGS84 或已是 GCJ-02，服务端盲目 wgs2gcj 会偏移数百米～数公里
- formatted_address 常落在附近大路（如滨江东路），而非小区名（如东昌花园）
"""
import json
import math
import urllib.parse
import urllib.request

_RESIDENTIAL_TYPES = (
    '商务住宅', '住宅区', '住宅小区', '宿舍', '社区', '居民', '小区', '家园', '花园', '公寓',
)
_BAD_POI_TYPES = (
    '通行设施', '临街院门', '公司企业', '丧葬设施',
)

# 默认「家」锚点（GCJ-02，来自高德门址：解放东路东昌花园12号楼）
_DEFAULT_HOME = {
    'lat': 43.843628,
    'lon': 126.577663,
    'label': '东昌花园',
    'detail': '解放东路12号楼',
    'radius_m': 500.0,
}


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p = math.pi / 180.0
    a = math.sin((lat2 - lat1) * p / 2) ** 2
    a += math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _home_anchor():
    """从 config_store 读家锚点；无配置时用东昌花园默认值。"""
    h = dict(_DEFAULT_HOME)
    try:
        import config_store as cs
        h['lat'] = cs.get_float('HOME_LAT', h['lat'])
        h['lon'] = cs.get_float('HOME_LON', h['lon'])
        h['label'] = cs.get('HOME_LABEL', h['label']) or h['label']
        h['detail'] = cs.get('HOME_DETAIL', h['detail']) or h['detail']
        h['radius_m'] = cs.get_float('HOME_RADIUS_M', h['radius_m'])
    except Exception:
        pass
    return h


def wgs2gcj(lat, lon):
    """WGS84 → GCJ-02（仅在中国大陆范围内偏移）。"""
    a, ee = 6378245.0, 0.00669342162296594323
    if lon < 72.004 or lon > 137.8347 or lat < 0.8293 or lat > 55.8271:
        return lat, lon
    dlat = -100 + 2 * lon + 3 * lat + 0.2 * lat * lat + 0.1 * lon * lat + 0.2 * math.sqrt(abs(lon))
    dlat += (20 * math.sin(6 * lon * math.pi) + 20 * math.sin(2 * lon * math.pi)) * 2 / 3
    dlat += (20 * math.sin(lat * math.pi) + 40 * math.sin(lat / 3 * math.pi)) * 2 / 3
    dlat += (160 * math.sin(lat / 12 * math.pi) + 320 * math.sin(lat * math.pi / 30)) * 2 / 3
    dlon = 300 + lon + 2 * lat + 0.1 * lon * lon + 0.1 * lon * lat + 0.1 * math.sqrt(abs(lon))
    dlon += (20 * math.sin(6 * lon * math.pi) + 20 * math.sin(2 * lon * math.pi)) * 2 / 3
    dlon += (20 * math.sin(lon * math.pi) + 40 * math.sin(lon / 3 * math.pi)) * 2 / 3
    dlon += (150 * math.sin(lon / 12 * math.pi) + 300 * math.sin(lon / 30 * math.pi)) * 2 / 3
    radlat = lat / 180 * math.pi
    magic = 1 - ee * math.sin(radlat) ** 2
    dlat = dlat * 180 / (a * (1 - ee) / (magic ** 1.5) * math.pi)
    dlon = dlon * 180 / (a / math.sqrt(magic) * math.cos(radlat) * math.pi)
    return lat + dlat, lon + dlon


def _load_amap_key():
    try:
        for line in open('/opt/frontend/.env'):
            if line.startswith('AMAP_KEY='):
                return line.split('=', 1)[1].strip()
    except Exception:
        pass
    return ''


def amap_regeo(amap_key, lon, lat, radius=300):
    """调用高德逆地理，失败返回 None。"""
    if not amap_key:
        return None
    url = (
        'https://restapi.amap.com/v3/geocode/regeo?key=' + urllib.parse.quote(amap_key)
        + '&location=' + f'{lon:.6f},{lat:.6f}'
        + '&extensions=all&radius=' + str(int(radius)) + '&roadlevel=0'
    )
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read())
        if data.get('status') == '1':
            return data
    except Exception:
        pass
    return None


def _street_line(ac):
    """从 addressComponent 拼出「路 + 门牌」。"""
    if not ac:
        return ''
    street = ac.get('street')
    if isinstance(street, list):
        street = street[0] if street else ''
    sn = ac.get('streetNumber') or {}
    if isinstance(sn, dict):
        st = sn.get('street') or street or ''
        num = sn.get('number') or ''
        if isinstance(st, list):
            st = st[0] if st else ''
        if st and num:
            return f'{st}{num}'
        return st or ''
    return street or ''


def _pick_poi(pois):
    """优先选住宅区/小区类 POI，避开临街院门/工厂。"""
    if not pois:
        return ''
    ranked = sorted(pois, key=lambda x: float(x.get('distance', 9999)))
    best_res = ''
    best_res_dist = 9999.0
    for p in ranked[:12]:
        typ = p.get('type') or ''
        dist = float(p.get('distance', 9999))
        name = (p.get('name') or '').strip()
        if not name:
            continue
        if any(b in typ for b in _BAD_POI_TYPES):
            continue
        if any(t in typ for t in _RESIDENTIAL_TYPES) or any(
            k in name for k in ('花园', '小区', '家园', '公寓', '苑', '居', '城', '里')
        ):
            if dist < best_res_dist:
                best_res = name
                best_res_dist = dist
    if best_res:
        return best_res
    for p in ranked[:5]:
        typ = p.get('type') or ''
        if any(b in typ for b in _BAD_POI_TYPES):
            continue
        name = (p.get('name') or '').strip()
        if name:
            return name
    return ranked[0].get('name', '') if ranked else ''


def score_regeocode(regeocode, lat=None, lon=None):
    """分数越高，越像「对人友好、像真实落脚点」的解析结果。"""
    if not regeocode:
        return -999
    score = 0.0
    home = _home_anchor()
    if lat is not None and lon is not None:
        d_home = haversine_m(lat, lon, home['lat'], home['lon'])
        score += max(0.0, 200.0 - d_home / 4.0)
        if d_home <= home['radius_m']:
            score += 120.0
    aois = regeocode.get('aois') or []
    if aois:
        dist = float(aois[0].get('distance', 9999))
        score += 140 - min(dist, 140)
    pois = regeocode.get('pois') or []
    for p in pois[:8]:
        typ = p.get('type') or ''
        dist = float(p.get('distance', 9999))
        name = p.get('name') or ''
        if any(b in typ for b in _BAD_POI_TYPES):
            score -= 35
        elif any(t in typ for t in _RESIDENTIAL_TYPES) or any(
            k in name for k in ('花园', '小区', '家园', '公寓', '苑')
        ):
            score += 90 - min(dist / 4.0, 90)
        else:
            score += 25 - min(dist / 10.0, 25)
    ac = regeocode.get('addressComponent') or {}
    if _street_line(ac):
        score += 35
    township = ac.get('township')
    if township and township not in ('', '[]'):
        score += 10
    formatted = regeocode.get('formatted_address') or ''
    if formatted:
        score += 5
        # 只有路名、没有门牌/小区时，不如 AOI/住宅 POI 可靠
        if '号' not in formatted and not aois:
            score -= 15
    return score


def build_place_labels(regeocode):
    """
    从 regeocode 提取展示用标签。
    返回 (primary, detail, city)
    - primary: 卡片主标题（优先小区/AOI/住宅 POI）
    - detail: 副标题（街道门牌或 formatted 精简）
    """
    if not regeocode:
        return '', '', ''
    ac = regeocode.get('addressComponent') or {}
    city = ac.get('city') or ac.get('province') or ''
    formatted = (regeocode.get('formatted_address') or '').strip()
    street = _street_line(ac)
    aois = regeocode.get('aois') or []
    pois = regeocode.get('pois') or []

    primary = ''
    if aois:
        primary = (aois[0].get('name') or '').strip()
    if not primary:
        primary = _pick_poi(pois)
    if not primary and street:
        primary = street
    if not primary:
        primary = formatted

    detail = ''
    if primary and street and primary not in street:
        detail = street
    elif primary and formatted and primary not in formatted and formatted != primary:
        # 去掉与 primary 重复的 formatted 前缀
        detail = formatted
        for part in (city, ac.get('district') or '', ac.get('township') or ''):
            if part and detail.startswith(str(part)):
                detail = detail[len(str(part)):]
        detail = detail.lstrip('省市区县乡').strip() or formatted

    return primary, detail, city


def _snap_home(lat, lon, address, poi):
    """GPS 漂移进家附近时，展示固定为家标签（地图仍用真实坐标）。"""
    home = _home_anchor()
    if haversine_m(lat, lon, home['lat'], home['lon']) <= home['radius_m']:
        return home['label'], home['detail'] or poi or address
    return address, poi


def resolve_location(lat, lon, amap_key=None, coord_type=None):
    """
    解析上报坐标，自动选择 WGS→GCJ 或原样 GCJ，并逆地理。

    coord_type: None（自动）| 'wgs84' | 'gcj02'
    返回 dict: lat_wgs, lon_wgs, lat_gcj, lon_gcj, address, poi, city, coord_src
    """
    lat, lon = float(lat), float(lon)
    amap_key = amap_key or _load_amap_key()

    def _pack(raw_lat, raw_lon, gcj_lat, gcj_lon, regeo, coord_src):
        rg = (regeo or {}).get('regeocode') or {}
        primary, detail, city = build_place_labels(rg)
        address = primary or detail or city or ''
        poi = detail or _pick_poi(rg.get('pois') or []) or ''
        address, poi = _snap_home(raw_lat, raw_lon, address, poi)
        return {
            'lat_wgs': raw_lat,
            'lon_wgs': raw_lon,
            'lat_gcj': gcj_lat,
            'lon_gcj': gcj_lon,
            'address': address,
            'poi': poi,
            'city': city or '',
            'coord_src': coord_src,
        }

    if coord_type == 'gcj02':
        geo = amap_regeo(amap_key, lon, lat)
        return _pack(lat, lon, lat, lon, geo, 'gcj02')

    if coord_type == 'wgs84':
        glat, glon = wgs2gcj(lat, lon)
        geo = amap_regeo(amap_key, glon, glat)
        return _pack(lat, lon, glat, glon, geo, 'wgs84')

    # 自动：两种坐标系都试，选逆地理结果更合理的一种
    glat, glon = wgs2gcj(lat, lon)
    candidates = []
    for src, clat, clon, raw_lat, raw_lon, gcj_lat, gcj_lon in (
        ('wgs84', glat, glon, lat, lon, glat, glon),
        ('gcj02', lat, lon, lat, lon, lat, lon),
    ):
        geo = amap_regeo(amap_key, clon, clat)
        if not geo:
            continue
        rg = geo.get('regeocode') or {}
        candidates.append((
            score_regeocode(rg, clat, clon),
            src,
            raw_lat, raw_lon, gcj_lat, gcj_lon,
            geo,
        ))
    if candidates:
        _, src, raw_lat, raw_lon, gcj_lat, gcj_lon, geo = max(candidates, key=lambda x: x[0])
        return _pack(raw_lat, raw_lon, gcj_lat, gcj_lon, geo, src)

    return _pack(lat, lon, glat, glon, None, 'wgs84_fallback')
