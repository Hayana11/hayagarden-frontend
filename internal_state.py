"""Internal State Shadow — Phase 0（纯只读影子核心）

目标：建立统一状态模型并验证数学与字段映射。**不接管任何生产行为。**

本模块保证：
  - 所有原始表读取使用 SQLite ``mode=ro`` URI，不写任何业务数据库；
  - 不通过 import 旧模块获取 DB 路径（直接读 ``MEMORIES_DB``）；
  - 默认不运行旧 getter；若启用，仅在隔离子进程 + 临时 DB 副本上运行；
  - 不修改 emotion_engine / desire / drive_engine 的逻辑；
  - 不进入任何 prompt；不被 gateway / system_builder / wake 引用；
  - 不创建 internal_state_v3 权威表。

禁止调用的旧系统写函数（本模块永不调用，测试有守卫）：
  touch_interaction, touch_hayana, rest, discharge, discharge_by_action,
  satisfy, apply_desire_delta(_async), score_and_update, score_async,
  calibrate_va, _flush

架构分两层：
  1. 纯计算核心 compute_snapshot()：输入 = 三张旧表的原始行 + 统一时钟 +
     observed_at，输出完全确定（同输入同输出）。驱动分两列：
       · legacy_drive_engine_replay —— attachment boost 用 longing_emotion_legacy
         （τ8），精确复现旧 drive_engine ← emotion_engine.get_longing
       · candidate_unified_drives —— attachment boost 用 longing_desire_legacy
         （τ18），明确标为候选设计，不得冒充旧逻辑
  2. 旧 getter 对照层（可选）：仅在隔离子进程对 DB 副本调用，结果只进
     diagnostics.legacy_readings，不参与候选计算。

时钟规则：
  - 唯一权威时钟 = chat.interaction_state.read_interaction_clock()
  - 三条 longing 全部使用 user_idle_hours（她真正出现距今多久）
  - effective_idle_hours 仅用于 Wake 限流诊断，绝不喂给 longing
  - emotion_state.last_interaction / desire_state.last_hayana_msg_time
    只作为诊断输出，不参与任何候选计算
  - 时钟不可靠时 fail closed：longing 全为 None，不制造 999h 思念
"""

from __future__ import annotations

import datetime
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict
from typing import Optional, Protocol, Callable

from chat.interaction_state import read_interaction_clock, InteractionClock

# ═══════════════════════════════════════════════════════════
# 常量：全部从旧模块的当前实现照抄，Phase 0 不调参
# ═══════════════════════════════════════════════════════════

DEFAULT_MEMORIES_DB = '/opt/frontend/memories.db'

# Bond 衰减（来源：emotion_engine.TAU_P / TAU_I）
TAU_P_HOURS = 6.0     # passion 半衰特征时间
TAU_I_HOURS = 96.0    # intimacy 半衰特征时间

# Longing — emotion_engine.get_longing 现行公式
LONGING_EMOTION_TAU = 8.0
LONGING_EMOTION_SCALE = 0.85
LONGING_EMOTION_CLAMP = 0.92

# Longing — desire._compute_longing 现行公式
LONGING_DESIRE_TAU = 18.0
LONGING_DESIRE_SCALE = 0.85
LONGING_DESIRE_CLAMP = 0.90

# 驱动积累（来源：drive_engine）
DRIVE_KEYS = ('attachment', 'curiosity', 'reflection', 'social',
              'duty', 'libido', 'stress', 'fatigue')
DRIVE_CAP = {
    'attachment': 0.75, 'curiosity': 0.70, 'reflection': 0.60,
    'social': 0.55, 'duty': 0.75, 'libido': 0.65, 'stress': 0.55,
}
DRIVE_GROWTH_K = {
    'attachment': 0.08, 'curiosity': 0.06, 'reflection': 0.05,
    'social': 0.04, 'duty': 0.08, 'libido': 0.06, 'stress': 0.04,
}
# 显式联动（原 drive_engine.CAP_BOOST，隐藏在 _get_emotion_factors 里）：
#   legacy replay: attachment cap += longing_emotion_legacy × 0.15
#   candidate:     attachment cap += longing_desire_legacy × 0.15（候选，非旧逻辑）
#   libido     cap += passion × 0.20   ← Bond.passion（τ6 衰减后）
#   stress     cap += na × 0.15        ← emotion_state.na 原始列
# cap 上限 0.92（原实现同）
CAP_BOOST_LIMIT = 0.92
FATIGUE_EQ = 0.35
FATIGUE_K = 0.05
FATIGUE_NA_COEF = 0.06     # 原实现：fatigue += na × 0.06

