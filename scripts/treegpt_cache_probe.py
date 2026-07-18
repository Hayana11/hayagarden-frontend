#!/usr/bin/env python3
"""TreeGPT / api_relay 短聊缓存探针（侧路，不改 GW_PROVIDER）。

对照 CC resident 验收场景 B：连续短聊，观察热轮 cache_creation 中位数。

设计约束：
- 严禁改 runtime GW_PROVIDER / 不写 chat_messages
- 复用生产路径：build_system(split_dynamic=True) + rolling BP4 + metadata.user_id
- 默认不带 tools / thinking，降低干扰与费用；--prod-like 才贴近主聊天

用法（在 VPS /opt/frontend 或本仓库根目录）：
  python3 scripts/treegpt_cache_probe.py
  python3 scripts/treegpt_cache_probe.py --turns 6 --prod-like
  python3 scripts/treegpt_cache_probe.py --out artifacts/treegpt-cache-probe.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# VPS 部署树优先
if os.path.isdir('/opt/frontend') and '/opt/frontend' not in sys.path:
    sys.path.insert(0, '/opt/frontend')


def _apply_rolling_cache_control(messages):
    """与 gateway._apply_rolling_cache_control 同语义（避免 import gateway 副作用）。"""
    marker = {'type': 'ephemeral'}

    def _add(content):
        if isinstance(content, str):
            return [{'type': 'text', 'text': content, 'cache_control': marker}]
        if isinstance(content, list):
            out = [dict(b) if isinstance(b, dict) else b for b in content]
            for i in range(len(out) - 1, -1, -1):
                b = out[i]
                if isinstance(b, dict) and b.get('type') == 'text' and b.get('text'):
                    b['cache_control'] = marker
                    return out
            for i in range(len(out) - 1, -1, -1):
                b = out[i]
                if isinstance(b, dict):
                    b['cache_control'] = marker
                    return out
        return content

    if not isinstance(messages, list):
        return messages
    user_idxs = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get('role') == 'user']
    if len(user_idxs) < 2:
        return messages
    idx = user_idxs[-2]
    msg = dict(messages[idx])
    msg['content'] = _add(msg.get('content', ''))
    messages[idx] = msg
    return messages


def _prepend_context_to_last_user(messages, context):
    context = (context or '').strip()
    if not context or not isinstance(messages, list):
        return messages
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') != 'user':
            continue
        msg = dict(messages[i])
        content = msg.get('content')
        if isinstance(content, str):
            msg['content'] = context + '\n\n' + content
        elif isinstance(content, list):
            msg['content'] = [{'type': 'text', 'text': context}] + list(content)
        else:
            msg['content'] = context
        messages[i] = msg
        break
    return messages


def _parse_usage_from_sse(resp) -> dict:
    usage = {
        'cache_read': 0,
        'cache_creation': 0,
        'cache_creation_5m': 0,
        'cache_creation_1h': 0,
        'input_tokens': 0,
        'output_tokens': 0,
    }
    text_parts = []
    for raw in resp:
        line = raw.decode('utf-8', 'ignore').strip()
        if not line.startswith('data:'):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except Exception:
            continue
        et = ev.get('type')
        if et == 'message_start':
            u = (ev.get('message') or {}).get('usage') or {}
            usage['cache_read'] = max(usage['cache_read'], int(u.get('cache_read_input_tokens') or 0))
            usage['cache_creation'] = max(
                usage['cache_creation'], int(u.get('cache_creation_input_tokens') or 0)
            )
            cc = u.get('cache_creation') or {}
            if isinstance(cc, dict):
                usage['cache_creation_5m'] = max(
                    usage['cache_creation_5m'], int(cc.get('ephemeral_5m_input_tokens') or 0)
                )
                usage['cache_creation_1h'] = max(
                    usage['cache_creation_1h'], int(cc.get('ephemeral_1h_input_tokens') or 0)
                )
            usage['input_tokens'] = max(usage['input_tokens'], int(u.get('input_tokens') or 0))
            usage['output_tokens'] = max(usage['output_tokens'], int(u.get('output_tokens') or 0))
        elif et == 'content_block_delta':
            d = ev.get('delta') or {}
            if d.get('type') == 'text_delta' and d.get('text'):
                text_parts.append(d['text'])
        elif et == 'message_delta':
            u = ev.get('usage') or {}
            if u.get('output_tokens'):
                usage['output_tokens'] = max(usage['output_tokens'], int(u['output_tokens']))
            if u.get('input_tokens'):
                usage['input_tokens'] = max(usage['input_tokens'], int(u['input_tokens']))
            if u.get('cache_read_input_tokens') is not None:
                usage['cache_read'] = max(usage['cache_read'], int(u['cache_read_input_tokens'] or 0))
            if u.get('cache_creation_input_tokens') is not None:
                usage['cache_creation'] = max(
                    usage['cache_creation'], int(u['cache_creation_input_tokens'] or 0)
                )
    usage['assistant_text'] = ''.join(text_parts).strip()
    return usage


def _summarize(turns: list[dict]) -> dict:
    ok = [t for t in turns if 'cache_creation' in t and 'error' not in t]
    creates = [int(t['cache_creation']) for t in ok]
    reads = [int(t['cache_read']) for t in ok]
    hot = ok[1:] if len(ok) > 1 else []
    hot_creates = [int(t['cache_creation']) for t in hot]
    return {
        'n_turns': len(turns),
        'n_ok': len(ok),
        'n_errors': sum(1 for t in turns if 'error' in t),
        'cold_cache_creation': creates[0] if creates else None,
        'cold_cache_read': reads[0] if reads else None,
        'hot_cache_creation_median': statistics.median(hot_creates) if hot_creates else None,
        'hot_cache_creation_mean': round(statistics.mean(hot_creates), 1) if hot_creates else None,
        'hot_cache_creation_min': min(hot_creates) if hot_creates else None,
        'hot_cache_creation_max': max(hot_creates) if hot_creates else None,
        'hot_cache_read_median': statistics.median([int(t['cache_read']) for t in hot]) if hot else None,
        'all_cache_creation': creates,
        'all_cache_read': reads,
    }


def run_probe(
    *,
    turns: int = 6,
    prod_like: bool = False,
    max_tokens: int = 64,
    user_id: str = 'hayana-fyodor-stable',
    model: str | None = None,
) -> dict:
    from chat.system_builder import build_system
    from relay.manager import relay as chat_relay

    chat_relay._reload_env()
    api_url = chat_relay.api_url or ''
    caps = chat_relay.caps or {}
    if 'treegpt' not in api_url and 'tree' not in (api_url or '').lower():
        # 仍允许跑（ACTIVE_RELAY 可能指向 tree），但标注警告
        warn = f'ACTIVE_RELAY url 不像 TreeGPT: {api_url}'
    else:
        warn = None
    override_model = (model or '').strip() or None

    prompts = [
        f'探针T{i + 1}：请只回一个字「好」。不要调用工具。'
        for i in range(turns)
    ]

    messages: list[dict] = []
    results: list[dict] = []

    for i, prompt in enumerate(prompts):
        built = build_system(split_dynamic=True)
        if isinstance(built, tuple):
            system, dynamic_context = built
        else:
            system, dynamic_context = built, ''

        messages = list(messages) + [{'role': 'user', 'content': prompt}]
        msgs = [dict(m) for m in messages]
        if not prod_like:
            # 默认：不带 tools，减少工具列表波动；仍走 rolling + split_dynamic
            pass
        msgs = _apply_rolling_cache_control(msgs)
        if dynamic_context:
            msgs = _prepend_context_to_last_user(msgs, dynamic_context)

        payload = {
            'max_tokens': max_tokens,
            'stream': True,
            'system': system,
            'messages': msgs,
            'metadata': {'user_id': user_id},
        }
        if override_model:
            payload['model'] = override_model
        if prod_like:
            try:
                import gateway as gw
                tools = gw.get_tools()
                payload['tools'] = tools
            except Exception as e:
                payload['tools_error'] = str(e)
            if caps.get('thinking'):
                payload['thinking'] = {'type': 'enabled', 'budget_tokens': 1024}

        t0 = time.monotonic()
        try:
            resp = chat_relay.call_stream(payload, timeout=180)
            usage = _parse_usage_from_sse(resp)
        except Exception as e:
            # 常见：TreeGPT 预扣费不足 → HTTP 403；把正文带进报告后中止
            err_body = ''
            if hasattr(e, 'read'):
                try:
                    err_body = e.read().decode('utf-8', 'ignore')[:800]
                except Exception:
                    err_body = ''
            row = {
                'turn': i + 1,
                'phase': 'cold' if i == 0 else 'hot',
                'error': f'{type(e).__name__}: {e}',
                'error_body': err_body,
            }
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            break
        elapsed = round(time.monotonic() - t0, 3)

        assistant = usage.pop('assistant_text', '') or '好'
        # 截断，避免历史膨胀干扰下一轮读数以外的语义
        if len(assistant) > 80:
            assistant = assistant[:80]
        messages.append({'role': 'assistant', 'content': assistant})

        row = {
            'turn': i + 1,
            'phase': 'cold' if i == 0 else 'hot',
            'elapsed_sec': elapsed,
            'prompt': prompt,
            'assistant_preview': assistant[:40],
            **{k: usage[k] for k in (
                'cache_read', 'cache_creation', 'cache_creation_5m',
                'cache_creation_1h', 'input_tokens', 'output_tokens',
            )},
        }
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    summary = _summarize(results)
    report = {
        'ts': datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
        'provider': 'api_relay',
        'relay_url': api_url,
        'relay_id': None,
        'caps': {k: caps.get(k) for k in ('thinking', 'cache', 'cache_1h', 'tools', 'vision')},
        'prod_like': prod_like,
        'max_tokens': max_tokens,
        'model': override_model or chat_relay.model,
        'metadata_user_id': user_id,
        'warning': warn,
        'turns': results,
        'summary': summary,
        'notes': [
            '侧路探针：不改 GW_PROVIDER，不写 chat_messages。',
            'system 每轮重建（与生产一致）；split_dynamic 后 BP2/BP3 进 user 前缀。',
            '对照目标：热轮 cache_creation 中位接近 0（历史 TreeGPT 健康段曾出现 create=0 / read≈33k）。',
            'CC resident 短聊热轮中位 ≈214（验收后）；勿把 CC 策略混进本探针。',
        ],
    }
    try:
        import config_store
        report['relay_id'] = config_store.get('ACTIVE_RELAY', '')
        report['gw_provider_untouched'] = config_store.get('GW_PROVIDER', '')
    except Exception:
        pass
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description='TreeGPT cache probe (side-path)')
    ap.add_argument('--turns', type=int, default=6)
    ap.add_argument('--prod-like', action='store_true', help='带 tools + 小 budget thinking')
    ap.add_argument('--max-tokens', type=int, default=64)
    ap.add_argument('--model', type=str, default='', help='覆盖 ACTIVE_RELAY 默认模型（如余额不足时试 haiku）')
    ap.add_argument('--out', type=str, default='')
    args = ap.parse_args(argv)

    report = run_probe(
        turns=max(2, args.turns),
        prod_like=args.prod_like,
        max_tokens=args.max_tokens,
        model=args.model or None,
    )
    summary = report['summary']
    print('--- summary ---', flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

    out = args.out
    if not out:
        out_dir = ROOT / 'artifacts'
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        out = str(out_dir / f'treegpt-cache-probe-{stamp}.json')
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'wrote {out}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
