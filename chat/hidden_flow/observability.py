"""Default-off, metadata-only Hidden Flow observability.

This module deliberately emits bounded structured logs instead of writing to
the chat or Hidden Flow databases.  It never receives message bodies, model
output, private guidance, or control-tag text.
"""

from __future__ import annotations

import datetime as _datetime
import json
import logging
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional


_LOGGER = logging.getLogger("chat.hidden_flow.observability")
_SCHEMA_VERSION = 1
_DEFAULT_MAX_EVENTS_PER_MINUTE = 120
_DEFAULT_RETENTION_DAYS = 7
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


def _safe_code(value: Any, default: str = "unknown") -> str:
    text = _CODE_RE.sub("_", str(value or "").strip())[:80]
    return text or default


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


def _take_budget(max_events: int) -> bool:
    now = time.monotonic()
    with _BUDGET_LOCK:
        while _EVENT_TIMES and now - _EVENT_TIMES[0] >= 60.0:
            _EVENT_TIMES.popleft()
        if len(_EVENT_TIMES) >= max_events:
            return False
        _EVENT_TIMES.append(now)
        return True


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
            if not _take_budget(self.max_events_per_minute):
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
                "retention_until": (
                    now + _datetime.timedelta(days=self.retention_days)
                ).isoformat(timespec="seconds"),
            }
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

    def record_request_attachment(self, *, attached: bool) -> None:
        self._emit(
            "request_attachment",
            private_guidance_attached=bool(attached),
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
        return HiddenFlowObservation()
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
    global _SETTINGS_CACHE
    with _SETTINGS_LOCK:
        _SETTINGS_CACHE = None
    with _BUDGET_LOCK:
        _EVENT_TIMES.clear()
