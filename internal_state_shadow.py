"""Internal State v3 — Phase 1A-4a/4b Shadow 基础设施与分阶段开关

职责：
  - 三道独立开关（默认全部 off）：
      ``INTERNAL_STATE_V3_SHADOW_ENABLED``
      ``INTERNAL_STATE_V3_SCORE_PROOF_ENABLED``
      ``INTERNAL_STATE_V3_USER_EVENTS_ENABLED``
  - 独立短连接 + busy_timeout；**不**改 journal_mode
  - 评分水位 ledger + 同事务 proof writer 辅助（强制 in_transaction）
  - proof readiness / gap 持久标记（bootstrap fail-closed）
  - 生产 bootstrap：同一 ``BEGIN IMMEDIATE`` 内采集 + 落地（线性化）
  - 统一 Shadow adapter（方案 A：事件路径不自动 bootstrap）

严格不做：
  - 不接 wake_outcome 生产入口（已迁至 gateway；默认 SHADOW=0 无操作）
  - 不修改 Prompt / Relationship Context
  - 不停止旧 discharge / satisfy（shadow 失败不得阻断聊天）
  - 不 import emotion_engine / drive_engine / desire / gateway / wake
  - 评分事务内禁止 DDL / ensure_schema

可靠边界（1A-4b 复审）：
  - user_rule / user_scored 经 durable outbox 与权威写入同事务入队，
    commit 后 drain；失败可重放；v3 event_key 幂等去重
  - proof gap：health 表可写则同事务标记；表缺失时写 sidecar，
    prepare-schema 迁入，禁止被初始化洗白
  - USER_EVENTS 必须三者同开（拒绝 SCORE_PROOF=0 的半套事件史）
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
import uuid
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import internal_state as isv3
import internal_state_events as events
import internal_state_store as store
from chat.interaction_state import read_interaction_clock_from_conn

logger = logging.getLogger(__name__)

SHADOW_ENABLED_ENV = 'INTERNAL_STATE_V3_SHADOW_ENABLED'
SCORE_PROOF_ENABLED_ENV = 'INTERNAL_STATE_V3_SCORE_PROOF_ENABLED'
USER_EVENTS_ENABLED_ENV = 'INTERNAL_STATE_V3_USER_EVENTS_ENABLED'
CAPTURE_ALERT_PATH_ENV = 'INTERNAL_STATE_V3_CAPTURE_ALERT_PATH'
BOOTSTRAP_EVENT_KEY = 'bootstrap:initial'
SCORE_APPLIED_TABLE = 'internal_state_score_applied'
PROOF_HEALTH_TABLE = 'internal_state_score_proof_health'
OUTBOX_TABLE = 'internal_state_shadow_outbox'
OUTBOX_QUEUE_MIG_TABLE = f'{OUTBOX_TABLE}_queue_mig'
GAP_INCIDENTS_TABLE = 'internal_state_shadow_gap_incidents'
GAP_ACK_TABLE = 'internal_state_shadow_gap_ack'
WATERMARK_SOURCE = 'internal_state_score_applied:max(message_id)'
PRODUCTION_BOOTSTRAP_SOURCE_ID = 'phase1a4a_shadow'
PROOF_SOURCE_SCORE_AND_UPDATE = 'emotion_engine.score_and_update'
CAPTURE_MODE_PRODUCTION = 'same_sqlite_snapshot_v1'
CAPTURE_MODE_TEST = 'test_injection'
PROOF_GAP_SIDECAR_SUFFIX = '.shadow_proof_gap.jsonl'  # legacy JSONL
PROOF_GAP_SIDECAR_LEGACY_SUFFIX = '.shadow_proof_gap.json'  # legacy single-slot
PROOF_GAP_INCIDENTS_DIR_SUFFIX = '.shadow_gap_incidents'  # one incident per file
OUTBOX_SIDECAR_SUFFIX = '.shadow_outbox.jsonl'  # legacy only; hot path removed
QUARANTINE_RECONCILE_TABLE = 'internal_state_shadow_quarantine_reconcile'
QUARANTINE_INTENT_TABLE = 'internal_state_shadow_quarantine_reconcile_intents'
PENDING_INCIDENT_ACTION_TABLE = 'internal_state_shadow_pending_incident_actions'
PENDING_INCIDENT_INTENT_TABLE = 'internal_state_shadow_pending_incident_intents'
CAPTURE_ALERT_ACK_TABLE = 'internal_state_shadow_capture_alert_ack'
CAPTURE_ALERT_INTENT_TABLE = 'internal_state_shadow_capture_alert_intents'
EVENT_TYPE_USER_RULE = 'user_rule'
EVENT_TYPE_USER_SCORED = 'user_scored'
_SCORE_HASH_FIELDS = (
    'valence', 'arousal', 'mood_word',
    'passion_delta', 'intimacy_delta', 'source',
)
_DEFAULT_VERSION_RETRIES = 2
_DEFAULT_DRAIN_LIMIT = 32

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
_capture_evidence_failures = 0


@dataclass(frozen=True)
class GapPersistenceResult:
    """独立 gap 持久化结果；调用方不得把失败误当成已落账。"""
    status: str
    ledger_recorded: bool = False
    sidecar_recorded: bool = False
    alert_recorded: bool = False
    error: Optional[str] = None


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
    score_proof_enabled: bool = False
    user_events_enabled: bool = False
    proof_gap: Optional[bool] = None
    proof_schema_ready: Optional[bool] = None
    proof_max_message_id: Optional[int] = None
    watermark_lag: Optional[bool] = None
    outbox_pending: Optional[int] = None
    outbox_schema_ready: Optional[bool] = None
    gap_incidents_unresolved: Optional[int] = None
    gap_sidecar_pending: Optional[int] = None
    quarantine_pending: Optional[int] = None
    capture_evidence_failures: Optional[int] = None
    capture_alert_pending: Optional[bool] = None
    capture_alert_intents_pending: Optional[int] = None
    pending_incident_intents_pending: Optional[int] = None
    quarantine_intents_pending: Optional[int] = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProofHealth:
    ready: bool
    gap_detected: bool
    failed_message_id: Optional[int] = None
    error_code: Optional[str] = None
    failed_at: Optional[str] = None

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


def _env_flag(name: str, *, environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(name, '0')).strip() == '1'


def is_shadow_enabled(*, environ: Optional[Mapping[str, str]] = None) -> bool:
    """默认关闭。仅 ``'1'`` 开启；不读 DB。"""
    return _env_flag(SHADOW_ENABLED_ENV, environ=environ)


def is_score_proof_enabled(*, environ: Optional[Mapping[str, str]] = None) -> bool:
    """proof-only 钥匙：旧评分成功事务中写证明行。不自动 bootstrap / 不写事件。"""
    return _env_flag(SCORE_PROOF_ENABLED_ENV, environ=environ)


def is_user_events_enabled(*, environ: Optional[Mapping[str, str]] = None) -> bool:
    """用户事件钥匙：须 SHADOW + SCORE_PROOF + USER_EVENTS 三者同开。

    拒绝 ``SCORE_PROOF=0, SHADOW=1, USER_EVENTS=1`` 的半套事件史
    （只有 user_rule、永远没有 user_scored）。
    """
    return (
        _env_flag(USER_EVENTS_ENABLED_ENV, environ=environ)
        and is_shadow_enabled(environ=environ)
        and is_score_proof_enabled(environ=environ)
    )


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


def _conn_file_path(conn: sqlite3.Connection) -> Optional[str]:
    try:
        row = conn.execute('PRAGMA database_list').fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        path = row['file'] if 'file' in row.keys() else row[2]
    else:
        path = row[2]
    if not path:
        return None
    return str(path)


def proof_gap_sidecar_path(db_path: Optional[str] = None) -> str:
    """Legacy JSONL path（仍会 migrate；热路径不再写入）。"""
    return isv3.memories_db_path(db_path) + PROOF_GAP_SIDECAR_SUFFIX


def proof_gap_sidecar_legacy_path(db_path: Optional[str] = None) -> str:
    return isv3.memories_db_path(db_path) + PROOF_GAP_SIDECAR_LEGACY_SUFFIX


def gap_incidents_dir(db_path: Optional[str] = None) -> Path:
    """一 incident 一文件目录：``{db}.shadow_gap_incidents/<uuid>.json``。"""
    return Path(isv3.memories_db_path(db_path) + PROOF_GAP_INCIDENTS_DIR_SUFFIX)


def outbox_sidecar_path(db_path: Optional[str] = None) -> str:
    """Legacy path only — USER_EVENTS 热路径不再写 outbox sidecar。"""
    return isv3.memories_db_path(db_path) + OUTBOX_SIDECAR_SUFFIX


def canonical_payload_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def compute_payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload_json(payload).encode('utf-8')).hexdigest()


def compute_score_hash(scores: Mapping[str, Any]) -> str:
    """冻结 scores 的稳定哈希；用作 proof / outbox 幂等键的一部分。"""
    if not isinstance(scores, Mapping):
        raise store.StoreError('scores must be a mapping')
    subset = {k: scores.get(k) for k in _SCORE_HASH_FIELDS}
    return compute_payload_hash(subset)


def _normalize_gap_mid(failed_message_id: Optional[int]) -> Optional[int]:
    if failed_message_id is None:
        return None
    try:
        return store.require_positive_message_id(
            failed_message_id, field='failed_message_id',
        )
    except store.StoreError:
        return None


def _fsync_dir(dir_path: Path) -> None:
    dir_fd = os.open(str(dir_path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def append_gap_incident_sidecar(
    db_path: Optional[str],
    *,
    failed_message_id: Optional[int],
    error_code: str,
) -> None:
    """一 incident 一文件：tmp → fsync → rename → fsync dir。

    不旋转共享 JSONL，避免 “open 后 rename” 竞态丢证据。
    """
    if not isinstance(error_code, str) or not error_code.strip():
        raise store.StoreError(f'error_code invalid: {error_code!r}')
    mid = _normalize_gap_mid(failed_message_id)
    code = error_code.strip()[:64]
    d = gap_incidents_dir(db_path)
    d.mkdir(parents=True, exist_ok=True)
    uid = uuid.uuid4().hex
    tmp = d / f'{uid}.tmp'
    final = d / f'{uid}.json'
    row = {
        'gap_detected': True,
        'failed_message_id': mid,
        'error_code': code,
        'failed_at': _now_beijing(),
    }
    body = json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    os.rename(str(tmp), str(final))
    _fsync_dir(d)
    logger.critical(
        'internal_state_shadow: gap incident file written code=%s '
        'message_id=%s path=%s',
        code, mid, final,
    )


def write_proof_gap_sidecar(
    db_path: Optional[str],
    *,
    failed_message_id: Optional[int],
    error_code: str,
) -> None:
    """兼容别名：现为一 incident 一文件。"""
    append_gap_incident_sidecar(
        db_path,
        failed_message_id=failed_message_id,
        error_code=error_code,
    )


def count_gap_sidecar_pending(db_path: Optional[str] = None) -> int:
    """per-file incidents + legacy JSONL/JSON + in-flight processing。"""
    base = isv3.memories_db_path(db_path)
    n = 0
    d = gap_incidents_dir(base)
    if d.is_dir():
        n += sum(1 for p in d.glob('*.json') if p.is_file())
        n += sum(1 for p in d.glob('*.processing') if p.is_file())
        # 崩溃残留的 .tmp 也算未解决证据
        n += sum(1 for p in d.glob('*.tmp') if p.is_file())
    path = Path(base + PROOF_GAP_SIDECAR_SUFFIX)
    legacy = Path(base + PROOF_GAP_SIDECAR_LEGACY_SUFFIX)
    if path.is_file():
        try:
            n += sum(1 for line in path.read_text(encoding='utf-8').splitlines() if line.strip())
        except Exception:
            n += 1
    if legacy.is_file():
        n += 1
    parent = Path(base).parent
    stem = Path(base).name + PROOF_GAP_SIDECAR_SUFFIX
    for proc in parent.glob(stem + '.processing.*'):
        if proc.is_file():
            try:
                n += sum(1 for line in proc.read_text(encoding='utf-8').splitlines() if line.strip())
            except Exception:
                n += 1
    return n


def list_gap_quarantine_files(db_path: Optional[str] = None) -> list[str]:
    base = isv3.memories_db_path(db_path)
    parent = Path(base).parent
    name = Path(base).name
    patterns = [
        name + PROOF_GAP_SIDECAR_SUFFIX + '.quarantine.*',
        name + PROOF_GAP_SIDECAR_LEGACY_SUFFIX + '.quarantine.*',
        name + OUTBOX_SIDECAR_SUFFIX + '.quarantine.*',
    ]
    found: list[str] = []
    for pat in patterns:
        for p in sorted(parent.glob(pat)):
            if p.is_file() and '.resolved.' not in p.name:
                found.append(str(p))
    d = gap_incidents_dir(base)
    if d.is_dir():
        for p in sorted(d.glob('*.quarantine.*')):
            if p.is_file() and '.resolved.' not in p.name:
                found.append(str(p))
    return found


def count_quarantine_pending(db_path: Optional[str] = None) -> int:
    return len(list_gap_quarantine_files(db_path))


def capture_alert_path(db_path: Optional[str] = None) -> Optional[Path]:
    """独立告警信道路径。

    USER_EVENTS 生产部署必须设置此环境变量到独立于 memories DB / gap
    incident 目录的持久卷。未配置时 preflight 明确失败，不能把日志当证据。
    """
    raw = str(os.environ.get(CAPTURE_ALERT_PATH_ENV, '')).strip()
    return Path(raw) if raw else None


def capture_alert_configured() -> bool:
    return capture_alert_path() is not None


def capture_alert_preflight(db_path: Optional[str] = None) -> dict:
    """实际探测独立 capture-alert 卷，而非仅检查环境变量字符串。"""
    alert = capture_alert_path(db_path)
    if alert is None:
        return {'ok': False, 'reason': f'{CAPTURE_ALERT_PATH_ENV} is unset'}
    try:
        alert = alert.resolve()
        alert.parent.mkdir(parents=True, exist_ok=True)
        db_parent = Path(isv3.memories_db_path(db_path)).resolve().parent
        incidents = gap_incidents_dir(db_path).resolve()
        alert_dev = os.stat(alert.parent).st_dev
        if alert_dev == os.stat(db_parent).st_dev:
            return {'ok': False, 'reason': 'alert path shares DB filesystem'}
        if incidents.exists() and alert_dev == os.stat(incidents).st_dev:
            return {'ok': False, 'reason': 'alert path shares incident filesystem'}
        probe = Path(f'{alert}.probe.{uuid.uuid4().hex}')
        renamed = Path(f'{probe}.renamed')
        with open(probe, 'w', encoding='utf-8') as fh:
            fh.write('capture-alert-preflight\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(probe, renamed)
        _fsync_dir(alert.parent)
        renamed.unlink()
        _fsync_dir(alert.parent)
        return {'ok': True, 'path': str(alert), 'device': alert_dev}
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'reason': str(exc), 'path': str(alert)}


def has_capture_alert(db_path: Optional[str] = None) -> bool:
    path = capture_alert_path(db_path)
    return bool(
        path is not None and (
            path.is_file() or any(
                '.resolved.' not in p.name
                for p in path.parent.glob(path.name + '.processing.*')
            )
        )
    )


def note_capture_evidence_failure(
    detail: str = '',
    *,
    db_path: Optional[str] = None,
) -> bool:
    """写入独立、跨进程的 sticky alert；失败仍保留 critical 日志。

    这里故意不复用 gap incident 文件位置：DB + sidecar 同时不可写时，
    只有预配的独立 alert volume 能让 supervisor / 管理 CLI 看见红灯。
    """
    global _capture_evidence_failures
    with _lock:
        _capture_evidence_failures += 1
        n = _capture_evidence_failures
    alert = capture_alert_path(db_path)
    recorded = False
    if alert is not None:
        try:
            alert.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(f'{alert}.tmp.{uuid.uuid4().hex}')
            body = {
                'capture_evidence_failed': True,
                'count': n,
                'detail': str(detail)[:1024],
                'detected_at': _now_beijing(),
                'db_path': isv3.memories_db_path(db_path),
            }
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(body, fh, ensure_ascii=False, sort_keys=True)
                fh.write('\n')
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, alert)
            _fsync_dir(alert.parent)
            recorded = True
        except Exception as exc:  # noqa: BLE001
            logger.critical(
                'internal_state_shadow: independent capture alert write failed: %s',
                exc,
            )
    logger.critical(
        'internal_state_shadow: capture evidence persistence failed '
        '(count=%s alert_recorded=%s) %s',
        n, recorded, detail,
    )
    return recorded


def capture_evidence_failure_count() -> int:
    with _lock:
        return int(_capture_evidence_failures)


def ack_capture_alert(
    conn: sqlite3.Connection,
    *,
    sha256: str,
    reason: str,
    db_path: Optional[str] = None,
) -> dict:
    """capture alert prepared state machine：可在 claim/归档断电后恢复。"""
    if not isinstance(reason, str) or not reason.strip():
        raise store.StoreError('capture alert ack reason required')
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise store.StoreError('capture alert sha256 must be 64 hex chars')
    alert = capture_alert_path(db_path)
    if alert is None or not alert.is_file():
        raise store.StoreError('no pending configured capture alert')
    processing = Path(f'{alert}.processing.{uuid.uuid4().hex}')
    archive = Path(f'{processing}.resolved.{uuid.uuid4().hex}')
    # Claim before hash: concurrent writers publish a fresh canonical marker and
    # cannot replace the file whose identity this ack verifies.
    os.rename(str(alert), str(processing))
    _fsync_dir(processing.parent)
    actual = _sha256_file(processing)
    if actual.lower() != sha256.lower():
        raise store.StoreError(f'capture alert sha256 mismatch: {actual}')
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {CAPTURE_ALERT_ACK_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            reason TEXT NOT NULL,
            archive_path TEXT NOT NULL,
            acked_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {CAPTURE_ALERT_INTENT_TABLE} (
            intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_path TEXT NOT NULL,
            processing_path TEXT NOT NULL UNIQUE,
            archive_path TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL,
            reason TEXT NOT NULL,
            prepared_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    # Claim already happened. If power fails before this insert, recovery scans
    # orphan processing files and synthesizes a durable recovery intent.
    conn.execute('BEGIN IMMEDIATE')
    try:
        cur = conn.execute(
            f"""
            INSERT INTO {CAPTURE_ALERT_INTENT_TABLE}
                (canonical_path, processing_path, archive_path, sha256, reason, prepared_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(alert), str(processing), str(archive), actual.lower(),
             reason.strip()[:512], _now_beijing()),
        )
        intent_id = int(cur.lastrowid)
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise
    return _complete_capture_alert_intent(conn, intent_id=intent_id)


