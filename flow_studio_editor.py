"""Owner-authenticated Flow Studio editor storage and runtime compiler.

The editor document is intentionally separate from Hidden Flow's runtime config.
Saving this module's document never writes hidden_flow_configs and never starts
or changes the Hidden Flow engine.
"""

from __future__ import annotations

import copy
import datetime as _datetime
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any, Optional

from flask import Blueprint, jsonify, request

import config_store
from daily_context_bff import same_origin_mutation_ok
from moments_auth import OwnerAuthError, require_owner
from chat.hidden_flow.config import normalize_flow_config
from chat.hidden_flow.types import AppliedGuide, FlowState, MAX_GLOBAL_GUIDANCE_CHARS, MAX_STAGE_GUIDANCE_CHARS

EDITOR_SCHEMA_VERSION = 1
RUNTIME_SCHEMA_VERSION = 1
DEFAULT_DB_PATH = os.environ.get("HAYA_DB_PATH", "/opt/frontend/memories.db")
MAX_BODY_BYTES = 1_500_000
MAX_STAGES = 64
MAX_POOLS = 128
MAX_ENTRIES_PER_POOL = 320
MAX_CUES = 256
MAX_TEXT_CHARS = 1200
MAX_NAME_CHARS = 200


def _now() -> str:
    return (_datetime.datetime.utcnow() + _datetime.timedelta(hours=8)).isoformat(
        timespec="seconds"
    )


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _is_bool(value: Any) -> bool:
    return type(value) is bool


def _is_int(value: Any) -> bool:
    return type(value) is int


def _response(payload: Mapping[str, Any], status: int = 200):
    response = jsonify(dict(payload))
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _error(code: str, message: str, status: int, **extra: Any):
    payload = {"ok": False, "code": code, "error": message}
    payload.update(extra)
    return _response(payload, status)


def _auth_error(exc: OwnerAuthError):
    response = _error("OWNER_AUTH_REQUIRED", exc.message, exc.status_code)
    if exc.status_code == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _guard(owner_guard: Callable[[Any], None]):
    try:
        owner_guard(request)
    except OwnerAuthError as exc:
        return _auth_error(exc)
    return None


def _body_too_large() -> bool:
    length = request.content_length
    return isinstance(length, int) and length > MAX_BODY_BYTES


def _request_payload() -> tuple[Optional[dict[str, Any]], Optional[Any]]:
    if _body_too_large():
        return None, _error(
            "EDITOR_REQUEST_TOO_LARGE",
            "editor request exceeds the size limit",
            413,
        )
    raw_body = request.get_data(cache=True)
    if len(raw_body) > MAX_BODY_BYTES:
        return None, _error(
            "EDITOR_REQUEST_TOO_LARGE",
            "editor request exceeds the size limit",
            413,
        )
    if not request.is_json:
        return None, _error(
            "EDITOR_JSON_REQUIRED",
            "application/json request body required",
            415,
        )
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None, _error(
            "EDITOR_JSON_INVALID",
            "request body must be a JSON object",
            400,
        )
    try:
        _json(payload)
    except (TypeError, ValueError):
        return None, _error(
            "EDITOR_JSON_INVALID",
            "request body contains non-finite or unsupported JSON",
            400,
        )
    return payload, None


