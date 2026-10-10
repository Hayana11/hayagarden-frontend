"""Default-off, metadata-only Hidden Flow observability.

This module deliberately emits bounded structured logs instead of writing to
the chat or Hidden Flow databases.  It never receives message bodies, model
output, private guidance, or control-tag text.
"""

from __future__ import annotations

import datetime as _datetime
import fcntl
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


_LOGGER = logging.getLogger("chat.hidden_flow.observability")
# Gunicorn's production default is INFO, but make this module's threshold
# explicit so a future parent/root logging change cannot discard enabled
# observations before they reach the journal or the dedicated sink.
_LOGGER.setLevel(logging.INFO)
_SCHEMA_VERSION = 1
_DEFAULT_MAX_EVENTS_PER_MINUTE = 120
_DEFAULT_RETENTION_DAYS = 7
_DEFAULT_LOG_PATH = Path(
    "/var/log/hayagarden/hidden-flow-observability.jsonl"
)
_ALLOWED_PREPARE_OUTCOMES = frozenset({"success", "skipped", "failed"})
_ALLOWED_CONTROL_RESULTS = frozenset({
    "none",
    "valid",
    "invalid",
    "flow_id_mismatch",
    "unknown",
})
_ALLOWED_SNAPSHOT_STATUSES = frozenset({
    "pending",
    "committed",
    "conflict",
    "rollback",
})
_CODE_RE = re.compile(r"[^A-Za-z0-9_.-]+")

_SETTINGS_LOCK = threading.Lock()
_SETTINGS_CACHE: tuple[float, tuple[bool, int, int]] | None = None
_BUDGET_LOCK = threading.Lock()
_EVENT_TIMES: deque[float] = deque()
_DROPPED_EVENTS = 0
_FILE_HANDLER_LOCK = threading.Lock()
_FILE_HANDLER: logging.Handler | None = None
_FILE_HANDLER_PATH: Path | None = None


class _LockedJsonFileHandler(logging.Handler):
    """Append one structured event at a time across Gunicorn workers."""

    def __init__(self, path: Path) -> None:
        super().__init__(level=logging.INFO)
        self.path = path
        self.lock_path = path.with_name(path.name + ".lock")
        self._thread_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record).rstrip("\n") + "\n"
            self.path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            with self._thread_lock:
                with self.lock_path.open("a+", encoding="utf-8") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    try:
                        with self.path.open("a", encoding="utf-8") as stream:
                            stream.write(message)
                            stream.flush()
                    finally:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except Exception:
            # Diagnostics must never change the chat or runtime outcome.
            return


def _configured_log_path() -> Path:
    configured = os.environ.get("HIDDEN_FLOW_OBSERVABILITY_LOG_PATH", "")
    return Path(configured.strip() or _DEFAULT_LOG_PATH)


def _remove_file_handler() -> None:
    global _FILE_HANDLER, _FILE_HANDLER_PATH
    with _FILE_HANDLER_LOCK:
        if _FILE_HANDLER is not None:
            _LOGGER.removeHandler(_FILE_HANDLER)
            _FILE_HANDLER.close()
        _FILE_HANDLER = None
        _FILE_HANDLER_PATH = None
        _LOGGER.propagate = True


def _ensure_file_handler() -> None:
    global _FILE_HANDLER, _FILE_HANDLER_PATH
    path = _configured_log_path()
    with _FILE_HANDLER_LOCK:
        if _FILE_HANDLER is not None and _FILE_HANDLER_PATH == path:
            return
        if _FILE_HANDLER is not None:
            _LOGGER.removeHandler(_FILE_HANDLER)
            _FILE_HANDLER.close()
        handler = _LockedJsonFileHandler(path)
        handler.setFormatter(logging.Formatter("%(message)s"))
        _LOGGER.addHandler(handler)
        # Do not duplicate these events into journald, whose global retention
        # policy cannot selectively delete only Hidden Flow observations.
        _LOGGER.propagate = False
        _FILE_HANDLER = handler
        _FILE_HANDLER_PATH = path


def _parse_utc(value: Any) -> _datetime.datetime | None:
    try:
        parsed = _datetime.datetime.fromisoformat(
            str(value or "").replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_datetime.timezone.utc)
    return parsed.astimezone(_datetime.timezone.utc)