def _complete_capture_alert_intent(conn: sqlite3.Connection, *, intent_id: int) -> dict:
    row = conn.execute(
        f"""
        SELECT canonical_path, processing_path, archive_path, sha256, reason, completed_at
        FROM {CAPTURE_ALERT_INTENT_TABLE} WHERE intent_id=?
        """, (intent_id,),
    ).fetchone()
    if row is None:
        raise store.StoreError(f'unknown capture alert intent {intent_id}')
    get = lambda k, i: row[k] if isinstance(row, sqlite3.Row) else row[i]
    canonical, processing, archive = (Path(str(get('canonical_path', 0))),
                                      Path(str(get('processing_path', 1))),
                                      Path(str(get('archive_path', 2))))
    digest, reason_s, completed = str(get('sha256', 3)), str(get('reason', 4)), get('completed_at', 5)
    if completed is not None:
        return {'acked': True, 'intent_id': intent_id, 'idempotent': True}
    if processing.is_file() and not archive.exists():
        if _sha256_file(processing).lower() != digest.lower():
            raise store.StoreError(f'capture intent {intent_id} processing hash mismatch')
        os.rename(str(processing), str(archive))
        _fsync_dir(archive.parent)
    if not archive.is_file() or _sha256_file(archive).lower() != digest.lower():
        raise store.StoreError(f'capture intent {intent_id} archive missing/mismatched')
    conn.execute('BEGIN IMMEDIATE')
    try:
        cur = conn.execute(
            f"""
            INSERT INTO {CAPTURE_ALERT_ACK_TABLE}
                (path, sha256, reason, archive_path, acked_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(canonical), digest, reason_s, str(archive), _now_beijing()),
        )
        ack_id = int(cur.lastrowid)
        conn.execute(
            f'UPDATE {CAPTURE_ALERT_INTENT_TABLE} SET completed_at=? WHERE intent_id=?',
            (_now_beijing(), intent_id),
        )
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise
    return {
        'acked': True, 'ack_id': ack_id, 'intent_id': intent_id,
        'path': str(canonical), 'sha256': digest, 'archive_path': str(archive),
    }


def inspect_capture_alert_acks(conn: sqlite3.Connection) -> list[dict]:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {CAPTURE_ALERT_ACK_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL,
            sha256 TEXT NOT NULL, reason TEXT NOT NULL,
            archive_path TEXT NOT NULL, acked_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {CAPTURE_ALERT_INTENT_TABLE} (
            intent_id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_path TEXT NOT NULL,
            processing_path TEXT NOT NULL UNIQUE, archive_path TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL, reason TEXT NOT NULL, prepared_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    rows = conn.execute(
        f'SELECT * FROM {CAPTURE_ALERT_INTENT_TABLE} WHERE completed_at IS NULL'
    ).fetchall()
    return [dict(x) if isinstance(x, sqlite3.Row) else dict(enumerate(x)) for x in rows]


def recover_capture_alert_acks(conn: sqlite3.Connection) -> list[dict]:
    """接续 source/processing/archive 任一 prepared 状态；不触碰新 canonical。"""
    rows = inspect_capture_alert_acks(conn)
    out = []
    for row in rows:
        iid = int(row['intent_id'])
        if str(row['reason']).startswith('AWAITING_REVIEW:'):
            out.append({'intent_id': iid, 'status': 'awaiting_review'})
            continue
        canonical, processing = Path(row['canonical_path']), Path(row['processing_path'])
        digest = str(row['sha256'])
        if canonical.is_file() and not processing.exists():
            if _sha256_file(canonical).lower() != digest.lower():
                out.append({'intent_id': iid, 'status': 'canonical_replaced'})
                continue
            os.rename(str(canonical), str(processing))
            _fsync_dir(processing.parent)
        out.append(_complete_capture_alert_intent(conn, intent_id=iid))
    # Claim-first crash before intent: adopt orphan processing as *awaiting
    # operator review*. Recovery must never auto-ack a marker that may be a
    # hash-mismatch claim of a newer alert.
    alert = capture_alert_path()
    if alert is not None:
        known = {x['processing_path'] for x in inspect_capture_alert_acks(conn)}
        for proc in alert.parent.glob(alert.name + '.processing.*'):
            if str(proc) in known or '.resolved.' in proc.name or not proc.is_file():
                continue
            digest = _sha256_file(proc)
            archive = Path(f'{proc}.resolved.{uuid.uuid4().hex}')
            conn.execute('BEGIN IMMEDIATE')
            try:
                cur = conn.execute(
                    f"""
                    INSERT INTO {CAPTURE_ALERT_INTENT_TABLE}
                        (canonical_path, processing_path, archive_path, sha256, reason, prepared_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (str(alert), str(proc), str(archive), digest,
                     'AWAITING_REVIEW: orphan processing claim', _now_beijing()),
                )
                oid = int(cur.lastrowid)
                conn.execute('COMMIT')
            except Exception:
                conn.execute('ROLLBACK')
                raise
            out.append({'intent_id': oid, 'status': 'awaiting_review'})
    return out


def ack_capture_alert_orphan(
    conn: sqlite3.Connection, *, path: str, sha256: str, reason: str,
) -> dict:
    """人工核对 orphan processing 后才允许归档/ACK。"""
    if not isinstance(reason, str) or not reason.strip():
        raise store.StoreError('orphan ack reason required')
    row = conn.execute(
        f"""
        SELECT intent_id, processing_path, sha256 FROM {CAPTURE_ALERT_INTENT_TABLE}
        WHERE processing_path=? AND completed_at IS NULL
        """, (path,),
    ).fetchone()
    if row is None:
        raise store.StoreError('no pending orphan capture intent for path')
    iid = int(row['intent_id'] if isinstance(row, sqlite3.Row) else row[0])
    expected = str(row['sha256'] if isinstance(row, sqlite3.Row) else row[2])
    if expected.lower() != str(sha256).lower():
        raise store.StoreError('orphan sha256 mismatch')
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute(
            f'UPDATE {CAPTURE_ALERT_INTENT_TABLE} SET reason=? WHERE intent_id=?',
            (reason.strip()[:512], iid),
        )
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise
    return _complete_capture_alert_intent(conn, intent_id=iid)


def read_proof_gap_sidecar(db_path: Optional[str] = None) -> Optional[dict]:
    """兼容旧 API：返回最新一条 pending sidecar incident（若有）。"""
    latest: Optional[dict] = None
    d = gap_incidents_dir(db_path)
    if d.is_dir():
        for p in sorted(d.glob('*.json')):
            try:
                raw = json.loads(p.read_text(encoding='utf-8').strip().splitlines()[0])
            except Exception:
                latest = {
                    'gap_detected': True,
                    'failed_message_id': None,
                    'error_code': 'sidecar_unreadable',
                    'failed_at': None,
                }
                continue
            if isinstance(raw, dict) and raw.get('gap_detected'):
                latest = raw
    path = Path(proof_gap_sidecar_path(db_path))
    legacy = Path(proof_gap_sidecar_legacy_path(db_path))
    if legacy.is_file():
        try:
            raw = json.loads(legacy.read_text(encoding='utf-8'))
            if isinstance(raw, dict) and raw.get('gap_detected'):
                latest = raw
        except Exception:
            latest = {
                'gap_detected': True,
                'failed_message_id': None,
                'error_code': 'sidecar_unreadable',
                'failed_at': None,
            }
    if path.is_file():
        try:
            for line in path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    latest = {
                        'gap_detected': True,
                        'failed_message_id': None,
                        'error_code': 'sidecar_unreadable',
                        'failed_at': None,
                    }
                    continue
                if isinstance(raw, dict) and raw.get('gap_detected'):
                    latest = raw
        except Exception:
            latest = {
                'gap_detected': True,
                'failed_message_id': None,
                'error_code': 'sidecar_unreadable',
                'failed_at': None,
            }
    return latest


def gap_incidents_schema_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (GAP_INCIDENTS_TABLE,),
    ).fetchone()
    return row is not None


