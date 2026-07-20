"""Relationship continuity from Ombre bucket files and local SQLite.

Content rules (module contract — do not violate in sources or assembly):
- Describe relationship facts, temperature, and recent continuity only.
- Forbidden second-order behavioral instructions, e.g. 她期待 / 她敏感 / 你应该 /
  回复要 / 表达得 / 安抚她 / 表现出.

Never calls a model. Never imports ombre server.py or MCP handoff.
"""

from __future__ import annotations

from dataclasses import dataclass
import glob
import hashlib
import logging
import os
import re
from typing import Callable, Optional

MAX_CONTEXT_CHARS = 400
RELATIONSHIP_BUCKET_DIR = '/opt/ombre-brain/buckets/permanent/恋爱'
_LOG = logging.getLogger('relationship_context')
_META_PATTERNS = (
    '应该', '需要你', '回复要', '表达得', '安抚她', '表现出',
    '请表达', '请表现', '不要泛泛安抚', '你要', '回复时', '语气要',
    '更主动', '更有感情', '表现得', '偏爱她', '她期待', '她敏感',
)


@dataclass(frozen=True)
class RelationshipContextResult:
    text: str
    fingerprint: str

    @property
    def slow_fingerprint(self) -> str:
        return self.fingerprint


def _log(msg: str) -> None:
    _LOG.warning(msg)


def _strip_frontmatter(text: str) -> str:
    if text.startswith('---'):
        end = text.find('\n---', 3)
        if end != -1:
            return text[end + 4:].lstrip('\n')
    return text


def _clean_text(value, limit: int) -> str:
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if not text:
        return ''
    sentences = re.split(r'(?<=[。！？!?；;])\s*', text)
    safe = [s for s in sentences if s and not any(p in s for p in _META_PATTERNS)]
    text = ' '.join(safe).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + '…'


def _file_fingerprint(path: str) -> str:
    try:
        st = os.stat(path)
        return f'{int(st.st_mtime)}:{int(st.st_size)}'
    except OSError:
        return ''


def _read_relationship_anchor(bucket_dir: str = RELATIONSHIP_BUCKET_DIR) -> tuple[str, Optional[str]]:
    """Read permanent relationship bucket markdown; return (snippet, file fingerprint)."""
    try:
        files = sorted(glob.glob(os.path.join(bucket_dir, '*.md')))
        if not files:
            return '', None
        path = files[0]
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        body = _clean_text(_strip_frontmatter(text), 250)
        return body, _file_fingerprint(path)
    except Exception as exc:
        _log(f'relationship anchor read failed: {exc}')
        return '', None


def _latest_daily_summary_head(get_db_fn: Callable, *, max_chars: int = 120) -> str:
    conn = get_db_fn()
    try:
        row = conn.execute(
            "SELECT content FROM posts WHERE type='DAILY_SUMMARY' "
            "AND COALESCE(resolved, 0)=0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except Exception:
        return ''
    finally:
        conn.close()
    if not row:
        return ''
    content = _clean_text(row[0] if not hasattr(row, 'keys') else row['content'], max_chars)
    if not content:
        return ''
    parts = re.split(r'(?<=[。！？!?；;])', content)
    head = ''.join(parts[:2]).strip() or content
    return _clean_text(head, max_chars)


def _emotion_engine_scores() -> tuple[float, float]:
    try:
        import emotion_engine as _ee
        state = _ee.get_state()
        return float(state.get('valence', 0.5)), float(state.get('arousal', 0.3))
    except Exception:
        return 0.5, 0.3


def _quantized_mood() -> str:
    """Map continuous V/A to a low-frequency quadrant label."""
    valence, arousal = _emotion_engine_scores()
    v_q = '偏暖' if valence >= 0.5 else '偏低'
    a_q = '高唤醒' if arousal >= 0.55 else '低唤醒'
    return f'当前基调：{a_q}、{v_q}'


def _fingerprint(anchor_fp: Optional[str], recent: str, mood: str) -> str:
    payload = '|'.join([
        anchor_fp or '',
        hashlib.sha256((recent or '').encode('utf-8')).hexdigest()[:16],
        mood or '',
    ])
    return 'rel-v2:' + hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]


def build_relationship_context(
    get_db_fn: Callable,
    *,
    bucket_dir: str = RELATIONSHIP_BUCKET_DIR,
) -> RelationshipContextResult:
    anchor, anchor_fp = _read_relationship_anchor(bucket_dir)
    recent = _latest_daily_summary_head(get_db_fn)
    mood = _quantized_mood()
    parts = [p for p in (anchor, recent, mood) if p]
    text = ('【近期关系脉络】\n' + '\n'.join(parts)) if parts else ''
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[: MAX_CONTEXT_CHARS - 1].rstrip() + '…'
    if not text:
        _log('WARNING: relationship_context EMPTY — all sources failed or blank')
    return RelationshipContextResult(
        text=text,
        fingerprint=_fingerprint(anchor_fp, recent, mood),
    )


def should_send_relationship(
    *,
    is_cold: bool,
    rel_fp: str,
    last_fp: Optional[str],
    turns_since_rel_sent: int,
) -> bool:
    if is_cold or not last_fp:
        return True
    if rel_fp != last_fp:
        return True
    if int(turns_since_rel_sent) >= 4:
        return True
    return False


def rel_context_status(text: str, *, sent: bool) -> str:
    if not (text or '').strip():
        return 'EMPTY'
    return 'sent' if sent else 'skipped_unchanged'
