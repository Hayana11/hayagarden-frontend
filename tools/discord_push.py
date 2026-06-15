#!/usr/bin/env python3.11
"""
官克bot 主动推送消息到 Discord 频道。
用法：python3.11 discord_push.py <channel_id> <message>
或：  python3.11 discord_push.py <message>  （用默认频道）
"""
import sys, json
from urllib.request import urlopen, Request
from urllib.error import HTTPError

TOKEN   = 'MTUxNTY2MjIyODY1OTg5NjM0MA.GQYaLV.Akz3n4902LYbGS-OrIjKMyWlLox7mwgFV6m58U'
DEFAULT_CHANNEL = '1515664907658596433'

def send(channel_id: str, content: str) -> dict:
    url = f'https://discord.com/api/v10/channels/{channel_id}/messages'
    body = json.dumps({'content': content}).encode()
    req = Request(url, data=body, method='POST', headers={
        'Authorization': f'Bot {TOKEN}',
        'Content-Type': 'application/json',
    })
    try:
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except HTTPError as e:
        return {'error': e.code, 'body': e.read().decode()}

if __name__ == '__main__':
    args = sys.argv[1:]
    if len(args) == 0:
        print("usage: discord_push.py [channel_id] <message>")
        sys.exit(1)
    elif len(args) == 1:
        ch, msg = DEFAULT_CHANNEL, args[0]
    else:
        ch, msg = args[0], ' '.join(args[1:])
    result = send(ch, msg)
    print(json.dumps(result, ensure_ascii=False))