def count_unresolved_gap_incidents(conn: sqlite3.Connection) -> int:
    if not gap_incidents_schema_ready(conn):
        return 0
    row = conn.execute(
        f'SELECT COUNT(*) FROM {GAP_INCIDENTS_TABLE} WHERE resolved_at IS NULL'
    ).fetchone()
    return int(row[0] if row else 0)


def list_unresolved_gap_incidents(
    conn: sqlite3.Connection, *, limit: int = 50,
) -> list[dict]:
    if not gap_incidents_schema_ready(conn):
        return []
    rows = conn.execute(
        f"""
        SELECT incident_id, message_id, error_code, detected_at,
               resolved_at, resolution_reason
        FROM {GAP_INCIDENTS_TABLE}
        WHERE resolved_at IS NULL
        ORDER BY incident_id ASC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        if isinstance(r, sqlite3.Row):
            out.append(dict(r))
        else:
            out.append({
                'incident_id': r[0],
                'message_id': r[1],
                'error_code': r[2],
                'detected_at': r[3],
                'resolved_at': r[4],
                'resolution_reason': r[5],
            })
    return out


def _refresh_proof_health_summary(conn: sqlite3.Connection) -> None:
    """health 单行仅作摘要；真源是 incident ledger。"""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (PROOF_HEALTH_TABLE,),
    ).fetchone() is None:
        return
    if not gap_incidents_schema_ready(conn):
        return
    row = conn.execute(
        f"""
        SELECT message_id, error_code, detected_at
        FROM {GAP_INCIDENTS_TABLE}
        WHERE resolved_at IS NULL
        ORDER BY incident_id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        conn.execute(
            f"""
            INSERT INTO {PROOF_HEALTH_TABLE}
                (id, ready, gap_detected, failed_message_id, error_code, failed_at)
            VALUES (1, 1, 0, NULL, NULL, NULL)
            ON CONFLICT(id) DO UPDATE SET
                ready=1,
                gap_detected=0,
                failed_message_id=NULL,
                error_code=NULL,
                failed_at=NULL
            """
        )
        return
    if isinstance(row, sqlite3.Row):
        mid, code, at = row['message_id'], row['error_code'], row['detected_at']
    else:
        mid, code, at = row[0], row[1], row[2]
    conn.execute(
        f"""
        INSERT INTO {PROOF_HEALTH_TABLE}
            (id, ready, gap_detected, failed_message_id, error_code, failed_at)
        VALUES (1, 0, 1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            ready=0,
            gap_detected=1,
            failed_message_id=excluded.failed_message_id,
            error_code=excluded.error_code,
            failed_at=excluded.failed_at
        """,
        (mid, code, at),
    )


def has_unresolved_proof_gap(
    conn: sqlite3.Connection,
    *,
    db_path: Optional[str] = None,
) -> bool:
    """任一 unresolved incident、sidecar/processing、或 quarantine 即未解决。"""
    if count_unresolved_gap_incidents(conn) > 0:
        return True
    path = db_path or _conn_file_path(conn)
    if count_gap_sidecar_pending(path) > 0:
        return True
    if count_quarantine_pending(path) > 0:
        return True
    if count_incomplete_recovery_intents(conn) > 0:
        return True
    return bool(read_proof_health(conn).gap_detected)


def _count_incomplete_table(conn: sqlite3.Connection, table: str) -> int:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
        ).fetchone()
        if row is None:
            return 0
        value = conn.execute(
            f'SELECT COUNT(*) FROM {table} WHERE completed_at IS NULL'
        ).fetchone()
        return int(value[0] if value else 0)
    except Exception:
        return 1


def count_capture_alert_intents_pending(conn: sqlite3.Connection) -> int:
    return _count_incomplete_table(conn, CAPTURE_ALERT_INTENT_TABLE)


def count_pending_incident_intents_pending(conn: sqlite3.Connection) -> int:
    return _count_incomplete_table(conn, PENDING_INCIDENT_INTENT_TABLE)


def count_quarantine_intents_pending(conn: sqlite3.Connection) -> int:
    return _count_incomplete_table(conn, QUARANTINE_INTENT_TABLE)


def count_incomplete_recovery_intents(conn: sqlite3.Connection) -> int:
    return (
        count_capture_alert_intents_pending(conn)
        + count_pending_incident_intents_pending(conn)
        + count_quarantine_intents_pending(conn)
    )


def _unique_quarantine_path(src: Path) -> Path:
    return Path(f'{src}.quarantine.{uuid.uuid4().hex}')


def _unique_resolved_archive_path(src: Path) -> Path:
    return Path(f'{src}.resolved.{uuid.uuid4().hex}')


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _quarantine_corrupt_payload(
    conn: sqlite3.Connection,
    *,
    db_path: Optional[str],
    quarantine_dest: Path,
    payload: str,
) -> None:
    quarantine_dest.parent.mkdir(parents=True, exist_ok=True)
    with open(quarantine_dest, 'w', encoding='utf-8') as fh:
        fh.write(payload if payload.endswith('\n') else payload + '\n')
        fh.flush()
        os.fsync(fh.fileno())
    mark_proof_gap(
        conn,
        failed_message_id=None,
        error_code='sidecar_corrupt',
        db_path=db_path,
        write_sidecar_if_missing=False,
    )
    logger.critical(
        'internal_state_shadow: corrupt gap payload quarantined at %s',
        quarantine_dest,
    )


def _migrate_gap_lines_into_incidents(
    conn: sqlite3.Connection,
    lines: list[str],
    *,
    db_path: Optional[str],
    quarantine_dest: Path,
) -> tuple[int, int]:
    """返回 (migrated_ok, corrupt_count)。corrupt 写入 quarantine 并记 sidecar_corrupt。"""
    migrated = 0
    corrupt = 0
    corrupt_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError:
            corrupt += 1
            corrupt_lines.append(stripped)
            continue
        if not isinstance(raw, dict) or not raw.get('gap_detected'):
            corrupt += 1
            corrupt_lines.append(stripped)
            continue
        mark_proof_gap(
            conn,
            failed_message_id=raw.get('failed_message_id'),
            error_code=str(raw.get('error_code') or 'sidecar_migrated'),
            db_path=db_path,
            write_sidecar_if_missing=False,
        )
        migrated += 1
    if corrupt_lines:
        _quarantine_corrupt_payload(
            conn,
            db_path=db_path,
            quarantine_dest=quarantine_dest,
            payload='\n'.join(corrupt_lines) + '\n',
        )
    return migrated, corrupt


def _migrate_one_incident_file(
    conn: sqlite3.Connection,
    src: Path,
    *,
    db_path: Optional[str],
) -> int:
    """原子 claim 单个 ``*.json`` → ``*.processing`` → 迁入 / quarantine。"""
    processing = Path(str(src) + '.processing')
    try:
        os.rename(str(src), str(processing))
    except FileNotFoundError:
        return 0
    except OSError:
        return 0
    try:
        text = processing.read_text(encoding='utf-8')
    except Exception as exc:  # noqa: BLE001
        logger.critical(
            'internal_state_shadow: gap incident file read failed: %s', exc,
        )
        mark_proof_gap(
            conn,
            failed_message_id=None,
            error_code='sidecar_processing_unreadable',
            db_path=db_path,
            write_sidecar_if_missing=False,
        )
        return 0
    lines = text.splitlines() or [text]
    q = _unique_quarantine_path(src)
    m, _c = _migrate_gap_lines_into_incidents(
        conn, lines, db_path=db_path, quarantine_dest=q,
    )
    try:
        processing.unlink()
    except FileNotFoundError:
        pass
    return m


def migrate_proof_gap_sidecar(
    conn: sqlite3.Connection,
    *,
    db_path: Optional[str] = None,
) -> int:
    """迁入 incident ledger。

    热路径：逐个原子 claim ``.shadow_gap_incidents/<uuid>.json``。
    兼容：仍 migrate 遗留 JSONL / 单槽 JSON（rename-first）。
    """
    path = db_path or _conn_file_path(conn)
    migrated = 0

    legacy = Path(proof_gap_sidecar_legacy_path(path))
    if legacy.is_file():
        processing = Path(f'{legacy}.processing.{uuid.uuid4().hex}')
        try:
            os.rename(str(legacy), str(processing))
        except (FileNotFoundError, OSError):
            processing = None
        if processing is not None:
            try:
                raw = json.loads(processing.read_text(encoding='utf-8'))
                lines = [json.dumps(raw, ensure_ascii=False)] if isinstance(raw, dict) else []
            except Exception:
                lines = [processing.read_text(encoding='utf-8')]
            q = _unique_quarantine_path(legacy)
            m, _c = _migrate_gap_lines_into_incidents(
                conn, lines, db_path=path, quarantine_dest=q,
            )
            migrated += m
            try:
                processing.unlink()
            except FileNotFoundError:
                pass

    jsonl = Path(proof_gap_sidecar_path(path))
    if jsonl.is_file():
        processing = Path(f'{jsonl}.processing.{uuid.uuid4().hex}')
        try:
            os.rename(str(jsonl), str(processing))
        except (FileNotFoundError, OSError):
            processing = None
        if processing is not None:
            try:
                lines = processing.read_text(encoding='utf-8').splitlines()
            except Exception as exc:  # noqa: BLE001
                logger.critical(
                    'internal_state_shadow: gap processing read failed: %s', exc,
                )
                mark_proof_gap(
                    conn,
                    failed_message_id=None,
                    error_code='sidecar_processing_unreadable',
                    db_path=path,
                    write_sidecar_if_missing=False,
                )
                return migrated
            q = _unique_quarantine_path(jsonl)
            m, _c = _migrate_gap_lines_into_incidents(
                conn, lines, db_path=path, quarantine_dest=q,
            )
            migrated += m
            try:
                processing.unlink()
            except FileNotFoundError:
                pass

    # 遗留 JSONL processing
    base = isv3.memories_db_path(path)
    parent = Path(base).parent
    stem = Path(base).name + PROOF_GAP_SIDECAR_SUFFIX
    for proc in parent.glob(stem + '.processing.*'):
        try:
            lines = proc.read_text(encoding='utf-8').splitlines()
        except Exception:
            mark_proof_gap(
                conn,
                failed_message_id=None,
                error_code='sidecar_processing_unreadable',
                db_path=path,
                write_sidecar_if_missing=False,
            )
            continue
        q = _unique_quarantine_path(Path(proof_gap_sidecar_path(path)))
        m, _c = _migrate_gap_lines_into_incidents(
            conn, lines, db_path=path, quarantine_dest=q,
        )
        migrated += m
        try:
            proc.unlink()
        except FileNotFoundError:
            pass

    # 一文件一 incident：只 claim *.json（忽略未完成的 .tmp）
    d = gap_incidents_dir(path)
    if d.is_dir():
        for src in sorted(d.glob('*.json')):
            migrated += _migrate_one_incident_file(conn, src, db_path=path)
        for proc in sorted(d.glob('*.json.processing')):
            # 崩溃残留 processing：再试一次
            try:
                text = proc.read_text(encoding='utf-8')
            except Exception:
                mark_proof_gap(
                    conn,
                    failed_message_id=None,
                    error_code='sidecar_processing_unreadable',
                    db_path=path,
                    write_sidecar_if_missing=False,
                )
                continue
            q = _unique_quarantine_path(Path(str(proc).removesuffix('.processing')))
            m, _c = _migrate_gap_lines_into_incidents(
                conn, text.splitlines(), db_path=path, quarantine_dest=q,
            )
            migrated += m
            try:
                proc.unlink()
            except FileNotFoundError:
                pass
    return migrated


def quarantine_legacy_outbox_sidecar(
    db_path: Optional[str] = None,
) -> Optional[str]:
    """热路径已禁用 outbox sidecar；残留文件移入唯一 quarantine 名。"""
    src = Path(outbox_sidecar_path(db_path))
    if not src.is_file():
        return None
    dest = _unique_quarantine_path(src)
    os.rename(str(src), str(dest))
    logger.critical(
        'internal_state_shadow: legacy outbox sidecar quarantined at %s '
        '(USER_EVENTS requires DB outbox; no hot-path sidecar)',
        dest,
    )
    return str(dest)


def _ensure_score_hash_column(conn: sqlite3.Connection) -> None:
    cols = {
        str(r[1]) for r in conn.execute(f'PRAGMA table_info({SCORE_APPLIED_TABLE})')
    }
    if 'score_hash' not in cols:
        conn.execute(
            f'ALTER TABLE {SCORE_APPLIED_TABLE} ADD COLUMN score_hash TEXT'
        )


def _outbox_table_columns(conn: sqlite3.Connection) -> set[str]:
    if not outbox_schema_ready(conn):
        return set()
    return {
        str(r[1]) for r in conn.execute(f'PRAGMA table_info({OUTBOX_TABLE})')
    }


def _shadow_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _outbox_reconcile_rows(
    conn: sqlite3.Connection,
    table: str,
) -> list[tuple]:
    """Comparable outbox rows for orphan reconciliation (queue_id-agnostic)."""
    return conn.execute(
        f"""
        SELECT event_key, payload_json, COALESCE(payload_hash, ''),
               attempts, last_error, delivered_at
        FROM {table}
        ORDER BY event_key ASC
        """
    ).fetchall()


def _outbox_tables_reconcile_equal(
    conn: sqlite3.Connection,
    left: str,
    right: str,
) -> bool:
    return _outbox_reconcile_rows(conn, left) == _outbox_reconcile_rows(
        conn, right,
    )