def _runtime_row(
    connection: sqlite3.Connection,
    flow_id: str,
) -> Optional[dict[str, Any]]:
    row = connection.execute(
        """
        SELECT flow_id, schema_version, enabled, config_json, version, created_at, updated_at
        FROM hidden_flow_configs
        WHERE flow_id=?
        """,
        (flow_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _decode_runtime(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = json.loads(str(row["config_json"]))
    if not isinstance(raw, dict):
        raise ValueError("runtime config is not a JSON object")
    return raw



def _ensure_stage_transition_modes(document: Mapping[str, Any]) -> dict[str, Any]:
    result = _copy(dict(document))
    if "guidancePrompt" not in result:
        result["guidancePrompt"] = result.get("guidance_prompt", "")
    result.pop("guidance_prompt", None)
    stages = result.get("stages") or []
    enabled = [stage for stage in stages if stage.get("enabled") is True]
    enabled_ids = [stage.get("id") for stage in enabled]
    enabled_positions = {
        stage.get("id"): position for position, stage in enumerate(enabled)
    }
    for stage in stages:
        stage.setdefault("guidancePrompt", "")
        mode = stage.get("nextStageMode")
        if mode not in {"sequence", "explicit"}:
            next_id = stage.get("nextStageId")
            position = enabled_positions.get(stage.get("id"))
            expected = (
                enabled_ids[position + 1]
                if position is not None and position + 1 < len(enabled_ids)
                else None
            )
            mode = (
                "sequence"
                if stage.get("terminal") is True
                or next_id is None
                or next_id == expected
                else "explicit"
            )
        stage["nextStageMode"] = mode
    return result


def _runtime_to_editor(flow_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    document: dict[str, Any] = _copy(dict(raw))
    document["schemaVersion"] = EDITOR_SCHEMA_VERSION
    document["flowId"] = flow_id
    document["enabled"] = bool(raw.get("enabled", False))
    document["guidancePrompt"] = _text(
        raw.get("guidancePrompt", raw.get("guidance_prompt", ""))
    )
    document["initialStage"] = _text(raw.get("initialStage")) or None

    stages: list[dict[str, Any]] = []
    for raw_stage in raw.get("stages") or []:
        if not isinstance(raw_stage, Mapping):
            continue
        stage = _copy(dict(raw_stage))
        stage_id = _text(stage.get("id"))
        stage["id"] = stage_id
        stage["name"] = _text(stage.get("name")) or stage_id
        stage["nextStageId"] = stage.get("nextStage")
        stage.pop("nextStage", None)
        stage.setdefault("enabled", False)
        stage.setdefault("minTurns", 1)
        stage.setdefault("repeatMinTurns", 1)
        stage.setdefault("terminal", False)
        stage.setdefault("guidancePrompt", "")
        stage.setdefault("poolIds", [])
        stages.append(stage)
    document["stages"] = stages
    document = _ensure_stage_transition_modes(document)

    pools: list[dict[str, Any]] = []
    for raw_pool in raw.get("pools") or []:
        if not isinstance(raw_pool, Mapping):
            continue
        pool = _copy(dict(raw_pool))
        pool_id = _text(pool.get("id"))
        pool["id"] = pool_id
        pool["name"] = _text(pool.get("name")) or pool_id
        pool["mode"] = "perStage" if pool.get("drawMode") == "cycle" else "perTurn"
        pool["count"] = pool.get("drawCount", 1)
        pool.pop("drawMode", None)
        pool.pop("drawCount", None)
        entries: list[dict[str, Any]] = []
        for raw_entry in pool.get("entries") or []:
            if isinstance(raw_entry, Mapping):
                entry = _copy(dict(raw_entry))
                entry["id"] = _text(entry.get("id"))
                entry["text"] = entry.get("text", "")
                entry.setdefault("enabled", True)
                entries.append(entry)
        pool["entries"] = entries
        pool.setdefault("enabled", False)
        pools.append(pool)
    document["pools"] = pools

    cues: list[dict[str, Any]] = []
    for raw_cue in raw.get("cues") or []:
        if not isinstance(raw_cue, Mapping):
            continue
        cue = _copy(dict(raw_cue))
        cue["id"] = _text(cue.get("id"))
        cue["key"] = cue.get("key", "")
        cue.setdefault("kind", "其他")
        cue.setdefault("enabled", False)
        cue.setdefault("poolIds", [])
        cues.append(cue)
    document["cues"] = cues
    document.setdefault("dimensions", [])
    return document


def _load_bundle(
    connection: sqlite3.Connection,
    flow_id: str,
) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
    runtime = _runtime_row(connection, flow_id)
    if runtime is None:
        raise KeyError(flow_id)
    editor_row = connection.execute(
        """
        SELECT flow_id, schema_version, document_json, revision, created_at, updated_at
        FROM flow_studio_editor_documents
        WHERE flow_id=?
        """,
        (flow_id,),
    ).fetchone()
    if editor_row is None:
        document = _runtime_to_editor(flow_id, _decode_runtime(runtime))
        return runtime, document, None
    try:
        document = json.loads(str(editor_row["document_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("stored editor document is invalid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("stored editor document is not an object")
    return runtime, _ensure_stage_transition_modes(document), dict(editor_row)


def _validate_editor_document(
    document: Any,
    flow_id: str,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    if not isinstance(document, dict):
        return None, ["document must be an object"]
    try:
        if len(_json(document).encode("utf-8")) > MAX_BODY_BYTES:
            return None, ["document exceeds the size limit"]
    except (TypeError, ValueError):
        return None, ["document contains unsupported JSON values"]

    errors: list[str] = []
    if document.get("schemaVersion") != EDITOR_SCHEMA_VERSION:
        errors.append("schemaVersion must be 1")
    if document.get("flowId") != flow_id:
        errors.append("flowId must match the requested flow")
    if not _is_bool(document.get("enabled")):
        errors.append("enabled must be boolean")
    guidance_prompt = document.get("guidancePrompt", "")
    if not isinstance(guidance_prompt, str):
        errors.append("guidancePrompt must be a string")
    elif len(guidance_prompt) > MAX_GLOBAL_GUIDANCE_CHARS:
        errors.append(
            f"guidancePrompt is too long (maximum {MAX_GLOBAL_GUIDANCE_CHARS} characters)"
        )
    initial_stage = document.get("initialStage")
    if initial_stage is not None and (
        not isinstance(initial_stage, str) or not initial_stage.strip()
    ):
        errors.append("initialStage must be a string or null")

    stages = document.get("stages")
    pools = document.get("pools")
    cues = document.get("cues")
    dimensions = document.get("dimensions", [])
    if not isinstance(stages, list) or len(stages) > MAX_STAGES:
        errors.append("stages must be an array within the size limit")
        stages = []
    if not isinstance(pools, list) or len(pools) > MAX_POOLS:
        errors.append("pools must be an array within the size limit")
        pools = []
    if not isinstance(cues, list) or len(cues) > MAX_CUES:
        errors.append("cues must be an array within the size limit")
        cues = []
    if not isinstance(dimensions, list):
        errors.append("dimensions must be an array")

    stage_ids: set[str] = set()
    for index, stage in enumerate(stages):
        path = f"stages[{index}]"
        if not isinstance(stage, dict):
            errors.append(f"{path} must be an object")
            continue
        stage_id = _text(stage.get("id"))
        if not stage_id or stage_id in stage_ids:
            errors.append(f"{path}.id must be unique and non-empty")
        stage_ids.add(stage_id)
        if not _is_bool(stage.get("enabled")):
            errors.append(f"{path}.enabled must be boolean")
        if not isinstance(stage.get("name"), str) or not stage["name"].strip():
            errors.append(f"{path}.name must be non-empty")
        elif len(stage["name"]) > MAX_NAME_CHARS:
            errors.append(f"{path}.name is too long")
        guidance_prompt = stage.get("guidancePrompt", "")
        if not isinstance(guidance_prompt, str):
            errors.append(f"{path}.guidancePrompt must be a string")
        elif len(guidance_prompt) > MAX_STAGE_GUIDANCE_CHARS:
            errors.append(f"{path}.guidancePrompt is too long")
        for field in ("minTurns", "repeatMinTurns"):
            if not _is_int(stage.get(field)):
                errors.append(f"{path}.{field} must be an integer")
            elif not 1 <= stage[field] <= 99:
                errors.append(f"{path}.{field} must be between 1 and 99")
        for field in ("terminal", "holdable"):
            if field in stage and not _is_bool(stage[field]):
                errors.append(f"{path}.{field} must be boolean")
        next_id = stage.get("nextStageId")
        if next_id is not None and (not isinstance(next_id, str) or not next_id.strip()):
            errors.append(f"{path}.nextStageId must be a string or null")
        transition_mode = stage.get("nextStageMode")
        if transition_mode is not None and transition_mode not in {"sequence", "explicit"}:
            errors.append(f"{path}.nextStageMode must be sequence or explicit")
        if transition_mode == "explicit" and next_id is None:
            errors.append(f"{path}.explicit transition requires nextStageId")
        pool_ids = stage.get("poolIds")
        if not isinstance(pool_ids, list) or any(
            not isinstance(item, str) or not item.strip() for item in pool_ids
        ):
            errors.append(f"{path}.poolIds must be an array of strings")

    pool_ids: set[str] = set()
    for index, pool in enumerate(pools):
        path = f"pools[{index}]"
        if not isinstance(pool, dict):
            errors.append(f"{path} must be an object")
            continue
        pool_id = _text(pool.get("id"))
        if not pool_id or pool_id in pool_ids:
            errors.append(f"{path}.id must be unique and non-empty")
        pool_ids.add(pool_id)
        if not _is_bool(pool.get("enabled")):
            errors.append(f"{path}.enabled must be boolean")
        if not isinstance(pool.get("name"), str) or not pool["name"].strip():
            errors.append(f"{path}.name must be non-empty")
        elif len(pool["name"]) > MAX_NAME_CHARS:
            errors.append(f"{path}.name is too long")
        if pool.get("mode") not in {"perTurn", "perStage"}:
            errors.append(f"{path}.mode must be perTurn or perStage")
        if not _is_int(pool.get("count")) or not 1 <= pool.get("count", 0) <= 99:
            errors.append(f"{path}.count must be between 1 and 99")
        entries = pool.get("entries")
        if not isinstance(entries, list) or len(entries) > MAX_ENTRIES_PER_POOL:
            errors.append(f"{path}.entries exceeds the size limit")
            continue
        entry_ids: set[str] = set()
        for entry_index, entry in enumerate(entries):
            entry_path = f"{path}.entries[{entry_index}]"
            if not isinstance(entry, dict):
                errors.append(f"{entry_path} must be an object")
                continue
            entry_id = _text(entry.get("id"))
            if not entry_id or entry_id in entry_ids:
                errors.append(f"{entry_path}.id must be unique and non-empty")
            entry_ids.add(entry_id)
            if not isinstance(entry.get("text"), str) or len(entry["text"]) > MAX_TEXT_CHARS:
                errors.append(f"{entry_path}.text is invalid or too long")
            if not _is_bool(entry.get("enabled")):
                errors.append(f"{entry_path}.enabled must be boolean")

    cue_ids: set[str] = set()
    for index, cue in enumerate(cues):
        path = f"cues[{index}]"
        if not isinstance(cue, dict):
            errors.append(f"{path} must be an object")
            continue
        cue_id = _text(cue.get("id"))
        if not cue_id or cue_id in cue_ids:
            errors.append(f"{path}.id must be unique and non-empty")
        cue_ids.add(cue_id)
        if not isinstance(cue.get("key"), str) or len(cue["key"]) > MAX_NAME_CHARS:
            errors.append(f"{path}.key is invalid or too long")
        if not isinstance(cue.get("kind"), str):
            errors.append(f"{path}.kind must be a string")
        if not _is_bool(cue.get("enabled")):
            errors.append(f"{path}.enabled must be boolean")
        if not isinstance(cue.get("poolIds"), list) or any(
            not isinstance(item, str) or not item.strip() for item in cue["poolIds"]
        ):
            errors.append(f"{path}.poolIds must be an array of strings")

    if not errors:
        normalized_document = _copy(document)
        normalized_document["guidancePrompt"] = _text(
            normalized_document.get("guidancePrompt")
        )
        for stage in normalized_document.get("stages") or []:
            if isinstance(stage, dict):
                stage["guidancePrompt"] = _text(stage.get("guidancePrompt"))
        return normalized_document, errors
    return None, errors


def _materialize_stage_order(document: dict[str, Any]) -> dict[str, Any]:
    result = _ensure_stage_transition_modes(document)
    stages = result.get("stages") or []
    enabled = [stage for stage in stages if stage.get("enabled") is True]
    result["initialStage"] = enabled[0]["id"] if enabled else None
    enabled_ids = [stage["id"] for stage in enabled]
    for index, stage in enumerate(enabled):
        if stage.get("nextStageMode") == "sequence":
            stage["nextStageId"] = (
                None
                if stage.get("terminal") is True or index + 1 >= len(enabled_ids)
                else enabled_ids[index + 1]
            )
    return result


def compile_editor_document(
    document: Mapping[str, Any],
) -> tuple[Optional[dict[str, Any]], list[str], list[str]]:
    """Compile without persistence or engine calls.

    Disabled editor stages and cues remain in the editor document but are
    excluded from Runtime FlowConfig v1. Disabled pools remain in the runtime
    config so their existing references stay valid; draw_pool() skips them.
    """
    document = _materialize_stage_order(_copy(document))
    runtime: dict[str, Any] = {
        "schemaVersion": RUNTIME_SCHEMA_VERSION,
        "flowId": document.get("flowId"),
        "enabled": bool(document.get("enabled")),
        "guidancePrompt": "",
        "initialStage": document.get("initialStage"),
        "stages": [],
        "cues": [],
        "pools": [],
    }
    errors: list[str] = []
    unsupported: list[str] = []

    guidance_prompt = document.get("guidancePrompt", "")
    if not isinstance(guidance_prompt, str) or len(guidance_prompt) > MAX_GLOBAL_GUIDANCE_CHARS:
        errors.append(
            f"guidancePrompt must be at most {MAX_GLOBAL_GUIDANCE_CHARS} characters"
        )
        guidance_prompt = ""
    else:
        guidance_prompt = _text(guidance_prompt)
    runtime["guidancePrompt"] = guidance_prompt

    stages = document.get("stages") or []
    pools = document.get("pools") or []
    cues = document.get("cues") or []
    existing_pool_ids = {
        pool["id"] for pool in pools
    }
    enabled_stage_ids = {
        stage["id"] for stage in stages if stage.get("enabled") is True
    }
    enabled_stage_order = [
        stage["id"] for stage in stages if stage.get("enabled") is True
    ]

    if document.get("dimensions"):
        unsupported.append("dimensions")
    if document.get("initialStage") is None and enabled_stage_order:
        unsupported.append("initialStage (derived from first enabled stage)")

    for pool in pools:
        pool_id = pool["id"]
        if pool.get("name"):
            unsupported.append(f"pools[{pool_id}].name")
        mode = {"perTurn": "turn", "perStage": "cycle"}.get(pool.get("mode"))
        if mode is None:
            errors.append(f"pool {pool_id}: unsupported draw mode")
            continue
        count = pool.get("count")
        if not _is_int(count) or not 1 <= count <= 5:
            errors.append(f"pool {pool_id}: draw count must be between 1 and 5")
            continue
        entries = []
        for entry in pool.get("entries") or []:
            if entry.get("enabled") is not True:
                unsupported.append(f"pools[{pool_id}].entries[{entry['id']}].enabled")
                continue
            text = entry.get("text")
            if not isinstance(text, str) or not text.strip():
                errors.append(f"pool {pool_id}: enabled entry text is empty")
                continue
            entries.append({"id": entry["id"], "text": text})
        if len(entries) > 160:
            errors.append(f"pool {pool_id}: enabled entries exceed runtime limit 160")
        runtime["pools"].append({
            "id": pool_id,
            "enabled": bool(pool.get("enabled")),
            "drawMode": mode,
            "drawCount": count,
            "entries": entries,
        })

    for stage in stages:
        stage_id = stage["id"]
        if stage.get("name"):
            unsupported.append(f"stages[{stage_id}].name")
        if stage.get("enabled") is not True:
            unsupported.append(f"stages[{stage_id}].enabled")
            continue
        min_turns = stage.get("minTurns")
        repeat_min_turns = stage.get("repeatMinTurns")
        if not _is_int(min_turns) or not 1 <= min_turns <= 12:
            errors.append(f"stage {stage_id}: minTurns must be between 1 and 12")
        if not _is_int(repeat_min_turns) or not 1 <= repeat_min_turns <= 12:
            errors.append(f"stage {stage_id}: repeatMinTurns must be between 1 and 12")
        guidance_prompt = stage.get("guidancePrompt", "")
        if not isinstance(guidance_prompt, str) or len(guidance_prompt) > MAX_STAGE_GUIDANCE_CHARS:
            errors.append(f"stage {stage_id}: guidancePrompt must be at most {MAX_STAGE_GUIDANCE_CHARS} characters")
            guidance_prompt = ""
        else:
            guidance_prompt = _text(guidance_prompt)
        if stage.get("nextStageMode") == "sequence":
            position = enabled_stage_order.index(stage_id)
            next_stage = (
                None
                if stage.get("terminal") is True or position + 1 >= len(enabled_stage_order)
                else enabled_stage_order[position + 1]
            )
            if next_stage is not None:
                unsupported.append(f"stages[{stage_id}].nextStageId (derived)")
        else:
            next_stage = stage.get("nextStageId")
        if next_stage is not None and next_stage not in enabled_stage_ids:
            errors.append(f"stage {stage_id}: nextStageId is not an enabled stage")
        if stage.get("terminal") is True and (next_stage is not None or stage.get("continueTarget")):
            errors.append(f"stage {stage_id}: terminal stage cannot continue")
        pool_refs = stage.get("poolIds") or []
        if any(pool_id not in existing_pool_ids for pool_id in pool_refs):
            errors.append(f"stage {stage_id}: references a missing pool")
        runtime["stages"].append({
            "id": stage_id,
            "enabled": True,
            "minTurns": min_turns,
            "repeatMinTurns": repeat_min_turns,
            "nextStage": next_stage,
            "terminal": bool(stage.get("terminal")),
            "continueTarget": stage.get("continueTarget"),
            "terminalWithoutContinue": bool(stage.get("terminalWithoutContinue", False)),
            "holdable": bool(stage.get("holdable", False)),
            "poolIds": list(pool_refs),
            "guidancePrompt": guidance_prompt,
        })

    initial_stage = runtime.get("initialStage")
    if initial_stage is None and enabled_stage_order:
        initial_stage = enabled_stage_order[0]
        runtime["initialStage"] = initial_stage
    if initial_stage not in enabled_stage_ids:
        errors.append("initialStage must reference an enabled stage")
    if enabled_stage_order and runtime["stages"] and runtime["stages"][-1].get("terminal") is not True:
        errors.append("the final enabled stage must be marked as terminal")

    enabled_cue_keys: set[str] = set()
    for cue in cues:
        cue_id = cue["id"]
        if cue.get("kind"):
            unsupported.append(f"cues[{cue_id}].kind")
        key = cue.get("key")
        if not isinstance(key, str) or not key.strip():
            errors.append(f"cue {cue_id}: key is empty")
            continue
        folded = key.casefold()
        if cue.get("enabled") is True:
            if folded in enabled_cue_keys:
                errors.append(f"cue {cue_id}: duplicate enabled key")
            enabled_cue_keys.add(folded)
        refs = cue.get("poolIds") or []
        if any(pool_id not in existing_pool_ids for pool_id in refs):
            errors.append(f"cue {cue_id}: references a missing pool")
        if cue.get("enabled") is not True:
            # Disabled cues remain editor-owned, but are not part of Runtime v1 matching.
            continue
        runtime["cues"].append({
            "id": cue_id,
            "key": key,
            "enabled": True,
            "poolIds": list(refs),
        })

    if not errors:
        try:
            normalized = normalize_flow_config(runtime)
        except Exception as exc:
            normalized = None
            errors.append(f"runtime normalization raised: {type(exc).__name__}")
        if normalized is None:
            errors.append("compiled runtime config failed FlowConfig v1 validation")
    return (runtime if not errors else runtime), sorted(set(errors)), sorted(set(unsupported))



_EDITOR_DOCUMENT_METADATA_FIELDS = {
    "version",
    "editorRevision",
    "savedAt",
    "runtimeVersion",
    "published",
}


_EDITOR_DOCUMENT_FIELDS = frozenset({
    "schemaVersion",
    "flowId",
    "enabled",
    "guidancePrompt",
    "initialStage",
    "stages",
    "pools",
    "cues",
    "dimensions",
}) | _EDITOR_DOCUMENT_METADATA_FIELDS

_EDITOR_STAGE_FIELDS = frozenset({
    "id",
    "name",
    "enabled",
    "minTurns",
    "repeatMinTurns",
    "nextStageId",
    "nextStageMode",
    "terminal",
    "continueTarget",
    "terminalWithoutContinue",
    "holdable",
    "poolIds",
    "guidancePrompt",
})
_EDITOR_POOL_FIELDS = frozenset({
    "id",
    "name",
    "enabled",
    "mode",
    "count",
    "entries",
})
_EDITOR_ENTRY_FIELDS = frozenset({"id", "text", "enabled"})
_EDITOR_CUE_FIELDS = frozenset({"id", "key", "kind", "enabled", "poolIds"})
_EDITOR_DIMENSION_FIELDS = frozenset({"id", "label", "detail", "tone"})

_RUNTIME_STATE_FIELDS = frozenset({
    "schemaVersion",
    "flowId",
    "active",
    "stage",
    "cycle",
    "stageTurn",
    "contextKeys",
    "fixedDraws",
    "startedAt",
})


def _unknown_editor_fields(document: Mapping[str, Any]) -> set[str]:
    """Find nested fields that compile_editor_document would otherwise ignore."""
    unsafe: set[str] = set()

    def record(value: Any, path: str, allowed: frozenset[str]) -> None:
        if not isinstance(value, Mapping):
            return
        for key in value:
            if key not in allowed:
                unsafe.add(f"{path}.{key}")

    record(document, "document", _EDITOR_DOCUMENT_FIELDS)

    def items(key: str) -> list[Any]:
        value = document.get(key)
        return value if isinstance(value, list) else []

    def item_path(collection: str, index: int, item: Any) -> str:
        identifier = _text(item.get("id")) if isinstance(item, Mapping) else ""
        return f"{collection}[{identifier or index}]"

    for index, stage in enumerate(items("stages")):
        record(stage, item_path("stages", index, stage), _EDITOR_STAGE_FIELDS)
    for index, pool in enumerate(items("pools")):
        pool_path = item_path("pools", index, pool)
        record(pool, pool_path, _EDITOR_POOL_FIELDS)
        entries = pool.get("entries") if isinstance(pool, Mapping) else None
        if isinstance(entries, list):
            for entry_index, entry in enumerate(entries):
                entry_id = _text(entry.get("id")) if isinstance(entry, Mapping) else ""
                record(
                    entry,
                    f"{pool_path}.entries[{entry_id or entry_index}]",
                    _EDITOR_ENTRY_FIELDS,
                )
    for index, cue in enumerate(items("cues")):
        record(cue, item_path("cues", index, cue), _EDITOR_CUE_FIELDS)
    for index, dimension in enumerate(items("dimensions")):
        record(
            dimension,
            item_path("dimensions", index, dimension),
            _EDITOR_DIMENSION_FIELDS,
        )
    return unsafe


def _decode_verified_flow_state(value: Any) -> FlowState:
    """Decode a runtime state without the fail-open behavior used by runtime reads."""
    if not isinstance(value, Mapping) or set(value) != _RUNTIME_STATE_FIELDS:
        raise ValueError("runtime state shape is not recognized")
    if type(value["schemaVersion"]) is not int:
        raise ValueError("runtime state schemaVersion is invalid")
    for field in ("flowId", "stage", "startedAt"):
        if not isinstance(value[field], str):
            raise ValueError(f"runtime state {field} is invalid")
    for field in ("cycle", "stageTurn"):
        if type(value[field]) is not int:
            raise ValueError(f"runtime state {field} is invalid")
    if not isinstance(value["contextKeys"], list) or not isinstance(
        value["fixedDraws"], dict
    ):
        raise ValueError("runtime state collections are invalid")
    return FlowState.from_dict(value)


def _verify_runtime_row(
    row: Mapping[str, Any],
    state: FlowState,
    flow_id: str,
) -> None:
    if type(row["schema_version"]) is not int:
        raise ValueError("runtime schemaVersion is invalid")
    if row["schema_version"] != state.schema_version:
        raise ValueError("runtime schemaVersion does not match state")
    if type(row["version"]) is not int or row["version"] < 0:
        raise ValueError("runtime version is invalid")
    if not isinstance(row["chat_id"], str) or not row["chat_id"].strip():
        raise ValueError("runtime chat id is invalid")
    if not isinstance(row["updated_at"], str) or not row["updated_at"].strip():
        raise ValueError("runtime updated_at is invalid")
    row_flow_id = row["flow_id"]
    if row_flow_id is not None and (
        not isinstance(row_flow_id, str) or not row_flow_id.strip()
    ):
        raise ValueError("runtime flow id is invalid")
    config_version = row["config_version"]
    if config_version is not None and (
        type(config_version) is not int or config_version < 0
    ):
        raise ValueError("runtime config version is invalid")
    if state.active:
        if row_flow_id != state.flow_id or state.flow_id != flow_id:
            raise ValueError("runtime active flow association is invalid")
        if config_version is None:
            raise ValueError("active runtime config version is missing")
    elif row_flow_id is not None:
        raise ValueError("inactive runtime has a flow id")
    pending = row["pending_guide_json"]
    if pending is not None:
        guide = AppliedGuide.from_dict(json.loads(str(pending)))
        if guide.status != "pending" or guide.flow.to_dict() != state.to_dict():
            raise ValueError("runtime pending guide does not match state")


def _decode_verified_snapshot_state(value: Any, flow_id: str) -> FlowState:
    state = _decode_verified_flow_state(value)
    if state.active and state.flow_id != flow_id:
        raise ValueError("snapshot flow association is invalid")
    return state


def _runtime_timestamp(value: Any) -> _datetime.datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("runtime lease timestamp is invalid")
    return _datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")



def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_runtime_config(value: Any) -> str:
    try:
        normalized = normalize_flow_config(value)
    except (TypeError, ValueError):
        normalized = None
    return _canonical(normalized.to_dict() if normalized is not None else value)


def _classify_publish_fields(
    document: Mapping[str, Any],
    unsupported: list[str],
) -> tuple[list[str], list[str]]:
    """Separate presentation-only compiler notices from unsafe omissions.

    The publish endpoint is intentionally fail-closed: names, cue kind labels,
    dimensions, and compiler-derived sequence links never affect Runtime v1.
    Disabled or unknown fields can affect behavior and therefore block publish
    until the editor document no longer contains an unsafe omission.
    """
    display_only: set[str] = set()
    unsafe: set[str] = set(_unknown_editor_fields(document))
    allowed = _EDITOR_DOCUMENT_FIELDS
    for key in document:
        if key not in allowed:
            unsafe.add(f"document.{key}")
    for field in unsupported:
        if (
            field == "dimensions"
            or field.endswith(".name")
            or field.endswith(".kind")
            or field.endswith(" (derived)")
            or field.startswith("initialStage (derived")
        ):
            display_only.add(field)
        else:
            unsafe.add(field)
    return sorted(display_only), sorted(unsafe)


def _publish_analysis(
    document: Mapping[str, Any],
) -> tuple[
    Optional[dict[str, Any]],
    list[str],
    list[str],
    list[str],
    list[str],
]:
    compiled, compile_errors, unsupported = compile_editor_document(document)
    display_only, unsafe = _classify_publish_fields(document, unsupported)
    return compiled, compile_errors, unsupported, display_only, unsafe


def _apply_compiled_runtime_config(
    connection: sqlite3.Connection,
    flow_id: str,
    runtime: Mapping[str, Any],
    compiled: Mapping[str, Any],
) -> tuple[dict[str, Any], int, bool]:
    current_config = _decode_runtime(runtime)
    current_version = int(runtime.get("version", 0))
    if _canonical_runtime_config(current_config) == _canonical_runtime_config(compiled):
        return dict(runtime), current_version, False
    next_version = current_version + 1
    stamp = _now()
    updated = connection.execute(
        """
        UPDATE hidden_flow_configs
        SET schema_version=?, enabled=?, config_json=?,
            version=?, updated_at=?
        WHERE flow_id=? AND version=?
        """,
        (
            RUNTIME_SCHEMA_VERSION,
            int(bool(compiled.get("enabled"))),
            _json(compiled),
            next_version,
            stamp,
            flow_id,
            current_version,
        ),
    )
    if int(updated.rowcount or 0) != 1:
        raise sqlite3.OperationalError("runtime configuration CAS failed")
    updated_runtime = dict(runtime)
    updated_runtime.update(
        {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "enabled": int(bool(compiled.get("enabled"))),
            "config_json": _json(compiled),
            "version": next_version,
            "updated_at": stamp,
        }
    )
    return updated_runtime, next_version, True


def _is_published(
    runtime_row: Mapping[str, Any],
    document: Mapping[str, Any],
) -> bool:
    compiled, errors, _unsupported, _display_only, unsafe = _publish_analysis(document)
    if errors or unsafe or compiled is None:
        return False
    try:
        return _canonical_runtime_config(compiled) == _canonical_runtime_config(_decode_runtime(runtime_row))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


_UNKNOWN_ACTIVE_FLOW = {"state": "unknown", "stage": None, "stageTurn": None}
_IDLE_ACTIVE_FLOW = {"state": "idle", "stage": None, "stageTurn": None}


def _runtime_meta(
    runtime: Mapping[str, Any],
    editor_row: Optional[Mapping[str, Any]],
    document: Optional[Mapping[str, Any]] = None,
    active_flow: Mapping[str, Any] = _UNKNOWN_ACTIVE_FLOW,
) -> dict[str, Any]:
    try:
        config_digest = hashlib.sha256(
            _canonical_runtime_config(_decode_runtime(runtime)).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError, json.JSONDecodeError):
        config_digest = ""
    published = (
        _is_published(runtime, document)
        if document is not None
        else False
    )
    apply_pending = False
    apply_blocked = False
    apply_blocked_reason: Optional[str] = None
    if document is not None and editor_row is not None and not published:
        _compiled, compile_errors, _unsupported, _display_only, unsafe = _publish_analysis(document)
        if compile_errors or unsafe or _compiled is None:
            apply_blocked = True
            apply_blocked_reason = "validation"
        else:
            apply_pending = True
    return {
        "schemaVersion": RUNTIME_SCHEMA_VERSION,
        "version": int(runtime.get("version", 0)),
        "configDigest": config_digest,
        "engineEnabled": bool(config_store.get_bool("HIDDEN_FLOW_ENGINE_ENABLED", False)),
        "activeFlow": dict(active_flow),
        "published": published,
        "applyPending": apply_pending,
        "applyBlocked": apply_blocked,
        "applyBlockedReason": apply_blocked_reason,
        "editorRevision": int(editor_row["revision"]) if editor_row else 0,
    }


def _bundle_payload(
    flow_id: str,
    runtime_row: Mapping[str, Any],
    document: Mapping[str, Any],
    editor_row: Optional[Mapping[str, Any]],
    active_flow: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "ok": True,
        "flowId": flow_id,
        "document": _copy(document),
        "editorRevision": int(editor_row["revision"]) if editor_row else 0,
        "savedAt": editor_row["updated_at"] if editor_row else None,
        "source": "editor" if editor_row else "runtime-import",
        "runtime": _runtime_meta(runtime_row, editor_row, document, active_flow),
    }



def _active_runtime_flow(
    connection: sqlite3.Connection,
    flow_id: str,
) -> dict[str, Any]:
    """Read the committed Hidden Flow state for a read-only editor summary."""
    active_states: list[tuple[str, int]] = []
    unknown = False
    try:
        config_row = connection.execute(
            "SELECT version FROM hidden_flow_configs WHERE flow_id=?",
            (flow_id,),
        ).fetchone()
        if config_row is None or type(config_row["version"]) is not int:
            return dict(_UNKNOWN_ACTIVE_FLOW)
        runtime_rows = connection.execute(
            """
            SELECT chat_id, schema_version, flow_id, state_json,
                   pending_guide_json, config_version, version, updated_at
            FROM hidden_flow_runtime
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return dict(_UNKNOWN_ACTIVE_FLOW)

    for row in runtime_rows:
        row_flow_id = row["flow_id"]
        try:
            state = _decode_verified_flow_state(
                json.loads(str(row["state_json"]))
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            if row_flow_id == flow_id:
                unknown = True
            continue
        relevant = row_flow_id == flow_id or (
            state.active and state.flow_id == flow_id
        )
        if not relevant:
            continue
        try:
            _verify_runtime_row(row, state, flow_id)
        except (TypeError, ValueError, json.JSONDecodeError):
            unknown = True
            continue
        if state.active:
            active_states.append((state.stage, state.stage_turn))

    if unknown:
        return dict(_UNKNOWN_ACTIVE_FLOW)
    if not active_states:
        return dict(_IDLE_ACTIVE_FLOW)
    # The endpoint has no chat selector. Never pick one of several active
    # sessions, even if their current stage happens to match.
    if len(active_states) != 1:
        return dict(_UNKNOWN_ACTIVE_FLOW)
    stage, stage_turn = active_states[0]
    return {
        "state": "active",
        "stage": stage,
        "stageTurn": stage_turn,
    }


def _active_runtime_status(
    connection: sqlite3.Connection,
    flow_id: str,
) -> str:
    """Return a fail-closed publish status using existing runtime/lease tables."""
    unknown = False
    active = False
    lease_active = False
    pending = False
    active_chat_ids: set[str] = set()
    pending_chat_ids: set[str] = set()
    now = _datetime.datetime.utcnow() + _datetime.timedelta(hours=8)

    try:
        config_row = connection.execute(
            "SELECT version FROM hidden_flow_configs WHERE flow_id=?",
            (flow_id,),
        ).fetchone()
        if config_row is None or type(config_row["version"]) is not int:
            return "unknown"
        runtime_rows = connection.execute(
            """
            SELECT chat_id, schema_version, flow_id, state_json,
                   pending_guide_json, config_version, version, updated_at
            FROM hidden_flow_runtime
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return "unknown"

    for row in runtime_rows:
        row_flow_id = row["flow_id"]
        try:
            state = _decode_verified_flow_state(
                json.loads(str(row["state_json"]))
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            if row_flow_id == flow_id:
                unknown = True
            continue
        relevant = row_flow_id == flow_id or (
            state.active and state.flow_id == flow_id
        )
        if not relevant:
            continue
        try:
            _verify_runtime_row(row, state, flow_id)
        except (TypeError, ValueError, json.JSONDecodeError):
            unknown = True
            continue
        if state.active:
            active = True
            active_chat_ids.add(str(row["chat_id"]))

    try:
        snapshot_rows = connection.execute(
            """
            SELECT chat_id, flow_id, status, config_version, state_before_json,
                   state_after_json, next_guide_json
            FROM hidden_flow_message_snapshots
            WHERE status=?
            """,
            ("PENDING",),
        ).fetchall()
    except sqlite3.OperationalError:
        return "unknown"

    for row in snapshot_rows:
        snapshot_flow_id = row["flow_id"]
        if snapshot_flow_id is None:
            unknown = True
            continue
        if snapshot_flow_id != flow_id:
            continue
        try:
            if type(row["config_version"]) is not int or row["config_version"] < 0:
                raise ValueError("snapshot config version is invalid")
            before = _decode_verified_snapshot_state(
                json.loads(str(row["state_before_json"])),
                flow_id,
            )
            after = _decode_verified_snapshot_state(
                json.loads(str(row["state_after_json"])),
                flow_id,
            )
            next_guide = row["next_guide_json"]
            if next_guide is not None:
                guide = AppliedGuide.from_dict(json.loads(str(next_guide)))
                if guide.status != "pending" or guide.flow.to_dict() != after.to_dict():
                    raise ValueError("snapshot pending guide is invalid")
            pending = True
            if row["chat_id"] is not None:
                pending_chat_ids.add(str(row["chat_id"]))
            _ = before
        except (TypeError, ValueError, json.JSONDecodeError):
            unknown = True

    try:
        lease_rows = connection.execute(
            """
            SELECT context_id, resident_generation, lease_owner,
                   request_message_id, acquired_at, expires_at, updated_at
            FROM daily_resident_turn_leases
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return "unknown"

    for row in lease_rows:
        try:
            if (
                type(row["context_id"]) is not int
                or row["context_id"] <= 0
                or type(row["resident_generation"]) is not int
                or row["resident_generation"] <= 0
                or not isinstance(row["lease_owner"], str)
                or not row["lease_owner"].strip()
                or type(row["request_message_id"]) is not int
                or row["request_message_id"] <= 0
            ):
                raise ValueError("turn lease identity is invalid")
            acquired_at = _runtime_timestamp(row["acquired_at"])
            expires_at = _runtime_timestamp(row["expires_at"])
            updated_at = _runtime_timestamp(row["updated_at"])
            if acquired_at > expires_at or updated_at > expires_at:
                raise ValueError("turn lease timestamps are invalid")
            if expires_at <= now:
                raise ValueError("turn lease is expired")
            lease_active = True
        except (TypeError, ValueError):
            unknown = True

    if unknown:
        return "unknown"
    if active:
        return "active"
    if lease_active:
        return "lease_active"
    if pending:
        return "pending"
    return "inactive"


def apply_saved_editor_document_if_safe(
    flow_id: str,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Apply the latest saved editor revision after all Flow blockers clear."""
    connection = _connect(db_path or DEFAULT_DB_PATH)
    try:
        connection.execute("BEGIN IMMEDIATE")
        runtime, document, editor_row = _load_bundle(connection, flow_id)
        if editor_row is None:
            connection.rollback()
            return {"status": "no_editor", "flowId": flow_id}
        document = _materialize_stage_order(document)
        normalized, validation_errors = _validate_editor_document(document, flow_id)
        if validation_errors or normalized is None:
            connection.rollback()
            return {
                "status": "blocked_validation",
                "reason": "validation",
                "errors": validation_errors,
                "editorRevision": int(editor_row["revision"]),
            }
        compiled, compile_errors, unsupported, display_only, unsafe = _publish_analysis(
            normalized
        )
        if compile_errors or unsafe or compiled is None:
            connection.rollback()
            return {
                "status": "blocked_validation",
                "reason": "validation",
                "errors": compile_errors,
                "unsupportedFields": unsupported,
                "displayOnlyFields": display_only,
                "unsafeFields": unsafe,
                "editorRevision": int(editor_row["revision"]),
            }
        active_status = _active_runtime_status(connection, flow_id)
        if active_status != "inactive":
            connection.rollback()
            return {
                "status": "deferred",
                "activeStatus": active_status,
                "editorRevision": int(editor_row["revision"]),
            }
        runtime, runtime_version, changed = _apply_compiled_runtime_config(
            connection,
            flow_id,
            runtime,
            compiled,
        )
        connection.commit()
        return {
            "status": "applied" if changed else "already_applied",
            "flowId": flow_id,
            "editorRevision": int(editor_row["revision"]),
            "runtimeVersion": runtime_version,
            "displayOnlyFields": display_only,
        }
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        connection.rollback()
        return {
            "status": "blocked_validation",
            "reason": "validation",
            "errors": [str(exc)],
        }
    except sqlite3.Error as exc:
        connection.rollback()
        return {
            "status": "failed",
            "code": "RUNTIME_AUTO_APPLY_FAILED",
            "error": str(exc),
        }
    finally:
        connection.close()


def create_flow_studio_blueprint(
    *,
    db_path: str = DEFAULT_DB_PATH,
    owner_guard: Callable[[Any], None] = require_owner,
):
    blueprint = Blueprint("flow_studio_editor", __name__)

    @blueprint.get("/api/flow-studio/editor/<flow_id>")
    def get_editor(flow_id: str):
        auth = _guard(owner_guard)
        if auth is not None:
            return auth
        connection = _connect(db_path)
        try:
            try:
                runtime, document, editor_row = _load_bundle(connection, flow_id)
            except KeyError:
                return _error("FLOW_NOT_FOUND", "flow configuration was not found", 404)
            except (ValueError, json.JSONDecodeError):
                return _error("FLOW_STORAGE_INVALID", "stored flow configuration is invalid", 500)
            return _response(_bundle_payload(
                flow_id,
                runtime,
                document,
                editor_row,
                _active_runtime_flow(connection, flow_id),
            ))
        finally:
            connection.close()

    @blueprint.put("/api/flow-studio/editor/<flow_id>")
    def save_editor(flow_id: str):
        auth = _guard(owner_guard)
        if auth is not None:
            return auth
        if not same_origin_mutation_ok(request):
            return _error("CROSS_ORIGIN_MUTATION", "cross-origin mutation rejected", 403)
        payload, payload_error = _request_payload()
        if payload_error is not None:
            return payload_error
        expected = payload.get("expectedRevision")
        if not _is_int(expected) or expected < 0:
            return _error("EDITOR_REVISION_REQUIRED", "expectedRevision must be a non-negative integer", 400)
        document, errors = _validate_editor_document(payload.get("document"), flow_id)
        if errors:
            return _error("EDITOR_DOCUMENT_INVALID", "editor document is structurally invalid", 422, errors=errors)

        document = _materialize_stage_order(document or {})
        encoded = _json(document)
        connection = _connect(db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            runtime = _runtime_row(connection, flow_id)
            if runtime is None:
                connection.rollback()
                return _error("FLOW_NOT_FOUND", "flow configuration was not found", 404)
            current = connection.execute(
                """
                SELECT flow_id, schema_version, document_json, revision, created_at, updated_at
                FROM flow_studio_editor_documents
                WHERE flow_id=?
                """,
                (flow_id,),
            ).fetchone()
            current_revision = int(current["revision"]) if current else 0
            if expected != current_revision:
                connection.rollback()
                try:
                    current_document = (
                        json.loads(current["document_json"])
                        if current is not None
                        else _runtime_to_editor(flow_id, _decode_runtime(runtime))
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    current_document = None
                return _error(
                    "EDITOR_REVISION_CONFLICT",
                    "editor document changed on the server",
                    409,
                    currentRevision=current_revision,
                    currentDocument=current_document,
                )
            stamp = _now()
            next_revision = current_revision + 1
            if current is None:
                connection.execute(
                    """
                    INSERT INTO flow_studio_editor_documents
                    (flow_id, schema_version, document_json, revision, created_at, updated_at)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (flow_id, EDITOR_SCHEMA_VERSION, encoded, next_revision, stamp, stamp),
                )
            else:
                connection.execute(
                    """
                    UPDATE flow_studio_editor_documents
                    SET schema_version=?, document_json=?, revision=?, updated_at=?
                    WHERE flow_id=?
                    """,
                    (EDITOR_SCHEMA_VERSION, encoded, next_revision, stamp, flow_id),
                )
            connection.commit()
            editor_row = {
                "revision": next_revision,
                "updated_at": stamp,
            }
            return _response(_bundle_payload(
                flow_id,
                runtime,
                document,
                editor_row,
                _active_runtime_flow(connection, flow_id),
            ))
        except sqlite3.Error:
            connection.rollback()
            return _error("EDITOR_STORAGE_ERROR", "editor document could not be saved", 500)
        finally:
            connection.close()

    @blueprint.post("/api/flow-studio/editor/<flow_id>/validate")
    def validate_editor(flow_id: str):
        auth = _guard(owner_guard)
        if auth is not None:
            return auth
        payload, payload_error = _request_payload()
        if payload_error is not None:
            return payload_error
        connection = _connect(db_path)
        try:
            runtime_row = _runtime_row(connection, flow_id)
            if runtime_row is None:
                return _error("FLOW_NOT_FOUND", "flow configuration was not found", 404)
            current = connection.execute(
                """
                SELECT revision
                FROM flow_studio_editor_documents
                WHERE flow_id=?
                """,
                (flow_id,),
            ).fetchone()
            current_revision = int(current["revision"]) if current else 0
            document = payload.get("document")
            document, errors = _validate_editor_document(document, flow_id)
            if errors:
                return _response(
                    {
                        "ok": True,
                        "valid": False,
                        "publishable": False,
                        "errors": errors,
                        "unsupportedFields": [],
                        "displayOnlyFields": [],
                        "unsafeFields": [],
                        "editorRevision": current_revision,
                        "runtime": {
                            "schemaVersion": RUNTIME_SCHEMA_VERSION,
                            "version": int(runtime_row["version"]),
                            "engineEnabled": bool(
                                config_store.get_bool(
                                    "HIDDEN_FLOW_ENGINE_ENABLED", False
                                )
                            ),
                            "published": False,
                        },
                    }
                )
            compiled, compile_errors, unsupported = compile_editor_document(
                document or {}
            )
            display_only, unsafe = _classify_publish_fields(
                document or {},
                unsupported,
            )
            return _response(
                {
                    "ok": True,
                    "valid": not compile_errors,
                    "publishable": not compile_errors and not unsafe,
                    "errors": compile_errors,
                    "unsupportedFields": unsupported,
                    "displayOnlyFields": display_only,
                    "unsafeFields": unsafe,
                    "editorRevision": current_revision,
                    "runtime": {
                        "schemaVersion": RUNTIME_SCHEMA_VERSION,
                        "version": int(runtime_row["version"]),
                        "config": compiled,
                        "engineEnabled": bool(
                            config_store.get_bool("HIDDEN_FLOW_ENGINE_ENABLED", False)
                        ),
                        "published": _is_published(runtime_row, document or {}),
                    },
                }
            )
        finally:
            connection.close()

    @blueprint.post("/api/flow-studio/editor/<flow_id>/publish")
    def publish_editor(flow_id: str):
        auth = _guard(owner_guard)
        if auth is not None:
            return auth
        if not same_origin_mutation_ok(request):
            return _error(
                "CROSS_ORIGIN_MUTATION",
                "cross-origin mutation rejected",
                403,
            )
        payload, payload_error = _request_payload()
        if payload_error is not None:
            return payload_error
        expected_revision = payload.get("expectedRevision")
        expected_runtime_version = payload.get("expectedRuntimeVersion")
        if not _is_int(expected_revision) or expected_revision < 0:
            return _error(
                "EDITOR_REVISION_REQUIRED",
                "expectedRevision must be a non-negative integer",
                400,
            )
        if not _is_int(expected_runtime_version) or expected_runtime_version < 0:
            return _error(
                "RUNTIME_VERSION_REQUIRED",
                "expectedRuntimeVersion must be a non-negative integer",
                400,
            )
        document, errors = _validate_editor_document(
            payload.get("document"),
            flow_id,
        )
        if errors:
            return _error(
                "EDITOR_DOCUMENT_INVALID",
                "editor document is structurally invalid",
                422,
                errors=errors,
            )
        document = _materialize_stage_order(document or {})
        connection = _connect(db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            runtime = _runtime_row(connection, flow_id)
            if runtime is None:
                connection.rollback()
                return _error(
                    "FLOW_NOT_FOUND",
                    "flow configuration was not found",
                    404,
                )
            current = connection.execute(
                """
                SELECT flow_id, schema_version, document_json, revision,
                       created_at, updated_at
                FROM flow_studio_editor_documents
                WHERE flow_id=?
                """,
                (flow_id,),
            ).fetchone()
            current_revision = int(current["revision"]) if current else 0
            current_runtime_version = int(runtime["version"])
            current_document = (
                json.loads(current["document_json"])
                if current is not None
                else _runtime_to_editor(flow_id, _decode_runtime(runtime))
            )
            current_document = _materialize_stage_order(current_document)
            if expected_revision != current_revision:
                connection.rollback()
                return _error(
                    "EDITOR_REVISION_CONFLICT",
                    "editor document changed on the server",
                    409,
                    currentRevision=current_revision,
                    currentRuntimeVersion=current_runtime_version,
                    currentDocument=current_document,
                )
            if expected_runtime_version != current_runtime_version:
                connection.rollback()
                return _error(
                    "RUNTIME_VERSION_CONFLICT",
                    "runtime configuration changed on the server",
                    409,
                    currentRevision=current_revision,
                    currentRuntimeVersion=current_runtime_version,
                    currentDocument=current_document,
                )
            if _canonical(document) != _canonical(current_document):
                connection.rollback()
                return _error(
                    "PUBLISH_DRAFT_NOT_SAVED",
                    "save the current editor draft before publishing",
                    409,
                    currentRevision=current_revision,
                    currentRuntimeVersion=current_runtime_version,
                    currentDocument=current_document,
                )

            compiled, compile_errors, unsupported, display_only, unsafe = _publish_analysis(
                document
            )
            if compile_errors or unsafe or compiled is None:
                connection.rollback()
                return _error(
                    "PUBLISH_VALIDATION_FAILED",
                    "draft cannot be published until validation passes",
                    422,
                    errors=compile_errors,
                    unsupportedFields=unsupported,
                    displayOnlyFields=display_only,
                    unsafeFields=unsafe,
                    currentRevision=current_revision,
                    currentRuntimeVersion=current_runtime_version,
                )

            active_status = _active_runtime_status(connection, flow_id)
            if active_status == "active":
                connection.rollback()
                return _error(
                    "FLOW_ACTIVE",
                    "an active flow is using this configuration; finish it before publishing",
                    409,
                    currentRevision=current_revision,
                    currentRuntimeVersion=current_runtime_version,
                )
            if active_status == "lease_active":
                connection.rollback()
                return _error(
                    "FLOW_TURN_LEASE_ACTIVE",
                    "a resident turn is still in flight; finish it before publishing",
                    409,
                    currentRevision=current_revision,
                    currentRuntimeVersion=current_runtime_version,
                )
            if active_status == "pending":
                connection.rollback()
                return _error(
                    "FLOW_PENDING_SNAPSHOT",
                    "a Hidden Flow snapshot is awaiting commit; finish it before publishing",
                    409,
                    currentRevision=current_revision,
                    currentRuntimeVersion=current_runtime_version,
                )
            if active_status == "unknown":
                connection.rollback()
                return _error(
                    "FLOW_ACTIVE_STATE_UNKNOWN",
                    "active flow state could not be verified; publish was blocked",
                    503,
                    currentRevision=current_revision,
                    currentRuntimeVersion=current_runtime_version,
                )

            runtime, next_version, _changed = _apply_compiled_runtime_config(
                connection,
                flow_id,
                runtime,
                compiled,
            )
            connection.commit()
            editor_row = (
                {
                    "revision": current_revision,
                    "updated_at": current["updated_at"],
                }
                if current is not None
                else None
            )
            response_payload = _bundle_payload(
                flow_id,
                runtime,
                document,
                editor_row,
                _active_runtime_flow(connection, flow_id),
            )
            response_payload["runtime"]["version"] = next_version
            response_payload["runtime"]["published"] = True
            response_payload["displayOnlyFields"] = display_only
            return _response(response_payload)
        except sqlite3.Error:
            connection.rollback()
            return _error(
                "RUNTIME_PUBLISH_FAILED",
                "runtime configuration was not changed",
                500,
            )
        finally:
            connection.close()

    return blueprint


__all__ = [
    "EDITOR_SCHEMA_VERSION",
    "RUNTIME_SCHEMA_VERSION",
    "compile_editor_document",
    "apply_saved_editor_document_if_safe",
    "create_flow_studio_blueprint",
]
