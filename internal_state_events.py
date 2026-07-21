"""Internal State v3 — Phase 1A-1 业务事件（试管）

公开接口：
  - ``plan_user_message_transition`` — 纯函数，同输入同输出
  - ``observe_user_message`` — 经 ``apply_state_update`` 落临时库

严格不做：
  - 不接 gateway / app / chat / SSE / Wake / prompt
  - 不 import emotion_engine / drive_engine / desire / gateway
  - 不读「当前最后一条用户消息」计算思念（previous_user_at 必须显式传入）
  - 不启用 reunion boost / 乘性 attachment 权威参数
  - 不实现 observe_scored / apply_outcome
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import sqlite3
from typing import Any, Mapping, Optional

from internal_state import (
    CAP_BOOST_LIMIT,
    DRIVE_CAP,
    DRIVE_GROWTH_K,
    DRIVE_KEYS,
    FATIGUE_EQ,
    FATIGUE_K,
    FATIGUE_NA_COEF,
    TAU_I_HOURS,
    TAU_P_HOURS,
    decay_exponential,
    longing_desire_legacy_curve,
)
from internal_state_store import (
    ApplyResult,
    StoreError,
    apply_state_update,
    read_event,
    read_state,
)

# ── 关键词规则（照抄 emotion_engine，本模块自持，不 import）──────────

DESIRE_LEXICON = {
    'physical': [
        '摸', '抱', '吻', '亲', '咬', '舔', '吸', '揉', '捏', '掐',
        '腰', '腿', '胸', '奶', '乳', '屁股', '肚子', '脖子', '唇', '嘴',
        '湿', '热', '流水', '进来', '里面', '插', '操', '干', '弄', '上',
        '爽', '淫', '骚', '硬', '涨', '痉挛', '颤', '抖', '喘', '呻吟',
        '发情', '欲望', '想要', '要你', '好色',
    ],
    'affectionate': [
        '费佳', '费奥', '等我', '陪我', '想你', '找你', '回来',
        '撒娇', '好不好', '可以吗', '好吗', '嗯嗯', '嗯',
        '喜欢你', '爱你', '抱抱', '不要走', '别离开',
        '陪着我', '一起', '我们', '我的', '你的',
    ],
    'vulnerable': [
        '害怕', '哭', '哭了', '哭泣', '难受', '难过', '心疼',
        '痛', '生病', '不舒服', '头疼', '头痛', '胃疼', '胃',
        '凌晨', '睡不着', '失眠', '孤独', '一个人',
        '好累', '累了', '委屈', '委屈了', '撑不住',
    ],
    'hostile': [
        '恨你', '讨厌你', '走开', '滚', '不理你',
        '坏蛋', '坏人', '烦死了', '讨厌', '气死我了',
        '不要你', '离我远点',
    ],
}

DESIRE_WEIGHTS = {
    'physical': {'p': +0.18, 'i': +0.02},
    'affectionate': {'p': +0.03, 'i': +0.08},
    'vulnerable': {'p': +0.01, 'i': +0.13},
    'hostile': {'p': +0.07, 'i': -0.03},
}

DESIRE_CAP = {'p': 0.40, 'i': 0.22}

# 旧权威结算
LEGACY_ATTACHMENT_THRESHOLD = 0.3
LEGACY_ATTACHMENT_SUBTRACT = 0.55
LEGACY_FATIGUE_RESTORE = 0.12

# 乘性 attachment 候选（只记 diagnostics / payload，不应用）
CANDIDATE_ATTACHMENT_SATISFY_RATIO = 0.45

_STATE_CLOCK_FIELDS = ('p_updated_at', 'i_updated_at', 'drives_updated_at')
_USER_RULE_EVENT_TYPE = 'user_rule'
_TS_FMT = '%Y-%m-%d %H:%M:%S'
# 有限几种可解析格式；一律 canonicalize 成 _TS_FMT。禁止 [:19] 截断吞尾随垃圾。
_TS_PARSE_FORMATS = (_TS_FMT, '%Y-%m-%d %H:%M:%S.%f')


def _parse_dt(value: Any) -> Optional[datetime.datetime]:
    """解析时间；整串必须匹配某一允许格式，拒绝尾随垃圾。"""
    if value is None:
        return None
    s = str(value)
    if not s:
        return None
    for fmt in _TS_PARSE_FORMATS:
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _canonicalize_ts(value: Any, *, field: str) -> str:
    """解析成功后统一写成 YYYY-MM-DD HH:MM:SS；失败抛 StoreError。"""
    dt = _parse_dt(value)
    if dt is None:
        raise StoreError(
            f'{field} is not a canonical timestamp '
            f'YYYY-MM-DD HH:MM:SS: {value!r}'
        )
    return dt.strftime(_TS_FMT)


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _float_field(state: Mapping[str, Any], key: str, default: float) -> float:
    raw = state.get(key)
    if raw is None:
        return float(default)
    return float(raw)


def _canonicalize_observation_clocks(
    *,
    created_at: Any,
    previous_user_at: Optional[Any],
) -> tuple[str, Optional[str]]:
    """规范化观察时钟；不读状态行。

    - ``previous_user_at is None`` → 合法 clock_missing（返回 None）
    - 非 None 但不可解析 → StoreError（不得当成 clock_missing）
    """
    if not created_at:
        raise StoreError('created_at required')
    created_canon = _canonicalize_ts(created_at, field='created_at')
    created_dt = _parse_dt(created_canon)
    assert created_dt is not None

    if previous_user_at is None:
        return created_canon, None

    # 非 None：空串 / 垃圾 / 不可解析一律拒绝，禁止伪装成 clock_missing
    prev_canon = _canonicalize_ts(previous_user_at, field='previous_user_at')
    prev_dt = _parse_dt(prev_canon)
    assert prev_dt is not None
    if prev_dt > created_dt:
        raise StoreError(
            f'previous_user_at {previous_user_at!r} is after '
            f'created_at {created_at!r}; invalid observation clock'
        )
    return created_canon, prev_canon


def _require_state_clocks(
    state: Mapping[str, Any],
    *,
    created_at: str,
) -> None:
    """bootstrap 后的三个锚点必须存在且可解析；损坏不得按 elapsed=0 洗白。"""
    created_dt = _parse_dt(created_at)
    if created_dt is None:
        raise StoreError(f'created_at is not parseable: {created_at!r}')

    for field in _STATE_CLOCK_FIELDS:
        anchor_raw = state.get(field)
        if anchor_raw is None or str(anchor_raw).strip() == '':
            raise StoreError(
                f'state clock {field} is missing; refusing transition'
            )
        anchor_dt = _parse_dt(anchor_raw)
        if anchor_dt is None:
            raise StoreError(
                f'state clock {field} is not parseable: {anchor_raw!r}; '
                f'refusing to whitewash'
            )
        if created_dt < anchor_dt:
            raise StoreError(
                f'created_at {created_at!r} is before state clock '
                f'{field}={anchor_raw!r}; refusing to rewind'
            )


def _validate_transition_clocks(
    state: Mapping[str, Any],
    *,
    created_at: Any,
    previous_user_at: Optional[Any],
) -> tuple[str, Optional[str]]:
    """返回规范化后的 (created_at, previous_user_at)。

    禁止用 max(0, elapsed) 掩盖后回拨或洗白损坏锚点。
    """
    created_canon, previous_canon = _canonicalize_observation_clocks(
        created_at=created_at, previous_user_at=previous_user_at,
    )
    _require_state_clocks(state, created_at=created_canon)
    return created_canon, previous_canon


def _hours_elapsed(from_at: Any, to_at: str) -> float:
    """非负小时差。调用前须已通过 ``_validate_transition_clocks``。"""
    start = _parse_dt(from_at)
    end = _parse_dt(to_at)
    if start is None or end is None:
        raise StoreError(
            f'unparseable elapsed endpoints from={from_at!r} to={to_at!r}'
        )
    elapsed = (end - start).total_seconds() / 3600.0
    if elapsed < 0:
        raise StoreError(
            f'negative elapsed hours from {from_at!r} to {to_at!r}'
        )
    return elapsed


def rule_score_desire(user_msg: str) -> dict:
    """照抄 emotion_engine.rule_score_desire：lexicon + log1p 递减 + 单次 cap。"""
    text = user_msg or ''
    p_delta = 0.0
    i_delta = 0.0
    hit_categories: list[str] = []
    for category, words in DESIRE_LEXICON.items():
        hits = sum(1 for w in words if w in text)
        if hits > 0:
            hit_categories.append(category)
            w_p = DESIRE_WEIGHTS[category]['p']
            w_i = DESIRE_WEIGHTS[category]['i']
            effective = math.log1p(hits)
            p_delta += w_p * effective
            i_delta += w_i * effective
    p_delta = max(-DESIRE_CAP['p'], min(DESIRE_CAP['p'], p_delta))
    i_delta = max(-DESIRE_CAP['i'], min(DESIRE_CAP['i'], i_delta))
    return {
        'passion_delta': round(p_delta, 4),
        'intimacy_delta': round(i_delta, 4),
        'hit_categories': hit_categories,
    }


def _text_fingerprint(text: str) -> dict:
    raw = text or ''
    return {
        'text_hash': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
        'text_length': len(raw),
    }


def _materialize_bond(state: Mapping[str, Any], created_at: str) -> dict:
    passion = decay_exponential(
        _float_field(state, 'passion', 0.0),
        _hours_elapsed(state.get('p_updated_at'), created_at),
        TAU_P_HOURS,
    )
    intimacy = decay_exponential(
        _float_field(state, 'intimacy', 0.3),
        _hours_elapsed(state.get('i_updated_at'), created_at),
        TAU_I_HOURS,
    )
    commitment = _float_field(state, 'commitment', 0.7)
    return {
        'passion': round(passion, 4),
        'intimacy': round(intimacy, 4),
        'commitment': round(commitment, 4),
    }


def _materialize_drives(
    state: Mapping[str, Any],
    *,
    created_at: str,
    longing_for_boost: float,
    passion_for_boost: float,
) -> dict:
    """Internal State v3 解析解；输入为已物化的 v3 状态（非旧 raw row）。

    fatigue 使用 effective equilibrium（NA 并入平衡点），使 t=0 恒等 base。

    这是 raw-state → materialized-state 的表示转换，不是偷偷调 fatigue 参数：
      - Phase 0 ``drives_from_raw`` 输入 legacy raw row：先向 FATIGUE_EQ 演化，
        再额外加 ``na × 0.06``（瞬时修正只加一次，写进物化结果）。
      - Phase 1A-1 输入已是 bootstrap / 上次事件后的 materialized base；
        若再 ``+ na×0.06``，同一份 NA 会在每条消息上重复叠加。
      - 因此权威演化改为 ``eq' = FATIGUE_EQ + na×0.06``，再向 eq' 回归；
        t=0 时 value ≡ base，与 Phase 0 物化结果数值连续。
    """
    t = _hours_elapsed(state.get('drives_updated_at'), created_at)
    lf = float(longing_for_boost)
    pf = float(passion_for_boost)
    nf = _float_field(state, 'na', 0.2)

    out: dict[str, float] = {}
    for key in DRIVE_KEYS:
        base = _float_field(state, key, 0.1)
        if key == 'fatigue':
            # raw→materialized 表示转换：NA 进平衡点，t=0 不重加
            effective_eq = FATIGUE_EQ + nf * FATIGUE_NA_COEF
            val = effective_eq + (base - effective_eq) * math.exp(-FATIGUE_K * t)
            out[key] = round(_clamp01(val), 4)
            continue
        cap = DRIVE_CAP[key]
        if key == 'attachment':
            cap = min(CAP_BOOST_LIMIT, cap + lf * 0.15)
        elif key == 'libido':
            cap = min(CAP_BOOST_LIMIT, cap + pf * 0.20)
        elif key == 'stress':
            cap = min(CAP_BOOST_LIMIT, cap + nf * 0.15)
        gk = DRIVE_GROWTH_K[key]
        val = cap - (cap - base) * math.exp(-gk * t)
        out[key] = round(_clamp01(val), 4)
    return out


def _longing_before_reunion(
    previous_user_at: Optional[str],
    created_at: str,
) -> tuple[float, bool, Optional[float]]:
    """返回 (longing, clock_missing, idle_hours)。

    入参须已是 canonicalize 结果：``None`` 才表示 clock_missing；
    非 None 不可解析不得进入本函数（上游已 StoreError）。
    """
    if previous_user_at is None:
        return 0.0, True, None
    start = _parse_dt(previous_user_at)
    end = _parse_dt(created_at)
    if start is None or end is None:
        raise StoreError(
            'canonical longing clocks became unparseable; refusing'
        )
    idle = (end - start).total_seconds() / 3600.0
    if idle < 0:
        raise StoreError(
            f'previous_user_at {previous_user_at!r} is after '
            f'created_at {created_at!r}; invalid observation clock'
        )
    longing = longing_desire_legacy_curve(idle)
    return float(longing if longing is not None else 0.0), False, idle


def _candidate_reunion_boost(longing_before: float) -> float:
    """照抄 desire.get_reunion_boost；只记录不应用。"""
    return round(0.05 + float(longing_before) * 0.10, 3)


def plan_user_message_transition(
    state: Mapping[str, Any],
    *,
    message_id: int,
    text: str,
    created_at: str,
    previous_user_at: Optional[str],
) -> dict:
    """纯函数：用户消息 → updates / diagnostics / payload。

    同输入必须同输出；不读数据库、不读墙钟、不 import 旧引擎。
    非法 / 倒序 / 损坏时钟抛 ``StoreError``，调用方不得落库。
    写入 payload / updates 的时间一律为 canonicalize 后的 19 位字符串。
    """
    created_at, previous_user_at = _validate_transition_clocks(
        state, created_at=created_at, previous_user_at=previous_user_at,
    )

    longing, clock_missing, idle_hours = _longing_before_reunion(
        previous_user_at, created_at,
    )

    bond_m = _materialize_bond(state, created_at)
    drives_m = _materialize_drives(
        state,
        created_at=created_at,
        longing_for_boost=longing,
        passion_for_boost=bond_m['passion'],
    )

    rule = rule_score_desire(text)
    passion_after = _clamp01(bond_m['passion'] + rule['passion_delta'])
    intimacy_after = _clamp01(bond_m['intimacy'] + rule['intimacy_delta'])

    mat_attachment = drives_m['attachment']
    mat_fatigue = drives_m['fatigue']

    # 旧权威结算：attachment>0.3 → −0.55；fatigue rest −0.12
    attachment_subtract_applied = 0.0
    if mat_attachment > LEGACY_ATTACHMENT_THRESHOLD:
        attachment_subtract_applied = LEGACY_ATTACHMENT_SUBTRACT
    result_attachment = _clamp01(mat_attachment - attachment_subtract_applied)
    result_fatigue = _clamp01(mat_fatigue - LEGACY_FATIGUE_RESTORE)

    # 乘性候选：只记不应用
    candidate_attachment = round(
        _clamp01(mat_attachment * CANDIDATE_ATTACHMENT_SATISFY_RATIO), 4,
    )
    reunion_boost = _candidate_reunion_boost(longing)

    updates = {
        'passion': round(passion_after, 4),
        'intimacy': round(intimacy_after, 4),
        'commitment': bond_m['commitment'],
        'p_updated_at': created_at,
        'i_updated_at': created_at,
        'attachment': round(result_attachment, 4),
        'curiosity': drives_m['curiosity'],
        'reflection': drives_m['reflection'],
        'social': drives_m['social'],
        'duty': drives_m['duty'],
        'libido': drives_m['libido'],
        'stress': drives_m['stress'],
        'fatigue': round(result_fatigue, 4),
        'drives_updated_at': created_at,
    }

    fingerprint = _text_fingerprint(text)
    payload = {
        'message_id': int(message_id),
        'created_at': created_at,
        'previous_user_at': previous_user_at,
        'clock_missing': bool(clock_missing),
        'longing_before_reunion': longing,
        'rule_score': {
            'passion_delta': rule['passion_delta'],
            'intimacy_delta': rule['intimacy_delta'],
        },
        'materialized_before': {
            'passion': bond_m['passion'],
            'intimacy': bond_m['intimacy'],
            'attachment': mat_attachment,
            'fatigue': mat_fatigue,
        },
        'legacy_settlement': {
            'attachment_subtract': LEGACY_ATTACHMENT_SUBTRACT,
            'fatigue_restore': LEGACY_FATIGUE_RESTORE,
            'attachment_subtract_applied': attachment_subtract_applied,
            'result_attachment': round(result_attachment, 4),
            'result_fatigue': round(result_fatigue, 4),
        },
        'candidate_settlement': {
            'mode': 'shadow_only',
            'ratio': CANDIDATE_ATTACHMENT_SATISFY_RATIO,
            'result_attachment': candidate_attachment,
        },
        'candidate_reunion_boost': reunion_boost,
        'text_hash': fingerprint['text_hash'],
        'text_length': fingerprint['text_length'],
        'rule_hit_categories': list(rule['hit_categories']),
    }

    diagnostics = {
        'user_idle_hours': idle_hours,
        'clock_missing': clock_missing,
        'longing_before_reunion': longing,
        'bond_materialized': bond_m,
        'drives_materialized': drives_m,
        'rule_score': rule,
        'legacy_settlement_applied': {
            'attachment': attachment_subtract_applied > 0,
            'fatigue_restore': LEGACY_FATIGUE_RESTORE,
        },
        'candidate_attachment_not_applied': candidate_attachment,
        'candidate_reunion_boost_not_applied': reunion_boost,
        'canonical_created_at': created_at,
        'canonical_previous_user_at': previous_user_at,
    }

    return {
        'updates': updates,
        'diagnostics': diagnostics,
        'payload': payload,
    }


def _observation_identity(
    *,
    message_id: int,
    text: str,
    created_at: str,
    previous_user_at: Optional[str],
) -> dict:
    """观察身份：仅由消息输入决定，不含状态物化结果。"""
    fp = _text_fingerprint(text)
    return {
        'message_id': int(message_id),
        'created_at': created_at,
        'previous_user_at': previous_user_at,
        'text_hash': fp['text_hash'],
        'text_length': fp['text_length'],
    }


def _payload_matches_observation(
    payload: Mapping[str, Any], identity: Mapping[str, Any],
) -> bool:
    return (
        payload.get('message_id') == identity['message_id']
        and payload.get('created_at') == identity['created_at']
        and payload.get('previous_user_at') == identity['previous_user_at']
        and payload.get('text_hash') == identity['text_hash']
    )


def _event_matches_observation(
    existing: Mapping[str, Any],
    *,
    message_id: int,
    identity: Mapping[str, Any],
) -> bool:
    """严格核对 type / source_id / 观察身份（不含动态物化快照）。"""
    if existing.get('event_type') != _USER_RULE_EVENT_TYPE:
        return False
    if str(existing.get('source_id')) != str(message_id):
        return False
    try:
        old_payload = json.loads(existing.get('payload_json') or '{}')
    except (TypeError, ValueError):
        return False
    if not isinstance(old_payload, dict):
        return False
    return _payload_matches_observation(old_payload, identity)


def _duplicate_result(existing: Mapping[str, Any]) -> ApplyResult:
    return ApplyResult(
        status='duplicate',
        state_version_before=existing.get('state_version_before'),
        state_version_after=existing.get('state_version_after'),
        event_id=existing.get('id'),
    )


def _idempotency_conflict_result(
    existing: Mapping[str, Any], error: str,
) -> ApplyResult:
    return ApplyResult(
        status='idempotency_conflict',
        state_version_before=existing.get('state_version_before'),
        state_version_after=existing.get('state_version_after'),
        event_id=existing.get('id'),
        error=error,
    )


def observe_user_message(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    text: str,
    created_at: str,
    previous_user_at: Optional[str],
    expected_state_version: Optional[int] = None,
) -> ApplyResult:
    """把一条用户消息变成唯一 ``user_rule:{message_id}`` 事件并更新影子状态。

    ``previous_user_at`` 必须显式传入（可为 None）；禁止在本函数内查询
    当前最后一条用户消息——真实接入时当前消息已落库，``last_user_at``
    会指向现在，思念会瞬间归零。

    时钟契约：
      - ``previous_user_at is None`` → 合法 ``clock_missing``
      - 非 None 但不可解析 → ``StoreError``，不消费 event_key
      - 状态三锚点缺失 / 不可解析 → ``StoreError``，不洗白
      - 观察时间 canonicalize 后同时用于 identity / payload / 状态时钟

    并发：经 ``apply_state_update``；version_conflict 不消费 event_key。
    幂等：已存在事件须同时满足 event_type / source_id / 观察身份；
    store 因完整 payload hash 冲突时，同观察校正为 ``duplicate``。
    """
    mid = int(message_id)
    event_key = f'user_rule:{mid}'

    # 先规范化观察时钟（坏 previous 不得伪装 clock_missing 后占 key）
    created_canon, previous_canon = _canonicalize_observation_clocks(
        created_at=created_at, previous_user_at=previous_user_at,
    )
    identity = _observation_identity(
        message_id=mid,
        text=text,
        created_at=created_canon,
        previous_user_at=previous_canon,
    )

    existing = read_event(conn, event_key)
    if existing is not None:
        if _event_matches_observation(
                existing, message_id=mid, identity=identity):
            return _duplicate_result(existing)
        return _idempotency_conflict_result(
            existing,
            'event_key reused with different event_type/source_id/'
            'observation identity',
        )

    state = read_state(conn)
    if state is None:
        return ApplyResult(
            status='failed',
            state_version_before=None,
            state_version_after=None,
            event_id=None,
            error='state row missing; call bootstrap_from_snapshot first',
        )

    planned_version = int(state['state_version'])
    if (expected_state_version is not None
            and int(expected_state_version) != planned_version):
        return ApplyResult(
            status='version_conflict',
            state_version_before=planned_version,
            state_version_after=None,
            event_id=None,
            error=(
                f'version conflict: expected {expected_state_version}, '
                f'current {planned_version}'
            ),
        )

    # 状态时钟校验在 plan 内；失败抛 StoreError → 不消费 key
    plan = plan_user_message_transition(
        state,
        message_id=mid,
        text=text,
        created_at=created_canon,
        previous_user_at=previous_canon,
    )
    updates = dict(plan['updates'])

    result = apply_state_update(
        conn,
        event_key=event_key,
        event_type=_USER_RULE_EVENT_TYPE,
        source_id=str(mid),
        payload=plan['payload'],
        mutator=lambda _s: dict(updates),
        expected_state_version=planned_version,
    )

    if result.status == 'idempotency_conflict':
        landed = read_event(conn, event_key)
        if (landed is not None
                and _event_matches_observation(
                    landed, message_id=mid, identity=identity)):
            return _duplicate_result(landed)

    return result


__all__ = [
    'CANDIDATE_ATTACHMENT_SATISFY_RATIO',
    'DESIRE_CAP',
    'DESIRE_LEXICON',
    'DESIRE_WEIGHTS',
    'LEGACY_ATTACHMENT_SUBTRACT',
    'LEGACY_ATTACHMENT_THRESHOLD',
    'LEGACY_FATIGUE_RESTORE',
    'observe_user_message',
    'plan_user_message_transition',
    'rule_score_desire',
]