def _recover_orphan_outbox_queue_mig(conn: sqlite3.Connection) -> bool:
    """Finish or disambiguate an interrupted queue_id migration without data loss."""
    if not _shadow_table_exists(conn, OUTBOX_QUEUE_MIG_TABLE):
        return False
    conn.execute('BEGIN IMMEDIATE')
    try:
        if not _shadow_table_exists(conn, OUTBOX_QUEUE_MIG_TABLE):
            conn.execute('COMMIT')
            return False

        mig_count = int(
            conn.execute(
                f'SELECT COUNT(*) FROM {OUTBOX_QUEUE_MIG_TABLE}'
            ).fetchone()[0]
        )
        outbox_exists = outbox_schema_ready(conn)

        if not outbox_exists:
            conn.execute(
                f'ALTER TABLE {OUTBOX_QUEUE_MIG_TABLE} RENAME TO {OUTBOX_TABLE}'
            )
            conn.execute('COMMIT')
            return True

        outbox_count = int(
            conn.execute(f'SELECT COUNT(*) FROM {OUTBOX_TABLE}').fetchone()[0]
        )
        has_queue_id = 'queue_id' in _outbox_table_columns(conn)

        if mig_count == 0 and outbox_count == 0:
            conn.execute(f'DROP TABLE {OUTBOX_QUEUE_MIG_TABLE}')
            conn.execute('COMMIT')
            return True

        if mig_count == 0 and outbox_count > 0:
            # CREATE-interrupt: empty mig shell; keep authoritative outbox.
            conn.execute(f'DROP TABLE {OUTBOX_QUEUE_MIG_TABLE}')
            conn.execute('COMMIT')
            return True

        if mig_count > 0 and outbox_count == 0:
            # Empty outbox shell (legacy or queue_id) with populated mig.
            conn.execute(f'DROP TABLE {OUTBOX_TABLE}')
            conn.execute(
                f'ALTER TABLE {OUTBOX_QUEUE_MIG_TABLE} RENAME TO {OUTBOX_TABLE}'
            )
            conn.execute('COMMIT')
            return True

        # Both tables non-empty: reconcile exactly or fail closed.
        if not _outbox_tables_reconcile_equal(
            conn, OUTBOX_TABLE, OUTBOX_QUEUE_MIG_TABLE,
        ):
            raise store.StoreError(
                'outbox_queue_mig_reconciliation_conflict: '
                f'{OUTBOX_TABLE} and {OUTBOX_QUEUE_MIG_TABLE} diverge'
            )

        if has_queue_id:
            conn.execute(f'DROP TABLE {OUTBOX_QUEUE_MIG_TABLE}')
        else:
            conn.execute(f'DROP TABLE {OUTBOX_TABLE}')
            conn.execute(
                f'ALTER TABLE {OUTBOX_QUEUE_MIG_TABLE} RENAME TO {OUTBOX_TABLE}'
            )
        conn.execute('COMMIT')
        return True
    except Exception:
        conn.execute('ROLLBACK')
        raise


def _ensure_outbox_queue_id_schema(conn: sqlite3.Connection) -> None:
    """Outbox drain order is queue_id only; never created_at/event_key."""
    _recover_orphan_outbox_queue_mig(conn)
    if not outbox_schema_ready(conn):
        return
    if 'queue_id' in _outbox_table_columns(conn):
        return
    conn.execute('BEGIN IMMEDIATE')
    try:
        if not outbox_schema_ready(conn):
            conn.execute('COMMIT')
            return
        if 'queue_id' in _outbox_table_columns(conn):
            conn.execute('COMMIT')
            return
        if _shadow_table_exists(conn, OUTBOX_QUEUE_MIG_TABLE):
            conn.execute('ROLLBACK')
            _recover_orphan_outbox_queue_mig(conn)
            return
        conn.execute(
            f"""
            CREATE TABLE {OUTBOX_QUEUE_MIG_TABLE} (
                queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0
                    CHECK (attempts >= 0),
                last_error TEXT,
                delivered_at TEXT
            )
            """
        )
        conn.execute(
            f"""
            INSERT INTO {OUTBOX_QUEUE_MIG_TABLE}
                (event_key, event_type, payload_json, payload_hash, created_at,
                 attempts, last_error, delivered_at)
            SELECT event_key, event_type, payload_json,
                   COALESCE(payload_hash, ''), created_at,
                   attempts, last_error, delivered_at
            FROM {OUTBOX_TABLE}
            ORDER BY created_at ASC, event_key ASC
            """
        )
        conn.execute(f'DROP TABLE {OUTBOX_TABLE}')
        conn.execute(
            f'ALTER TABLE {OUTBOX_QUEUE_MIG_TABLE} RENAME TO {OUTBOX_TABLE}'
        )
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise


def ensure_shadow_schema(
    conn: sqlite3.Connection,
    *,
    db_path: Optional[str] = None,
) -> None:
    """internal_state_v3/events + proof ledger + outbox + gap incidents。

    仅供 ``prepare-schema`` / 管理入口调用；**禁止**在评分事务内执行。
    """
    store.ensure_schema(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCORE_APPLIED_TABLE} (
            message_id INTEGER PRIMARY KEY
                CHECK (message_id > 0),
            applied_at TEXT NOT NULL,
            source TEXT NOT NULL,
            score_hash TEXT
        )
        """
    )
    _ensure_score_hash_column(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PROOF_HEALTH_TABLE} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            ready INTEGER NOT NULL DEFAULT 1
                CHECK (ready IN (0, 1)),
            gap_detected INTEGER NOT NULL DEFAULT 0
                CHECK (gap_detected IN (0, 1)),
            failed_message_id INTEGER,
            error_code TEXT,
            failed_at TEXT
        )
        """
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO {PROOF_HEALTH_TABLE}
            (id, ready, gap_detected, failed_message_id, error_code, failed_at)
        VALUES (1, 1, 0, NULL, NULL, NULL)
        """
    )
    _recover_orphan_outbox_queue_mig(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {OUTBOX_TABLE} (
            queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0
                CHECK (attempts >= 0),
            last_error TEXT,
            delivered_at TEXT
        )
        """
    )
    # 旧 outbox：补 payload_hash，再迁移 event_key-PK → queue_id-PK
    if outbox_schema_ready(conn):
        cols = _outbox_table_columns(conn)
        if 'payload_hash' not in cols:
            conn.execute(
                f'ALTER TABLE {OUTBOX_TABLE} ADD COLUMN payload_hash TEXT'
            )
        _ensure_outbox_queue_id_schema(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {GAP_INCIDENTS_TABLE} (
            incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER,
            error_code TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution_reason TEXT
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {GAP_ACK_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER,
            message_id INTEGER,
            reason TEXT NOT NULL,
            acked_at TEXT NOT NULL,
            previous_error_code TEXT,
            previous_failed_message_id INTEGER
        )
        """
    )
    ack_cols = {
        str(r[1]) for r in conn.execute(f'PRAGMA table_info({GAP_ACK_TABLE})')
    }
    if 'incident_id' not in ack_cols:
        conn.execute(
            f'ALTER TABLE {GAP_ACK_TABLE} ADD COLUMN incident_id INTEGER'
        )
    ensure_quarantine_reconcile_schema(conn)
    migrate_proof_gap_sidecar(conn, db_path=db_path)
    qpath = quarantine_legacy_outbox_sidecar(db_path or _conn_file_path(conn))
    if qpath is not None:
        mark_proof_gap(
            conn,
            failed_message_id=None,
            error_code='outbox_sidecar_quarantined',
            db_path=db_path,
            write_sidecar_if_missing=False,
        )
    _refresh_proof_health_summary(conn)


def score_proof_schema_ready(conn: sqlite3.Connection) -> bool:
    """只检查 proof 表是否存在；**不**建表、不 DDL。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (SCORE_APPLIED_TABLE,),
    ).fetchone()
    return row is not None


def outbox_schema_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (OUTBOX_TABLE,),
    ).fetchone()
    return row is not None


def read_proof_health(conn: sqlite3.Connection) -> ProofHealth:
    # 优先用 incident ledger 摘要
    if gap_incidents_schema_ready(conn):
        n = count_unresolved_gap_incidents(conn)
        if n > 0:
            latest = list_unresolved_gap_incidents(conn, limit=10)
            last = latest[-1] if latest else {}
            return ProofHealth(
                ready=False,
                gap_detected=True,
                failed_message_id=last.get('message_id'),
                error_code=last.get('error_code'),
                failed_at=last.get('detected_at'),
            )
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (PROOF_HEALTH_TABLE,),
    ).fetchone()
    if exists is None:
        return ProofHealth(ready=False, gap_detected=False)
    row = conn.execute(
        f"""
        SELECT ready, gap_detected, failed_message_id, error_code, failed_at
        FROM {PROOF_HEALTH_TABLE} WHERE id=1
        """
    ).fetchone()
    if row is None:
        return ProofHealth(ready=False, gap_detected=False)
    if isinstance(row, sqlite3.Row):
        return ProofHealth(
            ready=bool(int(row['ready'])),
            gap_detected=bool(int(row['gap_detected'])),
            failed_message_id=(
                int(row['failed_message_id'])
                if row['failed_message_id'] is not None else None
            ),
            error_code=row['error_code'],
            failed_at=row['failed_at'],
        )
    return ProofHealth(
        ready=bool(int(row[0])),
        gap_detected=bool(int(row[1])),
        failed_message_id=int(row[2]) if row[2] is not None else None,
        error_code=row[3],
        failed_at=row[4],
    )


def mark_proof_gap(
    conn: sqlite3.Connection,
    *,
    failed_message_id: Optional[int],
    error_code: str,
    db_path: Optional[str] = None,
    write_sidecar_if_missing: bool = True,
) -> Optional[int]:
    """追加一条 gap incident；不得覆盖既有未解决缺口。

    incidents 表可写时 INSERT ledger 行。
    表缺失时 append JSONL sidecar。
    """
    if not isinstance(error_code, str) or not error_code.strip():
        raise store.StoreError(f'error_code invalid: {error_code!r}')
    mid = _normalize_gap_mid(failed_message_id)
    ts = _now_beijing()
    code = error_code.strip()[:64]
    path = db_path or _conn_file_path(conn)
    if not gap_incidents_schema_ready(conn):
        logger.critical(
            'internal_state_shadow: proof gap (no incidents table) '
            'code=%s message_id=%s',
            code, mid,
        )
        if write_sidecar_if_missing and path:
            append_gap_incident_sidecar(
                path, failed_message_id=mid, error_code=code,
            )
        return None
    cur = conn.execute(
        f"""
        INSERT INTO {GAP_INCIDENTS_TABLE}
            (message_id, error_code, detected_at, resolved_at, resolution_reason)
        VALUES (?, ?, ?, NULL, NULL)
        """,
        (mid, code, ts),
    )
    incident_id = int(cur.lastrowid)
    _refresh_proof_health_summary(conn)
    logger.critical(
        'internal_state_shadow: proof gap incident=%s code=%s message_id=%s',
        incident_id, code, mid,
    )
    return incident_id


def clear_proof_gap(
    conn: sqlite3.Connection,
    *,
    db_path: Optional[str] = None,
) -> None:
    """危险内部 API：仅刷新摘要。不得 unlink canonical sidecar。"""
    path = db_path or _conn_file_path(conn)
    migrate_proof_gap_sidecar(conn, db_path=path)
    _refresh_proof_health_summary(conn)
    if (
        count_unresolved_gap_incidents(conn) == 0
        and count_gap_sidecar_pending(path) == 0
        and count_quarantine_pending(path) == 0
    ):
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (PROOF_HEALTH_TABLE,),
        ).fetchone() is not None:
            conn.execute(
                f"""
                INSERT INTO {PROOF_HEALTH_TABLE}
                    (id, ready, gap_detected, failed_message_id, error_code, failed_at)
                VALUES (1, 1, 0, NULL, NULL, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    ready=1,
                    gap_detected=0,
                    failed_message_id=NULL,
                    error_code=NULL,
                    failed_at=NULL
                """
            )


