"""Internal State v3 — Phase 1A-1/1A-2/1A-3 业务事件（试管）

公开接口：
  - ``plan_user_message_transition`` / ``observe_user_message``
  - ``plan_scored_transition`` / ``observe_scored`` / ``get_scored_event_stats``
  - ``plan_outcome_transition`` / ``apply_outcome``

严格不做：
  - 不接 gateway / app / chat / SSE / Wake / prompt / wake.executor
  - 不 import emotion_engine / drive_engine / desire / gateway / wake
  - 不调用 DeepSeek / 渐变脑 / score_async
  - 不启用 reunion boost / 新 attachment ratio 权威参数
  - 不停止旧 discharge / satisfy（本 PR 无生产调用点）
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
    ConditionalDecision,
    StoreError,
    apply_conditional_state_update,
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
_BOND_CLOCK_FIELDS = ('p_updated_at', 'i_updated_at')
_USER_RULE_EVENT_TYPE = 'user_rule'
_USER_SCORED_EVENT_TYPE = 'user_scored'
_TS_FMT = '%Y-%m-%d %H:%M:%S'
# 有限几种可解析格式。禁止 [:19] 截断吞尾随垃圾。
# 小数秒仅允许全 0（兼容 .0 / .000000）；非零微秒在 canonicalize 拒绝。
_TS_PARSE_FORMATS = (_TS_FMT, '%Y-%m-%d %H:%M:%S.%f')


def _parse_dt(value: Any) -> Optional[datetime.datetime]:
    """解析时间；整串必须匹配某一允许格式，拒绝尾随垃圾。

    注意：本函数可能返回带 microsecond 的 datetime；业务入口必须再经
    ``_canonicalize_ts`` / ``_require_state_clocks``，禁止先截断再比较。
    """
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
    """规范化为 YYYY-MM-DD HH:MM:SS。

    - 标准 19 位：接受
    - 小数部分全为 0（如 ``.0`` / ``.000000``）：接受并去掉小数
    - ``microsecond != 0``：``StoreError``（不得静默截成整秒掩盖倒序）
    """
    dt = _parse_dt(value)
    if dt is None:
        raise StoreError(
            f'{field} is not a canonical timestamp '
            f'YYYY-MM-DD HH:MM:SS: {value!r}'
        )
    if dt.microsecond != 0:
        raise StoreError(
            f'{field} has non-zero fractional seconds: {value!r}; '
            f'only whole seconds or zero-fraction (.000000) are accepted'
        )
    return dt.strftime(_TS_FMT)


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _float_field(state: Mapping[str, Any], key: str, default: float) -> float:
    raw = state.get(key)
    if raw is None:
        return float(default)
    return float(raw)


_SQLITE_MAX_S64 = (2 ** 63) - 1


def _require_message_id(value: Any) -> int:
    """事件身份用 message_id：只接受正整数，禁止 bool / 浮点静默截断。

    ``user_rule`` 与 ``user_scored`` 共用；非法值抛 ``StoreError`` 且不得
    消费 event_key（调用方须在写库前先校验）。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise StoreError(
            f'message_id must be a positive integer: {value!r}'
        )
    if value <= 0 or value > _SQLITE_MAX_S64:
        raise StoreError(
            f'message_id out of SQLite integer range: {value!r}'
        )
    return value


