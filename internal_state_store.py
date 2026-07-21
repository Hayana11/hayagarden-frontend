"""Internal State v3 — Phase 1A-0/1A-2 存储底座

只提供：
  - ``internal_state_v3`` / ``internal_state_events`` schema
  - ``BEGIN IMMEDIATE`` 事务封装
  - ``event_key`` 幂等、``state_version`` 乐观校验
  - ``apply_state_update`` / ``apply_conditional_state_update``
  - 显式传入 snapshot 的 bootstrap（不接启动路径）

严格不做：
  - 不修改 journal_mode（不开启/关闭 WAL）
  - 不接 gateway / chat / Wake / prompt
  - 不实现 observe_* / apply_outcome 业务语义（由 events 模块调用本层）
  - 不 import emotion_engine / drive_engine / desire
  - 不写生产库（调用方传入临时 conn / 路径）
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

# 与生产库当前实测 busy_timeout 对齐；本模块绝不改 journal_mode
DEFAULT_BUSY_TIMEOUT_MS = 5000

# DB 落库允许的 status；API 另可返回 version_conflict / idempotency_conflict
EVENT_STATUSES = (
    'applied',
    'duplicate',
    'stale_skipped',
    'shadow_only',
    'failed',
)

_SYSTEM_MANAGED_FIELDS = frozenset({'id', 'state_version', 'updated_at'})

_UNIT_FIELDS = (
    'pa', 'na', 'valence', 'arousal',
    'intimacy', 'passion', 'commitment',
    'attachment', 'curiosity', 'reflection', 'social',
    'duty', 'libido', 'stress', 'fatigue',
)

_STATE_COLUMNS = (
    'id',
    'pa', 'na', 'valence', 'arousal',
    'mood_word', 'mood_source_message_id',
    'intimacy', 'passion', 'commitment',
    'p_updated_at', 'i_updated_at',
    'attachment', 'curiosity', 'reflection', 'social',
    'duty', 'libido', 'stress', 'fatigue',
    'drives_updated_at',
    'last_scored_message_id',
    'state_version',
    'updated_at',
)

_STATE_COLUMN_SET = frozenset(_STATE_COLUMNS)


@dataclass(frozen=True)
class ApplyResult:
    status: str
    state_version_before: Optional[int]
    state_version_after: Optional[int]
    event_id: Optional[int]
    error: Optional[str] = None
    # 与 payload 分离的审计结果（如 shadow diagnostics）；duplicate 时回放原值
    result: Optional[dict] = None

    @property
    def applied(self) -> bool:
        return self.status == 'applied'


@dataclass(frozen=True)
class ConditionalDecision:
    """``apply_conditional_state_update`` 在事务内的业务裁决。

    - ``status='applied'``：必须提供非空 ``updates``
    - ``status='stale_skipped'``：不更新状态；``error`` 记录 watermark 等原因
    - ``result``：可选审计字典，写入 ``result_json``，**不**参与 payload_hash
    """
    status: str
    updates: Optional[Mapping[str, Any]] = None
    error: Optional[str] = None
    result: Optional[Mapping[str, Any]] = None


class VersionConflictError(RuntimeError):
    """expected_state_version 与当前行不一致；不静默覆盖。"""


class StoreError(RuntimeError):
    pass


# SQLite INTEGER 有符号 64 位上限；message_id 契约与 events 模块对齐
_SQLITE_MAX_S64 = 2**63 - 1


def require_positive_message_id(value: Any, *, field: str = 'message_id') -> int:
    """正整数 message_id；禁止 bool / 浮点 / 字符串静默转换。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise StoreError(f'{field} must be a positive integer: {value!r}')
    if value <= 0 or value > _SQLITE_MAX_S64:
        raise StoreError(f'{field} out of SQLite integer range: {value!r}')
    return value


def _now_beijing() -> str:
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime(
        '%Y-%m-%d %H:%M:%S'
    )


def _normalize_source_id(source_id: Optional[Any]) -> Optional[str]:
    if source_id is None:
        return None
    return str(source_id)


