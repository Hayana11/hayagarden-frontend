"""Read and update per-memory valence/arousal from ombre-brain markdown files."""

from __future__ import annotations

import glob
import os
from typing import Callable

from valence_scale import normalize_arousal, normalize_valence

BUCKET_DIR = os.environ.get('OMBRE_BRAIN_BUCKET', '/opt/ombre-brain/buckets/dynamic')


def _emotion_label(v: float, a: float) -> str:
    if v >= 0.3 and a >= 0.60:
        return '喜悦'
    if v >= 0.3 and a >= 0.40:
        return '愉悦'
    if v >= 0.3:
        return '平静'
    if v >= -0.1 and a >= 0.65:
        return '兴奋'
    if v >= -0.1 and a < 0.35:
        return '松弛'
    if v < -0.3 and a >= 0.60:
        return '焦虑'
    if v < -0.3 and a >= 0.35:
        return '沉重'
    if v < -0.3:
        return '低落'
    return '迷离'


def _safe_bucket_path(path: str) -> str:
    clean = os.path.abspath((path or '').strip())
    bucket = os.path.abspath(BUCKET_DIR)
    if not clean.startswith(bucket + os.sep) and clean != bucket:
        raise ValueError('path outside ombre-brain bucket')
    return clean


def list_memory_points(*, limit: int = 15, load_frontmatter: Callable | None = None) -> list[dict]:
    try:
        import frontmatter as fm
    except ImportError as exc:
        raise RuntimeError('frontmatter not installed') from exc

    loader = load_frontmatter or fm.load
    items: list[dict] = []
    for path in glob.glob(f'{BUCKET_DIR}/**/*.md', recursive=True):
        try:
            post = loader(path)
            meta = post.metadata
            raw_v = meta.get('valence')
            raw_a = meta.get('arousal')
            if raw_v is None or raw_a is None:
                continue
            fv = normalize_valence(float(raw_v), scale=meta.get('valence_scale'))
            fa = normalize_arousal(float(raw_a))
            note = (post.content or '').replace('[[', '').replace(']]', '').strip()[:80]
            items.append({
                'path': path,
                'time': (meta.get('last_active') or meta.get('created', ''))[:10],
                'valence': round(fv, 2),
                'arousal': round(fa, 2),
                'emotion': _emotion_label(fv, fa),
                'note': note,
                'domain': '、'.join(meta.get('domain', [])),
                'scale': 'bipolar',
            })
        except Exception:
            continue
    items.sort(key=lambda row: row['time'], reverse=True)
    return items[:limit]


def update_memory_point(path: str, valence: float, arousal: float) -> dict:
    try:
        import frontmatter as fm
    except ImportError as exc:
        raise RuntimeError('frontmatter not installed') from exc

    safe_path = _safe_bucket_path(path)
    bipolar_v = max(-1.0, min(1.0, float(valence)))
    arousal_v = max(0.0, min(1.0, float(arousal)))

    post = fm.load(safe_path)
    post.metadata['valence'] = round(bipolar_v, 3)
    post.metadata['arousal'] = round(arousal_v, 3)
    post.metadata['valence_scale'] = 'bipolar'
    with open(safe_path, 'w', encoding='utf-8') as handle:
        handle.write(fm.dumps(post))

    return {
        'path': safe_path,
        'time': (post.metadata.get('last_active') or post.metadata.get('created', ''))[:10],
        'valence': round(bipolar_v, 2),
        'arousal': round(arousal_v, 2),
        'emotion': _emotion_label(bipolar_v, arousal_v),
        'note': (post.content or '').replace('[[', '').replace(']]', '').strip()[:80],
        'domain': '、'.join(post.metadata.get('domain', [])),
        'scale': 'bipolar',
    }
