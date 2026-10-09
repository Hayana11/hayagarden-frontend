"""Owner-authenticated Flow Studio editor storage and runtime compiler.

The editor document is intentionally separate from Hidden Flow's runtime config.
Saving this module's document never writes hidden_flow_configs and never starts
or changes the Hidden Flow engine.
"""

from __future__ import annotations

import copy
import datetime as _datetime
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


def ensure_editor_schema(
    db_path: str = DEFAULT_DB_PATH,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    owned = conn is None
    connection = conn or _connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS flow_studio_editor_documents (
                flow_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                document_json TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.commit()
    finally:
        if owned:
            connection.close()


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
    stages = result.get("stages") or []
    enabled = [stage for stage in stages if stage.get("enabled") is True]
    enabled_ids = [stage.get("id") for stage in enabled]
    for index, stage in enumerate(stages):
        mode = stage.get("nextStageMode")
        if mode not in {"sequence", "explicit"}:
            next_id = stage.get("nextStageId")
            expected = (
                enabled_ids[enabled.index(stage) + 1]
                if stage in enabled and enabled.index(stage) + 1 < len(enabled_ids)
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

    return (_copy(document) if not errors else None), errors


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

    Disabled editor objects remain in the editor document but are excluded from
    Runtime FlowConfig v1. Any such loss is reported explicitly.
    """
    document = _materialize_stage_order(_copy(document))
    runtime: dict[str, Any] = {
        "schemaVersion": RUNTIME_SCHEMA_VERSION,
        "flowId": document.get("flowId"),
        "enabled": bool(document.get("enabled")),
        "initialStage": document.get("initialStage"),
        "stages": [],
        "cues": [],
        "pools": [],
    }
    errors: list[str] = []
    unsupported: list[str] = []

    stages = document.get("stages") or []
    pools = document.get("pools") or []
    cues = document.get("cues") or []
    enabled_pool_ids = {
        pool["id"] for pool in pools if pool.get("enabled") is True
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
        if pool.get("enabled") is not True:
            unsupported.append(f"pools[{pool_id}].enabled")
            continue
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
            "enabled": True,
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
        if any(pool_id not in enabled_pool_ids for pool_id in pool_refs):
            errors.append(f"stage {stage_id}: references a disabled or missing pool")
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
        if cue.get("enabled") is not True:
            unsupported.append(f"cues[{cue_id}].enabled")
            continue
        key = cue.get("key")
        if not isinstance(key, str) or not key.strip():
            errors.append(f"cue {cue_id}: key is empty")
            continue
        folded = key.casefold()
        if folded in enabled_cue_keys:
            errors.append(f"cue {cue_id}: duplicate enabled key")
        enabled_cue_keys.add(folded)
        refs = cue.get("poolIds") or []
        if any(pool_id not in enabled_pool_ids for pool_id in refs):
            errors.append(f"cue {cue_id}: references a disabled or missing pool")
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


def _runtime_meta(runtime: Mapping[str, Any], editor_row: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schemaVersion": RUNTIME_SCHEMA_VERSION,
        "version": int(runtime.get("version", 0)),
        "engineEnabled": bool(config_store.get_bool("HIDDEN_FLOW_ENGINE_ENABLED", False)),
        "published": False,
        "editorRevision": int(editor_row["revision"]) if editor_row else 0,
    }


def _bundle_payload(
    flow_id: str,
    runtime_row: Mapping[str, Any],
    document: Mapping[str, Any],
    editor_row: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "ok": True,
        "flowId": flow_id,
        "document": _copy(document),
        "editorRevision": int(editor_row["revision"]) if editor_row else 0,
        "savedAt": editor_row["updated_at"] if editor_row else None,
        "source": "editor" if editor_row else "runtime-import",
        "runtime": _runtime_meta(runtime_row, editor_row),
    }


def create_flow_studio_blueprint(
    *,
    db_path: str = DEFAULT_DB_PATH,
    owner_guard: Callable[[Any], None] = require_owner,
):
    blueprint = Blueprint("flow_studio_editor", __name__)

    @blueprint.before_request
    def _ensure_schema():
        ensure_editor_schema(db_path)

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
            return _response(_bundle_payload(flow_id, runtime, document, editor_row))
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
            return _response(_bundle_payload(flow_id, runtime, document, editor_row))
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
            document = payload.get("document")
            document, errors = _validate_editor_document(document, flow_id)
            if errors:
                return _response(
                    {
                        "ok": True,
                        "valid": False,
                        "errors": errors,
                        "unsupportedFields": [],
                    }
                )
            compiled, compile_errors, unsupported = compile_editor_document(
                document or {}
            )
            return _response(
                {
                    "ok": True,
                    "valid": not compile_errors,
                    "errors": compile_errors,
                    "unsupportedFields": unsupported,
                    "runtime": {
                        "schemaVersion": RUNTIME_SCHEMA_VERSION,
                        "config": compiled,
                        "engineEnabled": bool(
                            config_store.get_bool("HIDDEN_FLOW_ENGINE_ENABLED", False)
                        ),
                        "published": False,
                    },
                }
            )
        finally:
            connection.close()

    return blueprint


__all__ = [
    "EDITOR_SCHEMA_VERSION",
    "RUNTIME_SCHEMA_VERSION",
    "compile_editor_document",
    "create_flow_studio_blueprint",
    "ensure_editor_schema",
]