def _canonicalize_observation_clocks(
    *,
    created_at: Any,
    previous_user_at: Optional[Any],
) -> tuple[str, Optional[str]]:
    """规范化观察时钟；不读状态行。

    - ``previous_user_at is None`` → 合法 clock_missing（返回 None）
    - 非 None 但不可解析 / 非零微秒 → StoreError（不得当成 clock_missing）
    """
    if not created_at:
        raise StoreError('created_at required')
    created_canon = _canonicalize_ts(created_at, field='created_at')
    created_dt = _parse_dt(created_canon)
    assert created_dt is not None

    if previous_user_at is None:
        return created_canon, None

    # 非 None：空串 / 垃圾 / 非零微秒一律拒绝，禁止伪装成 clock_missing
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
    """bootstrap 后的三个锚点必须存在、可解析、且无非零微秒。

    损坏 / 非零小数秒不得按 elapsed=0 或截断后比较来洗白。
    """
    created_dt = _parse_dt(created_at)
    if created_dt is None or created_dt.microsecond != 0:
        raise StoreError(f'created_at is not a whole-second timestamp: {created_at!r}')

    for field in _STATE_CLOCK_FIELDS:
        anchor_raw = state.get(field)
        if anchor_raw is None or str(anchor_raw).strip() == '':
            raise StoreError(
                f'state clock {field} is missing; refusing transition'
            )
        # 非零微秒在此失败，绝不先截断再跟 created_at 比先后
        try:
            anchor_canon = _canonicalize_ts(
                anchor_raw, field=f'state clock {field}',
            )
        except StoreError as exc:
            raise StoreError(
                f'state clock {field} invalid: {anchor_raw!r}; '
                f'refusing to whitewash ({exc})'
            ) from exc
        anchor_dt = _parse_dt(anchor_canon)
        assert anchor_dt is not None
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
    mid = _require_message_id(message_id)
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
        'message_id': mid,
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
    mid = _require_message_id(message_id)
    fp = _text_fingerprint(text)
    return {
        'message_id': mid,
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
    mid = _require_message_id(message_id)
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


def apply_planned_user_message(
    conn: sqlite3.Connection,
    *,
    envelope: Mapping[str, Any],
    expected_state_version: Optional[int] = None,
) -> ApplyResult:
    """应用已冻结、无原文的 user_rule envelope。

    生产 outbox 仅保存 plan 的 payload/updates；drain 不得重新取得用户原文。
    """
    if not isinstance(envelope, Mapping):
        raise StoreError('planned user_rule envelope must be a mapping')
    payload = envelope.get('payload')
    updates = envelope.get('updates')
    if not isinstance(payload, Mapping) or not isinstance(updates, Mapping):
        raise StoreError('planned user_rule envelope missing payload/updates')
    mid = _require_message_id(payload.get('message_id'))
    # Strictly reject raw-text keys before persistence/application.
    if any(k in payload for k in ('text', 'content', 'raw_text')):
        raise StoreError('planned user_rule payload must not contain text')
    required = (
        'created_at', 'previous_user_at', 'text_hash', 'text_length',
        'rule_score', 'rule_hit_categories',
    )
    if any(k not in payload for k in required):
        raise StoreError('planned user_rule payload incomplete')
    if not isinstance(payload['text_hash'], str) or len(payload['text_hash']) != 64:
        raise StoreError('planned user_rule text_hash invalid')
    if isinstance(payload['text_length'], bool) or not isinstance(payload['text_length'], int):
        raise StoreError('planned user_rule text_length invalid')
    created, previous = _canonicalize_observation_clocks(
        created_at=payload['created_at'], previous_user_at=payload['previous_user_at'],
    )
    if created != payload['created_at'] or previous != payload['previous_user_at']:
        raise StoreError('planned user_rule timestamps not canonical')
    event_key = f'user_rule:{mid}'
    existing = read_event(conn, event_key)
    identity = {
        'message_id': mid, 'created_at': created, 'previous_user_at': previous,
        'text_hash': payload['text_hash'], 'text_length': payload['text_length'],
    }
    if existing is not None:
        if _event_matches_observation(existing, message_id=mid, identity=identity):
            return _duplicate_result(existing)
        return _idempotency_conflict_result(existing, 'planned user_rule identity conflict')
    state = read_state(conn)
    if state is None:
        return ApplyResult('failed', None, None, None, 'state row missing')
    version = int(state['state_version'])
    if expected_state_version is not None and int(expected_state_version) != version:
        return ApplyResult('version_conflict', version, None, None, 'version conflict')
    return apply_state_update(
        conn, event_key=event_key, event_type=_USER_RULE_EVENT_TYPE,
        source_id=str(mid), payload=dict(payload),
        mutator=lambda _s: dict(updates), expected_state_version=version,
    )


# ═══════════════════════════════════════════════════════════
# Phase 1A-2 — observe_scored（异步评分；试管）
# ═══════════════════════════════════════════════════════════
#
# 迁移债务（故意保留，本 PR 不去重 / 不降权 / 不调参）：
#   user_rule 已对 P/I 做即时关键词更新；
#   user_scored 又对 P/I 应用异步评分 delta；
#   Phase 1A 双通道叠加与旧 emotion_engine 行为对齐。
#

_SCORED_PASSION_DELTA_RANGE = (-0.3, 0.3)
_SCORED_INTIMACY_DELTA_RANGE = (-0.2, 0.2)
_MOOD_WORD_MAX_LEN = 30
_BOU_PA_BASE = 0.5
_BOU_NA_BASE = 0.2
_BOU_RATE = 0.08


def _require_finite_in_range(
    value: Any, *, field: str, lo: float, hi: float,
) -> float:
    """越界 / 非有限 / 错误类型 → StoreError；禁止静默 clamp。

    只接受真正的 ``int`` / ``float``（排除 ``bool``）；拒绝 str / bytes /
    None / 容器等。禁止先 ``float(value)`` 把 ``True`` 或 ``"0.8"`` 洗成数字。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StoreError(
            f'{field} must be int or float (not bool/str): {value!r}'
        )
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StoreError(f'{field} must be numeric: {value!r}') from exc
    if not math.isfinite(v):
        raise StoreError(f'{field} must be finite: {value!r}')
    if v < lo or v > hi:
        raise StoreError(
            f'{field} out of range [{lo}, {hi}]: {value!r}'
        )
    return v


def normalize_scored_scores(scores: Mapping[str, Any]) -> dict:
    """校验并规范化已混合完成的评分结果；不调网络、不重新评分。"""
    if not isinstance(scores, Mapping):
        raise StoreError(f'scores must be a mapping, got {type(scores)!r}')

    required = (
        'valence', 'arousal', 'mood_word',
        'passion_delta', 'intimacy_delta',
    )
    missing = [k for k in required if k not in scores]
    if missing:
        raise StoreError(f'scores missing fields: {missing}')

    valence = _require_finite_in_range(
        scores['valence'], field='valence', lo=0.0, hi=1.0)
    arousal = _require_finite_in_range(
        scores['arousal'], field='arousal', lo=0.0, hi=1.0)
    passion_delta = _require_finite_in_range(
        scores['passion_delta'], field='passion_delta',
        lo=_SCORED_PASSION_DELTA_RANGE[0], hi=_SCORED_PASSION_DELTA_RANGE[1])
    intimacy_delta = _require_finite_in_range(
        scores['intimacy_delta'], field='intimacy_delta',
        lo=_SCORED_INTIMACY_DELTA_RANGE[0], hi=_SCORED_INTIMACY_DELTA_RANGE[1])

    mood_word = scores['mood_word']
    if not isinstance(mood_word, str):
        raise StoreError(f'mood_word must be str, got {type(mood_word)!r}')
    if not mood_word or len(mood_word) > _MOOD_WORD_MAX_LEN:
        raise StoreError(
            f'mood_word must be non-empty and <= {_MOOD_WORD_MAX_LEN} chars: '
            f'{mood_word!r}'
        )

    source = scores.get('source')
    if source is not None and not isinstance(source, str):
        raise StoreError(f'source must be str or None, got {type(source)!r}')

    return {
        'valence': valence,
        'arousal': arousal,
        'mood_word': mood_word,
        'passion_delta': passion_delta,
        'intimacy_delta': intimacy_delta,
        'source': source,
    }


def _require_bond_clocks_for_scored(
    state: Mapping[str, Any], *, scored_at: str,
) -> None:
    """applied 路径：scored_at 不得早于 p/i 锚点；损坏锚点 fail closed。"""
    scored_dt = _parse_dt(scored_at)
    if scored_dt is None or scored_dt.microsecond != 0:
        raise StoreError(
            f'scored_at is not a whole-second timestamp: {scored_at!r}'
        )
    for field in _BOND_CLOCK_FIELDS:
        anchor_raw = state.get(field)
        if anchor_raw is None or str(anchor_raw).strip() == '':
            raise StoreError(
                f'state clock {field} is missing; refusing scored transition'
            )
        try:
            anchor_canon = _canonicalize_ts(
                anchor_raw, field=f'state clock {field}',
            )
        except StoreError as exc:
            raise StoreError(
                f'state clock {field} invalid: {anchor_raw!r}; '
                f'refusing scored transition ({exc})'
            ) from exc
        anchor_dt = _parse_dt(anchor_canon)
        assert anchor_dt is not None
        if scored_dt < anchor_dt:
            raise StoreError(
                f'scored_at {scored_at!r} is before state clock '
                f'{field}={anchor_raw!r}; refusing to rewind'
            )


def plan_scored_transition(
    state: Mapping[str, Any],
    *,
    message_id: int,
    scores: Mapping[str, Any],
    scored_at: str,
) -> dict:
    """纯函数：规范化评分 → Affect/Bond updates。

    复现 emotion_engine.score_and_update 的状态数学（不 import 旧引擎）。
    仅用于非 stale 路径；调用前须已判定 message_id > watermark。
    """
    mid = _require_message_id(message_id)
    scored_canon = _canonicalize_ts(scored_at, field='scored_at')
    norm = normalize_scored_scores(scores)
    _require_bond_clocks_for_scored(state, scored_at=scored_canon)

    old_pa = _float_field(state, 'pa', 0.5)
    old_na = _float_field(state, 'na', 0.2)
    final_v = norm['valence']
    final_a = norm['arousal']

    new_pa = _clamp01(0.75 * old_pa + 0.25 * final_v)
    na_signal = final_a * (1.0 - final_v) * 0.5 + 0.05
    new_na = _clamp01(0.75 * old_na + 0.25 * na_signal)
    # BOU 均值回归（与 emotion_engine._bou_revert 一致）
    new_pa = new_pa + _BOU_RATE * (_BOU_PA_BASE - new_pa)
    new_na = new_na + _BOU_RATE * (_BOU_NA_BASE - new_na)

    bond_p = decay_exponential(
        _float_field(state, 'passion', 0.0),
        _hours_elapsed(state.get('p_updated_at'), scored_canon),
        TAU_P_HOURS,
    )
    bond_i = decay_exponential(
        _float_field(state, 'intimacy', 0.3),
        _hours_elapsed(state.get('i_updated_at'), scored_canon),
        TAU_I_HOURS,
    )
    commitment = _float_field(state, 'commitment', 0.7)
    new_p = _clamp01(bond_p + norm['passion_delta'])
    new_i = _clamp01(bond_i + norm['intimacy_delta'])

    updates = {
        'pa': round(new_pa, 4),
        'na': round(new_na, 4),
        'valence': round(final_v, 4),
        'arousal': round(final_a, 4),
        'mood_word': norm['mood_word'],
        'mood_source_message_id': mid,
        'passion': round(new_p, 4),
        'intimacy': round(new_i, 4),
        'commitment': commitment,
        'p_updated_at': scored_canon,
        'i_updated_at': scored_canon,
        'last_scored_message_id': mid,
    }

    payload = {
        'message_id': mid,
        'scored_at': scored_canon,
        'scores': {
            'valence': norm['valence'],
            'arousal': norm['arousal'],
            'mood_word': norm['mood_word'],
            'passion_delta': norm['passion_delta'],
            'intimacy_delta': norm['intimacy_delta'],
            'source': norm['source'],
        },
    }

    diagnostics = {
        'materialized_bond': {
            'passion': round(bond_p, 4),
            'intimacy': round(bond_i, 4),
        },
        'affect_before': {'pa': old_pa, 'na': old_na},
        'affect_after': {
            'pa': updates['pa'],
            'na': updates['na'],
            'valence': updates['valence'],
            'arousal': updates['arousal'],
        },
        # 双通道叠加：本 transition 不再次应用关键词；见模块注释
        'dual_channel_note': (
            'Phase 1A keeps user_rule keyword P/I deltas and '
            'user_scored async deltas stacked without dedupe'
        ),
    }

    return {
        'updates': updates,
        'payload': payload,
        'diagnostics': diagnostics,
    }


def _scored_payload(
    *,
    message_id: int,
    scored_at: str,
    scores: Mapping[str, Any],
) -> dict:
    mid = _require_message_id(message_id)
    norm = normalize_scored_scores(scores)
    scored_canon = _canonicalize_ts(scored_at, field='scored_at')
    return {
        'message_id': mid,
        'scored_at': scored_canon,
        'scores': {
            'valence': norm['valence'],
            'arousal': norm['arousal'],
            'mood_word': norm['mood_word'],
            'passion_delta': norm['passion_delta'],
            'intimacy_delta': norm['intimacy_delta'],
            'source': norm['source'],
        },
    }


def observe_scored(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    scores: Mapping[str, Any],
    scored_at: str,
    expected_state_version: Optional[int] = None,
) -> ApplyResult:
    """异步评分事件 ``user_scored:{message_id}``。

    乱序契约（同一 ``BEGIN IMMEDIATE`` 内读 watermark）：
      - ``message_id > last_scored_message_id``（或 watermark 为空）→ applied
      - 否则 → ``stale_skipped``：落账、消费 key、不升版本、不改状态

    ``expected_state_version`` 与业务 stale 的优先级：
      存储层先做乐观版本校验，再调用 ``decide()``。因此若调用方携带
      **过期** expected version，迟到评分会先得到 ``version_conflict``
      （**不消费** event_key，**不是**评分终态）。调用方须重读当前
      ``state_version`` 后用同一 payload 重试；重试后才会进入
      ``stale_skipped`` 并纳入 ``stale_score_rate``。
      下一阶段接入评分线程时，应对 version_conflict 做有限重试，
      否则迟到率统计会系统性漏样本。

    不调用 DeepSeek / 渐变脑；只接受已规范化的 scores。
    """
    mid = _require_message_id(message_id)
    event_key = f'user_scored:{mid}'

    # 输入校验在事务外失败 → 不消费 key
    payload = _scored_payload(
        message_id=mid, scored_at=scored_at, scores=scores,
    )
    scored_canon = payload['scored_at']

    def decide(state: dict) -> ConditionalDecision:
        last = state.get('last_scored_message_id')
        if last is not None and mid <= int(last):
            return ConditionalDecision(
                status='stale_skipped',
                error=(
                    f'stale_skipped: message_id={mid} <= '
                    f'last_scored_message_id={int(last)}'
                ),
            )
        # stale 优先于 Bond：仅 applied 路径才做物化 / 时钟校验
        plan = plan_scored_transition(
            state,
            message_id=mid,
            scores=payload['scores'],
            scored_at=scored_canon,
        )
        return ConditionalDecision(
            status='applied',
            updates=plan['updates'],
        )

    return apply_conditional_state_update(
        conn,
        event_key=event_key,
        event_type=_USER_SCORED_EVENT_TYPE,
        source_id=str(mid),
        payload=payload,
        decide=decide,
        expected_state_version=expected_state_version,
    )


def get_scored_event_stats(conn: sqlite3.Connection) -> dict:
    """只读诊断：applied / stale 计数与迟到率。

    ``total_decided = applied + stale_skipped``；
    ``stale_score_count`` 为规格别名，等于 ``stale_skipped``。
    duplicate 不进分母（不会新增账本行）。
    空账本时 ``stale_score_rate`` 为 ``None``（非 0.0），避免零样本被
    误读成“零迟到率”。
    """
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS n
        FROM internal_state_events
        WHERE event_type = ?
          AND status IN ('applied', 'stale_skipped')
        GROUP BY status
        """,
        (_USER_SCORED_EVENT_TYPE,),
    ).fetchall()
    counts = {str(r[0]): int(r[1]) for r in rows}
    applied = counts.get('applied', 0)
    stale = counts.get('stale_skipped', 0)
    total = applied + stale
    rate = (stale / total) if total else None
    return {
        'applied': applied,
        'stale_skipped': stale,
        'stale_score_count': stale,
        'total_decided': total,
        'stale_score_rate': rate,
    }