TRIGGER_THRESHOLD = 0.35
FATIGUE_GATE = 0.72

# Longing 候选公式（仅供比较，不作为结论）。
# 注意：当前仅使用 attachment 调制 τ，**尚未加入 intimacy 调制**，
# 报告与日志中不得声称已实现 intimacy + attachment 联动。
LONGING_CANDIDATE_BASE_TAU = 18.0
LONGING_CANDIDATE_CLAMP = 0.90

# Chat View 意图枚举（固定，状态系统不得自由写提示词）
INTENTS = ('none', 'reassure_attachment', 'express_longing', 'seek_closeness',
           'share_reflection', 'release_stress', 'pursue_curiosity')

_DRIVE_TO_INTENT = {
    'attachment': 'reassure_attachment',
    'libido': 'seek_closeness',
    'reflection': 'share_reflection',
    'stress': 'release_stress',
    'curiosity': 'pursue_curiosity',
    'social': 'pursue_curiosity',
    'duty': 'none',   # 现有枚举无对应项；dominant_drive 仍会记录 duty
}

# 每个 intent 只允许机器 token 形式的内容目标，禁止中文自由文案
_INTENT_CONTENT_TARGETS = {
    'none': (),
    'reassure_attachment': ('confirm_being_thought_of', 'initiate_checkin'),
    'express_longing': ('acknowledge_absence', 'reach_out'),
    'seek_closeness': ('seek_affection',),
    'share_reflection': ('offer_reflection',),
    'release_stress': ('vent_or_confide',),
    'pursue_curiosity': ('bring_external_topic',),
}

# 形式/文风指导禁词——本模块任何输出都不得包含（测试扫描用）
FORBIDDEN_STYLE_TOKENS = ('低唤醒', '高唤醒', '话少', '简短', '克制',
                          '语气', '文风', '安静等待', '偏暖', '偏低')


# ═══════════════════════════════════════════════════════════
# 数据结构（全部 frozen，不可变）
# ═══════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Affect:
    pa: Optional[float]
    na: Optional[float]
    valence: Optional[float]
    arousal: Optional[float]
    mood_word: Optional[str]   # 仅兼容展示；不进入 Chat prompt


@dataclass(frozen=True)
class Bond:
    intimacy: Optional[float]
    passion: Optional[float]
    commitment: Optional[float]


@dataclass(frozen=True)
class Drives:
    attachment: Optional[float]
    curiosity: Optional[float]
    reflection: Optional[float]
    social: Optional[float]
    duty: Optional[float]
    libido: Optional[float]
    stress: Optional[float]
    fatigue: Optional[float]

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in DRIVE_KEYS}


@dataclass(frozen=True)
class Derived:
    user_idle_hours: Optional[float]
    effective_idle_hours: Optional[float]   # 仅 Wake 限流诊断用
    longing_emotion_legacy: Optional[float]
    longing_desire_legacy: Optional[float]
    longing_candidate: Optional[float]
    dominant_drive: Optional[str]
    candidate_intent: str


@dataclass(frozen=True)
class Diagnostics:
    source_timestamps: dict
    source_health: dict
    legacy_readings: dict     # 旧 getter 只读输出（墙钟），仅对照
    linkage_sources: dict     # 显式联动来源说明
    drive_comparison: dict    # legacy / candidate / 逐维 diff
    warnings: tuple


@dataclass(frozen=True)
class InternalStateSnapshot:
    observed_at: str
    affect: Affect
    bond: Bond
    legacy_drive_engine_replay: Drives   # 精确复现旧 drive_engine
    candidate_unified_drives: Drives     # 候选设计，不得冒充旧逻辑
    derived: Derived
    diagnostics: Diagnostics


