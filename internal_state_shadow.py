"""Internal State v3 — Phase 1A-4a Shadow 基础设施（默认关闭）

职责：
  - ``INTERNAL_STATE_V3_SHADOW_ENABLED`` 安全门（默认 off）
  - 独立短连接 + busy_timeout；**不**改 journal_mode
  - 评分水位 ledger（证明旧异步评分已落入权威状态）
  - 生产 bootstrap（显式 Phase 0 snapshot + 可靠 watermark）
  - 统一 Shadow adapter 包装（本 PR 无生产调用点）

严格不做：
  - 不接 gateway / chat / SSE / Wake / Prompt
  - 不停止旧 discharge / satisfy / score_async
  - 不 import emotion_engine / drive_engine / desire / gateway / wake
  - 不启用开关部署
"""

from __future__ import annotations

import datetime
import logging
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Optional

import internal_state as isv3
import internal_state_events as events
import internal_state_store as store

logger = logging.getLogger(__name__)

SHADOW_ENABLED_ENV = 'INTERNAL_STATE_V3_SHADOW_ENABLED'
BOOTSTRAP_EVENT_KEY = 'bootstrap:initial'
SCORE_APPLIED_TABLE = 'internal_state_score_applied'
WATERMARK_SOURCE = 'internal_state_score_applied:max(message_id)'
_DEFAULT_VERSION_RETRIES = 2

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

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ShadowResult:
    """adapter 对外结果；永不抛到主流程。"""
    ok: bool
    status: str
    error: Optional[str] = None
    elapsed_ms: Optional[float] = None
    apply: Optional[store.ApplyResult] = None


def _now_beijing() -> str:
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime(
        '%Y-%m-%d %H:%M:%S'
    )


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


def resolve_scored_watermark(conn: sqlite3.Connection) -> int:
    """从评分水位 ledger 读取已成功落入旧权威的最大 message_id。

    fail closed：表缺失 / 表空 / 非法值。禁止用 chat_messages 最大 id 冒充。
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (SCORE_APPLIED_TABLE,),
    ).fetchone()
    if row is None:
        raise store.StoreError(
            f'scored watermark table {SCORE_APPLIED_TABLE!r} missing; '
            f'refusing bootstrap (fail closed)'
        )
    max_row = conn.execute(
        f'SELECT MAX(message_id) AS m FROM {SCORE_APPLIED_TABLE}'
    ).fetchone()
    raw = max_row[0] if max_row is not None else None
    if raw is None:
        raise store.StoreError(
            f'scored watermark table {SCORE_APPLIED_TABLE!r} is empty; '
            f'refusing to invent 0/None (fail closed)'
        )
    return store.require_positive_message_id(
        int(raw), field='last_scored_message_id',
    )


def is_bootstrapped(conn: sqlite3.Connection) -> bool:
    state = store.read_state(conn)
    event = store.read_event(conn, BOOTSTRAP_EVENT_KEY)
    return state is not None and event is not None


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
        )

    conn = None
    try:
        conn = open_shadow_connection(db_path)
        journal = store.get_journal_mode(conn)
        state = store.read_state(conn)
        event = store.read_event(conn, BOOTSTRAP_EVENT_KEY)
        bootstrapped = state is not None and event is not None
        return ShadowHealth(
            enabled=True,
            bootstrapped=bootstrapped,
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
        )
    finally:
        if conn is not None:
            conn.close()


def ensure_bootstrapped(
    db_path: Optional[str] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    get_db_fn: Optional[Callable[[], sqlite3.Connection]] = None,
    snapshot: Any = None,
) -> ShadowResult:
    """开关开启时确保 schema + bootstrap；关闭时零操作。

    watermark 必须来自 ``internal_state_score_applied``；不可靠则 fail closed。
    ``snapshot`` 仅供测试注入；生产路径使用 ``capture_shadow_snapshot``。
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

        watermark = resolve_scored_watermark(conn)

        if snapshot is None:
            def _ro_db():
                if get_db_fn is not None:
                    return get_db_fn()
                c = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
                c.row_factory = sqlite3.Row
                return c

            snap = isv3.capture_shadow_snapshot(
                _ro_db,
                include_legacy=False,
                db_path=path,
            )
        else:
            snap = snapshot

        result = store.bootstrap_from_snapshot(
            conn,
            snap,
            last_scored_message_id=watermark,
            last_scored_message_id_source=WATERMARK_SOURCE,
            event_key=BOOTSTRAP_EVENT_KEY,
            source_id='phase1a4a_shadow',
        )
        after = store.get_journal_mode(conn)
        if after != before_journal:
            raise store.StoreError(
                f'journal_mode changed: {before_journal!r} -> {after!r}'
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
        _record_error(f'ensure_bootstrapped: {msg}')
        return ShadowResult(
            ok=False,
            status=result.status,
            error=msg,
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
    """统一安全包装：开关 → bootstrap → 有限 version 重试 → 捕异常。"""
    if not is_shadow_enabled(environ=environ):
        return ShadowResult(ok=True, status='disabled')

    t0 = time.monotonic()
    boot = ensure_bootstrapped(db_path=db_path, environ=environ)
    if not boot.ok:
        return ShadowResult(
            ok=False,
            status='bootstrap_failed',
            error=boot.error,
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )

    conn = None
    try:
        conn = open_shadow_connection(db_path)
        if not is_bootstrapped(conn):
            raise store.StoreError('shadow not bootstrapped; refusing event')

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
    """Shadow ``user_rule``；输入须在进入前冻结。绝不抛向主流程。"""
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


def observe_scored_shadow(
    *,
    message_id: int,
    scores: dict,
    scored_at: str,
    db_path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> ShadowResult:
    """Shadow ``user_scored``；scores/scored_at 冻结后重试。"""
    mid, sc, at = message_id, dict(scores), scored_at

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
    """Shadow ``wake_outcome``；idle/outcome_at 冻结后重试。"""
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


__all__ = [
    'BOOTSTRAP_EVENT_KEY',
    'SCORE_APPLIED_TABLE',
    'SHADOW_ENABLED_ENV',
    'WATERMARK_SOURCE',
    'ShadowHealth',
    'ShadowResult',
    'apply_outcome_shadow',
    'ensure_bootstrapped',
    'ensure_shadow_schema',
    'get_shadow_health',
    'is_bootstrapped',
    'is_shadow_enabled',
    'observe_scored_shadow',
    'observe_user_message_shadow',
    'open_shadow_connection',
    'resolve_scored_watermark',
]