# ═══════════════════════════════════════════════════════════
# Phase 1A-3 — apply_outcome（Wake 结算；试管）
# ═══════════════════════════════════════════════════════════
#
# 一个 wake_run_id 只能进结算室一次。
# Phase 1 权威路径只应用 drive_engine 兼容的固定减法 + fatigue±0.04；
# desire 乘性 / live 双结算 / attachment×0.45 写入 result_json（审计），
# 不进入 payload_hash，不写 state。
#
# action 合同拆分：
#   executor_action — Wake 合同：none / message / diary / explore
#                     固定结算 + legacy_live_double（真实 desire.satisfy 入参）
#   desire_action   — desire.ACTION_SATISFY 键（可选）；仅 mapping 对照
#   user_idle_hours — 权威用户空闲；内部 τ18 派生 longing，禁止外传 scalar
#

_WAKE_OUTCOME_EVENT_TYPE = 'wake_outcome'
_WAKE_RUN_ID_MAX_LEN = 128

_EXECUTOR_ACTIONS = frozenset({'none', 'message', 'diary', 'explore'})

_FIRED_DRIVE_ENUM = frozenset({
    'attachment', 'curiosity', 'reflection', 'social',
    'duty', 'libido', 'stress',
})

# drive_engine.DISCHARGE（不含 fatigue）
_LEGACY_FIXED_DISCHARGE = {
    'attachment': 0.55,
    'curiosity': 0.45,
    'reflection': 0.40,
    'social': 0.40,
    'duty': 0.50,
    'libido': 0.60,
    'stress': 0.50,
}
_LEGACY_FATIGUE_BEHAVIOR_COST = 0.04
_LEGACY_FATIGUE_NONE_RESTORE = 0.04
_LEGACY_DESIRE_FATIGUE_COST = 0.08