def prune_observability_log(
    path: str | os.PathLike[str] | None = None,
    *,
    now: _datetime.datetime | None = None,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
) -> tuple[int, int]:
    """Delete expired events from the dedicated sink under a cross-worker lock."""
    log_path = Path(path) if path is not None else _configured_log_path()
    if not log_path.is_file():
        return 0, 0
    current = now or _datetime.datetime.now(_datetime.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_datetime.timezone.utc)
    current = current.astimezone(_datetime.timezone.utc)
    fallback_days = max(1, min(int(retention_days), 30))
    lock_path = log_path.with_name(log_path.name + ".lock")
    kept_lines: list[str] = []
    removed = 0
    kept = 0
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with log_path.open("r", encoding="utf-8") as source:
                for line in source:
                    try:
                        payload = json.loads(line)
                    except (TypeError, ValueError):
                        kept_lines.append(line)
                        kept += 1
                        continue
                    if payload.get("event") != "hidden_flow_observation":
                        kept_lines.append(line)
                        kept += 1
                        continue
                    expires = _parse_utc(payload.get("retention_until"))
                    if expires is None:
                        observed = _parse_utc(payload.get("observed_at"))
                        if observed is not None:
                            expires = observed + _datetime.timedelta(
                                days=fallback_days
                            )
                    if expires is not None and expires <= current:
                        removed += 1
                    else:
                        kept_lines.append(line)
                        kept += 1
            if removed:
                temporary = log_path.with_name(log_path.name + ".tmp")
                with temporary.open("w", encoding="utf-8") as target:
                    target.writelines(kept_lines)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, log_path)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return removed, kept


def _safe_code(value: Any, default: str = "unknown") -> str:
    text = _CODE_RE.sub("_", str(value or "").strip())[:80]
    return text or default


def request_contains_private_guidance(
    content: Any,
    expected_block: str,
) -> bool:
    """Verify the exact private block is present in the final request carrier."""
    expected = str(expected_block or "")
    if not expected:
        return False
    if isinstance(content, str):
        return expected in content
    if isinstance(content, (list, tuple)):
        for part in content:
            if not isinstance(part, Mapping):
                continue
            text = part.get("text")
            if isinstance(text, str) and expected in text:
                return True
    return False


def _load_settings() -> tuple[bool, int, int]:
    global _SETTINGS_CACHE
    now = time.monotonic()
    with _SETTINGS_LOCK:
        if _SETTINGS_CACHE is not None and now - _SETTINGS_CACHE[0] < 1.0:
            return _SETTINGS_CACHE[1]
        enabled = False
        max_events = _DEFAULT_MAX_EVENTS_PER_MINUTE
        retention_days = _DEFAULT_RETENTION_DAYS
        try:
            import config_store
            enabled = bool(config_store.get_bool(
                "HIDDEN_FLOW_OBSERVABILITY_ENABLED",
                False,
            ))
            max_events = int(config_store.get_int(
                "HIDDEN_FLOW_OBSERVABILITY_MAX_EVENTS_PER_MINUTE",
                _DEFAULT_MAX_EVENTS_PER_MINUTE,
            ))
            retention_days = int(config_store.get_int(
                "HIDDEN_FLOW_OBSERVABILITY_RETENTION_DAYS",
                _DEFAULT_RETENTION_DAYS,
            ))
        except Exception:
            enabled = False
        max_events = max(1, min(max_events, 5000))
        retention_days = max(1, min(retention_days, 30))
        value = (enabled, max_events, retention_days)
        _SETTINGS_CACHE = (now, value)
        return value


def _take_budget(max_events: int) -> tuple[bool, int]:
    global _DROPPED_EVENTS
    now = time.monotonic()
    with _BUDGET_LOCK:
        while _EVENT_TIMES and now - _EVENT_TIMES[0] >= 60.0:
            _EVENT_TIMES.popleft()
        if len(_EVENT_TIMES) >= max_events:
            _DROPPED_EVENTS += 1
            return False, 0
        dropped = _DROPPED_EVENTS
        _DROPPED_EVENTS = 0
        _EVENT_TIMES.append(now)
        return True, dropped


