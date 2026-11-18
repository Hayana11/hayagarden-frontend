"""Read the most recent existing phone report for Gateway and Internal MCP."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import urllib.parse


def read_location(db_path):
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT lat_gcj, lon_gcj, accuracy, address, poi, city, created_at, "
            "CAST((julianday('now','+8 hours')-julianday(created_at))*86400 AS INT) AS age_sec "
            "FROM geo_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return '还没有位置记录——她手机 App 的后台定位可能没开或没授权。'
    d = dict(row)
    age = int(d.get('age_sec') or 0)
    if age < 0:
        age = 0
    if age < 60:
        ago = '刚刚'
    elif age < 3600:
        ago = '%d 分钟前' % (age // 60)
    elif age < 86400:
        ago = '%d 小时前' % (age // 3600)
    else:
        ago = '%d 天前' % (age // 86400)
    addr = d.get('address') or d.get('city') or '未知位置'
    lines = ['📍 ' + addr]
    poi = (d.get('poi') or '').strip()
    if poi and poi != addr and poi not in addr:
        lines.append('附近 · ' + poi)
    meta = []
    if d.get('city'):
        meta.append(d['city'])
    if d.get('accuracy'):
        meta.append('精度约 %d 米' % int(d['accuracy']))
    if meta:
        lines.append('🏙 ' + ' · '.join(meta))
    lines.append('🕐 %s（%s）' % (d.get('created_at', ''), ago))
    if d.get('lat_gcj') and d.get('lon_gcj'):
        lines.append('🔗 https://uri.amap.com/marker?position=%.6f,%.6f&name=%s' % (
            d['lon_gcj'], d['lat_gcj'], urllib.parse.quote((d.get('poi') or addr)[:30])))
    if age >= 3600:
        lines.append('（这是 %s 的定位，可能不是她此刻的位置）' % ago)
    return '\n'.join(lines)


if __name__ == "__main__":
    payload = json.load(sys.stdin)
    print(json.dumps(read_location(payload.get("db_path") or "/opt/frontend/memories.db"), ensure_ascii=False))
