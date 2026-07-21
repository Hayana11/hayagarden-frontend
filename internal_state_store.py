"""Internal State v3 — Phase 1A-0 存储底座

只提供：
  - ``internal_state_v3`` / ``internal_state_events`` schema
  - ``BEGIN IMMEDIATE`` 事务封装
  - ``event_key`` 幂等、``state_version`` 乐观校验
  - 显式传入 snapshot 的 bootstrap（不接启动路径）

严格不做：
  - 不修改 journal_mode（不开启/关闭 WAL）
  - 不接 gateway / chat / Wake / prompt
  - 不实现 observe_* / apply_outcome 业务
  - 不 import emotion_engine / drive_engine / desire
  - 不写生产库（调用方传入临时 conn / 路径）
"""

from __future__ import annotations

import datetime
import hashlib
import json
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

    @property
    def applied(self) -> bool:
        return self.status == 'applied'


class VersionConflictError(RuntimeError):
    """expected_state_version 与当前行不一致；不静默覆盖。"""


class StoreError(RuntimeError):
    pass


def _now_beijing() -> str:
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime(
        '%Y-%m-%d %H:%M:%S'
    )


def _payload_hash(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def _ensure_explicit_tx(conn: sqlite3.Connection) -> None:
    """公开写路径兼容普通 sqlite3.Connection：改用显式事务。"""
    if getattr(conn, 'isolation_level', None) is not None:
        conn.isolation_level = None


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
    """打开可写连接；设置 busy_timeout；**不**改 journal_mode。"""
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
    """创建两张表（可重复调用）。不修改 journal_mode。"""
    _ensure_explicit_tx(conn)
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
            error TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_internal_state_events_type
            ON internal_state_events(event_type);
        CREATE INDEX IF NOT EXISTS idx_internal_state_events_applied_at
            ON internal_state_events(applied_at);
        """
    )


def read_state(conn: sqlite3.Connection) -> Optional[dict]:
    return _fetchone_dict(conn, 'SELECT * FROM internal_state_v3 WHERE id=1')


def read_event(conn: sqlite3.Connection, event_key: str) -> Optional[dict]:
    return _fetchone_dict(
        conn,
        'SELECT * FROM internal_state_events WHERE event_key=?',
        (event_key,),
    )


def _clamp01(value: Any, default: float) -> float:
    if value is None:
        v = float(default)
    else:
        v = float(value)
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
    same_source = existing.get('source_id') == source_id
    same_hash = existing.get('payload_hash') == payload_hash
    if same_type and same_source and same_hash:
        return ApplyResult(
            status='duplicate',
            state_version_before=existing.get('state_version_before'),
            state_version_after=existing.get('state_version_after'),
            event_id=existing.get('id'),
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


def apply_state_update(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    event_type: str,
    source_id: Optional[str],
    payload: Mapping[str, Any],
    mutator: Callable[[dict], Mapping[str, Any]],
    expected_state_version: Optional[int] = None,
    mark_stale: bool = False,
) -> ApplyResult:
    """在同一 ``BEGIN IMMEDIATE`` 事务内写事件 + 更新状态。

    幂等 / 冲突语义：
      - 同 key + 同语义 payload → ``duplicate``（不更新状态）
      - 同 key + 不同语义 → ``idempotency_conflict``（不更新、不插新行）
      - 基础设施版本冲突（默认）→ **不消费** event_key，rollback 后
        ``version_conflict``，允许同 key 重试
      - 业务确认过期（``mark_stale=True``）→ 落 ``stale_skipped`` 终态
      - 状态行缺失 → **不消费** event_key，返回 ``failed``，bootstrap 后可重试
      - mutator 未知字段 / 空更新 / 改系统字段 → rollback + ``StoreError``
      - 成功时 ``state_version`` 恰好 +1
    """
    if not event_key:
        raise StoreError('event_key required')
    if event_type is None or event_type == '':
        raise StoreError('event_type required')

    _ensure_explicit_tx(conn)

    payload_obj = dict(payload)
    p_json = json.dumps(payload_obj, ensure_ascii=False, default=str)
    p_hash = _payload_hash(payload_obj)
    now = _now_beijing()

    try:
        _begin_immediate(conn)

        existing = _fetchone_dict(
            conn,
            'SELECT id, event_type, source_id, payload_hash, status, '
            'state_version_before, state_version_after '
            'FROM internal_state_events WHERE event_key=?',
            (event_key,),
        )
        if existing is not None:
            conn.execute('COMMIT')
            return _existing_event_result(
                existing,
                event_type=event_type,
                source_id=source_id,
                payload_hash=p_hash,
            )

        state = read_state(conn)
        if state is None:
            # 不消费 event_key：rollback，bootstrap 后可同 key 重试
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
            if mark_stale:
                # 业务终态：消费 key，状态不改
                conn.execute(
                    """
                    INSERT INTO internal_state_events (
                        event_key, event_type, source_id, payload_json,
                        payload_hash, status, state_version_before,
                        state_version_after, applied_at, error
                    ) VALUES (?, ?, ?, ?, ?, 'stale_skipped', ?, NULL, ?, ?)
                    """,
                    (event_key, event_type, source_id, p_json, p_hash,
                     version_before, now, err),
                )
                conn.execute('COMMIT')
                return ApplyResult(
                    status='stale_skipped',
                    state_version_before=version_before,
                    state_version_after=None,
                    event_id=_last_event_id(conn, event_key),
                    error=err,
                )
            # 基础设施乐观锁冲突：不消费 key
            _rollback(conn)
            return ApplyResult(
                status='version_conflict',
                state_version_before=version_before,
                state_version_after=None,
                event_id=None,
                error=err,
            )

        raw_updates = _validate_mutator_updates(mutator(dict(state)))
        version_after = version_before + 1
        updates = dict(raw_updates)
        updates['state_version'] = version_after
        updates['updated_at'] = now

        for key in _UNIT_FIELDS:
            if key in updates:
                updates[key] = _clamp01(updates[key], float(state[key]))

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
                state_version_after, applied_at, error
            ) VALUES (?, ?, ?, ?, ?, 'applied', ?, ?, ?, NULL)
            """,
            (event_key, event_type, source_id, p_json, p_hash,
             version_before, version_after, now),
        )
        conn.execute('COMMIT')
        return ApplyResult(
            status='applied',
            state_version_before=version_before,
            state_version_after=version_after,
            event_id=_last_event_id(conn, event_key),
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
    event_key: str = 'bootstrap:initial',
    source_id: Optional[str] = 'phase0_snapshot',
) -> ApplyResult:
    """用显式传入的 Phase 0 snapshot 初始化 id=1。

    Phase 0 数值已物化到 ``observed_at``，因此：
      ``p_updated_at`` / ``i_updated_at`` / ``drives_updated_at``
      一律写成 ``observed_at``，避免后续再衰减同一段时间。
    旧来源时间保留在 bootstrap event payload 的
    ``legacy_source_timestamps`` 中。
    """
    _ensure_explicit_tx(conn)
    ensure_schema(conn)

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

    seed = {
        'pa': _clamp01(_g(affect, 'pa'), 0.5),
        'na': _clamp01(_g(affect, 'na'), 0.2),
        'valence': _clamp01(_g(affect, 'valence'), 0.6),
        'arousal': _clamp01(_g(affect, 'arousal'), 0.3),
        'mood_word': _g(affect, 'mood_word') or '平静',
        'mood_source_message_id': None,
        'intimacy': _clamp01(_g(bond, 'intimacy'), 0.3),
        'passion': _clamp01(_g(bond, 'passion'), 0.0),
        'commitment': _clamp01(_g(bond, 'commitment'), 0.7),
        # 已物化到 observed_at：时间基准必须对齐，禁止双倍衰减
        'p_updated_at': observed_at,
        'i_updated_at': observed_at,
        'attachment': _clamp01(_g(drives, 'attachment'), 0.10),
        'curiosity': _clamp01(_g(drives, 'curiosity'), 0.20),
        'reflection': _clamp01(_g(drives, 'reflection'), 0.10),
        'social': _clamp01(_g(drives, 'social'), 0.10),
        'duty': _clamp01(_g(drives, 'duty'), 0.15),
        'libido': _clamp01(_g(drives, 'libido'), 0.00),
        'stress': _clamp01(_g(drives, 'stress'), 0.10),
        'fatigue': _clamp01(_g(drives, 'fatigue'), 0.20),
        'drives_updated_at': observed_at,
        'last_scored_message_id': None,
        'state_version': 0,
        'updated_at': observed_at,
    }

    payload = {
        'source': 'bootstrap_from_snapshot',
        'observed_at': observed_at,
        'legacy_source_timestamps': legacy_ts,
        'seed': seed,
    }
    p_json = json.dumps(payload, ensure_ascii=False, default=str)
    p_hash = _payload_hash(payload)
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

        if existing_state is not None:
            if existing_event is None:
                conn.execute(
                    """
                    INSERT INTO internal_state_events (
                        event_key, event_type, source_id, payload_json,
                        payload_hash, status, state_version_before,
                        state_version_after, applied_at, error
                    ) VALUES (?, 'bootstrap', ?, ?, ?, 'duplicate',
                              ?, ?, ?, 'state already present')
                    """,
                    (event_key, source_id, p_json, p_hash,
                     existing_state['state_version'],
                     existing_state['state_version'], now),
                )
                eid = _last_event_id(conn, event_key)
            else:
                eid = existing_event['id']
            conn.execute('COMMIT')
            return ApplyResult(
                status='duplicate',
                state_version_before=int(existing_state['state_version']),
                state_version_after=int(existing_state['state_version']),
                event_id=eid,
            )

        if existing_event is not None:
            conn.execute('COMMIT')
            return ApplyResult(
                status='failed',
                state_version_before=None,
                state_version_after=None,
                event_id=existing_event['id'],
                error='bootstrap event exists without state row',
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
            (event_key, source_id, p_json, p_hash, now),
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
    'VersionConflictError',
    'StoreError',
    'open_store',
    'get_busy_timeout_ms',
    'get_journal_mode',
    'ensure_schema',
    'read_state',
    'read_event',
    'apply_state_update',
    'bootstrap_from_snapshot',
]
