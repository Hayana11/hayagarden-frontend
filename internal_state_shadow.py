"""Internal State v3 — Phase 1A-4a Shadow 基础设施（默认关闭）

职责：
  - ``INTERNAL_STATE_V3_SHADOW_ENABLED`` 安全门（默认 off）
  - 独立短连接 + busy_timeout；**不**改 journal_mode
  - 评分水位 ledger + 同事务 proof writer 辅助（强制 in_transaction）
  - 生产 bootstrap：同一 ``BEGIN IMMEDIATE`` 内采集 + 落地（线性化）
  - 严格 snapshot 校验（禁止默认值洗白；单位区间 fail-closed）
  - 统一 Shadow adapter（方案 A：事件路径不自动 bootstrap）

严格不做：
  - 不接 gateway / chat / SSE / Wake / Prompt
  - 不停止旧 discharge / satisfy / score_async
  - 不 import emotion_engine / drive_engine / desire / gateway / wake
  - 不启用开关部署
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Optional

import internal_state as isv3
import internal_state_events as events
import internal_state_store as store
from chat.interaction_state import read_interaction_clock_from_conn

logger = logging.getLogger(__name__)

SHADOW_ENABLED_ENV = 'INTERNAL_STATE_V3_SHADOW_ENABLED'
BOOTSTRAP_EVENT_KEY = 'bootstrap:initial'
SCORE_APPLIED_TABLE = 'internal_state_score_applied'
WATERMARK_SOURCE = 'internal_state_score_applied:max(message_id)'
PRODUCTION_BOOTSTRAP_SOURCE_ID = 'phase1a4a_shadow'
CAPTURE_MODE_PRODUCTION = 'same_sqlite_snapshot_v1'
CAPTURE_MODE_TEST = 'test_injection'
_DEFAULT_VERSION_RETRIES = 2

_AFFECT_FIELDS = ('pa', 'na', 'valence', 'arousal')
_BOND_FIELDS = ('intimacy', 'passion', 'commitment')
_DRIVE_FIELDS = (
    'attachment', 'curiosity', 'reflection', 'social',
    'duty', 'libido', 'stress', 'fatigue',
)

_lock = threading.Lock()
_last_error: Optional[str] = None
_last_error_at: Optional[str] = None
_last_status: Optional[str] = None


@dataclass(frozen=True)
class ShadowHealth:
    enabled: bool
    bootstrapped: bool
    state_version: Optional[int]
    last_scored_message_id: Optional[int]
    last_error: Optional[str]
    last_error_at: Optional[str]
    last_status: Optional[str] = None
    journal_mode: Optional[str] = None
    provenance_ok: Optional[bool] = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ShadowResult:
    """adapter 对外结果；公开 wrapper 永不向主流程抛异常。"""
    ok: bool
    status: str
    error: Optional[str] = None
    elapsed_ms: Optional[float] = None
    apply: Optional[store.ApplyResult] = None


@dataclass(frozen=True)
class BootstrapBundle:
    """诊断用只读截面（生产 bootstrap 不再经此两阶段落地）。"""
    snapshot: Any
    watermark: int
    watermark_applied_at: str
    watermark_row_source: str
    observed_at: str


def _now_beijing() -> str:
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime(
        '%Y-%m-%d %H:%M:%S'
    )


def _now_beijing_dt() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def is_shadow_enabled(*, environ: Optional[Mapping[str, str]] = None) -> bool:
    """默认关闭。仅 ``'1'`` 开启；不读 DB。"""
    env = os.environ if environ is None else environ
    return str(env.get(SHADOW_ENABLED_ENV, '0')).strip() == '1'


def _record_error(message: str) -> None:
    global _last_error, _last_error_at, _last_status
    ts = _now_beijing()
    with _lock:
        _last_error = message
        _last_error_at = ts
        _last_status = 'failed'
    logger.warning('internal_state_shadow: %s', message)


def _record_status(status: str) -> None:
    global _last_status
    with _lock:
        _last_status = status


def _clear_error() -> None:
    global _last_error, _last_error_at
    with _lock:
        _last_error = None
        _last_error_at = None


def open_shadow_connection(
    db_path: Optional[str] = None,
    *,
    busy_timeout_ms: int = store.DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """打开独立可写短连接；不改 journal_mode。"""
    path = isv3.memories_db_path(db_path)
    return store.open_store(path, busy_timeout_ms=busy_timeout_ms)


def ensure_shadow_schema(conn: sqlite3.Connection) -> None:
    """internal_state_v3/events + 评分水位 ledger。"""
    store.ensure_schema(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCORE_APPLIED_TABLE} (
            message_id INTEGER PRIMARY KEY
                CHECK (message_id > 0),
            applied_at TEXT NOT NULL,
            source TEXT NOT NULL
        )
        """
    )