@dataclass(frozen=True)
class ChatStateView:
    intent: str
    intensity: float
    content_targets: tuple
    source_dimensions: tuple


# ═══════════════════════════════════════════════════════════
# 纯数学函数（可单测）
# ═══════════════════════════════════════════════════════════

def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _float_or_default(row: dict, key: str, default: float) -> float:
    """仅当值为 None 时用默认值；0.0 必须保留。"""
    raw = row.get(key)
    if raw is None:
        return float(default)
    return float(raw)


def decay_exponential(value: float, hours_elapsed: float, tau_hours: float) -> float:
    """value × exp(-t/τ)（原 emotion_engine._decay 的纯化版本）"""
    return max(0.0, value * math.exp(-max(0.0, hours_elapsed) / tau_hours))


def longing_emotion_legacy_curve(idle_hours: Optional[float]) -> Optional[float]:
    """旧 emotion_engine 公式，τ=8，clamp 0.92（时钟改用 user_idle_hours）"""
    if idle_hours is None:
        return None
    t = max(0.0, idle_hours)
    L = LONGING_EMOTION_SCALE * (1 - (1 + t / LONGING_EMOTION_TAU) ** (-0.8))
    return round(min(L, LONGING_EMOTION_CLAMP), 3)


def longing_desire_legacy_curve(idle_hours: Optional[float]) -> Optional[float]:
    """旧 desire 公式，τ=18，clamp 0.90"""
    if idle_hours is None:
        return None
    t = max(0.0, idle_hours)
    L = LONGING_DESIRE_SCALE * (1 - (1 + t / LONGING_DESIRE_TAU) ** (-0.8))
    return round(min(L, LONGING_DESIRE_CLAMP), 3)


def longing_candidate_curve(idle_hours: Optional[float],
                            attachment: Optional[float]) -> Optional[float]:
    """候选公式：τ = 18 × (1.2 − 0.4 × attachment)。

    仅供对照观察，不作为结论。仅使用 attachment 调制；
    intimacy 调制**尚未实现**。
    """
    if idle_hours is None:
        return None
    att = _clamp01(attachment if attachment is not None else 0.0)
    tau = LONGING_CANDIDATE_BASE_TAU * (1.2 - 0.4 * att)
    t = max(0.0, idle_hours)
    L = 0.85 * (1 - (1 + t / tau) ** (-0.8))
    return round(min(L, LONGING_CANDIDATE_CLAMP), 3)


def bond_from_emotion_row(row: dict, observed_at: datetime.datetime) -> Bond:
    """从 emotion_state 原始行按各自时间戳重算衰减后的 I/P/C。

    P 用 p_updated_at + τ6，I 用 i_updated_at + τ96，C 不衰减
    ——与 emotion_engine.get_desire 数学一致，但时间可注入。
    零值保留：sternberg_i/p/c 仅在 None 时用默认值。
    """
    def _hours_since(ts):
        dt = _parse_dt(ts)
        if dt is None:
            return 0.0
        return max(0.0, (observed_at - dt).total_seconds() / 3600.0)

    p = decay_exponential(_float_or_default(row, 'sternberg_p', 0.0),
                          _hours_since(row.get('p_updated_at')), TAU_P_HOURS)
    i = decay_exponential(_float_or_default(row, 'sternberg_i', 0.3),
                          _hours_since(row.get('i_updated_at')), TAU_I_HOURS)
    c = _float_or_default(row, 'sternberg_c', 0.7)
    return Bond(intimacy=round(i, 4), passion=round(p, 4), commitment=round(c, 4))