# desire.ACTION_SATISFY（照抄；不 import desire）
_LEGACY_DESIRE_ACTION_SATISFY = {
    'co_read': {'reflection': 0.45, 'curiosity': 0.85},
    'github': {'curiosity': 0.50},
    'web_search': {'curiosity': 0.48},
    'web_browse': {'social': 0.48, 'curiosity': 0.82},
    'none': {'duty': 0.80},
    'tease': {'libido': 0.55},
    'vent': {'stress': 0.45},
}
_DESIRE_ACTIONS = frozenset(_LEGACY_DESIRE_ACTION_SATISFY.keys())


def _require_wake_run_id(value: Any) -> str:
    if not isinstance(value, str):
        raise StoreError(
            f'wake_run_id must be a non-empty str: {value!r}'
        )
    rid = value.strip()
    if not rid or len(rid) > _WAKE_RUN_ID_MAX_LEN:
        raise StoreError(
            f'wake_run_id must be non-empty and <= {_WAKE_RUN_ID_MAX_LEN}: '
            f'{value!r}'
        )
    return rid


def _require_executor_action(value: Any) -> str:
    if not isinstance(value, str):
        raise StoreError(
            f'executor_action must be one of {sorted(_EXECUTOR_ACTIONS)}: '
            f'{value!r}'
        )
    action = value.strip()
    if action not in _EXECUTOR_ACTIONS:
        raise StoreError(
            f'executor_action must be one of {sorted(_EXECUTOR_ACTIONS)}: '
            f'{value!r}'
        )
    return action