def ack_proof_gap(
    conn: sqlite3.Connection,
    *,
    message_id: Optional[int] = None,
    reason: str,
    db_path: Optional[str] = None,
    incident_id: Optional[int] = None,
) -> dict:
    """受审计地解决指定 incident（或该 message_id 下全部未解决项）。

    - 按 message_id 批量 ack 时 ``message_id`` 必填且为正整数；
    - 按 ``incident_id`` ack 时：若该行 ``message_id IS NULL``，
      **不得**伪造 message_id（``message_id`` 参数可省略）。
    不得一键清除其它 message 的缺口；也不得 unlink quarantine。
    """
    if not isinstance(reason, str) or not reason.strip():
        raise store.StoreError('ack reason required')
    if incident_id is None and message_id is None:
        raise store.StoreError('ack-gap requires --message-id or --incident-id')
    if not gap_incidents_schema_ready(conn):
        raise store.StoreError(
            f'{GAP_INCIDENTS_TABLE} missing; run prepare-schema before ack-gap'
        )
    # 先迁入 sidecar，再 ack
    migrate_proof_gap_sidecar(conn, db_path=db_path)
    ack_mid: Optional[int] = None
    if incident_id is not None:
        row = conn.execute(
            f"""
            SELECT incident_id, message_id, error_code
            FROM {GAP_INCIDENTS_TABLE}
            WHERE incident_id=? AND resolved_at IS NULL
            """,
            (int(incident_id),),
        ).fetchone()
        if row is None:
            raise store.StoreError(
                f'no unresolved incident_id={incident_id}'
            )
        row_mid = row['message_id'] if isinstance(row, sqlite3.Row) else row[1]
        if row_mid is None:
            if message_id is not None:
                raise store.StoreError(
                    f'incident {incident_id} has NULL message_id; '
                    f'do not pass --message-id'
                )
            ack_mid = None
        else:
            if message_id is None:
                ack_mid = int(row_mid)
            else:
                mid = store.require_positive_message_id(
                    message_id, field='message_id',
                )
                if int(row_mid) != int(mid):
                    raise store.StoreError(
                        f'incident {incident_id} message_id {row_mid} != ack {mid}'
                    )
                ack_mid = mid
        targets = [row]
    else:
        mid = store.require_positive_message_id(message_id, field='message_id')
        ack_mid = mid
        targets = conn.execute(
            f"""
            SELECT incident_id, message_id, error_code
            FROM {GAP_INCIDENTS_TABLE}
            WHERE resolved_at IS NULL AND message_id=?
            ORDER BY incident_id ASC
            """,
            (mid,),
        ).fetchall()
        if not targets:
            raise store.StoreError(
                f'no unresolved gap incidents for message_id={mid}'
            )
    ts = _now_beijing()
    reason_s = reason.strip()[:512]
    resolved_ids = []
    for row in targets:
        if isinstance(row, sqlite3.Row):
            iid, row_mid, code = row['incident_id'], row['message_id'], row['error_code']
        else:
            iid, row_mid, code = row[0], row[1], row[2]
        conn.execute(
            f"""
            UPDATE {GAP_INCIDENTS_TABLE}
            SET resolved_at=?, resolution_reason=?
            WHERE incident_id=? AND resolved_at IS NULL
            """,
            (ts, reason_s, iid),
        )
        conn.execute(
            f"""
            INSERT INTO {GAP_ACK_TABLE}
                (incident_id, message_id, reason, acked_at,
                 previous_error_code, previous_failed_message_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (iid, ack_mid, reason_s, ts, code, row_mid),
        )
        resolved_ids.append(int(iid))
    _refresh_proof_health_summary(conn)
    # 不得 unlink canonical sidecar / quarantine；quarantine 走 reconcile
    return {
        'acked': True,
        'message_id': ack_mid,
        'incident_ids': resolved_ids,
        'reason': reason_s,
        'acked_at': ts,
        'remaining_unresolved': count_unresolved_gap_incidents(conn),
        'quarantine_pending': count_quarantine_pending(
            db_path or _conn_file_path(conn)
        ),
    }


def inspect_quarantine(db_path: Optional[str] = None) -> list[dict]:
    """列出 pending quarantine 文件及其 sha256（供 reconcile 核对）。"""
    out: list[dict] = []
    for path_s in list_gap_quarantine_files(db_path):
        p = Path(path_s)
        try:
            digest = _sha256_file(p)
            size = p.stat().st_size
        except Exception as exc:  # noqa: BLE001
            out.append({
                'path': path_s,
                'sha256': None,
                'size': None,
                'error': str(exc),
                'kind': _quarantine_kind(path_s),
            })
            continue
        out.append({
            'path': path_s,
            'sha256': digest,
            'size': size,
            'error': None,
            'kind': _quarantine_kind(path_s),
        })
    return out


def inspect_pending_incidents(
    db_path: Optional[str] = None,
    *,
    stale_after_seconds: int = 0,
) -> list[dict]:
    """列出未完成 ``.tmp``；只报告，绝不自动碰活 writer。"""
    d = gap_incidents_dir(db_path)
    now = time.time()
    out: list[dict] = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob('*.tmp')):
        try:
            age = max(0.0, now - p.stat().st_mtime)
            out.append({
                'path': str(p),
                'age_seconds': age,
                'stale': age >= max(0, int(stale_after_seconds)),
                'sha256': _sha256_file(p),
            })
        except Exception as exc:  # noqa: BLE001
            out.append({
                'path': str(p), 'age_seconds': None, 'stale': True,
                'sha256': None, 'error': str(exc),
            })
    return out


def recover_pending_incident_tmp(
    conn: sqlite3.Connection,
    *,
    path: str,
    action: str,
    reason: str,
    stale_after_seconds: int,
    db_path: Optional[str] = None,
) -> dict:
    """受审计处理陈旧 tmp：promote / quarantine / discard。

    必须显式调用且文件年龄达到阈值，避免误处理仍在 fsync 的 writer。
    """
    if action not in ('promote', 'quarantine', 'discard'):
        raise store.StoreError('tmp action must be promote/quarantine/discard')
    if not isinstance(reason, str) or not reason.strip():
        raise store.StoreError('tmp recovery reason required')
    src = Path(path)
    d = gap_incidents_dir(db_path or _conn_file_path(conn))
    if src.parent != d or src.suffix != '.tmp' or not src.is_file():
        raise store.StoreError('path is not a pending incident tmp in this db')
    age = time.time() - src.stat().st_mtime
    if age < max(0, int(stale_after_seconds)):
        raise store.StoreError(f'tmp not stale yet: age={age:.3f}s')
    try:
        raw = json.loads(src.read_text(encoding='utf-8').strip())
        valid = isinstance(raw, dict) and bool(raw.get('gap_detected'))
    except Exception:
        raw, valid = None, False
    ensure_quarantine_reconcile_schema(conn)
    digest = _sha256_file(src)
    existing = conn.execute(
        f"""
        SELECT intent_id, archive_path, sha256, action, reason, completed_at
        FROM {PENDING_INCIDENT_INTENT_TABLE} WHERE source_path=?
        """,
        (str(src),),
    ).fetchone()
    if existing is not None:
        get = lambda k, i: existing[k] if isinstance(existing, sqlite3.Row) else existing[i]
        if (
            str(get('sha256', 2)) != digest
            or str(get('action', 3)) != action
            or str(get('reason', 4)) != reason.strip()[:512]
        ):
            raise store.StoreError('prepared tmp intent does not match retry parameters')
        intent_id = int(get('intent_id', 0))
        recover_pending_incident_intents(conn, db_path=db_path)
        archive = str(get('archive_path', 1))
        return {
            'action': action, 'path': str(src), 'intent_id': intent_id,
            'reused_prepared_intent': True, 'archive': archive,
        }
    if action == 'promote':
        if not valid:
            raise store.StoreError('cannot promote invalid tmp; use quarantine/discard')
        dest = Path(str(src).removesuffix('.tmp') + '.json')
    elif action == 'quarantine':
        dest = _unique_quarantine_path(src)
    else:
        dest = Path(f'{src}.discarded.{uuid.uuid4().hex}')
    reason_s = reason.strip()[:512]
    conn.execute('BEGIN IMMEDIATE')
    try:
        cur = conn.execute(
            f"""
            INSERT INTO {PENDING_INCIDENT_INTENT_TABLE}
                (source_path, archive_path, sha256, action, reason, prepared_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(src), str(dest), digest, action, reason_s, _now_beijing()),
        )
        intent_id = int(cur.lastrowid)
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise
    os.rename(str(src), str(dest))
    _fsync_dir(d)
    conn.execute('BEGIN IMMEDIATE')
    try:
        if action == 'quarantine':
            mark_proof_gap(
                conn, failed_message_id=None, error_code='sidecar_corrupt',
                db_path=db_path, write_sidecar_if_missing=False,
            )
        conn.execute(
            f"""
            INSERT INTO {PENDING_INCIDENT_ACTION_TABLE}
                (source_path, action, reason, archive_path, acted_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(src), action, reason_s, str(dest), _now_beijing()),
        )
        conn.execute(
            f"""
            UPDATE {PENDING_INCIDENT_INTENT_TABLE}
            SET completed_at=? WHERE intent_id=? AND completed_at IS NULL
            """,
            (_now_beijing(), intent_id),
        )
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise
    key = {'promote': 'promoted', 'quarantine': 'quarantine', 'discard': 'archive'}[action]
    result = {'action': action, 'path': str(src), key: str(dest), 'intent_id': intent_id}
    return result


def recover_pending_incident_intents(
    conn: sqlite3.Connection,
    *,
    db_path: Optional[str] = None,
) -> list[dict]:
    """恢复 stale-tmp action prepared intent；archive 存在才可完成审计。"""
    ensure_quarantine_reconcile_schema(conn)
    rows = conn.execute(
        f"""
        SELECT intent_id, source_path, archive_path, sha256, action, reason
        FROM {PENDING_INCIDENT_INTENT_TABLE} WHERE completed_at IS NULL
        ORDER BY intent_id ASC
        """
    ).fetchall()
    out = []
    for row in rows:
        get = lambda k, i: row[k] if isinstance(row, sqlite3.Row) else row[i]
        iid = int(get('intent_id', 0))
        src, archive = Path(str(get('source_path', 1))), Path(str(get('archive_path', 2)))
        digest, action, reason = str(get('sha256', 3)), str(get('action', 4)), str(get('reason', 5))
        if src.is_file() and not archive.exists():
            if _sha256_file(src) != digest:
                out.append({'intent_id': iid, 'status': 'source_hash_mismatch'})
                continue
            os.rename(str(src), str(archive))
            _fsync_dir(archive.parent)
        if archive.is_file() and not src.exists() and _sha256_file(archive) == digest:
            conn.execute('BEGIN IMMEDIATE')
            try:
                if action == 'quarantine':
                    mark_proof_gap(
                        conn, failed_message_id=None, error_code='sidecar_corrupt',
                        db_path=db_path, write_sidecar_if_missing=False,
                    )
                conn.execute(
                    f"""
                    INSERT INTO {PENDING_INCIDENT_ACTION_TABLE}
                        (source_path, action, reason, archive_path, acted_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (str(src), action, reason, str(archive), _now_beijing()),
                )
                conn.execute(
                    f'UPDATE {PENDING_INCIDENT_INTENT_TABLE} SET completed_at=? WHERE intent_id=?',
                    (_now_beijing(), iid),
                )
                conn.execute('COMMIT')
                out.append({'intent_id': iid, 'status': 'completed'})
            except Exception:
                conn.execute('ROLLBACK')
                raise
        else:
            out.append({'intent_id': iid, 'status': 'split_brain'})
    return out


def _quarantine_kind(path_s: str) -> str:
    name = Path(path_s).name
    if OUTBOX_SIDECAR_SUFFIX in name:
        return 'outbox_sidecar'
    return 'gap_sidecar'


def ensure_quarantine_reconcile_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {QUARANTINE_RECONCILE_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            reason TEXT NOT NULL,
            resolved_archive TEXT NOT NULL,
            incident_id INTEGER,
            backfill_event_key TEXT,
            reconciled_at TEXT NOT NULL
        )
        """
    )
    cols = {
        str(r[1]) for r in conn.execute(
            f'PRAGMA table_info({QUARANTINE_RECONCILE_TABLE})'
        )
    }
    if 'intent_id' not in cols:
        conn.execute(
            f'ALTER TABLE {QUARANTINE_RECONCILE_TABLE} ADD COLUMN intent_id INTEGER'
        )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {QUARANTINE_INTENT_TABLE} (
            intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL UNIQUE,
            archive_path TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL,
            reason TEXT NOT NULL,
            incident_id INTEGER NOT NULL,
            backfill_event_key TEXT,
            prepared_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PENDING_INCIDENT_ACTION_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL,
            archive_path TEXT,
            acted_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PENDING_INCIDENT_INTENT_TABLE} (
            intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL UNIQUE,
            archive_path TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL,
            prepared_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )


def reconcile_quarantine(
    conn: sqlite3.Connection,
    *,
    path: str,
    sha256: str,
    reason: str,
    db_path: Optional[str] = None,
    backfill_event_key: Optional[str] = None,
    incident_id: Optional[int] = None,
) -> dict:
    """两阶段受审计地 reconcile 单个 quarantine 文件。

    SQLite 与文件系统不能共享原子提交；durable prepared intent 是恢复边界：
    intent commit → rename/fsync → 单一 SQLite finalization transaction。
    """
    if not isinstance(reason, str) or not reason.strip():
        raise store.StoreError('reconcile reason required')
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise store.StoreError('sha256 must be 64 hex chars')
    ensure_quarantine_reconcile_schema(conn)
    if not gap_incidents_schema_ready(conn):
        raise store.StoreError(
            f'{GAP_INCIDENTS_TABLE} missing; run prepare-schema before reconcile'
        )
    src = Path(path)
    pending = set(list_gap_quarantine_files(db_path or _conn_file_path(conn)))
    if str(src) not in pending:
        raise store.StoreError(f'path is not a pending quarantine file: {path}')
    if not src.is_file():
        raise store.StoreError(f'quarantine file missing: {path}')
    actual = _sha256_file(src)
    if actual.lower() != sha256.lower():
        raise store.StoreError(
            f'sha256 mismatch: expected {sha256} actual {actual}'
        )
    # 所有参数必须在文件移动前验证。
    reason_s = reason.strip()[:512]
    bf = None
    if backfill_event_key is not None:
        if not isinstance(backfill_event_key, str) or not backfill_event_key.strip():
            raise store.StoreError('backfill_event_key invalid')
        bf = backfill_event_key.strip()[:128]
    kind = _quarantine_kind(str(src))
    want_code = (
        'outbox_sidecar_quarantined' if kind == 'outbox_sidecar'
        else 'sidecar_corrupt'
    )
    if incident_id is not None:
        row = conn.execute(
            f"""
            SELECT incident_id, message_id, error_code
            FROM {GAP_INCIDENTS_TABLE}
            WHERE incident_id=? AND resolved_at IS NULL
            """,
            (int(incident_id),),
        ).fetchone()
        if row is None:
            raise store.StoreError(f'no unresolved incident_id={incident_id}')
        code = row['error_code'] if isinstance(row, sqlite3.Row) else row[2]
        if str(code) != want_code:
            raise store.StoreError(
                f'incident {incident_id} error_code {code!r} != {want_code!r}'
            )
        target_iid = int(incident_id)
    else:
        row = conn.execute(
            f"""
            SELECT incident_id FROM {GAP_INCIDENTS_TABLE}
            WHERE resolved_at IS NULL AND error_code=?
            ORDER BY incident_id ASC LIMIT 1
            """,
            (want_code,),
        ).fetchone()
        if row is None:
            raise store.StoreError(
                f'no unresolved {want_code} incident to reconcile'
            )
        target_iid = int(row[0] if not isinstance(row, sqlite3.Row) else row['incident_id'])

    prepared = conn.execute(
        f"""
        SELECT intent_id, archive_path, sha256, incident_id, completed_at
        FROM {QUARANTINE_INTENT_TABLE} WHERE source_path=?
        """,
        (str(src),),
    ).fetchone()
    if prepared is not None:
        get = (lambda k, i: prepared[k] if isinstance(prepared, sqlite3.Row)
               else prepared[i])
        intent_id = int(get('intent_id', 0))
        archive = Path(str(get('archive_path', 1)))
        if str(get('sha256', 2)).lower() != actual.lower() or int(
            get('incident_id', 3)
        ) != target_iid:
            raise store.StoreError(
                f'prepared intent {intent_id} does not match retry parameters'
            )
        if get('completed_at', 4) is not None:
            return _complete_quarantine_intent(
                conn, intent_id=intent_id, db_path=db_path,
            )
    else:
        archive = _unique_resolved_archive_path(src)
        prepared_at = _now_beijing()
        conn.execute('BEGIN IMMEDIATE')
        try:
            cur = conn.execute(
                f"""
                INSERT INTO {QUARANTINE_INTENT_TABLE}
                    (source_path, archive_path, sha256, reason, incident_id,
                     backfill_event_key, prepared_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (str(src), str(archive), actual.lower(), reason_s, target_iid, bf,
                 prepared_at),
            )
            intent_id = int(cur.lastrowid)
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise

    # Filesystem phase is deliberately after durable intent, and before final DB txn.
    if src.exists() and not archive.exists():
        os.rename(str(src), str(archive))
        _fsync_dir(archive.parent)
    return _complete_quarantine_intent(
        conn, intent_id=intent_id, db_path=db_path,
    )


def _complete_quarantine_intent(
    conn: sqlite3.Connection,
    *,
    intent_id: int,
    db_path: Optional[str],
) -> dict:
    """在一个 SQLite 事务中完成 resolve + ack + audit + intent。"""
    row = conn.execute(
        f"""
        SELECT source_path, archive_path, sha256, reason, incident_id,
               backfill_event_key, prepared_at, completed_at
        FROM {QUARANTINE_INTENT_TABLE} WHERE intent_id=?
        """,
        (int(intent_id),),
    ).fetchone()
    if row is None:
        raise store.StoreError(f'unknown quarantine intent {intent_id}')
    get = (lambda k, i: row[k] if isinstance(row, sqlite3.Row) else row[i])
    src, archive = str(get('source_path', 0)), str(get('archive_path', 1))
    digest, reason_s = str(get('sha256', 2)), str(get('reason', 3))
    target_iid, bf = int(get('incident_id', 4)), get('backfill_event_key', 5)
    completed = get('completed_at', 7)
    if completed is not None:
        return {
            'reconciled': True, 'intent_id': int(intent_id), 'path': src,
            'sha256': digest, 'resolved_archive': archive,
            'incident_id': target_iid, 'reason': reason_s,
            'backfill_event_key': bf, 'reconciled_at': completed,
            'idempotent': True,
        }
    if not Path(archive).is_file():
        raise store.StoreError(
            f'intent {intent_id} not finalizable: archive missing {archive}'
        )
    if _sha256_file(Path(archive)).lower() != digest.lower():
        raise store.StoreError(f'intent {intent_id} archive sha256 mismatch')

    ts = _now_beijing()
    conn.execute('BEGIN IMMEDIATE')
    try:
        incident = conn.execute(
            f"""
            SELECT message_id, error_code, resolved_at
            FROM {GAP_INCIDENTS_TABLE} WHERE incident_id=?
            """,
            (target_iid,),
        ).fetchone()
        if incident is None:
            raise store.StoreError(f'intent {intent_id} incident missing')
        mid = incident['message_id'] if isinstance(incident, sqlite3.Row) else incident[0]
        code = incident['error_code'] if isinstance(incident, sqlite3.Row) else incident[1]
        resolved = incident['resolved_at'] if isinstance(incident, sqlite3.Row) else incident[2]
        if resolved is None:
            conn.execute(
                f"""
                UPDATE {GAP_INCIDENTS_TABLE}
                SET resolved_at=?, resolution_reason=?
                WHERE incident_id=? AND resolved_at IS NULL
                """,
                (ts, f'reconcile-quarantine: {reason_s}', target_iid),
            )
            conn.execute(
                f"""
                INSERT INTO {GAP_ACK_TABLE}
                    (incident_id, message_id, reason, acked_at,
                     previous_error_code, previous_failed_message_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (target_iid, mid, reason_s, ts, code, mid),
            )
        conn.execute(
            f"""
            INSERT INTO {QUARANTINE_RECONCILE_TABLE}
                (path, sha256, reason, resolved_archive, incident_id,
                 backfill_event_key, reconciled_at, intent_id)
            SELECT ?, ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM {QUARANTINE_RECONCILE_TABLE} WHERE intent_id=?
            )
            """,
            (src, digest.lower(), reason_s, archive, target_iid, bf, ts,
             int(intent_id), int(intent_id)),
        )
        conn.execute(
            f"""
            UPDATE {QUARANTINE_INTENT_TABLE}
            SET completed_at=? WHERE intent_id=? AND completed_at IS NULL
            """,
            (ts, int(intent_id)),
        )
        _refresh_proof_health_summary(conn)
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise
    return {
        'reconciled': True,
        'intent_id': int(intent_id),
        'path': src,
        'sha256': digest.lower(),
        'resolved_archive': archive,
        'incident_id': target_iid,
        'reason': reason_s,
        'backfill_event_key': bf,
        'reconciled_at': ts,
        'remaining_quarantine': count_quarantine_pending(
            db_path or _conn_file_path(conn)
        ),
        'remaining_unresolved': count_unresolved_gap_incidents(conn),
    }


def recover_quarantine_intents(
    conn: sqlite3.Connection,
    *,
    db_path: Optional[str] = None,
) -> list[dict]:
    """恢复已 prepared 的 reconcile intent；不猜测 source 仍存在的操作员意图。"""
    ensure_quarantine_reconcile_schema(conn)
    rows = conn.execute(
        f"""
        SELECT intent_id, source_path, archive_path, completed_at
        FROM {QUARANTINE_INTENT_TABLE} WHERE completed_at IS NULL
        ORDER BY intent_id ASC
        """
    ).fetchall()
    out = []
    for row in rows:
        iid = int(row['intent_id'] if isinstance(row, sqlite3.Row) else row[0])
        src = Path(row['source_path'] if isinstance(row, sqlite3.Row) else row[1])
        archive = Path(row['archive_path'] if isinstance(row, sqlite3.Row) else row[2])
        if archive.is_file() and not src.exists():
            out.append(_complete_quarantine_intent(
                conn, intent_id=iid, db_path=db_path,
            ))
        elif src.is_file() and not archive.exists():
            out.append({'intent_id': iid, 'status': 'prepared_source_present'})
        else:
            out.append({'intent_id': iid, 'status': 'split_brain'})
    return out


def lookup_score_proof(
    conn: sqlite3.Connection, message_id: int,
) -> Optional[dict]:
    mid = store.require_positive_message_id(message_id, field='message_id')
    if not score_proof_schema_ready(conn):
        return None
    row = conn.execute(
        f"""
        SELECT message_id, applied_at, source, score_hash
        FROM {SCORE_APPLIED_TABLE}
        WHERE message_id=?
        """,
        (mid,),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return {
            'message_id': int(row['message_id']),
            'applied_at': row['applied_at'],
            'source': row['source'],
            'score_hash': row['score_hash'],
        }
    return {
        'message_id': int(row[0]),
        'applied_at': row[1],
        'source': row[2],
        'score_hash': row[3] if len(row) > 3 else None,
    }


def record_score_proof_in_txn(
    conn: sqlite3.Connection,
    message_id: int,
    *,
    applied_at: str,
    source: str,
    score_hash: str,
) -> str:
    """在调用方事务内写入评分证明行；**不** COMMIT / ROLLBACK。

    安全门：
      - 必须 ``conn.in_transaction``
      - ``message_id`` 为强幂等键，辅以 ``score_hash``
      - 同 message_id + 同 score_hash → 返回 ``duplicate``（整次应 no-op）
      - 同 message_id、不同 score_hash → ``StoreError``

    返回 ``inserted`` / ``duplicate``。
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
    if not isinstance(score_hash, str) or not score_hash.strip() or len(score_hash) > 128:
        raise store.StoreError(f'score_hash invalid: {score_hash!r}')
    hash_canon = score_hash.strip()

    existing = lookup_score_proof(conn, mid)
    if existing is not None:
        prev_hash = existing.get('score_hash')
        if prev_hash is not None and str(prev_hash) == hash_canon:
            return 'duplicate'
        if prev_hash in (None, ''):
            raise store.StoreError(
                f'legacy_hash_unknown for message_id={mid}: '
                f'refusing to backfill score_hash without proof'
            )
        raise store.StoreError(
            f'score proof payload conflict for message_id={mid}: '
            f'existing_hash={prev_hash!r} new_hash={hash_canon!r}'
        )

    conn.execute(
        f"""
        INSERT INTO {SCORE_APPLIED_TABLE}
            (message_id, applied_at, source, score_hash)
        VALUES (?, ?, ?, ?)
        """,
        (mid, applied_canon, source_canon, hash_canon),
    )
    return 'inserted'


def max_score_proof_message_id(conn: sqlite3.Connection) -> Optional[int]:
    """当前 proof 表最大 message_id；表空返回 None。"""
    if not score_proof_schema_ready(conn):
        return None
    row = conn.execute(
        f'SELECT MAX(message_id) FROM {SCORE_APPLIED_TABLE}'
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def enqueue_outbox_in_txn(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    event_type: str,
    payload: Mapping[str, Any],
) -> str:
    """权威写入同事务入队；**不** COMMIT。要求 ``conn.in_transaction``。

    同 key + 同 payload_hash → ``duplicate``；同 key 不同载荷 → 冲突。
    USER_EVENTS 热路径禁止 JSONL sidecar；缺表即 StoreError（fail closed）。
    """
    if not conn.in_transaction:
        raise store.StoreError(
            'enqueue_outbox_in_txn requires an active caller-owned transaction'
        )
    if not outbox_schema_ready(conn):
        raise store.StoreError(
            f'{OUTBOX_TABLE} missing; run prepare-schema before user events'
        )
    if not isinstance(event_key, str) or not event_key.strip() or len(event_key) > 128:
        raise store.StoreError(f'event_key invalid: {event_key!r}')
    if event_type not in (EVENT_TYPE_USER_RULE, EVENT_TYPE_USER_SCORED):
        raise store.StoreError(f'event_type unsupported: {event_type!r}')
    if not isinstance(payload, Mapping):
        raise store.StoreError('payload must be a mapping')
    key = event_key.strip()
    body = canonical_payload_json(payload)
    phash = compute_payload_hash(payload)
    ts = _now_beijing()
    row = conn.execute(
        f"""
        SELECT payload_json, payload_hash FROM {OUTBOX_TABLE}
        WHERE event_key=?
        """,
        (key,),
    ).fetchone()
    if row is not None:
        if isinstance(row, sqlite3.Row):
            prev_body, prev_hash = row['payload_json'], row['payload_hash']
        else:
            prev_body, prev_hash = row[0], row[1]
        if prev_hash and str(prev_hash) == phash:
            return 'duplicate'
        # 旧行无 hash：按规范化 JSON 比较
        try:
            prev_obj = json.loads(prev_body)
            if (
                isinstance(prev_obj, dict)
                and compute_payload_hash(prev_obj) == phash
            ):
                if not prev_hash:
                    conn.execute(
                        f"""
                        UPDATE {OUTBOX_TABLE} SET payload_hash=?
                        WHERE event_key=? AND (payload_hash IS NULL OR payload_hash='')
                        """,
                        (phash, key),
                    )
                return 'duplicate'
        except Exception:
            pass
        raise store.StoreError(
            f'outbox payload conflict for event_key={key}: '
            f'existing_hash={prev_hash!r} new_hash={phash!r}'
        )
    conn.execute(
        f"""
        INSERT INTO {OUTBOX_TABLE}
            (event_key, event_type, payload_json, payload_hash, created_at,
             attempts, last_error, delivered_at)
        VALUES (?, ?, ?, ?, ?, 0, NULL, NULL)
        """,
        (key, event_type, body, phash, ts),
    )
    return 'inserted'


def enqueue_user_rule_in_txn(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    text: str,
    created_at: str,
    previous_user_at: Optional[str],
    environ: Optional[Mapping[str, str]] = None,
) -> bool:
    """与 chat_messages INSERT 同事务写入 user_rule outbox。

    缺 outbox 表时抛 StoreError（调用方应记 outbox_capture_gap，不得 sidecar）。
    """
    if not is_user_events_enabled(environ=environ):
        return False
    if not outbox_schema_ready(conn):
        raise store.StoreError(
            f'{OUTBOX_TABLE} missing; USER_EVENTS fail-closed without sidecar'
        )
    mid = store.require_positive_message_id(message_id, field='message_id')
    if not isinstance(text, str):
        raise store.StoreError('user_rule text must be str')
    observation = events.sanitize_user_rule_observation(
        message_id=mid, text=text, created_at=created_at,
        previous_user_at=previous_user_at,
    )
    enqueue_outbox_in_txn(
        conn,
        event_key=f'user_rule:{mid}',
        event_type=EVENT_TYPE_USER_RULE,
        payload={
            'envelope': {
                'observation': observation,
            },
        },
    )
    return True


def enqueue_user_scored_in_txn(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    scores: Mapping[str, Any],
    scored_at: str,
    environ: Optional[Mapping[str, str]] = None,
) -> bool:
    """与 emotion UPDATE + proof 同事务写入 user_scored outbox。"""
    if not is_user_events_enabled(environ=environ):
        return False
    if not outbox_schema_ready(conn):
        raise store.StoreError(
            f'{OUTBOX_TABLE} missing; USER_EVENTS fail-closed without sidecar'
        )
    mid = store.require_positive_message_id(message_id, field='message_id')
    if not isinstance(scores, Mapping):
        raise store.StoreError('scores must be a mapping')
    enqueue_outbox_in_txn(
        conn,
        event_key=f'user_scored:{mid}',
        event_type=EVENT_TYPE_USER_SCORED,
        payload={
            'message_id': mid,
            'scores': dict(scores),
            'scored_at': scored_at,
        },
    )
    return True


def count_pending_outbox(conn: sqlite3.Connection) -> int:
    if not outbox_schema_ready(conn):
        return 0
    row = conn.execute(
        f'SELECT COUNT(*) FROM {OUTBOX_TABLE} WHERE delivered_at IS NULL'
    ).fetchone()
    return int(row[0] if row else 0)


def _deliver_outbox_row(
    row: Mapping[str, Any],
    *,
    db_path: Optional[str],
    environ: Optional[Mapping[str, str]],
) -> ShadowResult:
    et = row['event_type']
    try:
        payload = json.loads(row['payload_json'])
    except Exception as exc:  # noqa: BLE001
        return ShadowResult(ok=False, status='failed', error=f'payload: {exc}')
    if not isinstance(payload, dict):
        return ShadowResult(ok=False, status='failed', error='payload not object')
    if et == EVENT_TYPE_USER_RULE:
        envelope = payload.get('envelope')
        if not isinstance(envelope, dict):
            return ShadowResult(ok=False, status='failed', error='sanitized user_rule envelope missing')
        return observe_planned_user_message_shadow(
            envelope=envelope, db_path=db_path, environ=environ,
        )
    if et == EVENT_TYPE_USER_SCORED:
        scores = payload.get('scores')
        if not isinstance(scores, dict):
            return ShadowResult(ok=False, status='failed', error='scores missing')
        return observe_scored_shadow(
            message_id=int(payload['message_id']),
            scores=scores,
            scored_at=str(payload['scored_at']),
            db_path=db_path,
            environ=environ,
        )
    return ShadowResult(ok=False, status='failed', error=f'unknown type {et}')


def drain_shadow_outbox(
    db_path: Optional[str] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    limit: int = _DEFAULT_DRAIN_LIMIT,
) -> dict:
    """投递 pending outbox；失败保留行；v3 event_key 幂等负责去重。"""
    summary = {
        'attempted': 0,
        'delivered': 0,
        'failed': 0,
        'skipped_disabled': False,
        'pending_after': None,
    }
    if not is_user_events_enabled(environ=environ):
        summary['skipped_disabled'] = True
        return summary
    path = isv3.memories_db_path(db_path)
    conn = None
    try:
        conn = open_shadow_connection(path)
        quarantine_legacy_outbox_sidecar(path)
        if not outbox_schema_ready(conn):
            summary['failed'] = 1
            summary['error'] = f'{OUTBOX_TABLE} missing'
            return summary
        rows = conn.execute(
            f"""
            SELECT queue_id, event_key, event_type, payload_json, attempts
            FROM {OUTBOX_TABLE}
            WHERE delivered_at IS NULL
            ORDER BY queue_id ASC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        for row in rows:
            if isinstance(row, sqlite3.Row):
                item = dict(row)
            else:
                item = {
                    'queue_id': row[0],
                    'event_key': row[1],
                    'event_type': row[2],
                    'payload_json': row[3],
                    'attempts': row[4],
                }
            summary['attempted'] += 1
            result = _deliver_outbox_row(item, db_path=path, environ=environ)
            ok_statuses = ('applied', 'duplicate', 'stale_skipped')
            if result.ok and result.status in ok_statuses:
                conn.execute(
                    f"""
                    UPDATE {OUTBOX_TABLE}
                    SET delivered_at=?, last_error=NULL
                    WHERE event_key=?
                    """,
                    (_now_beijing(), item['event_key']),
                )
                summary['delivered'] += 1
            else:
                err = result.error or result.status
                conn.execute(
                    f"""
                    UPDATE {OUTBOX_TABLE}
                    SET attempts=attempts+1, last_error=?
                    WHERE event_key=?
                    """,
                    (str(err)[:256], item['event_key']),
                )
                summary['failed'] += 1
                _record_error(f'drain_outbox {item["event_key"]}: {err}')
                # State events are causal. Never let a later observation advance
                # clocks past a failed head row and turn it into a rewind poison.
                break
        if conn.in_transaction:
            conn.execute('COMMIT')
        summary['pending_after'] = count_pending_outbox(conn)
        return summary
    except Exception as exc:  # noqa: BLE001
        _record_error(f'drain_shadow_outbox: {exc}')
        summary['error'] = str(exc)
        summary['failed'] = max(summary['failed'], 1)
        return summary
    finally:
        if conn is not None:
            conn.close()


def drain_shadow_outbox_best_effort(
    db_path: Optional[str] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> None:
    """生产路径：drain 失败只打日志，不影响主流程。"""
    try:
        if not is_user_events_enabled(environ=environ):
            return
        drain_shadow_outbox(db_path=db_path, environ=environ)
    except Exception as exc:  # noqa: BLE001
        logger.warning('internal_state_shadow: drain best-effort failed: %s', exc)


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


def _refuse_future_clock(
    value: Any,
    *,
    field: str,
    observed_dt: datetime.datetime,
) -> None:
    """bootstrap 专属：任一来源时间不得晚于线性化 observed_at。"""
    if value is None:
        return
    if isinstance(value, datetime.datetime):
        dt = value.replace(microsecond=0)
    else:
        canon = events._canonicalize_ts(value, field=field)  # noqa: SLF001
        dt = datetime.datetime.strptime(canon, '%Y-%m-%d %H:%M:%S')
    if dt > observed_dt.replace(microsecond=0):
        raise store.StoreError(
            f'bootstrap refuses future {field}: {dt} > observed_at '
            f'{observed_dt.replace(microsecond=0)}'
        )


def _read_legacy_bundle_on_conn(
    conn: sqlite3.Connection,
    *,
    observed_dt: datetime.datetime,
) -> BootstrapBundle:
    """在调用方已打开的事务内读取一致截面并严格校验。"""
    observed_dt = observed_dt.replace(microsecond=0)
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

    _refuse_future_clock(
        wm['applied_at'], field='watermark.applied_at', observed_dt=observed_dt,
    )
    _refuse_future_clock(
        clock.last_user_at, field='clock.last_user_at', observed_dt=observed_dt,
    )
    _refuse_future_clock(
        clock.last_wake_message_at,
        field='clock.last_wake_message_at',
        observed_dt=observed_dt,
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
    """正式出生证明；初始水位不可变，当前水位只允许前进。

    合法：``state.last_scored_message_id >= proof.message_id``。
    第一次新评分把当前水位推到 101 不得作废出生证明。
    """
    if not _structural_bootstrap_present(state, event):
        return False
    if event.get('source_id') != PRODUCTION_BOOTSTRAP_SOURCE_ID:
        return False
    # bootstrap 事件版本语义：before/after 均为 0
    try:
        if int(event.get('state_version_before')) != 0:
            return False
        if int(event.get('state_version_after')) != 0:
            return False
    except Exception:
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
        proof_mid = store.require_positive_message_id(
            proof.get('message_id'), field='watermark_proof.message_id',
        )
        payload_mid = store.require_positive_message_id(
            payload.get('last_scored_message_id'),
            field='payload.last_scored_message_id',
        )
        state_mid = store.require_positive_message_id(
            state.get('last_scored_message_id'),
            field='state.last_scored_message_id',
        )
    except store.StoreError:
        return False
    if payload_mid != proof_mid:
        return False
    # 当前水位落后于初始出生水位 → 损坏 / 回拨
    if state_mid < proof_mid:
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
    """结构意义：state + applied bootstrap 事件同时存在。

    事件路径必须另过 ``_bootstrap_provenance_ok``；不得只靠本函数放行。
    """
    state = store.read_state(conn)
    event = store.read_event(conn, BOOTSTRAP_EVENT_KEY)
    return _structural_bootstrap_present(state, event)


def _read_bootstrap_gate(
    conn: sqlite3.Connection,
) -> tuple[Optional[dict], Optional[dict], str]:
    """返回 ``(state, event, gate)``；gate 为 ok / missing / invalid。"""
    state = store.read_state(conn)
    event = store.read_event(conn, BOOTSTRAP_EVENT_KEY)
    if not _structural_bootstrap_present(state, event):
        return state, event, 'missing'
    assert state is not None and event is not None
    if _bootstrap_provenance_ok(state, event):
        return state, event, 'ok'
    return state, event, 'invalid'


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
        capture_fail_n = int(_capture_evidence_failures)
    capture_alert = has_capture_alert(db_path)
    capture_preflight = capture_alert_preflight(db_path)
    proof_on = is_score_proof_enabled(environ=environ)
    events_on = is_user_events_enabled(environ=environ)
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
            score_proof_enabled=proof_on,
            user_events_enabled=events_on,
            proof_gap=None,
            proof_schema_ready=None,
            proof_max_message_id=None,
            watermark_lag=None,
            outbox_pending=None,
            outbox_schema_ready=None,
            gap_incidents_unresolved=None,
            gap_sidecar_pending=None,
            quarantine_pending=None,
            capture_evidence_failures=capture_fail_n,
            capture_alert_pending=capture_alert,
        )

    conn = None
    try:
        path = isv3.memories_db_path(db_path)
        conn = open_shadow_connection(path)
        journal = store.get_journal_mode(conn)
        state = store.read_state(conn)
        event = store.read_event(conn, BOOTSTRAP_EVENT_KEY)
        structural = _structural_bootstrap_present(state, event)
        prov = (
            _bootstrap_provenance_ok(state, event)
            if structural else False
        )
        gap = has_unresolved_proof_gap(conn, db_path=path)
        capture_intents_n = count_capture_alert_intents_pending(conn)
        pending_intents_n = count_pending_incident_intents_pending(conn)
        quarantine_intents_n = count_quarantine_intents_pending(conn)
        incidents_n = count_unresolved_gap_incidents(conn)
        sidecar_n = count_gap_sidecar_pending(path)
        quarantine_n = count_quarantine_pending(path)
        proof_max = None
        if score_proof_schema_ready(conn):
            try:
                proof_max = resolve_scored_watermark(conn)
            except store.StoreError:
                proof_max = None
        last_scored = (
            state.get('last_scored_message_id') if state is not None else None
        )
        lag = None
        if proof_max is not None and last_scored is not None:
            lag = int(proof_max) > int(last_scored)
        elif proof_max is not None and last_scored is None:
            lag = True
        pending = count_pending_outbox(conn) if outbox_schema_ready(conn) else None
        if gap and status not in ('proof_gap',):
            status = 'proof_gap'
        elif lag and (pending or 0) == 0 and status not in ('proof_gap',):
            status = 'watermark_lag'
        elif pending and pending > 0 and status not in ('proof_gap', 'watermark_lag'):
            status = 'outbox_pending'
        elif events_on and not capture_preflight['ok'] and status not in (
            'proof_gap', 'watermark_lag', 'outbox_pending',
        ):
            status = 'capture_alert_unready'
        elif (capture_fail_n > 0 or capture_alert) and status not in (
            'proof_gap', 'watermark_lag', 'outbox_pending',
        ):
            status = 'capture_evidence_failure'
        return ShadowHealth(
            enabled=True,
            bootstrapped=structural,
            state_version=(
                int(state['state_version']) if state is not None else None
            ),
            last_scored_message_id=last_scored,
            last_error=err,
            last_error_at=err_at,
            last_status=status,
            journal_mode=journal,
            provenance_ok=prov,
            score_proof_enabled=proof_on,
            user_events_enabled=events_on,
            proof_gap=gap,
            proof_schema_ready=score_proof_schema_ready(conn),
            proof_max_message_id=proof_max,
            watermark_lag=lag,
            outbox_pending=pending,
            outbox_schema_ready=outbox_schema_ready(conn),
            gap_incidents_unresolved=incidents_n,
            gap_sidecar_pending=sidecar_n,
            quarantine_pending=quarantine_n,
            capture_evidence_failures=capture_fail_n,
            capture_alert_pending=capture_alert,
            capture_alert_intents_pending=capture_intents_n,
            pending_incident_intents_pending=pending_intents_n,
            quarantine_intents_pending=quarantine_intents_n,
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
            score_proof_enabled=proof_on,
            user_events_enabled=events_on,
            proof_gap=None,
            proof_schema_ready=None,
            proof_max_message_id=None,
            watermark_lag=None,
            outbox_pending=None,
            outbox_schema_ready=None,
            gap_incidents_unresolved=None,
            gap_sidecar_pending=None,
            quarantine_pending=None,
            capture_evidence_failures=capture_fail_n,
            capture_alert_pending=capture_alert,
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
        ensure_shadow_schema(conn, db_path=path)

        proof_health = read_proof_health(conn)
        if has_unresolved_proof_gap(conn, db_path=path):
            side = read_proof_gap_sidecar(path) or {}
            code = proof_health.error_code or side.get('error_code')
            mid = proof_health.failed_message_id
            if mid is None:
                mid = side.get('failed_message_id')
            _record_error(
                'ensure_bootstrapped: proof gap unresolved '
                f'(code={code!r} message_id={mid!r})'
            )
            _record_status('proof_gap')
            return ShadowResult(
                ok=False,
                status='proof_gap',
                error=(
                    'score proof gap unresolved; refusing bootstrap with '
                    'possibly torn emotion/proof surface'
                ),
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
            )

        _state, _event, gate = _read_bootstrap_gate(conn)
        if gate == 'ok':
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
        if gate == 'invalid':
            _record_error(
                'ensure_bootstrapped: structural bootstrap exists but '
                'provenance is invalid; refusing to overwrite'
            )
            _record_status('bootstrap_provenance_invalid')
            return ShadowResult(
                ok=False,
                status='bootstrap_provenance_invalid',
                error=(
                    'bootstrap event/state present but provenance invalid; '
                    'refusing to overwrite or re-bootstrap'
                ),
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
            )

        # 线性化：先拿到写锁，再冻结 observed_at（不得在排队前填手术结束时间）
        conn.execute('BEGIN IMMEDIATE')
        try:
            # 拿锁后再读一次 gap（含 sidecar），避免与 proof writer 竞态
            if has_unresolved_proof_gap(conn, db_path=path):
                conn.execute('ROLLBACK')
                _record_status('proof_gap')
                return ShadowResult(
                    ok=False,
                    status='proof_gap',
                    error='proof gap appeared under lock; refusing bootstrap',
                    elapsed_ms=(time.monotonic() - t0) * 1000.0,
                )
            _state, _event, gate = _read_bootstrap_gate(conn)
            if gate == 'ok':
                conn.execute('COMMIT')
                _record_status('already_bootstrapped')
                _clear_error()
                return ShadowResult(
                    ok=True,
                    status='already_bootstrapped',
                    elapsed_ms=(time.monotonic() - t0) * 1000.0,
                )
            if gate == 'invalid':
                conn.execute('ROLLBACK')
                _record_error(
                    'ensure_bootstrapped: structural bootstrap exists but '
                    'provenance is invalid; refusing to overwrite'
                )
                _record_status('bootstrap_provenance_invalid')
                return ShadowResult(
                    ok=False,
                    status='bootstrap_provenance_invalid',
                    error=(
                        'bootstrap event/state present but provenance invalid; '
                        'refusing to overwrite or re-bootstrap'
                    ),
                    elapsed_ms=(time.monotonic() - t0) * 1000.0,
                )

            observed_dt = _now_beijing_dt().replace(microsecond=0)
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
        ensure_shadow_schema(conn, db_path=path)
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
    """方案 A：必须正式 provenance；非法出生证明不得跑事件。"""
    if not is_shadow_enabled(environ=environ):
        return ShadowResult(ok=True, status='disabled')

    t0 = time.monotonic()
    conn = None
    try:
        conn = open_shadow_connection(db_path)
        st, _event, gate = _read_bootstrap_gate(conn)
        if gate == 'missing':
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
        if gate == 'invalid':
            _record_status('bootstrap_provenance_invalid')
            return ShadowResult(
                ok=False,
                status='bootstrap_provenance_invalid',
                error=(
                    'bootstrap provenance invalid; refusing event '
                    '(no runner, no state_version bump, no event key)'
                ),
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
            )

        last: Optional[store.ApplyResult] = None
        for _attempt in range(max_version_retries + 1):
            st = store.read_state(conn)
            if st is None:
                raise store.StoreError('state missing')
            # 每次重试前再验 provenance，防止并发损坏后继续跑
            _st2, _ev2, gate2 = _read_bootstrap_gate(conn)
            if gate2 != 'ok':
                _record_status('bootstrap_provenance_invalid')
                return ShadowResult(
                    ok=False,
                    status='bootstrap_provenance_invalid',
                    error='bootstrap provenance became invalid mid-call',
                    elapsed_ms=(time.monotonic() - t0) * 1000.0,
                )
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


def observe_planned_user_message_shadow(
    *,
    envelope: dict,
    db_path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> ShadowResult:
    """应用无原文的已冻结 user_rule envelope。"""
    try:
        if not is_shadow_enabled(environ=environ):
            return ShadowResult(ok=True, status='disabled')
        frozen = dict(envelope)

        def make_runner(expected: Optional[int]):
            def runner(conn: sqlite3.Connection) -> store.ApplyResult:
                return events.apply_planned_user_message(
                    conn, envelope=frozen, expected_state_version=expected,
                )
            return runner

        return _shadow_call(
            'observe_planned_user_message_shadow', make_runner,
            db_path=db_path, environ=environ,
        )
    except Exception as exc:  # noqa: BLE001
        _record_error(f'observe_planned_user_message_shadow: {exc}')
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


def record_wake_outcome_shadow_if_enabled(
    *,
    wake_run_id: str,
    mode: str,
    action: str,
    fired_drive: Optional[str],
    desire_driven: bool,
    user_idle_hours: float,
    db_path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> None:
    """Best-effort wake_outcome capture after legacy executor succeeds."""
    if not wake_run_id or mode in ('dream', 'summarize'):
        return
    if not is_shadow_enabled(environ=environ):
        return
    path = isv3.memories_db_path(db_path)
    try:
        outcome_at = _now_beijing()
        result = apply_outcome_shadow(
            wake_run_id=wake_run_id,
            executor_action=action,
            desire_action=None,
            fired_drive=fired_drive,
            desire_driven=desire_driven,
            user_idle_hours=float(user_idle_hours),
            outcome_at=outcome_at,
            db_path=path,
            environ=environ,
        )
        if not result.ok:
            mark_proof_gap_standalone(
                db_path=path,
                failed_message_id=None,
                error_code='wake_outcome_capture_failed',
            )
    except Exception:
        try:
            mark_proof_gap_standalone(
                db_path=path,
                failed_message_id=None,
                error_code='wake_outcome_capture_failed',
            )
        except Exception:
            pass


def emit_user_rule_if_enabled(
    *,
    message_id: int,
    text: str,
    created_at: str,
    previous_user_at: Optional[str],
    db_path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> ShadowResult:
    """直接投递 user_rule（测试 / 管理）；生产路径应 outbox 同事务 + drain。"""
    if not is_user_events_enabled(environ=environ):
        return ShadowResult(ok=True, status='disabled')
    return observe_user_message_shadow(
        message_id=message_id,
        text=text,
        created_at=created_at,
        previous_user_at=previous_user_at,
        db_path=db_path,
        environ=environ,
    )


def emit_user_scored_if_enabled(
    *,
    message_id: int,
    scores: dict,
    scored_at: str,
    db_path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> ShadowResult:
    """直接投递 user_scored（测试 / 管理）；生产路径应 outbox 同事务 + drain。"""
    if not is_user_events_enabled(environ=environ):
        return ShadowResult(ok=True, status='disabled')
    return observe_scored_shadow(
        message_id=message_id,
        scores=scores,
        scored_at=scored_at,
        db_path=db_path,
        environ=environ,
    )


def mark_proof_gap_standalone(
    *,
    db_path: Optional[str] = None,
    failed_message_id: Optional[int],
    error_code: str,
) -> GapPersistenceResult:
    """独立短连接标记 gap，返回明确的持久化结果，永不抛向聊天主流程。"""
    path = isv3.memories_db_path(db_path)
    conn = None
    try:
        conn = open_shadow_connection(path)
        mark_proof_gap(
            conn,
            failed_message_id=failed_message_id,
            error_code=error_code,
            db_path=path,
        )
        if conn.in_transaction:
            conn.execute('COMMIT')
        return GapPersistenceResult(status='persisted_db', ledger_recorded=True)
    except Exception as exc:  # noqa: BLE001
        logger.critical(
            'internal_state_shadow: failed to persist proof gap: %s', exc,
        )
        try:
            write_proof_gap_sidecar(
                path,
                failed_message_id=failed_message_id,
                error_code=error_code,
            )
            return GapPersistenceResult(
                status='persisted_sidecar',
                sidecar_recorded=True,
                error=str(exc),
            )
        except Exception as exc2:  # noqa: BLE001
            logger.critical(
                'internal_state_shadow: sidecar gap write also failed: %s',
                exc2,
            )
            alert_recorded = note_capture_evidence_failure(
                f'gap={error_code} db={exc} sidecar={exc2}',
                db_path=path,
            )
            return GapPersistenceResult(
                status='failed',
                alert_recorded=alert_recorded,
                error=f'db={exc}; sidecar={exc2}',
            )
    finally:
        if conn is not None:
            conn.close()


__all__ = [
    'BOOTSTRAP_EVENT_KEY',
    'CAPTURE_MODE_PRODUCTION',
    'CAPTURE_MODE_TEST',
    'CAPTURE_ALERT_PATH_ENV',
    'CAPTURE_ALERT_ACK_TABLE',
    'CAPTURE_ALERT_INTENT_TABLE',
    'EVENT_TYPE_USER_RULE',
    'EVENT_TYPE_USER_SCORED',
    'GAP_ACK_TABLE',
    'GAP_INCIDENTS_TABLE',
    'OUTBOX_TABLE',
    'PRODUCTION_BOOTSTRAP_SOURCE_ID',
    'PROOF_HEALTH_TABLE',
    'PROOF_SOURCE_SCORE_AND_UPDATE',
    'SCORE_APPLIED_TABLE',
    'SCORE_PROOF_ENABLED_ENV',
    'SHADOW_ENABLED_ENV',
    'USER_EVENTS_ENABLED_ENV',
    'WATERMARK_SOURCE',
    'BootstrapBundle',
    'ProofHealth',
    'GapPersistenceResult',
    'ShadowHealth',
    'ShadowResult',
    'QUARANTINE_RECONCILE_TABLE',
    'QUARANTINE_INTENT_TABLE',
    'PENDING_INCIDENT_ACTION_TABLE',
    'PENDING_INCIDENT_INTENT_TABLE',
    'ack_proof_gap',
    'ack_capture_alert',
    'ack_capture_alert_orphan',
    'inspect_capture_alert_acks',
    'append_gap_incident_sidecar',
    'apply_outcome_shadow',
    'record_wake_outcome_shadow_if_enabled',
    'capture_bootstrap_bundle',
    'capture_alert_configured',
    'capture_alert_path',
    'capture_alert_preflight',
    'capture_evidence_failure_count',
    'clear_proof_gap',
    'compute_score_hash',
    'count_gap_sidecar_pending',
    'count_quarantine_pending',
    'count_pending_outbox',
    'count_incomplete_recovery_intents',
    'count_capture_alert_intents_pending',
    'count_pending_incident_intents_pending',
    'count_quarantine_intents_pending',
    'count_unresolved_gap_incidents',
    'drain_shadow_outbox',
    'drain_shadow_outbox_best_effort',
    'emit_user_rule_if_enabled',
    'emit_user_scored_if_enabled',
    'enqueue_outbox_in_txn',
    'enqueue_user_rule_in_txn',
    'enqueue_user_scored_in_txn',
    'ensure_bootstrapped',
    'ensure_shadow_schema',
    'gap_incidents_dir',
    'get_shadow_health',
    'has_unresolved_proof_gap',
    'has_capture_alert',
    'inspect_quarantine',
    'inspect_pending_incidents',
    'is_bootstrapped',
    'is_score_proof_enabled',
    'is_shadow_enabled',
    'is_user_events_enabled',
    'list_unresolved_gap_incidents',
    'lookup_score_proof',
    'list_gap_quarantine_files',
    'max_score_proof_message_id',
    'mark_proof_gap',
    'mark_proof_gap_standalone',
    'migrate_proof_gap_sidecar',
    'note_capture_evidence_failure',
    'observe_scored_shadow',
    'observe_user_message_shadow',
    'open_shadow_connection',
    'outbox_schema_ready',
    'outbox_sidecar_path',
    'proof_gap_sidecar_path',
    'quarantine_legacy_outbox_sidecar',
    'read_proof_gap_sidecar',
    'read_proof_health',
    'reconcile_quarantine',
    'recover_pending_incident_tmp',
    'recover_pending_incident_intents',
    'recover_capture_alert_acks',
    'recover_quarantine_intents',
    'record_score_proof_in_txn',
    'resolve_scored_watermark',
    'resolve_scored_watermark_row',
    'score_proof_schema_ready',
    'validate_bootstrap_snapshot',
    'write_proof_gap_sidecar',
    '_ensure_bootstrapped_for_test',
]