def drives_from_raw(drive_row: dict,
                    observed_at: datetime.datetime,
                    longing_for_boost: Optional[float],
                    passion_for_boost: Optional[float],
                    na_for_boost: Optional[float]) -> Drives:
    """从 drive_state 原始行显式重算积累 + 联动。

    原 drive_engine.get_drive 的纯化版本；隐藏的 import emotion_engine
    被展开为三个显式入参。调用方决定 longing_for_boost 用哪条 longing：
      · legacy_drive_engine_replay → longing_emotion_legacy（τ8）
      · candidate_unified_drives   → longing_desire_legacy（τ18，候选）
    """
    last = _parse_dt(drive_row.get('last_updated'))
    t = max(0.0, (observed_at - last).total_seconds() / 3600.0) if last else 0.0
    lf = 0.0 if longing_for_boost is None else float(longing_for_boost)
    pf = 0.0 if passion_for_boost is None else float(passion_for_boost)
    nf = 0.2 if na_for_boost is None else float(na_for_boost)

    out = {}
    for key in DRIVE_KEYS:
        base = _float_or_default(drive_row, key, 0.1)
        if key == 'fatigue':
            val = FATIGUE_EQ + (base - FATIGUE_EQ) * math.exp(-FATIGUE_K * t)
            val += nf * FATIGUE_NA_COEF
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
    return Drives(**out)


# 兼容旧测试名
candidate_drives_from_raw = drives_from_raw


def pick_candidate_intent(drives: Drives,
                          longing_desire_legacy: Optional[float]) -> tuple:
    """(dominant_drive, intent)。fatigue≥0.72 → ('fatigue','none')；
    阈值 0.35 以下 → (dominant, 'none')。
    attachment 主导且 longing≥0.35 时细分为 express_longing。
    """
    d = drives.as_dict()
    fatigue = d.get('fatigue') or 0.0
    if fatigue >= FATIGUE_GATE:
        return 'fatigue', 'none'
    candidates = {k: v for k, v in d.items()
                  if k != 'fatigue' and v is not None}
    if not candidates:
        return None, 'none'
    dominant = max(candidates, key=lambda k: candidates[k])
    if candidates[dominant] < TRIGGER_THRESHOLD:
        return dominant, 'none'
    intent = _DRIVE_TO_INTENT.get(dominant, 'none')
    if dominant == 'attachment' and (longing_desire_legacy or 0.0) >= 0.35:
        intent = 'express_longing'
    return dominant, intent


def _drive_diff(candidate: Drives, legacy: Drives) -> dict:
    """逐维 candidate − legacy；任一侧为 None 则该维为 None。"""
    out = {}
    for key in DRIVE_KEYS:
        c = getattr(candidate, key)
        l = getattr(legacy, key)
        if c is None or l is None:
            out[key] = None
        else:
            out[key] = round(float(c) - float(l), 4)
    return out


# ═══════════════════════════════════════════════════════════
# 只读采集层
# ═══════════════════════════════════════════════════════════

