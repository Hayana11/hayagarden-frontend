"""Relationship continuity from Ombre bucket files and local SQLite.

Content rules (module contract — do not violate in sources or assembly):
- Describe relationship facts, temperature, and recent continuity only.
- Forbidden second-order behavioral instructions, e.g. 她期待 / 她敏感 / 你应该 /
  回复要 / 表达得 / 安抚她 / 表现出.

Never calls a model. Never imports ombre server.py or MCP handoff.
Note: system_builder / CC cold_once may still call _ombre_handoff_sync elsewhere;
this module only guarantees relationship_context itself does not depend on handoff.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import glob
import hashlib
import logging
import os
import re
from typing import Callable, Optional

MAX_CONTEXT_CHARS = 400
RELATIONSHIP_BUCKET_DIR = '/opt/ombre-brain/buckets/permanent/恋爱'
SOURCE_OK = 'ok'
SOURCE_MISSING = 'missing'
SOURCE_ERROR = 'error'
_LOG = logging.getLogger('relationship_context')
_META_PATTERNS = (
    '应该', '需要你', '回复要', '表达得', '安抚她', '表现出',
    '请表达', '请表现', '不要泛泛安抚', '你要', '回复时', '语气要',
    '更主动', '更有感情', '表现得', '偏爱她', '她期待', '她敏感',
)

# Mood hysteresis: enter thresholds vs hold thresholds (avoid 0.49/0.51 flip-flop).
_V_ENTER_WARM = 0.50
_V_HOLD_WARM = 0.45
_A_ENTER_HIGH = 0.55
_A_HOLD_HIGH = 0.50


@dataclass(frozen=True)
class RelationshipContextResult:
    text: str
    fingerprint: str
    sources: dict = field(default_factory=dict)
    mood_key: Optional[str] = None

    @property
    def slow_fingerprint(self) -> str:
        return self.fingerprint

    @property
    def degraded(self) -> bool:
        return any(status != SOURCE_OK for status in (self.sources or {}).values())


def _log(msg: str) -> None:
    _LOG.warning(msg)


def _strip_frontmatter(text: str) -> str:
    if text.startswith('---'):
        end = text.find('\n---', 3)
        if end != -1:
            return text[end + 4:].lstrip('\n')
    return text


def _clean_text(value, limit: int) -> str:
    text = re.sub(r'\[\[|\]\]', '', str(value or ''))
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return ''
    sentences = re.split(r'(?<=[。！？!?；;])\s*', text)
    safe = [s for s in sentences if s and not any(p in s for p in _META_PATTERNS)]
    text = ' '.join(safe).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + '…'


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()[:16]


def _read_relationship_anchor(
    bucket_dir: str = RELATIONSHIP_BUCKET_DIR,
    *,
    total_limit: int = 250,
) -> tuple[str, Optional[str], str]:
    """Read all permanent relationship bucket markdown files (sorted by name).

    Selection rule: every ``*.md`` under the directory is included.  Each file
    gets a fair share of ``total_limit`` chars
    (``max(30, total_limit // n)``) so a long first file cannot starve later
    buckets.  Fingerprint is a hash of the cleaned merged body.
    """
    try:
        if not os.path.isdir(bucket_dir):
            _log(f'relationship anchor missing: bucket dir not found: {bucket_dir}')
            return '', None, SOURCE_MISSING
        files = sorted(glob.glob(os.path.join(bucket_dir, '*.md')))
        if not files:
            _log(f'relationship anchor missing: no .md files in {bucket_dir}')
            return '', None, SOURCE_MISSING
        per_file_limit = max(30, total_limit // len(files))
        snippets = []
        for path in files:
            with open(path, encoding='utf-8') as fh:
                raw = _strip_frontmatter(fh.read())
            snippet = _clean_text(raw, per_file_limit)
            if snippet:
                snippets.append(snippet)
        body = _clean_text('\n'.join(snippets), total_limit)
        if not body:
            _log('relationship anchor missing: bucket files empty after clean')
            return '', None, SOURCE_MISSING
        return body, _content_hash(body), SOURCE_OK
    except Exception as exc:
        _log(f'relationship anchor error: {exc}')
        return '', None, SOURCE_ERROR


def _latest_daily_summary_head(
    get_db_fn: Callable, *, max_chars: int = 120,
) -> tuple[str, str]:
    """Return (snippet, health). Connection creation is inside try — never raises."""
    conn = None
    try:
        conn = get_db_fn()
        row = conn.execute(
            "SELECT content FROM posts WHERE type='DAILY_SUMMARY' "
            "AND COALESCE(resolved, 0)=0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except Exception as exc:
        _log(f'daily source error: {exc}')
        return '', SOURCE_ERROR
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if not row:
        return '', SOURCE_MISSING
    content = _clean_text(
        row[0] if not hasattr(row, 'keys') else row['content'], max_chars,
    )
    if not content:
        return '', SOURCE_MISSING
    parts = re.split(r'(?<=[。！？!?；;])', content)
    head = ''.join(parts[:2]).strip() or content
    return _clean_text(head, max_chars), SOURCE_OK


def _emotion_engine_scores() -> tuple[Optional[float], Optional[float], str]:
    """Read continuous V/A. On failure return (None, None, error) — never fake defaults."""
    try:
        import emotion_engine as _ee
        state = _ee.get_state()
        return float(state.get('valence', 0.5)), float(state.get('arousal', 0.3)), SOURCE_OK
    except Exception as exc:
        _log(f'mood source error: {exc}')
        return None, None, SOURCE_ERROR


def _apply_mood_hysteresis(
    valence: float,
    arousal: float,
    previous_mood: Optional[str],
) -> str:
    """Return mood_key ``高唤醒|偏暖`` with enter/hold thresholds."""
    prev_a, prev_v = None, None
    if previous_mood and '|' in previous_mood:
        prev_a, prev_v = previous_mood.split('|', 1)

    if prev_v == '偏暖':
        v_q = '偏暖' if valence >= _V_HOLD_WARM else '偏低'
    else:
        v_q = '偏暖' if valence >= _V_ENTER_WARM else '偏低'

    if prev_a == '高唤醒':
        a_q = '高唤醒' if arousal >= _A_HOLD_HIGH else '低唤醒'
    else:
        a_q = '高唤醒' if arousal >= _A_ENTER_HIGH else '低唤醒'

    return f'{a_q}|{v_q}'


def _quantized_mood(
    previous_mood: Optional[str] = None,
) -> tuple[str, Optional[str], str]:
    """Map continuous V/A to a low-frequency quadrant label with hysteresis.

    Returns (text, mood_key, health). Emotion-engine failure → empty text + error
    (never synthesize a fake default mood that would mask EMPTY).
    """
    valence, arousal, health = _emotion_engine_scores()
    if health != SOURCE_OK or valence is None or arousal is None:
        return '', None, health
    mood_key = _apply_mood_hysteresis(valence, arousal, previous_mood)
    a_q, v_q = mood_key.split('|', 1)
    return f'当前基调：{a_q}、{v_q}', mood_key, SOURCE_OK


def _fingerprint(anchor_fp: Optional[str], recent: str, mood: str) -> str:
    payload = '|'.join([
        anchor_fp or '',
        _content_hash(recent or ''),
        mood or '',
    ])
    return 'rel-v2:' + hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]


def build_relationship_context(
    get_db_fn: Callable,
    *,
    bucket_dir: str = RELATIONSHIP_BUCKET_DIR,
    previous_mood: Optional[str] = None,
) -> RelationshipContextResult:
    anchor, anchor_fp, anchor_health = _read_relationship_anchor(bucket_dir)
    recent, daily_health = _latest_daily_summary_head(get_db_fn)
    mood, mood_key, mood_health = _quantized_mood(previous_mood)
    sources = {
        'anchor': anchor_health,
        'daily': daily_health,
        'mood': mood_health,
    }
    parts = [p for p in (anchor, recent, mood) if p]
    text = ('【近期关系脉络】\n' + '\n'.join(parts)) if parts else ''
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[: MAX_CONTEXT_CHARS - 1].rstrip() + '…'
    if not text:
        _log(
            'WARNING: relationship_context EMPTY — all sources failed or blank '
            f'(sources={sources})'
        )
    elif any(h != SOURCE_OK for h in sources.values()):
        _log(f'relationship_context degraded: sources={sources}')
    return RelationshipContextResult(
        text=text,
        fingerprint=_fingerprint(anchor_fp, recent, mood),
        sources=sources,
        mood_key=mood_key,
    )


def should_send_relationship(
    *,
    is_cold: bool,
    rel_fp: str,
    last_fp: Optional[str],
    turns_since_rel_sent: int,
    user_turn: bool = True,
) -> bool:
    """Decide whether to inject relationship context this turn.

    Cadence uses the *effective* user-turn count (already-committed skips plus
    the current user turn), so after a send the next refresh lands on the
    5th user turn: send → skip → skip → skip → send.
    Non-user turns never force a periodic refresh by themselves.
    """
    if is_cold or not last_fp:
        return True
    if rel_fp != last_fp:
        return True
    effective_turns = int(turns_since_rel_sent) + (1 if user_turn else 0)
    if effective_turns >= 4:
        return True
    return False


def rel_context_status(
    text: str,
    *,
    sent: bool,
    sources: Optional[dict] = None,
) -> str:
    """Usage marker: EMPTY | sent | degraded | skipped_unchanged.

    Observability note: ``daily=missing`` alone (fresh deploy / before the
    first daily summary) marks a send as ``degraded``.  Treat a single
    ``daily=missing`` as expected; escalate only if it persists across days.
    """
    if not (text or '').strip():
        return 'EMPTY'
    degraded = any(status != SOURCE_OK for status in (sources or {}).values())
    if sent:
        return 'degraded' if degraded else 'sent'
    return 'skipped_unchanged'