@dataclass
class HiddenFlowObservation:
    enabled: bool = False
    turn_id: str = ""
    provider: str = ""
    turn_kind: str = ""
    gate_enabled: bool = False
    eligible: bool = False
    max_events_per_minute: int = _DEFAULT_MAX_EVENTS_PER_MINUTE
    retention_days: int = _DEFAULT_RETENTION_DAYS

    def _emit(self, phase: str, **fields: Any) -> None:
        if not self.enabled or not self.turn_id:
            return
        try:
            allowed, dropped_events = _take_budget(
                self.max_events_per_minute,
            )
            if not allowed:
                return
            now = _datetime.datetime.now(_datetime.timezone.utc)
            payload = {
                "event": "hidden_flow_observation",
                "schema_version": _SCHEMA_VERSION,
                "phase": _safe_code(phase),
                "turn_id": self.turn_id,
                "provider": _safe_code(self.provider),
                "turn_kind": _safe_code(self.turn_kind),
                "gate_enabled": bool(self.gate_enabled),
                "eligible": bool(self.eligible),
                "observed_at": now.isoformat(timespec="seconds"),
                "retention_days": int(self.retention_days),
                "retention_until": (
                    now + _datetime.timedelta(days=self.retention_days)
                ).isoformat(timespec="seconds"),
            }
            if dropped_events:
                payload["rate_limited_dropped_events"] = int(dropped_events)
            payload.update(fields)
            _LOGGER.info(json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ))
        except Exception:
            # Diagnostics must never change the chat or runtime outcome.
            return

    def record_prepare(
        self,
        *,
        outcome: str,
        available_config_count: int = 0,
        private_guidance_nonempty: bool = False,
        error_code: Optional[str] = None,
    ) -> None:
        result = str(outcome or "unknown").strip().lower()
        if result not in _ALLOWED_PREPARE_OUTCOMES:
            result = "failed"
        self._emit(
            "prepare",
            prepare=result,
            available_config_count=max(0, int(available_config_count or 0)),
            private_guidance_nonempty=bool(private_guidance_nonempty),
            **({"error_code": _safe_code(error_code)} if error_code else {}),
        )

    def record_request_attachment(
        self,
        *,
        attached: bool,
        expected_block_nonempty: bool = True,
    ) -> None:
        verified = bool(attached) and bool(expected_block_nonempty)
        status = (
            "verified"
            if verified
            else ("missing" if expected_block_nonempty else "empty")
        )
        self._emit(
            "request_attachment",
            private_guidance_attached=bool(attached),
            private_guidance_attachment_verified=verified,
            attachment_status=status,
        )

    def record_control_parse(self, result: str) -> None:
        value = str(result or "unknown").strip().lower()
        if value not in _ALLOWED_CONTROL_RESULTS:
            value = "unknown"
        self._emit("control_parse", control_parse=value)

    def record_transition(self, *, created: bool) -> None:
        self._emit("transition", transition_created=bool(created))

    def record_snapshot(self, status: str, *, error_code: Optional[str] = None) -> None:
        value = str(status or "rollback").strip().lower()
        if value not in _ALLOWED_SNAPSHOT_STATUSES:
            value = "rollback"
        self._emit(
            "snapshot",
            snapshot_status=value,
            **({"error_code": _safe_code(error_code)} if error_code else {}),
        )


def begin_turn(
    *,
    provider: str,
    turn_kind: str,
    gate_enabled: bool,
    eligible: bool,
) -> HiddenFlowObservation:
    enabled, max_events, retention_days = _load_settings()
    if not enabled:
        _remove_file_handler()
        return HiddenFlowObservation()
    _ensure_file_handler()
    try:
        turn_id = secrets.token_hex(9)
    except Exception:
        return HiddenFlowObservation()
    return HiddenFlowObservation(
        enabled=True,
        turn_id=turn_id,
        provider=str(provider or ""),
        turn_kind=str(turn_kind or ""),
        gate_enabled=bool(gate_enabled),
        eligible=bool(eligible),
        max_events_per_minute=max_events,
        retention_days=retention_days,
    )


def reset_for_tests() -> None:
    global _SETTINGS_CACHE, _DROPPED_EVENTS
    _remove_file_handler()
    with _SETTINGS_LOCK:
        _SETTINGS_CACHE = None
    with _BUDGET_LOCK:
        _EVENT_TIMES.clear()
        _DROPPED_EVENTS = 0