def _require_desire_action(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _DESIRE_ACTIONS:
        raise StoreError(
            f'desire_action must be None or one of '
            f'{sorted(_DESIRE_ACTIONS)}: {value!r}'
        )
    return value


def _require_fired_drive(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _FIRED_DRIVE_ENUM:
        raise StoreError(
            f'fired_drive must be None or one of {sorted(_FIRED_DRIVE_ENUM)}: '
            f'{value!r}'
        )
    return value


def _require_desire_driven(value: Any) -> bool:
    if type(value) is not bool:
        raise StoreError(
            f'desire_driven must be a real bool: {value!r}'
        )
    return value


def _require_user_idle_hours(value: Any) -> float:
    """稳定源输入：真实用户空闲小时；禁止 bool / 字符串 / NaN / Inf / 负值。"""
    if type(value) is bool or type(value) not in (int, float):
        raise StoreError(
            f'user_idle_hours must be a finite int/float >= 0: {value!r}'
        )
    v = float(value)
    if not math.isfinite(v) or v < 0.0:
        raise StoreError(
            f'user_idle_hours must be a finite int/float >= 0: {value!r}'
        )
    return v


def _require_drives_clock_for_outcome(
    state: Mapping[str, Any], *, outcome_at: str,
) -> None:
    """outcome_at 不得早于 drives_updated_at；损坏锚点 fail closed。"""
    outcome_dt = _parse_dt(outcome_at)
    if outcome_dt is None or outcome_dt.microsecond != 0:
        raise StoreError(
            f'outcome_at is not a whole-second timestamp: {outcome_at!r}'
        )
    anchor_raw = state.get('drives_updated_at')
    if anchor_raw is None or str(anchor_raw).strip() == '':
        raise StoreError(
            'state clock drives_updated_at is missing; refusing outcome'
        )
    try:
        anchor_canon = _canonicalize_ts(
            anchor_raw, field='state clock drives_updated_at',
        )
    except StoreError as exc:
        raise StoreError(
            f'state clock drives_updated_at invalid: {anchor_raw!r}; '
            f'refusing outcome ({exc})'
        ) from exc
    anchor_dt = _parse_dt(anchor_canon)
    assert anchor_dt is not None
    if outcome_dt < anchor_dt:
        raise StoreError(
            f'outcome_at {outcome_at!r} is before drives_updated_at '
            f'{anchor_raw!r}; refusing to rewind'
        )


def _drives_dict_copy(drives: Mapping[str, float]) -> dict:
    return {k: float(drives[k]) for k in DRIVE_KEYS}


def _apply_legacy_fixed_settlement(
    materialized: Mapping[str, float],
    *,
    executor_action: str,
    fired_drive: Optional[str],
) -> dict:
    """Phase 1 权威：drive_engine 兼容固定减法 / none 恢复。

    非 none 必须已由调用方保证 fired_drive 非空；此处不再猜最高 drive。
    """
    out = _drives_dict_copy(materialized)
    if executor_action == 'none':
        # fired_drive 可非空（例如 desire duty→none）；固定路径仍只休息
        out['fatigue'] = round(
            max(0.0, out['fatigue'] - _LEGACY_FATIGUE_NONE_RESTORE), 4,
        )
        return out
    assert fired_drive is not None
    sub = _LEGACY_FIXED_DISCHARGE[fired_drive]
    out[fired_drive] = round(max(0.0, out[fired_drive] - sub), 4)
    out['fatigue'] = round(
        min(1.0, out['fatigue'] + _LEGACY_FATIGUE_BEHAVIOR_COST), 4,
    )
    return out


def _apply_legacy_desire_ratio(
    drives: Mapping[str, float], *, desire_action: Optional[str],
) -> dict:
    """Hypothetical mapping comparison：按 desire_action 查 ACTION_SATISFY。

    不宣称是线上真实执行结果；desire_action=None 时仅 fatigue +0.08。
    """
    out = _drives_dict_copy(drives)
    ratios = (
        _LEGACY_DESIRE_ACTION_SATISFY.get(desire_action, {})
        if desire_action is not None else {}
    )
    for key, ratio in ratios.items():
        out[key] = round(max(0.0, out[key] * ratio), 4)
    out['fatigue'] = round(
        min(1.0, out['fatigue'] + _LEGACY_DESIRE_FATIGUE_COST), 4,
    )
    return out


def _apply_legacy_live_desire_satisfy(
    drives: Mapping[str, float], *, executor_action: str,
) -> dict:
    """Exact current-live：``desire.satisfy(executor_action)``。

    生产 executor 把同一 Wake action 原样传入；ACTION_SATISFY 在 executor
    词表中仅 ``none`` 有乘性行，``message/diary/explore`` 只加 fatigue。
    """
    out = _drives_dict_copy(drives)
    for key, ratio in _LEGACY_DESIRE_ACTION_SATISFY.get(
            executor_action, {}).items():
        out[key] = round(max(0.0, out[key] * ratio), 4)
    out['fatigue'] = round(
        min(1.0, out['fatigue'] + _LEGACY_DESIRE_FATIGUE_COST), 4,
    )
    return out


def plan_outcome_transition(
    state: Mapping[str, Any],
    *,
    wake_run_id: str,
    executor_action: str,
    desire_action: Optional[str],
    fired_drive: Optional[str],
    desire_driven: bool,
    user_idle_hours: float,
    outcome_at: str,
) -> dict:
    """纯函数：Wake 结果 → drives updates + 对照 diagnostics。

    权威 state 只应用一次固定减法路径；ratio / live-double / attachment×0.45
    仅记录。不修改 Affect / Bond / last_scored_message_id。

    非 ``none`` 的 executor_action 必须显式提供 fired_drive；缺失 fail closed。
    Longing 仅由 ``user_idle_hours`` 经冻结 τ18 公式派生，禁止外传 scalar。
    """
    rid = _require_wake_run_id(wake_run_id)
    exec_act = _require_executor_action(executor_action)
    des_act = _require_desire_action(desire_action)
    fired = _require_fired_drive(fired_drive)
    driven = _require_desire_driven(desire_driven)
    idle = _require_user_idle_hours(user_idle_hours)
    longing = longing_desire_legacy_curve(idle)
    if longing is None:
        raise StoreError(
            f'longing_desire_legacy_curve returned None for idle={idle!r}'
        )
    outcome_canon = _canonicalize_ts(outcome_at, field='outcome_at')
    _require_drives_clock_for_outcome(state, outcome_at=outcome_canon)

    if exec_act != 'none' and fired is None:
        raise StoreError(
            'fired_drive is required when executor_action is not '
            f"'none' (got executor_action={exec_act!r})"
        )

    # Bond 只读衰减供 libido cap；attachment cap 使用派生 longing
    bond_m = _materialize_bond(state, outcome_canon)
    drives_m = _materialize_drives(
        state,
        created_at=outcome_canon,
        longing_for_boost=float(longing),
        passion_for_boost=bond_m['passion'],
    )

    legacy_fixed = _apply_legacy_fixed_settlement(
        drives_m, executor_action=exec_act, fired_drive=fired,
    )
    # mapping comparison（可用独立 desire_action）
    legacy_ratio = _apply_legacy_desire_ratio(
        drives_m, desire_action=des_act,
    )
    # exact current-live：fixed 后再 desire.satisfy(executor_action)
    if driven:
        legacy_double = _apply_legacy_live_desire_satisfy(
            legacy_fixed, executor_action=exec_act,
        )
    else:
        legacy_double = None
    candidate_att = round(
        max(0.0, drives_m['attachment'] * CANDIDATE_ATTACHMENT_SATISFY_RATIO),
        4,
    )

    updates = {
        **{k: legacy_fixed[k] for k in DRIVE_KEYS},
        'drives_updated_at': outcome_canon,
    }

    payload = {
        'wake_run_id': rid,
        'executor_action': exec_act,
        'desire_action': des_act,
        'fired_drive': fired,
        'desire_driven': driven,
        'user_idle_hours': idle,
        'outcome_at': outcome_canon,
    }

    diagnostics = {
        'materialized_before': _drives_dict_copy(drives_m),
        'legacy_fixed_after': _drives_dict_copy(legacy_fixed),
        'legacy_desire_ratio_after': _drives_dict_copy(legacy_ratio),
        'legacy_live_double_after': (
            _drives_dict_copy(legacy_double) if legacy_double is not None
            else None
        ),
        'candidate_attachment_ratio_after': candidate_att,
        'user_idle_hours': idle,
        'longing_for_boost': float(longing),
        'executor_action': exec_act,
        'desire_action': des_act,
        'authority': 'legacy_fixed_discharge_phase1',
        'diagnostics_roles': {
            'legacy_desire_ratio_after': (
                'hypothetical ACTION_SATISFY mapping comparison '
                '(uses desire_action; not claimed as live execution)'
            ),
            'legacy_live_double_after': (
                'exact current-live simulation: fixed discharge then '
                'desire.satisfy(executor_action)'
            ),
        },
        'note': (
            'ratio / live-double / attachment×0.45 are diagnostics only; '
            'state applies fixed discharge once; result_json holds this audit'
        ),
    }

    return {
        'updates': updates,
        'payload': payload,
        'diagnostics': diagnostics,
    }


def apply_outcome(
    conn: sqlite3.Connection,
    *,
    wake_run_id: str,
    executor_action: str,
    desire_action: Optional[str],
    fired_drive: Optional[str],
    desire_driven: bool,
    user_idle_hours: float,
    outcome_at: str,
    expected_state_version: Optional[int] = None,
) -> ApplyResult:
    """Wake 结算事件 ``wake_outcome:{wake_run_id}``：一票只进结算室一次。

    ``payload_json/hash`` 仅含稳定观察输入；``result_json`` 保存事务内
    基于最新 state 算出的 diagnostics（duplicate 回放原值，不重算覆盖）。
    version_conflict 不消费 key，重读版本后可同 key 重试。
    """
    rid = _require_wake_run_id(wake_run_id)
    exec_act = _require_executor_action(executor_action)
    des_act = _require_desire_action(desire_action)
    fired = _require_fired_drive(fired_drive)
    driven = _require_desire_driven(desire_driven)
    idle = _require_user_idle_hours(user_idle_hours)
    outcome_canon = _canonicalize_ts(outcome_at, field='outcome_at')

    if exec_act != 'none' and fired is None:
        raise StoreError(
            'fired_drive is required when executor_action is not '
            f"'none' (got executor_action={exec_act!r})"
        )

    event_key = f'wake_outcome:{rid}'
    payload = {
        'wake_run_id': rid,
        'executor_action': exec_act,
        'desire_action': des_act,
        'fired_drive': fired,
        'desire_driven': driven,
        'user_idle_hours': idle,
        'outcome_at': outcome_canon,
    }

    def decide(state: dict) -> ConditionalDecision:
        plan = plan_outcome_transition(
            state,
            wake_run_id=rid,
            executor_action=exec_act,
            desire_action=des_act,
            fired_drive=fired,
            desire_driven=driven,
            user_idle_hours=idle,
            outcome_at=outcome_canon,
        )
        return ConditionalDecision(
            status='applied',
            updates=plan['updates'],
            result=plan['diagnostics'],
        )

    return apply_conditional_state_update(
        conn,
        event_key=event_key,
        event_type=_WAKE_OUTCOME_EVENT_TYPE,
        source_id=rid,
        payload=payload,
        decide=decide,
        expected_state_version=expected_state_version,
    )


__all__ = [
    'CANDIDATE_ATTACHMENT_SATISFY_RATIO',
    'DESIRE_CAP',
    'DESIRE_LEXICON',
    'DESIRE_WEIGHTS',
    'LEGACY_ATTACHMENT_SUBTRACT',
    'LEGACY_ATTACHMENT_THRESHOLD',
    'LEGACY_FATIGUE_RESTORE',
    'apply_outcome',
    'get_scored_event_stats',
    'normalize_scored_scores',
    'observe_scored',
    'observe_user_message',
    'plan_outcome_transition',
    'plan_scored_transition',
    'plan_user_message_transition',
    'rule_score_desire',
]