def _parse_dt(value) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.strptime(str(value)[:19], '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


def _now_beijing() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def memories_db_path(override: Optional[str] = None) -> str:
    """统一 DB 路径：显式 override > MEMORIES_DB 环境变量 > 默认。

    不 import emotion_engine / drive_engine / desire（它们的 ensure_table
    会在 import 时写库）。
    """
    if override:
        return override
    return os.environ.get('MEMORIES_DB', DEFAULT_MEMORIES_DB)


def _readonly_connect(db_path: str) -> sqlite3.Connection:
    """以 mode=ro URI 打开；失败抛出（调用方决定）。"""
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _read_single_row(db_path: Optional[str], table: str) -> Optional[dict]:
    """SELECT-only 只读 URI 读取单例行。任何异常返回 None，绝不建表。"""
    if not db_path:
        return None
    try:
        conn = _readonly_connect(db_path)
        try:
            row = conn.execute(f"SELECT * FROM {table} WHERE id=1").fetchone()
        finally:
            conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def _copy_db_via_backup(src_path: str, dst_path: str) -> None:
    """从只读源库 backup 到目标文件（WAL 安全，不写源库）。"""
    src = _readonly_connect(src_path)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def gather_legacy_readings(user_idle_hours: Optional[float]) -> dict:
    """调用旧系统的只读 getter（**会 import 旧模块，触发 ensure_table**）。

    仅允许在隔离子进程 + 临时 DB 副本上调用（见
    gather_legacy_readings_isolated）。切勿在主进程对生产库调用。
    """
    readings: dict = {}

    def _safe(name, fn):
        try:
            readings[name] = fn()
        except Exception as exc:
            readings[name] = None
            readings.setdefault('_errors', []).append(f'{name}: {exc}')

    try:
        import emotion_engine as _ee
        import drive_engine as _de
        import desire as _des
    except Exception as exc:
        readings['_errors'] = [f'import: {exc}']
        return readings

    _safe('emotion_engine.get_state', _ee.get_state)
    _safe('emotion_engine.get_desire', _ee.get_desire)
    _safe('emotion_engine.get_longing', _ee.get_longing)
    _safe('drive_engine.get_drive', _de.get_drive)
    _safe('desire.get_drive', _des.get_drive)
    if user_idle_hours is not None:
        _safe('desire.get_longing',
              lambda: _des.get_longing(t_hours_override=user_idle_hours))
    else:
        readings['desire.get_longing'] = None  # fail closed：无权威时钟不算思念
    return readings


def gather_legacy_readings_isolated(
        db_path: str,
        user_idle_hours: Optional[float],
        timeout_sec: float = 30.0) -> dict:
    """安全对照：backup 到临时库，在子进程中 import 旧模块。

    生产库始终以 mode=ro 打开做 backup，旧模块的 ensure_table 只碰副本。
    """
    root = os.path.dirname(os.path.abspath(__file__))
    try:
        with tempfile.TemporaryDirectory(prefix='ist_legacy_') as td:
            copy_path = os.path.join(td, 'memories.db')
            _copy_db_via_backup(db_path, copy_path)
            idle_repr = 'None' if user_idle_hours is None else repr(float(user_idle_hours))
            script = (
                'import json, os, sys\n'
                f'sys.path.insert(0, {root!r})\n'
                f'os.environ["MEMORIES_DB"] = {copy_path!r}\n'
                'import internal_state as ist\n'
                f'print(json.dumps(ist.gather_legacy_readings({idle_repr}), '
                'ensure_ascii=False, default=str))\n'
            )
            env = os.environ.copy()
            env['MEMORIES_DB'] = copy_path
            proc = subprocess.run(
                [sys.executable, '-c', script],
                capture_output=True, text=True, timeout=timeout_sec, env=env,
                cwd=root,
            )
            if proc.returncode != 0:
                return {
                    '_errors': [
                        f'isolated subprocess exit {proc.returncode}: '
                        f'{(proc.stderr or proc.stdout or "")[-500:]}'
                    ]
                }
            line = (proc.stdout or '').strip().splitlines()[-1]
            return json.loads(line)
    except Exception as exc:
        return {'_errors': [f'isolated: {exc}']}


# ═══════════════════════════════════════════════════════════
# 纯计算核心
# ═══════════════════════════════════════════════════════════

def compute_snapshot(emotion_row: Optional[dict],
                     drive_row: Optional[dict],
                     desire_row: Optional[dict],
                     clock: InteractionClock,
                     observed_at: datetime.datetime,
                     legacy_readings: Optional[dict] = None) -> InternalStateSnapshot:
    """同输入同输出。所有候选值只依赖入参，不读任何全局状态。"""
    warnings = []

    # ── Affect：emotion_state 原始列（存储值即当前值，无需衰减）──
    if emotion_row:
        affect = Affect(
            pa=emotion_row.get('pa'), na=emotion_row.get('na'),
            valence=emotion_row.get('valence'), arousal=emotion_row.get('arousal'),
            mood_word=emotion_row.get('mood_word'),
        )
    else:
        affect = Affect(None, None, None, None, None)
        warnings.append('emotion_state row missing')

    # ── Bond：按各自时间戳在 observed_at 下重算衰减 ──
    if emotion_row:
        bond = bond_from_emotion_row(emotion_row, observed_at)
    else:
        bond = Bond(None, None, None)

    # ── 时钟与三条 longing（全部 user_idle_hours；fail closed）──
    user_idle = clock.user_idle_hours if clock.reliable else None
    effective_idle = clock.effective_idle_hours if clock.reliable else None
    if not clock.reliable:
        warnings.append(f'clock unreliable: {clock.reason} — longing 全部置空，不制造虚假思念')

    l_emotion = longing_emotion_legacy_curve(user_idle)
    l_desire = longing_desire_legacy_curve(user_idle)

    # ── 两组 drives：旧复现 vs 统一候选（不得混列）──
    linkage = {
        'legacy_drive_engine_replay.attachment_cap_boost':
            'longing_emotion_legacy × 0.15（精确复现 drive_engine ← emotion_engine.get_longing，τ8）',
        'candidate_unified_drives.attachment_cap_boost':
            'longing_desire_legacy × 0.15（候选设计，τ18；不得冒充旧逻辑）',
        'libido_cap_boost': 'Bond.passion × 0.20（emotion_state P，τ6 衰减后）',
        'stress_cap_boost': 'emotion_state.na × 0.15（原始列）',
        'fatigue_na_adjust': 'emotion_state.na × 0.06（原始列）',
        'longing_candidate_tau': '仅 attachment 调制；intimacy 调制尚未实现',
    }
    if drive_row:
        legacy_drives = drives_from_raw(
            drive_row, observed_at,
            longing_for_boost=l_emotion,
            passion_for_boost=bond.passion,
            na_for_boost=affect.na,
        )
        candidate_drives = drives_from_raw(
            drive_row, observed_at,
            longing_for_boost=l_desire,
            passion_for_boost=bond.passion,
            na_for_boost=affect.na,
        )
    else:
        legacy_drives = Drives(*([None] * 8))
        candidate_drives = Drives(*([None] * 8))
        warnings.append('drive_state row missing')

    l_candidate = longing_candidate_curve(user_idle, candidate_drives.attachment)

    dominant, intent = pick_candidate_intent(candidate_drives, l_desire)

    derived = Derived(
        user_idle_hours=round(user_idle, 4) if user_idle is not None else None,
        effective_idle_hours=round(effective_idle, 4) if effective_idle is not None else None,
        longing_emotion_legacy=l_emotion,
        longing_desire_legacy=l_desire,
        longing_candidate=l_candidate,
        dominant_drive=dominant,
        candidate_intent=intent,
    )

    drive_comparison = {
        'legacy_drive_engine_replay': legacy_drives.as_dict(),
        'candidate_unified_drives': candidate_drives.as_dict(),
        'diff_candidate_minus_legacy': _drive_diff(candidate_drives, legacy_drives),
        'attachment_boost_sources': {
            'legacy': 'longing_emotion_legacy',
            'candidate': 'longing_desire_legacy',
            'longing_emotion_legacy': l_emotion,
            'longing_desire_legacy': l_desire,
        },
    }

    diagnostics = Diagnostics(
        source_timestamps={
            'observed_at': observed_at.strftime('%Y-%m-%d %H:%M:%S'),
            'clock.last_user_at': clock.last_user_at.strftime('%Y-%m-%d %H:%M:%S') if clock.last_user_at else None,
            'clock.last_wake_message_at': clock.last_wake_message_at.strftime('%Y-%m-%d %H:%M:%S') if clock.last_wake_message_at else None,
            # 旧时钟字段仅诊断展示，不参与计算：
            'legacy.emotion_state.last_interaction': (emotion_row or {}).get('last_interaction'),
            'legacy.desire_state.last_hayana_msg_time': (desire_row or {}).get('last_hayana_msg_time'),
            'legacy.emotion_state.p_updated_at': (emotion_row or {}).get('p_updated_at'),
            'legacy.emotion_state.i_updated_at': (emotion_row or {}).get('i_updated_at'),
            'legacy.drive_state.last_updated': (drive_row or {}).get('last_updated'),
        },
        source_health={
            'clock_reliable': clock.reliable,
            'clock_reason': clock.reason,
            'emotion_state': emotion_row is not None,
            'drive_state': drive_row is not None,
            'desire_state': desire_row is not None,
        },
        legacy_readings=legacy_readings or {},
        linkage_sources=linkage,
        drive_comparison=drive_comparison,
        warnings=tuple(warnings),
    )

    return InternalStateSnapshot(
        observed_at=observed_at.strftime('%Y-%m-%d %H:%M:%S'),
        affect=affect, bond=bond,
        legacy_drive_engine_replay=legacy_drives,
        candidate_unified_drives=candidate_drives,
        derived=derived, diagnostics=diagnostics,
    )


def capture_shadow_snapshot(get_db_fn: Callable,
                            now: Optional[datetime.datetime] = None,
                            include_legacy: bool = False,
                            db_path: Optional[str] = None) -> InternalStateSnapshot:
    """采集一次完整影子快照。

    默认 include_legacy=False（不 import 旧模块）。
    原始状态表一律 mode=ro 读取；路径来自 MEMORIES_DB / db_path，
    不通过 import 旧模块获取。
    """
    observed_at = now or _now_beijing()
    clock = read_interaction_clock(get_db_fn, now=observed_at)

    path = memories_db_path(db_path)
    emotion_row = _read_single_row(path, 'emotion_state')
    drive_row = _read_single_row(path, 'drive_state')
    desire_row = _read_single_row(path, 'desire_state')

    legacy = None
    if include_legacy:
        legacy = gather_legacy_readings_isolated(
            path, clock.user_idle_hours if clock.reliable else None)

    return compute_snapshot(emotion_row, drive_row, desire_row,
                            clock, observed_at, legacy_readings=legacy)


# ═══════════════════════════════════════════════════════════
# 候选 Chat View：只返回结构化枚举，不生成自由文本
# ═══════════════════════════════════════════════════════════

def build_chat_view(snapshot: InternalStateSnapshot) -> ChatStateView:
    intent = snapshot.derived.candidate_intent
    if intent not in INTENTS:
        intent = 'none'
    d = snapshot.candidate_unified_drives.as_dict()
    dominant = snapshot.derived.dominant_drive
    if intent == 'none' or dominant is None or dominant == 'fatigue':
        intensity = 0.0
        sources: tuple = (dominant,) if dominant else ()
    else:
        intensity = round(float(d.get(dominant) or 0.0), 4)
        sources = (dominant,)
        if intent == 'express_longing':
            sources = (dominant, 'longing_desire_legacy')
    return ChatStateView(
        intent=intent,
        intensity=intensity,
        content_targets=_INTENT_CONTENT_TARGETS.get(intent, ()),
        source_dimensions=tuple(s for s in sources if s),
    )


# ═══════════════════════════════════════════════════════════
# 序列化
# ═══════════════════════════════════════════════════════════

def snapshot_to_dict(snapshot: InternalStateSnapshot) -> dict:
    return asdict(snapshot)


def snapshot_to_json(snapshot: InternalStateSnapshot, compact: bool = False) -> str:
    data = snapshot_to_dict(snapshot)
    if compact:
        return json.dumps(data, ensure_ascii=False, separators=(',', ':'),
                          default=str)
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


# ═══════════════════════════════════════════════════════════
# 未来写接口（Phase 0 只定义签名，不实现落库）
# 每个更新必须携带唯一事件键，未来实现必须幂等：
#   同一 message_id 的规则评分只应用一次；
#   同一 message_id 的异步评分只应用一次；
#   同一 wake event_id 的 discharge 只应用一次。
# ═══════════════════════════════════════════════════════════

class StateWriteInterface(Protocol):
    def observe_user_message(self, message_id: int, text: str,
                             created_at: datetime.datetime) -> None: ...

    def observe_scored(self, message_id: int, scores: dict,
                       scored_at: datetime.datetime) -> None: ...

    def apply_outcome(self, event_id: int, intent: str,
                      result: dict) -> None: ...


__all__ = [
    'Affect', 'Bond', 'Drives', 'Derived', 'Diagnostics',
    'InternalStateSnapshot', 'ChatStateView', 'StateWriteInterface',
    'INTENTS', 'FORBIDDEN_STYLE_TOKENS', 'DRIVE_KEYS',
    'DEFAULT_MEMORIES_DB',
    'longing_emotion_legacy_curve', 'longing_desire_legacy_curve',
    'longing_candidate_curve', 'bond_from_emotion_row',
    'drives_from_raw', 'candidate_drives_from_raw', 'pick_candidate_intent',
    'compute_snapshot', 'capture_shadow_snapshot',
    'gather_legacy_readings', 'gather_legacy_readings_isolated',
    'memories_db_path', 'build_chat_view',
    'snapshot_to_dict', 'snapshot_to_json',
]