def record_score_proof_in_txn(
    conn: sqlite3.Connection,
    message_id: int,
    *,
    applied_at: str,
    source: str,
) -> None:
    """在调用方事务内写入评分证明行；**不** COMMIT / ROLLBACK。

    安全门：
      - 必须 ``conn.in_transaction``（``open_store`` 为 autocommit，
        未 BEGIN 时单条 INSERT 会立刻提交，故强制拒绝）
      - 严格幂等：同 message_id + 同 applied_at/source → no-op
      - 同 message_id、不同内容 → ``StoreError``（禁止改写历史证据）

    #117 必须与 ``UPDATE emotion_state`` 处于同一 ``BEGIN…COMMIT``。
    """
    if not conn.in_transaction:
        raise store.StoreError(
            'record_score_proof_in_txn requires an active caller-owned '
            'transaction'
        )
    mid = store.require_positive_message_id(
        message_id, field='message_id',
    )
    applied_canon = events._canonicalize_ts(applied_at, field='applied_at')  # noqa: SLF001
    if not isinstance(source, str) or not source.strip() or len(source) > 64:
        raise store.StoreError(f'source must be a non-empty str <= 64: {source!r}')
    source_canon = source.strip()

    row = conn.execute(
        f"""
        SELECT applied_at, source FROM {SCORE_APPLIED_TABLE}
        WHERE message_id=?
        """,
        (mid,),
    ).fetchone()
    if row is not None:
        if isinstance(row, sqlite3.Row):
            prev_at, prev_src = row['applied_at'], row['source']
        else:
            prev_at, prev_src = row[0], row[1]
        if str(prev_at) == applied_canon and str(prev_src) == source_canon:
            return
        raise store.StoreError(
            f'score proof conflict for message_id={mid}: '
            f'existing=({prev_at!r}, {prev_src!r}) '
            f'new=({applied_canon!r}, {source_canon!r})'
        )

    conn.execute(
        f"""
        INSERT INTO {SCORE_APPLIED_TABLE} (message_id, applied_at, source)
        VALUES (?, ?, ?)
        """,
        (mid, applied_canon, source_canon),
    )


