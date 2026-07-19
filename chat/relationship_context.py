"""Deterministic, zero-model relationship continuity for chat providers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Callable, Optional


MAX_CONTEXT_CHARS = 400
_META_PATTERNS = (
    '应该', '需要你', '回复要', '表达得', '安抚她', '表现出',
    '请表达', '请表现', '不要泛泛安抚', '你要', '回复时', '语气要',
    '更主动', '更有感情', '表现得', '偏爱她',
)
_AFFECTION_MARKERS = (
    '爸爸', '小猫', '宝贝', '亲爱', '抱抱', '爱你', '乖乖', '宝宝',
)
_REPAIR_MARKERS = ('对不起', '生气', '难过', '吵架', '修复', '伤心', '委屈')


@dataclass(frozen=True)
class BandThresholds:
    warm_enter: float = 0.65
    warm_exit: float = 0.58
    tense_enter: float = 0.38
    tense_exit: float = 0.45
    high_arousal_enter: float = 0.65
    high_arousal_exit: float = 0.55
    intimate_enter: float = 0.65
    intimate_exit: float = 0.58


@dataclass(frozen=True)
class RelationshipContextResult:
    text: str
    slow_fingerprint: str
    emotion_band: str
    relationship_band: str

    @property
    def band_key(self) -> str:
        return self.emotion_band + '|' + self.relationship_band


def _clean_text(value, limit: int) -> str:
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if not text:
        return ''
    # Relationship continuity describes facts.  User-authored behavioral
    # instructions are not copied into this higher-priority context block.
    sentences = re.split(r'(?<=[。！？!?；;])\s*', text)
    safe = [s for s in sentences if s and not any(p in s for p in _META_PATTERNS)]
    text = ' '.join(safe).strip()
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + '…'


def _table_exists(conn, table: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None
    except Exception:
        return False


def _safe_rows(conn, sql: str, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:
        return []


def _fact_text(value, limit: int) -> str:
    return _clean_text(value, limit).rstrip('。！？!?；; ')


def classify_emotion_band(
    valence: float,
    arousal: float,
    previous: Optional[str] = None,
    thresholds: BandThresholds = BandThresholds(),
) -> str:
    """Quantize raw emotion with separate enter/exit thresholds."""
    previous = previous or ''
    was_warm = previous.startswith('warm_')
    was_tense = previous.startswith('tense_')
    was_high = previous.endswith('_high_arousal')

    warm = valence >= (thresholds.warm_exit if was_warm else thresholds.warm_enter)
    tense = valence <= (thresholds.tense_exit if was_tense else thresholds.tense_enter)
    high = arousal >= (
        thresholds.high_arousal_exit if was_high else thresholds.high_arousal_enter
    )
    if tense and high:
        return 'tense_high_arousal'
    if warm and high:
        return 'warm_high_arousal'
    if warm:
        return 'warm_low_arousal'
    return 'neutral_low_arousal'


def classify_relationship_band(
    recent_text: str,
    emotion_band: str,
    intimacy: float = 0.0,
    previous: Optional[str] = None,
    thresholds: BandThresholds = BandThresholds(),
    calibrated: bool = False,
) -> str:
    recent_text = str(recent_text or '')
    affectionate = any(marker in recent_text for marker in _AFFECTION_MARKERS)
    repairing = any(marker in recent_text for marker in _REPAIR_MARKERS)
    if repairing:
        return 'repairing'

    previous_intimate = (previous or '').startswith('intimate_')
    numerical_intimate = False
    if calibrated:
        numerical_intimate = intimacy >= (
            thresholds.intimate_exit if previous_intimate else thresholds.intimate_enter
        )
    intimate = affectionate or numerical_intimate
    if intimate and emotion_band == 'tense_high_arousal':
        return 'intimate_tense'
    if intimate:
        return 'intimate_relaxed'
    return 'task_focused'


def split_band_key(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not value or '|' not in value:
        return None, None
    emotion, relationship = value.split('|', 1)
    return emotion or None, relationship or None


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or '').encode('utf-8')).hexdigest()[:16]


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')
    return 'rel-v1:' + hashlib.sha256(encoded).hexdigest()


def build_relationship_context(
    get_db_fn: Callable,
    *,
    previous_emotion_band: Optional[str] = None,
    previous_relationship_band: Optional[str] = None,
    calibrated: bool = False,
    thresholds: BandThresholds = BandThresholds(),
) -> RelationshipContextResult:
    """Build one relationship block using SQLite only; never invokes a model."""
    conn = get_db_fn()
    try:
        summaries = []
        if _table_exists(conn, 'posts'):
            summaries = _safe_rows(conn,
                "SELECT id, content FROM posts WHERE type='DAILY_SUMMARY' "
                "AND COALESCE(resolved,0)=0 ORDER BY id DESC LIMIT 2"
            )

        continuity_rows = []
        if _table_exists(conn, 'posts'):
            # The persisted cross-window memo is the local, zero-model
            # handoff-like source.  Never call Ombre/dehydrator here.
            continuity_rows = _safe_rows(conn,
                "SELECT id, content FROM posts WHERE type='MEMORY' "
                "AND tags LIKE '%memo%' AND COALESCE(resolved,0)=0 "
                "ORDER BY id DESC LIMIT 2"
            )

        recent_rows = []
        if _table_exists(conn, 'chat_messages'):
            recent_rows = _safe_rows(conn,
                "SELECT id, content FROM chat_messages "
                "WHERE author='user' "
                "ORDER BY id DESC LIMIT 4"
            )

        board_rows = []
        if _table_exists(conn, 'board'):
            board_rows = _safe_rows(conn,
                "SELECT id, content, status FROM board WHERE status='open' "
                "ORDER BY id DESC LIMIT 2"
            )

        todo_rows = []
        if _table_exists(conn, 'todos'):
            todo_rows = _safe_rows(conn,
                "SELECT id, content, due_date FROM todos WHERE COALESCE(done,0)=0 "
                "ORDER BY id DESC LIMIT 2"
            )

        important_ids = []
        if _table_exists(conn, 'posts'):
            important_ids = [
                int(row[0]) for row in _safe_rows(conn,
                    "SELECT id FROM posts WHERE COALESCE(resolved,0)=0 "
                    "AND COALESCE(importance,0)>=7 ORDER BY id DESC LIMIT 3"
                )
            ]

        emotion = None
        if _table_exists(conn, 'emotion_state'):
            rows = _safe_rows(conn,
                "SELECT valence, arousal, sternberg_i FROM emotion_state WHERE id=1"
            )
            emotion = rows[0] if rows else None
    finally:
        conn.close()

    summary_items = [
        (int(row[0]), _fact_text(row[1], 130)) for row in summaries
        if _fact_text(row[1], 130)
    ]
    continuity_items = [
        (int(row[0]), _fact_text(row[1], 130)) for row in continuity_rows
        if _fact_text(row[1], 130)
    ]
    recent_items = [
        _fact_text(row[1], 58) for row in reversed(recent_rows)
        if _fact_text(row[1], 58)
    ]
    board_items = [
        (int(row[0]), _fact_text(row[1], 70), str(row[2] or ''))
        for row in board_rows if _fact_text(row[1], 70)
    ]
    todo_items = [
        (int(row[0]), _fact_text(row[1], 70), str(row[2] or ''))
        for row in todo_rows if _fact_text(row[1], 70)
    ]

    valence = float(emotion[0]) if emotion and emotion[0] is not None else 0.5
    arousal = float(emotion[1]) if emotion and emotion[1] is not None else 0.3
    intimacy = float(emotion[2]) if emotion and emotion[2] is not None else 0.0
    if calibrated:
        emotion_band = classify_emotion_band(
            valence, arousal, previous_emotion_band, thresholds,
        )
    else:
        # Production history is not yet diverse enough to calibrate thresholds.
        # Keep the low-frequency band stable until calibration is explicitly on.
        emotion_band = previous_emotion_band or 'neutral_low_arousal'
    recent_joined = '；'.join(recent_items)
    relationship_band = classify_relationship_band(
        recent_joined,
        emotion_band,
        intimacy,
        previous_relationship_band,
        thresholds,
        calibrated,
    )

    relationship_labels = {
        'intimate_relaxed': '当前互动偏亲密放松',
        'intimate_tense': '当前互动亲密，但情绪张力较高',
        'repairing': '当前互动处在修复与重新靠近中',
        'task_focused': '当前互动以共同处理事情为主',
    }
    lines = [
        '【近期关系脉络】',
        relationship_labels.get(relationship_band, relationship_labels['task_focused']) + '。',
    ]
    if recent_joined:
        lines.append('最近在聊：' + _clean_text(recent_joined, 145) + '。')
    if continuity_items:
        lines.append('最近连续性：' + continuity_items[0][1] + '。')
    elif summary_items:
        lines.append('最近连续性：' + summary_items[0][1] + '。')
    unfinished = [item[1] for item in board_items] + [item[1] for item in todo_items]
    if unfinished:
        lines.append('未结束事项：' + '；'.join(unfinished[:2]) + '。')
    if len(lines) == 2:
        lines.append('最近连续性：正在延续当前这段对话。')
    text = '\n'.join(lines)
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[:MAX_CONTEXT_CHARS - 1].rstrip() + '…'

    slow_payload = {
        'summaries': [(sid, _hash_text(content)) for sid, content in summary_items],
        'continuity': [(mid, _hash_text(content)) for mid, content in continuity_items],
        'open_board': [(bid, status, _hash_text(content)) for bid, content, status in board_items],
        'open_todos': [(tid, due, _hash_text(content)) for tid, content, due in todo_items],
        'important_event_ids': important_ids,
    }
    return RelationshipContextResult(
        text=text,
        slow_fingerprint=_fingerprint(slow_payload),
        emotion_band=emotion_band,
        relationship_band=relationship_band,
    )


def relationship_refresh_reason(
    *,
    is_cold: bool,
    current_fingerprint: str,
    current_band: str,
    current_user_turn: int,
    last_fingerprint: Optional[str],
    last_band: Optional[str],
    last_turn: int,
    idle_seconds: Optional[float],
    idle_refresh_seconds: float,
) -> Optional[str]:
    if is_cold or not last_fingerprint:
        return 'cold'
    if current_fingerprint != last_fingerprint:
        return 'slow_fingerprint'
    turns_since = max(0, int(current_user_turn) - int(last_turn or 0))
    if current_band != (last_band or '') and turns_since >= 2:
        return 'band_change'
    if turns_since >= 4:
        return 'periodic'
    if idle_seconds is not None and idle_seconds >= idle_refresh_seconds:
        return 'idle_return'
    return None