def _canonical_payload_json(payload: Mapping[str, Any]) -> str:
    """只接受标准 JSON 可序列化值；禁止 default=str。

    返回的字符串同时用于落库 ``payload_json`` 与 ``payload_hash``。
    """
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StoreError(f'payload is not JSON-serializable: {exc}') from exc


def _payload_hash_from_json(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()


def _canonical_result_json(result: Optional[Mapping[str, Any]]) -> Optional[str]:
    """审计结果 JSON；None → SQL NULL。不进入 payload_hash。"""
    if result is None:
        return None
    return _canonical_payload_json(result)


def _parse_stored_result(raw: Any) -> Optional[dict]:
    if raw is None or raw == '':
        return None
    try:
        obj = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StoreError(f'corrupt result_json: {exc}') from exc
    if not isinstance(obj, dict):
        raise StoreError(f'result_json must be a JSON object, got {type(obj)!r}')
    return obj


def _require_clean_write_connection(conn: sqlite3.Connection) -> None:
    """公开写入口拒绝已有未提交事务，避免没收调用方事务所有权。

    不修改 ``isolation_level``。``executescript`` 会隐式 COMMIT 挂起事务，
    因此必须先确认连接干净。
    """
    if conn.in_transaction:
        raise StoreError(
            'store write requires a connection with no active transaction'
        )


def _fetchone_dict(conn: sqlite3.Connection, sql: str,
                   params: Sequence[Any] = ()) -> Optional[dict]:
    """兼容 sqlite3.Row 与默认 tuple row。"""
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    cols = [d[0] for d in cur.description]
    return {cols[i]: row[i] for i in range(len(cols))}


def open_store(db_path: str,
               busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """打开可写连接；设置 busy_timeout；**不**改 journal_mode。

    返回的连接由本模块拥有，可安全使用显式 ``BEGIN IMMEDIATE``。
    """
    conn = sqlite3.connect(db_path, timeout=max(1.0, busy_timeout_ms / 1000.0))
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute(f'PRAGMA busy_timeout={int(busy_timeout_ms)}')
    return conn


def get_busy_timeout_ms(conn: sqlite3.Connection) -> int:
    row = conn.execute('PRAGMA busy_timeout').fetchone()
    return int(row[0])


def get_journal_mode(conn: sqlite3.Connection) -> str:
    return str(conn.execute('PRAGMA journal_mode').fetchone()[0]).lower()


def ensure_schema(conn: sqlite3.Connection) -> None:
    """创建两张表（可重复调用）。不修改 journal_mode / isolation_level。"""
    _require_clean_write_connection(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS internal_state_v3 (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            pa REAL NOT NULL DEFAULT 0.5
                CHECK (pa >= 0.0 AND pa <= 1.0),
            na REAL NOT NULL DEFAULT 0.2
                CHECK (na >= 0.0 AND na <= 1.0),
            valence REAL NOT NULL DEFAULT 0.6
                CHECK (valence >= 0.0 AND valence <= 1.0),
            arousal REAL NOT NULL DEFAULT 0.3
                CHECK (arousal >= 0.0 AND arousal <= 1.0),
            mood_word TEXT NOT NULL DEFAULT '平静',
            mood_source_message_id INTEGER,
            intimacy REAL NOT NULL DEFAULT 0.3
                CHECK (intimacy >= 0.0 AND intimacy <= 1.0),
            passion REAL NOT NULL DEFAULT 0.0
                CHECK (passion >= 0.0 AND passion <= 1.0),
            commitment REAL NOT NULL DEFAULT 0.7
                CHECK (commitment >= 0.0 AND commitment <= 1.0),
            p_updated_at TEXT,
            i_updated_at TEXT,
            attachment REAL NOT NULL DEFAULT 0.10
                CHECK (attachment >= 0.0 AND attachment <= 1.0),
            curiosity REAL NOT NULL DEFAULT 0.20
                CHECK (curiosity >= 0.0 AND curiosity <= 1.0),
            reflection REAL NOT NULL DEFAULT 0.10
                CHECK (reflection >= 0.0 AND reflection <= 1.0),
            social REAL NOT NULL DEFAULT 0.10
                CHECK (social >= 0.0 AND social <= 1.0),
            duty REAL NOT NULL DEFAULT 0.15
                CHECK (duty >= 0.0 AND duty <= 1.0),
            libido REAL NOT NULL DEFAULT 0.00
                CHECK (libido >= 0.0 AND libido <= 1.0),
            stress REAL NOT NULL DEFAULT 0.10
                CHECK (stress >= 0.0 AND stress <= 1.0),
            fatigue REAL NOT NULL DEFAULT 0.20
                CHECK (fatigue >= 0.0 AND fatigue <= 1.0),
            drives_updated_at TEXT,
            last_scored_message_id INTEGER,
            state_version INTEGER NOT NULL DEFAULT 0
                CHECK (state_version >= 0),
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS internal_state_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            source_id TEXT,
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN (
                    'applied', 'duplicate', 'stale_skipped',
                    'shadow_only', 'failed'
                )),
            state_version_before INTEGER,
            state_version_after INTEGER,
            applied_at TEXT NOT NULL,
            error TEXT,
            result_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_internal_state_events_type
            ON internal_state_events(event_type);
        CREATE INDEX IF NOT EXISTS idx_internal_state_events_applied_at
            ON internal_state_events(applied_at);
        """
    )
    _ensure_events_result_json_column(conn)


def _ensure_events_result_json_column(conn: sqlite3.Connection) -> None:
    """旧库 CREATE IF NOT EXISTS 不会加列；幂等补齐 result_json。"""
    cols = {
        row[1]
        for row in conn.execute('PRAGMA table_info(internal_state_events)')
    }
    if 'result_json' not in cols:
        conn.execute(
            'ALTER TABLE internal_state_events ADD COLUMN result_json TEXT'
        )


def read_state(conn: sqlite3.Connection) -> Optional[dict]:
    return _fetchone_dict(conn, 'SELECT * FROM internal_state_v3 WHERE id=1')


def read_event(conn: sqlite3.Connection, event_key: str) -> Optional[dict]:
    return _fetchone_dict(
        conn,
        'SELECT * FROM internal_state_events WHERE event_key=?',
        (event_key,),
    )


def _clamp01(value: Any, default: float, *, field: str = 'value') -> float:
    """写入前强制有限且落在 [0,1]；NaN/Inf 不得被 clamp 成 0/1。"""
    if value is None:
        v = float(default)
    else:
        try:
            v = float(value)
        except (TypeError, ValueError) as exc:
            raise StoreError(f'non-numeric value for {field}: {value!r}') from exc
    if not math.isfinite(v):
        raise StoreError(f'non-finite value for {field}: {value!r}')
    return max(0.0, min(1.0, v))


def _begin_immediate(conn: sqlite3.Connection) -> None:
    conn.execute('BEGIN IMMEDIATE')


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute('ROLLBACK')
    except sqlite3.Error:
        pass


def _validate_mutator_updates(raw_updates: Mapping[str, Any]) -> dict:
    """先校验业务字段，再允许调用方叠加系统字段。"""
    updates = dict(raw_updates)
    unknown = set(updates) - _STATE_COLUMN_SET
    if unknown:
        raise StoreError(f'unknown state fields: {sorted(unknown)}')
    managed = set(updates) & _SYSTEM_MANAGED_FIELDS
    if managed:
        raise StoreError(
            f'mutator may not update system-managed fields: {sorted(managed)}'
        )
    if not updates:
        raise StoreError('mutator returned no updatable fields')
    return updates


def _existing_event_result(
    existing: dict,
    *,
    event_type: str,
    source_id: Optional[str],
    payload_hash: str,
) -> ApplyResult:
    """同 key：语义相同 → duplicate；语义不同 → idempotency_conflict。"""
    same_type = existing.get('event_type') == event_type
    same_source = _normalize_source_id(existing.get('source_id')) == source_id
    same_hash = existing.get('payload_hash') == payload_hash
    if same_type and same_source and same_hash:
        return ApplyResult(
            status='duplicate',
            state_version_before=existing.get('state_version_before'),
            state_version_after=existing.get('state_version_after'),
            event_id=existing.get('id'),
            result=_parse_stored_result(existing.get('result_json')),
        )
    return ApplyResult(
        status='idempotency_conflict',
        state_version_before=existing.get('state_version_before'),
        state_version_after=existing.get('state_version_after'),
        event_id=existing.get('id'),
        error=(
            'event_key reused with different event_type/source_id/payload; '
            'refusing to treat as duplicate'
        ),
    )


def apply_conditional_state_update(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    event_type: str,
    source_id: Optional[Any],
    payload: Mapping[str, Any],
    decide: Callable[[dict], ConditionalDecision],
    expected_state_version: Optional[int] = None,
) -> ApplyResult:
    """在同一 ``BEGIN IMMEDIATE`` 内由 ``decide(state)`` 裁决 applied / stale。

    - ``applied``：写状态（version +1）+ 事件 ``status=applied``
    - ``stale_skipped``：不改状态、不升版本；仍插入事件并**消费** event_key；
      ``state_version_before == state_version_after``；原因写入 ``error``
    - ``version_conflict`` / 缺状态：**不消费** event_key
    - 同 key 幂等语义与 ``apply_state_update`` 一致
    """
    if not event_key:
        raise StoreError('event_key required')
    if event_type is None or event_type == '':
        raise StoreError('event_type required')

    _require_clean_write_connection(conn)

    source_id_norm = _normalize_source_id(source_id)
    payload_obj = dict(payload)
    p_json = _canonical_payload_json(payload_obj)
    p_hash = _payload_hash_from_json(p_json)
    now = _now_beijing()

    try:
        _begin_immediate(conn)

        existing = _fetchone_dict(
            conn,
            'SELECT id, event_type, source_id, payload_hash, status, '
            'state_version_before, state_version_after, result_json '
            'FROM internal_state_events WHERE event_key=?',
            (event_key,),
        )
        if existing is not None:
            conn.execute('COMMIT')
            return _existing_event_result(
                existing,
                event_type=event_type,
                source_id=source_id_norm,
                payload_hash=p_hash,
            )

        state = read_state(conn)
        if state is None:
            _rollback(conn)
            return ApplyResult(
                status='failed',
                state_version_before=None,
                state_version_after=None,
                event_id=None,
                error='state row missing; call bootstrap_from_snapshot first',
            )

        version_before = int(state['state_version'])
        if (expected_state_version is not None
                and int(expected_state_version) != version_before):
            err = (
                f'version conflict: expected {expected_state_version}, '
                f'current {version_before}'
            )
            _rollback(conn)
            return ApplyResult(
                status='version_conflict',
                state_version_before=version_before,
                state_version_after=None,
                event_id=None,
                error=err,
            )

        decision = decide(dict(state))
        if not isinstance(decision, ConditionalDecision):
            raise StoreError(
                'decide() must return ConditionalDecision, '
                f'got {type(decision)!r}'
            )

        result_obj = (
            dict(decision.result) if decision.result is not None else None
        )
        r_json = _canonical_result_json(result_obj)

        if decision.status == 'stale_skipped':
            conn.execute(
                """
                INSERT INTO internal_state_events (
                    event_key, event_type, source_id, payload_json,
                    payload_hash, status, state_version_before,
                    state_version_after, applied_at, error, result_json
                ) VALUES (?, ?, ?, ?, ?, 'stale_skipped', ?, ?, ?, ?, ?)
                """,
                (event_key, event_type, source_id_norm, p_json, p_hash,
                 version_before, version_before, now, decision.error, r_json),
            )
            conn.execute('COMMIT')
            return ApplyResult(
                status='stale_skipped',
                state_version_before=version_before,
                state_version_after=version_before,
                event_id=_last_event_id(conn, event_key),
                error=decision.error,
                result=result_obj,
            )

        if decision.status != 'applied':
            raise StoreError(
                f'decide() returned unsupported status: {decision.status!r}'
            )

        raw_updates = _validate_mutator_updates(decision.updates or {})
        version_after = version_before + 1
        updates = dict(raw_updates)
        updates['state_version'] = version_after
        updates['updated_at'] = now

        for key in _UNIT_FIELDS:
            if key in updates:
                updates[key] = _clamp01(updates[key], float(state[key]), field=key)

        cols = [c for c in updates.keys() if c in _STATE_COLUMN_SET and c != 'id']
        sets = ', '.join(f'{c}=?' for c in cols)
        values = [updates[c] for c in cols]
        cur = conn.execute(
            f'UPDATE internal_state_v3 SET {sets} '
            f'WHERE id=1 AND state_version=?',
            values + [version_before],
        )
        if cur.rowcount != 1:
            raise VersionConflictError(
                f'lost race on state_version={version_before}'
            )

        conn.execute(
            """
            INSERT INTO internal_state_events (
                event_key, event_type, source_id, payload_json,
                payload_hash, status, state_version_before,
                state_version_after, applied_at, error, result_json
            ) VALUES (?, ?, ?, ?, ?, 'applied', ?, ?, ?, NULL, ?)
            """,
            (event_key, event_type, source_id_norm, p_json, p_hash,
             version_before, version_after, now, r_json),
        )
        conn.execute('COMMIT')
        return ApplyResult(
            status='applied',
            state_version_before=version_before,
            state_version_after=version_after,
            event_id=_last_event_id(conn, event_key),
            result=result_obj,
        )
    except VersionConflictError as exc:
        _rollback(conn)
        return ApplyResult(
            status='version_conflict',
            state_version_before=None,
            state_version_after=None,
            event_id=None,
            error=str(exc),
        )
    except Exception:
        _rollback(conn)
        raise


def apply_state_update(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    event_type: str,
    source_id: Optional[Any],
    payload: Mapping[str, Any],
    mutator: Callable[[dict], Mapping[str, Any]],
    expected_state_version: Optional[int] = None,
) -> ApplyResult:
    """在同一 ``BEGIN IMMEDIATE`` 事务内写事件 + 更新状态。

    幂等 / 冲突语义：
      - 同 key + 同语义 payload → ``duplicate``（不更新状态）
      - 同 key + 不同语义 → ``idempotency_conflict``（不更新、不插新行）
      - 基础设施版本冲突 → **不消费** event_key，rollback 后
        ``version_conflict``，允许同 key 重试
      - 状态行缺失 → **不消费** event_key，返回 ``failed``，bootstrap 后可重试
      - mutator 未知字段 / 空更新 / 改系统字段 → rollback + ``StoreError``
      - 成功时 ``state_version`` 恰好 +1

    实现上为 ``apply_conditional_state_update`` 的 applied 包装；
    行为与 Phase 1A-0 保持兼容。业务 stale 请用条件接口。
    """
    def _decide(state: dict) -> ConditionalDecision:
        return ConditionalDecision(
            status='applied',
            updates=mutator(dict(state)),
        )

    return apply_conditional_state_update(
        conn,
        event_key=event_key,
        event_type=event_type,
        source_id=source_id,
        payload=payload,
        decide=_decide,
        expected_state_version=expected_state_version,
    )


def _last_event_id(conn: sqlite3.Connection, event_key: str) -> Optional[int]:
    row = _fetchone_dict(
        conn,
        'SELECT id FROM internal_state_events WHERE event_key=?',
        (event_key,),
    )
    return int(row['id']) if row else None


def bootstrap_from_snapshot(
    conn: sqlite3.Connection,
    snapshot: Any,
    *,
    last_scored_message_id: int,
    last_scored_message_id_source: str = 'explicit_argument',
    event_key: str = 'bootstrap:initial',
    source_id: Optional[Any] = 'phase0_snapshot',
) -> ApplyResult:
    """用显式传入的 Phase 0 snapshot 初始化 id=1。

    Phase 0 数值已物化到 ``observed_at``，因此：
      ``p_updated_at`` / ``i_updated_at`` / ``drives_updated_at``
      一律写成 ``observed_at``，避免后续再衰减同一段时间。
    旧来源时间保留在 bootstrap event payload 的
    ``legacy_source_timestamps`` 中。

    ``last_scored_message_id`` **必填**正整数：禁止 None / 0 / 猜测。
    来源说明写入 payload（不含消息正文 / 评分原文 / Prompt）。

    幂等与 ``apply_state_update`` 一致：同 key 仅当 type/source/payload
    语义相同才算 duplicate，否则 ``idempotency_conflict``。

    若状态行已存在但缺少 bootstrap 事件：fail closed
    （``bootstrap_inconsistent``），绝不伪造 provenance。
    """
    watermark = require_positive_message_id(
        last_scored_message_id, field='last_scored_message_id',
    )
    if not isinstance(last_scored_message_id_source, str):
        raise StoreError(
            'last_scored_message_id_source must be a str: '
            f'{last_scored_message_id_source!r}'
        )
    source_note = last_scored_message_id_source.strip()
    if not source_note or len(source_note) > 128:
        raise StoreError(
            'last_scored_message_id_source must be non-empty and <= 128 chars'
        )

    _require_clean_write_connection(conn)
    ensure_schema(conn)
    # ensure_schema 可能经由 executescript 提交 DDL；确认仍无挂起事务
    _require_clean_write_connection(conn)

    def _g(obj: Any, *path: str, default=None):
        cur = obj
        for p in path:
            if cur is None:
                return default
            if isinstance(cur, Mapping):
                cur = cur.get(p, default)
            else:
                cur = getattr(cur, p, default)
        return cur

    affect = _g(snapshot, 'affect')
    bond = _g(snapshot, 'bond')
    drives = (_g(snapshot, 'candidate_unified_drives')
              or _g(snapshot, 'drives'))
    observed_at = _g(snapshot, 'observed_at') or _now_beijing()
    ts = _g(snapshot, 'diagnostics', 'source_timestamps') or {}
    legacy_ts = dict(ts) if isinstance(ts, Mapping) else {}
    source_id_norm = _normalize_source_id(source_id)

    seed = {
        'pa': _clamp01(_g(affect, 'pa'), 0.5, field='pa'),
        'na': _clamp01(_g(affect, 'na'), 0.2, field='na'),
        'valence': _clamp01(_g(affect, 'valence'), 0.6, field='valence'),
        'arousal': _clamp01(_g(affect, 'arousal'), 0.3, field='arousal'),
        'mood_word': _g(affect, 'mood_word') or '平静',
        'mood_source_message_id': None,
        'intimacy': _clamp01(_g(bond, 'intimacy'), 0.3, field='intimacy'),
        'passion': _clamp01(_g(bond, 'passion'), 0.0, field='passion'),
        'commitment': _clamp01(_g(bond, 'commitment'), 0.7, field='commitment'),
        # 已物化到 observed_at：时间基准必须对齐，禁止双倍衰减
        'p_updated_at': observed_at,
        'i_updated_at': observed_at,
        'attachment': _clamp01(_g(drives, 'attachment'), 0.10, field='attachment'),
        'curiosity': _clamp01(_g(drives, 'curiosity'), 0.20, field='curiosity'),
        'reflection': _clamp01(_g(drives, 'reflection'), 0.10, field='reflection'),
        'social': _clamp01(_g(drives, 'social'), 0.10, field='social'),
        'duty': _clamp01(_g(drives, 'duty'), 0.15, field='duty'),
        'libido': _clamp01(_g(drives, 'libido'), 0.00, field='libido'),
        'stress': _clamp01(_g(drives, 'stress'), 0.10, field='stress'),
        'fatigue': _clamp01(_g(drives, 'fatigue'), 0.20, field='fatigue'),
        'drives_updated_at': observed_at,
        'last_scored_message_id': watermark,
        'state_version': 0,
        'updated_at': observed_at,
    }

    payload = {
        'source': 'bootstrap_from_snapshot',
        'observed_at': observed_at,
        'legacy_source_timestamps': legacy_ts,
        'last_scored_message_id': watermark,
        'last_scored_message_id_source': source_note,
        'seed': seed,
    }
    p_json = _canonical_payload_json(payload)
    p_hash = _payload_hash_from_json(p_json)
    now = _now_beijing()

    try:
        _begin_immediate(conn)

        existing_event = _fetchone_dict(
            conn,
            'SELECT id, status, state_version_before, state_version_after, '
            'event_type, source_id, payload_hash '
            'FROM internal_state_events WHERE event_key=?',
            (event_key,),
        )
        existing_state = read_state(conn)

        if existing_event is not None:
            collision = _existing_event_result(
                existing_event,
                event_type='bootstrap',
                source_id=source_id_norm,
                payload_hash=p_hash,
            )
            if existing_state is None:
                _rollback(conn)
                if collision.status == 'duplicate':
                    return ApplyResult(
                        status='failed',
                        state_version_before=None,
                        state_version_after=None,
                        event_id=collision.event_id,
                        error='bootstrap event exists without state row',
                    )
                return collision
            # 状态已在：绝不改写；仅按 payload 语义返回 duplicate / conflict
            _rollback(conn)
            ver = int(existing_state['state_version'])
            return ApplyResult(
                status=collision.status,
                state_version_before=ver,
                state_version_after=ver if collision.status == 'duplicate' else None,
                event_id=collision.event_id,
                error=collision.error,
            )

        if existing_state is not None:
            # 状态在、bootstrap 事件缺失：fail closed，绝不伪造 provenance
            _rollback(conn)
            return ApplyResult(
                status='bootstrap_inconsistent',
                state_version_before=int(existing_state['state_version']),
                state_version_after=None,
                event_id=None,
                error=(
                    'internal_state_v3 exists without bootstrap event; '
                    'refusing to forge provenance'
                ),
            )

        cols: Sequence[str] = (
            'id',
            'pa', 'na', 'valence', 'arousal',
            'mood_word', 'mood_source_message_id',
            'intimacy', 'passion', 'commitment',
            'p_updated_at', 'i_updated_at',
            'attachment', 'curiosity', 'reflection', 'social',
            'duty', 'libido', 'stress', 'fatigue',
            'drives_updated_at',
            'last_scored_message_id',
            'state_version',
            'updated_at',
        )
        values = [1] + [seed[c] for c in cols if c != 'id']
        placeholders = ','.join('?' * len(cols))
        conn.execute(
            f'INSERT INTO internal_state_v3 ({",".join(cols)}) '
            f'VALUES ({placeholders})',
            values,
        )
        conn.execute(
            """
            INSERT INTO internal_state_events (
                event_key, event_type, source_id, payload_json,
                payload_hash, status, state_version_before,
                state_version_after, applied_at, error
            ) VALUES (?, 'bootstrap', ?, ?, ?, 'applied', 0, 0, ?, NULL)
            """,
            (event_key, source_id_norm, p_json, p_hash, now),
        )
        conn.execute('COMMIT')
        return ApplyResult(
            status='applied',
            state_version_before=0,
            state_version_after=0,
            event_id=_last_event_id(conn, event_key),
        )
    except Exception:
        _rollback(conn)
        raise


__all__ = [
    'DEFAULT_BUSY_TIMEOUT_MS',
    'EVENT_STATUSES',
    'ApplyResult',
    'ConditionalDecision',
    'VersionConflictError',
    'StoreError',
    'open_store',
    'get_busy_timeout_ms',
    'get_journal_mode',
    'ensure_schema',
    'read_state',
    'read_event',
    'apply_conditional_state_update',
    'apply_state_update',
    'bootstrap_from_snapshot',
    'require_positive_message_id',
]