def _fetch_row_dict(conn: sqlite3.Connection, table: str) -> Optional[dict]:
    """同连接读单例行；表缺失 / 无行返回 None（不吞其它错误）。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if exists is None:
        return None
    row = conn.execute(f'SELECT * FROM {table} WHERE id=1').fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    cols = [d[0] for d in conn.execute(f'SELECT * FROM {table} LIMIT 0').description]
    return {cols[i]: row[i] for i in range(len(cols))}


def resolve_scored_watermark_row(conn: sqlite3.Connection) -> dict:
    """在调用方事务内读取水位行；fail closed。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (SCORE_APPLIED_TABLE,),
    ).fetchone()
    if exists is None:
        raise store.StoreError(
            f'scored watermark table {SCORE_APPLIED_TABLE!r} missing; '
            f'refusing bootstrap (fail closed)'
        )
    row = conn.execute(
        f"""
        SELECT message_id, applied_at, source
        FROM {SCORE_APPLIED_TABLE}
        ORDER BY message_id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise store.StoreError(
            f'scored watermark table {SCORE_APPLIED_TABLE!r} is empty; '
            f'refusing to invent 0/None (fail closed)'
        )
    if isinstance(row, sqlite3.Row):
        mid, applied_at, source = row['message_id'], row['applied_at'], row['source']
    else:
        mid, applied_at, source = row[0], row[1], row[2]
    mid = store.require_positive_message_id(int(mid), field='last_scored_message_id')
    applied_canon = events._canonicalize_ts(applied_at, field='watermark.applied_at')  # noqa: SLF001
    if not isinstance(source, str) or not str(source).strip():
        raise store.StoreError(f'watermark source invalid: {source!r}')
    return {
        'message_id': mid,
        'applied_at': applied_canon,
        'source': str(source).strip(),
    }


def resolve_scored_watermark(conn: sqlite3.Connection) -> int:
    """兼容：仅返回 MAX(message_id)。"""
    return int(resolve_scored_watermark_row(conn)['message_id'])


def _require_unit_interval(value: Any, *, field: str) -> float:
    """有限且落在 [0, 1]；禁止靠底层 clamp 洗白越界值。"""
    if type(value) is bool or type(value) not in (int, float):
        raise store.StoreError(
            f'bootstrap snapshot {field} missing or non-numeric: {value!r}'
        )
    v = float(value)
    if not math.isfinite(v):
        raise store.StoreError(
            f'bootstrap snapshot {field} is not finite: {value!r}'
        )
    if v < 0.0 or v > 1.0:
        raise store.StoreError(
            f'bootstrap snapshot {field} out of [0,1]: {value!r}'
        )
    return v


def validate_bootstrap_snapshot(snapshot: Any) -> None:
    """严格入口：任一必需值缺失 / 非有限 / 越界 / 时钟不可靠 → StoreError。

    禁止 bootstrap_from_snapshot 用默认值或 clamp 洗白。
    """
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

    observed = _g(snapshot, 'observed_at')
    if not isinstance(observed, str) or not observed.strip():
        raise store.StoreError(
            f'bootstrap observed_at missing/invalid: {observed!r}'
        )
    try:
        events._canonicalize_ts(observed, field='observed_at')  # noqa: SLF001
    except store.StoreError:
        raise
    except Exception as exc:
        raise store.StoreError(
            f'bootstrap observed_at unusable: {observed!r} ({exc})'
        ) from exc

    health = _g(snapshot, 'diagnostics', 'source_health') or {}
    if not health.get('clock_reliable'):
        reason = _g(snapshot, 'diagnostics', 'source_health', 'clock_reason')
        if reason is None:
            reason = _g(snapshot, 'diagnostics', 'source_timestamps')
        raise store.StoreError(
            f'bootstrap refuses unreliable interaction clock '
            f'(reason={reason!r})'
        )
    if not health.get('emotion_state'):
        raise store.StoreError(
            'bootstrap refuses missing/unreadable emotion_state'
        )
    if not health.get('drive_state'):
        raise store.StoreError(
            'bootstrap refuses missing/unreadable drive_state'
        )

    for field in _AFFECT_FIELDS:
        _require_unit_interval(_g(snapshot, 'affect', field), field=f'affect.{field}')
    mood = _g(snapshot, 'affect', 'mood_word')
    if not isinstance(mood, str) or not mood.strip():
        raise store.StoreError(
            f'bootstrap affect.mood_word missing/invalid: {mood!r}'
        )

    for field in _BOND_FIELDS:
        _require_unit_interval(_g(snapshot, 'bond', field), field=f'bond.{field}')

    drives = (
        _g(snapshot, 'candidate_unified_drives')
        or _g(snapshot, 'drives')
    )
    for field in _DRIVE_FIELDS:
        _require_unit_interval(_g(drives, field), field=f'drives.{field}')

    warnings = _g(snapshot, 'diagnostics', 'warnings') or ()
    for w in warnings:
        text = str(w)
        if 'missing' in text or 'unreliable' in text:
            raise store.StoreError(
                f'bootstrap refuses snapshot warning: {text}'
            )


def _read_legacy_bundle_on_conn(
    conn: sqlite3.Connection,
    *,
    observed_dt: datetime.datetime,
) -> BootstrapBundle:
    """在调用方已打开的事务内读取一致截面并严格校验。"""
    wm = resolve_scored_watermark_row(conn)
    clock = read_interaction_clock_from_conn(conn, now=observed_dt)
    emotion_row = _fetch_row_dict(conn, 'emotion_state')
    drive_row = _fetch_row_dict(conn, 'drive_state')
    desire_row = _fetch_row_dict(conn, 'desire_state')

    if emotion_row is None:
        raise store.StoreError(
            'bootstrap refuses missing/unreadable emotion_state'
        )
    if drive_row is None:
        raise store.StoreError(
            'bootstrap refuses missing/unreadable drive_state'
        )
    if not clock.reliable:
        raise store.StoreError(
            f'bootstrap refuses unreliable interaction clock '
            f'(reason={clock.reason!r})'
        )

    snap = isv3.compute_snapshot(
        emotion_row, drive_row, desire_row, clock, observed_dt,
        legacy_readings=None,
    )
    validate_bootstrap_snapshot(snap)
    return BootstrapBundle(
        snapshot=snap,
        watermark=int(wm['message_id']),
        watermark_applied_at=str(wm['applied_at']),
        watermark_row_source=str(wm['source']),
        observed_at=snap.observed_at,
    )


def capture_bootstrap_bundle(
    db_path: Optional[str] = None,
    *,
    now: Optional[datetime.datetime] = None,
) -> BootstrapBundle:
    """诊断：只读事务采集截面。生产 bootstrap **不**经此两阶段落地。"""
    path = isv3.memories_db_path(db_path)
    observed_dt = (now or _now_beijing_dt()).replace(microsecond=0)

    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN')
        try:
            return _read_legacy_bundle_on_conn(conn, observed_dt=observed_dt)
        finally:
            try:
                conn.execute('COMMIT')
            except sqlite3.Error:
                pass
    finally:
        conn.close()


def _structural_bootstrap_present(
    state: Optional[Mapping[str, Any]],
    event: Optional[Mapping[str, Any]],
) -> bool:
    if state is None or event is None:
        return False
    return (
        event.get('event_type') == 'bootstrap'
        and event.get('status') == 'applied'
    )


def _bootstrap_provenance_ok(
    state: Mapping[str, Any], event: Mapping[str, Any],
) -> bool:
    """正式出生证明；测试注入不得通过。"""
    if not _structural_bootstrap_present(state, event):
        return False
    if event.get('source_id') != PRODUCTION_BOOTSTRAP_SOURCE_ID:
        return False
    try:
        payload = json.loads(event['payload_json'])
    except Exception:
        return False
    if payload.get('capture_mode') != CAPTURE_MODE_PRODUCTION:
        return False
    proof = payload.get('watermark_proof')
    if not isinstance(proof, Mapping):
        return False
    if proof.get('resolver') != WATERMARK_SOURCE:
        return False
    try:
        mid = int(proof['message_id'])
        wm_state = int(state['last_scored_message_id'])
    except Exception:
        return False
    if mid != wm_state:
        return False
    if payload.get('last_scored_message_id') != mid:
        return False
    try:
        events._canonicalize_ts(  # noqa: SLF001
            proof.get('applied_at'), field='watermark_proof.applied_at',
        )
    except Exception:
        return False
    row_source = proof.get('row_source')
    if not isinstance(row_source, str) or not row_source.strip() or len(row_source) > 64:
        return False
    return True


def is_bootstrapped(conn: sqlite3.Connection) -> bool:
    """结构意义：state + applied bootstrap 事件同时存在（供 wrapper 边界）。

    生产启用前另查 ``get_shadow_health().provenance_ok``。
    """
    state = store.read_state(conn)
    event = store.read_event(conn, BOOTSTRAP_EVENT_KEY)
    return _structural_bootstrap_present(state, event)


def get_shadow_health(
    db_path: Optional[str] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> ShadowHealth:
    """结构化健康结果；开关关闭时零 DB 操作。"""
    enabled = is_shadow_enabled(environ=environ)
    with _lock:
        err = _last_error
        err_at = _last_error_at
        status = _last_status
    if not enabled:
        return ShadowHealth(
            enabled=False,
            bootstrapped=False,
            state_version=None,
            last_scored_message_id=None,
            last_error=err,
            last_error_at=err_at,
            last_status=status,
            journal_mode=None,
            provenance_ok=None,
        )

    conn = None
    try:
        conn = open_shadow_connection(db_path)
        journal = store.get_journal_mode(conn)
        state = store.read_state(conn)
        event = store.read_event(conn, BOOTSTRAP_EVENT_KEY)
        structural = _structural_bootstrap_present(state, event)
        prov = (
            _bootstrap_provenance_ok(state, event)
            if structural else False
        )
        return ShadowHealth(
            enabled=True,
            bootstrapped=structural,
            state_version=(
                int(state['state_version']) if state is not None else None
            ),
            last_scored_message_id=(
                state.get('last_scored_message_id') if state is not None else None
            ),
            last_error=err,
            last_error_at=err_at,
            last_status=status,
            journal_mode=journal,
            provenance_ok=prov,
        )
    except Exception as exc:  # noqa: BLE001
        _record_error(f'get_shadow_health: {exc}')
        return ShadowHealth(
            enabled=True,
            bootstrapped=False,
            state_version=None,
            last_scored_message_id=None,
            last_error=str(exc),
            last_error_at=_last_error_at,
            last_status='failed',
            journal_mode=None,
            provenance_ok=False,
        )
    finally:
        if conn is not None:
            conn.close()


def ensure_bootstrapped(
    db_path: Optional[str] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> ShadowResult:
    """生产两阶段启用入口；事件 wrapper **不会**调用本函数。

    在同一 ``BEGIN IMMEDIATE`` 内：
      读 watermark + clock + legacy 三表 → 严格校验 → 写 v3 + bootstrap event。

    旧 writer 在此极短窗口外排队，消除「截面已过期却仍落地」的 cutover 缝隙。
    """
    if not is_shadow_enabled(environ=environ):
        return ShadowResult(ok=True, status='disabled')

    t0 = time.monotonic()
    conn = None
    try:
        path = isv3.memories_db_path(db_path)
        probe = store.open_store(path)
        try:
            before_journal = store.get_journal_mode(probe)
        finally:
            probe.close()

        conn = open_shadow_connection(path)
        ensure_shadow_schema(conn)

        if is_bootstrapped(conn):
            # 已结构 bootstrap：若正式 provenance 齐全则 already；否则仍算已落地
            _record_status('already_bootstrapped')
            _clear_error()
            after = store.get_journal_mode(conn)
            if after != before_journal:
                raise store.StoreError(
                    f'journal_mode changed: {before_journal!r} -> {after!r}'
                )
            return ShadowResult(
                ok=True,
                status='already_bootstrapped',
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
            )

        observed_dt = _now_beijing_dt().replace(microsecond=0)
        conn.execute('BEGIN IMMEDIATE')
        try:
            if is_bootstrapped(conn):
                conn.execute('COMMIT')
                _record_status('already_bootstrapped')
                _clear_error()
                return ShadowResult(
                    ok=True,
                    status='already_bootstrapped',
                    elapsed_ms=(time.monotonic() - t0) * 1000.0,
                )

            bundle = _read_legacy_bundle_on_conn(conn, observed_dt=observed_dt)
            proof = {
                'resolver': WATERMARK_SOURCE,
                'message_id': bundle.watermark,
                'applied_at': bundle.watermark_applied_at,
                'row_source': bundle.watermark_row_source,
            }
            result = store.bootstrap_from_snapshot(
                conn,
                bundle.snapshot,
                last_scored_message_id=bundle.watermark,
                last_scored_message_id_source=WATERMARK_SOURCE,
                watermark_proof=proof,
                capture_mode=CAPTURE_MODE_PRODUCTION,
                event_key=BOOTSTRAP_EVENT_KEY,
                source_id=PRODUCTION_BOOTSTRAP_SOURCE_ID,
                manage_transaction=False,
            )
            if result.status not in ('applied', 'duplicate'):
                conn.execute('ROLLBACK')
                msg = result.error or result.status
                _record_error(f'ensure_bootstrapped: {msg}')
                return ShadowResult(
                    ok=False,
                    status=result.status,
                    error=msg,
                    elapsed_ms=(time.monotonic() - t0) * 1000.0,
                    apply=result,
                )
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except Exception:
                pass
            raise

        after = store.get_journal_mode(conn)
        if after != before_journal:
            raise store.StoreError(
                f'journal_mode changed: {before_journal!r} -> {after!r}'
            )

        _clear_error()
        _record_status(result.status)
        return ShadowResult(
            ok=True,
            status=result.status,
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
            apply=result,
        )
    except Exception as exc:  # noqa: BLE001
        _record_error(f'ensure_bootstrapped: {exc}')
        return ShadowResult(
            ok=False,
            status='failed',
            error=str(exc),
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )
    finally:
        if conn is not None:
            conn.close()


def _ensure_bootstrapped_for_test(
    db_path: Optional[str] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    snapshot: Any,
    last_scored_message_id: int,
    last_scored_message_id_source: str = CAPTURE_MODE_TEST,
) -> ShadowResult:
    """测试专用注入；``capture_mode=test_injection``，``provenance_ok`` 必为 False。"""
    if not is_shadow_enabled(environ=environ):
        return ShadowResult(ok=True, status='disabled')

    t0 = time.monotonic()
    conn = None
    try:
        path = isv3.memories_db_path(db_path)
        conn = open_shadow_connection(path)
        ensure_shadow_schema(conn)
        if is_bootstrapped(conn):
            _record_status('already_bootstrapped')
            return ShadowResult(
                ok=True,
                status='already_bootstrapped',
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
            )
        validate_bootstrap_snapshot(snapshot)
        watermark = store.require_positive_message_id(
            last_scored_message_id, field='last_scored_message_id',
        )
        observed = getattr(snapshot, 'observed_at', None) or _now_beijing()
        # 测试注入故意使用非生产 source_id / capture_mode / resolver
        result = store.bootstrap_from_snapshot(
            conn,
            snapshot,
            last_scored_message_id=watermark,
            last_scored_message_id_source=last_scored_message_id_source,
            watermark_proof={
                'resolver': 'test_injection',
                'message_id': watermark,
                'applied_at': str(observed),
                'row_source': 'unit_test',
            },
            capture_mode=CAPTURE_MODE_TEST,
            event_key=BOOTSTRAP_EVENT_KEY,
            source_id='phase1a4a_shadow_test',
        )
        if result.status in ('applied', 'duplicate'):
            _clear_error()
            _record_status(result.status)
            return ShadowResult(
                ok=True,
                status=result.status,
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
                apply=result,
            )
        msg = result.error or result.status
        _record_error(f'_ensure_bootstrapped_for_test: {msg}')
        return ShadowResult(
            ok=False,
            status=result.status,
            error=msg,
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
            apply=result,
        )
    except Exception as exc:  # noqa: BLE001
        _record_error(f'_ensure_bootstrapped_for_test: {exc}')
        return ShadowResult(
            ok=False,
            status='failed',
            error=str(exc),
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )
    finally:
        if conn is not None:
            conn.close()


def _shadow_call(
    name: str,
    make_runner: Callable[
        [Optional[int]], Callable[[sqlite3.Connection], store.ApplyResult]
    ],
    *,
    db_path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    max_version_retries: int = _DEFAULT_VERSION_RETRIES,
) -> ShadowResult:
    """方案 A：开关开启后若尚未 bootstrap → ``bootstrap_required``，绝不临时 bootstrap。"""
    if not is_shadow_enabled(environ=environ):
        return ShadowResult(ok=True, status='disabled')

    t0 = time.monotonic()
    conn = None
    try:
        conn = open_shadow_connection(db_path)
        if not is_bootstrapped(conn):
            _record_status('bootstrap_required')
            return ShadowResult(
                ok=False,
                status='bootstrap_required',
                error=(
                    'shadow enabled but not bootstrapped; '
                    'call ensure_bootstrapped explicitly (phase A)'
                ),
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
            )

        last: Optional[store.ApplyResult] = None
        for _attempt in range(max_version_retries + 1):
            st = store.read_state(conn)
            if st is None:
                raise store.StoreError('state missing')
            expected = int(st['state_version'])
            last = make_runner(expected)(conn)
            if last.status != 'version_conflict':
                _record_status(last.status)
                if last.status in ('applied', 'duplicate', 'stale_skipped'):
                    _clear_error()
                elif last.status in ('idempotency_conflict', 'failed'):
                    _record_error(f'{name}: {last.error or last.status}')
                return ShadowResult(
                    ok=last.status in (
                        'applied', 'duplicate', 'stale_skipped',
                    ),
                    status=last.status,
                    error=last.error,
                    elapsed_ms=(time.monotonic() - t0) * 1000.0,
                    apply=last,
                )
        _record_error(
            f'{name}: version_conflict exhausted after '
            f'{max_version_retries} retries'
        )
        return ShadowResult(
            ok=False,
            status='version_conflict',
            error=last.error if last else 'version_conflict',
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
            apply=last,
        )
    except Exception as exc:  # noqa: BLE001
        _record_error(f'{name}: {exc}')
        return ShadowResult(
            ok=False,
            status='failed',
            error=str(exc),
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )
    finally:
        if conn is not None:
            conn.close()


def observe_user_message_shadow(
    *,
    message_id: int,
    text: str,
    created_at: str,
    previous_user_at: Optional[str],
    db_path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> ShadowResult:
    """Shadow ``user_rule``。绝不抛向主流程；disabled 时不触碰参数。"""
    try:
        if not is_shadow_enabled(environ=environ):
            return ShadowResult(ok=True, status='disabled')
        mid, txt, created, prev = message_id, text, created_at, previous_user_at

        def make_runner(expected: Optional[int]):
            def runner(conn: sqlite3.Connection) -> store.ApplyResult:
                return events.observe_user_message(
                    conn,
                    message_id=mid,
                    text=txt,
                    created_at=created,
                    previous_user_at=prev,
                    expected_state_version=expected,
                )
            return runner

        return _shadow_call(
            'observe_user_message_shadow', make_runner,
            db_path=db_path, environ=environ,
        )
    except Exception as exc:  # noqa: BLE001
        _record_error(f'observe_user_message_shadow: {exc}')
        return ShadowResult(ok=False, status='failed', error=str(exc))


def observe_scored_shadow(
    *,
    message_id: int,
    scores: dict,
    scored_at: str,
    db_path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> ShadowResult:
    """Shadow ``user_scored``。disabled 时不得 ``dict(scores)``。"""
    try:
        if not is_shadow_enabled(environ=environ):
            return ShadowResult(ok=True, status='disabled')
        mid = message_id
        sc = dict(scores)
        at = scored_at

        def make_runner(expected: Optional[int]):
            def runner(conn: sqlite3.Connection) -> store.ApplyResult:
                return events.observe_scored(
                    conn,
                    message_id=mid,
                    scores=sc,
                    scored_at=at,
                    expected_state_version=expected,
                )
            return runner

        return _shadow_call(
            'observe_scored_shadow', make_runner,
            db_path=db_path, environ=environ,
        )
    except Exception as exc:  # noqa: BLE001
        _record_error(f'observe_scored_shadow: {exc}')
        return ShadowResult(ok=False, status='failed', error=str(exc))


def apply_outcome_shadow(
    *,
    wake_run_id: str,
    executor_action: str,
    desire_action: Optional[str],
    fired_drive: Optional[str],
    desire_driven: bool,
    user_idle_hours: float,
    outcome_at: str,
    db_path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> ShadowResult:
    """Shadow ``wake_outcome``。绝不抛向主流程。"""
    try:
        if not is_shadow_enabled(environ=environ):
            return ShadowResult(ok=True, status='disabled')
        frozen = dict(
            wake_run_id=wake_run_id,
            executor_action=executor_action,
            desire_action=desire_action,
            fired_drive=fired_drive,
            desire_driven=desire_driven,
            user_idle_hours=user_idle_hours,
            outcome_at=outcome_at,
        )

        def make_runner(expected: Optional[int]):
            def runner(conn: sqlite3.Connection) -> store.ApplyResult:
                return events.apply_outcome(
                    conn,
                    expected_state_version=expected,
                    **frozen,
                )
            return runner

        return _shadow_call(
            'apply_outcome_shadow', make_runner,
            db_path=db_path, environ=environ,
        )
    except Exception as exc:  # noqa: BLE001
        _record_error(f'apply_outcome_shadow: {exc}')
        return ShadowResult(ok=False, status='failed', error=str(exc))


__all__ = [
    'BOOTSTRAP_EVENT_KEY',
    'CAPTURE_MODE_PRODUCTION',
    'CAPTURE_MODE_TEST',
    'PRODUCTION_BOOTSTRAP_SOURCE_ID',
    'SCORE_APPLIED_TABLE',
    'SHADOW_ENABLED_ENV',
    'WATERMARK_SOURCE',
    'BootstrapBundle',
    'ShadowHealth',
    'ShadowResult',
    'apply_outcome_shadow',
    'capture_bootstrap_bundle',
    'ensure_bootstrapped',
    'ensure_shadow_schema',
    'get_shadow_health',
    'is_bootstrapped',
    'is_shadow_enabled',
    'observe_scored_shadow',
    'observe_user_message_shadow',
    'open_shadow_connection',
    'record_score_proof_in_txn',
    'resolve_scored_watermark',
    'resolve_scored_watermark_row',
    'validate_bootstrap_snapshot',
    '_ensure_bootstrapped_for_test',
]
