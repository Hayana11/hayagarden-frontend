"""Persistent, immutable input authority for production Continuity generation."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from continuity.sealing import DEFAULT_SEALING_POLICY, SealingPolicy

PROMPT_POLICY_VERSION = "continuity_chunk_prompt_v2"
PERSONA_POLICY_VERSION = "persona_first_five_sha256_v1"
_ALLOWED_PROVIDERS = frozenset(("claude_code", "api_relay"))


class SettingsAuthorityError(RuntimeError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def select_persona_sections(text: str, count: int = 5) -> str:
    text = str(text or "")
    headings = list(re.finditer(r"(?m)^## ", text))
    if count <= 0 or len(headings) < count:
        raise SettingsAuthorityError("persona_contract_unavailable")
    result = text[headings[0].start():headings[count].start() if len(headings) > count else len(text)]
    if not result.strip():
        raise SettingsAuthorityError("persona_contract_unavailable")
    return result


def _persona_from_runtime() -> str:
    # Read the established runtime file directly; persona_store may bootstrap it.
    try:
        return Path("/var/lib/hayagarden/persona.md").read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsAuthorityError("runtime_persona_unavailable") from exc


def _config(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = conn.execute(
            "SELECT key,value FROM runtime_config WHERE key IN "
            "('CHAT_PROVIDER','GW_PROVIDER','CC_CHAT_MODEL')"
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {str(row[0]): str(row[1] or "") for row in rows}


def _env(key: str) -> str:
    # Avoid importing config_store: its import initializes runtime_config.
    try:
        lines = Path("/opt/frontend/.env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    value = ""
    for line in lines:
        line = line.strip()
        if line.startswith(key + "="):
            value = line.split("=", 1)[1].strip()
    return value


def capture_primary_authority(conn: sqlite3.Connection) -> tuple[str, str]:
    values = _config(conn)
    provider = values.get("CHAT_PROVIDER", "").strip() or _env("CHAT_PROVIDER")
    if not provider:
        provider = values.get("GW_PROVIDER", "").strip() or _env("GW_PROVIDER") or "api_relay"
    provider = provider.strip().lower()
    if provider not in _ALLOWED_PROVIDERS:
        raise SettingsAuthorityError("primary_provider_unavailable")
    if provider != "claude_code":
        # Relay model identity is preset-derived; never infer it from MODEL.
        raise SettingsAuthorityError("relay_model_authority_unavailable")
    model = values.get("CC_CHAT_MODEL", "").strip()
    identity = "explicit:" + model if model else "default"
    if identity != "default" and not re.fullmatch(r"explicit:claude-[a-z0-9]+(?:-[a-z0-9]+)*", identity):
        raise SettingsAuthorityError("primary_model_identity_invalid")
    return provider, identity


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS continuity_settings_revisions(
      revision_id TEXT PRIMARY KEY, revision_seq INTEGER NOT NULL UNIQUE,
      created_at TEXT NOT NULL, created_by TEXT NOT NULL, source TEXT NOT NULL,
      lifecycle TEXT NOT NULL CHECK(lifecycle='immutable'),
      target_logical_size INTEGER NOT NULL, max_completed_turns INTEGER NOT NULL,
      provider TEXT NOT NULL, model_identity TEXT NOT NULL,
      prompt_body TEXT NOT NULL, prompt_hash TEXT NOT NULL, prompt_revision TEXT NOT NULL,
      prompt_policy_version TEXT NOT NULL, persona_body TEXT NOT NULL, persona_hash TEXT NOT NULL,
      persona_revision TEXT NOT NULL, persona_policy_version TEXT NOT NULL,
      measurement_semantics TEXT NOT NULL, sealing_policy_version TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS continuity_settings_authority(
      singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1), active_revision_id TEXT NOT NULL,
      pending_revision_id TEXT, pending_anchor_json TEXT, updated_at TEXT NOT NULL,
      FOREIGN KEY(active_revision_id) REFERENCES continuity_settings_revisions(revision_id),
      FOREIGN KEY(pending_revision_id) REFERENCES continuity_settings_revisions(revision_id)
    );
    CREATE TABLE IF NOT EXISTS continuity_settings_promotion_receipts(
      receipt_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
      from_revision_id TEXT NOT NULL, to_revision_id TEXT NOT NULL,
      candidate_id TEXT NOT NULL UNIQUE, generation_job_id TEXT NOT NULL UNIQUE,
      promoted_at TEXT NOT NULL, evidence_hash TEXT NOT NULL, evidence_json TEXT NOT NULL,
      FOREIGN KEY(from_revision_id) REFERENCES continuity_settings_revisions(revision_id),
      FOREIGN KEY(to_revision_id) REFERENCES continuity_settings_revisions(revision_id)
    );
    CREATE TRIGGER IF NOT EXISTS continuity_settings_revisions_no_update
      BEFORE UPDATE ON continuity_settings_revisions
      BEGIN SELECT RAISE(ABORT,'continuity_settings_revision_immutable'); END;
    CREATE TRIGGER IF NOT EXISTS continuity_settings_revisions_no_delete
      BEFORE DELETE ON continuity_settings_revisions
      BEGIN SELECT RAISE(ABORT,'continuity_settings_revision_immutable'); END;
    CREATE TRIGGER IF NOT EXISTS continuity_settings_receipts_no_update
      BEFORE UPDATE ON continuity_settings_promotion_receipts
      BEGIN SELECT RAISE(ABORT,'continuity_settings_receipt_immutable'); END;
    CREATE TRIGGER IF NOT EXISTS continuity_settings_receipts_no_delete
      BEFORE DELETE ON continuity_settings_promotion_receipts
      BEGIN SELECT RAISE(ABORT,'continuity_settings_receipt_immutable'); END;
    """)


def _row_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return {k: row[k] for k in row.keys()}
    return dict(row)


def _revision(conn: sqlite3.Connection, revision_id: str | None) -> dict[str, Any] | None:
    if not revision_id:
        return None
    return _row_dict(conn.execute(
        "SELECT * FROM continuity_settings_revisions WHERE revision_id=?", (revision_id,)
    ).fetchone())


def load_authority(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM continuity_settings_authority WHERE singleton_id=1"
    ).fetchone()
    if row is None:
        raise SettingsAuthorityError("settings_authority_missing")
    raw = _row_dict(row) or {}
    active = _revision(conn, str(raw.get("active_revision_id") or ""))
    pending = _revision(conn, str(raw.get("pending_revision_id") or ""))
    if active is None or (raw.get("pending_revision_id") and pending is None):
        raise SettingsAuthorityError("settings_authority_revision_missing")
    anchor = None
    if raw.get("pending_anchor_json"):
        try:
            anchor = json.loads(str(raw["pending_anchor_json"]))
        except (TypeError, ValueError) as exc:
            raise SettingsAuthorityError("settings_pending_anchor_corrupt") from exc
        if not isinstance(anchor, dict):
            raise SettingsAuthorityError("settings_pending_anchor_corrupt")
    return {"active_revision": active, "pending_revision": pending, "pending_anchor": anchor}


def ensure_authority(
    conn: sqlite3.Connection, *, provider: str | None = None,
    model_identity: str | None = None, persona_text: str | None = None,
    prompt_body: str | None = None, now: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    if conn.execute("SELECT 1 FROM continuity_settings_authority WHERE singleton_id=1").fetchone():
        return load_authority(conn)
    existing = conn.execute(
        "SELECT revision_id FROM continuity_settings_revisions ORDER BY revision_seq LIMIT 1"
    ).fetchone()
    if existing:
        conn.execute(
            "INSERT INTO continuity_settings_authority VALUES(1,?,NULL,NULL,?)",
            (str(existing[0]), str(now or _now())),
        )
        conn.commit()
        return load_authority(conn)
    if provider is None or model_identity is None:
        captured = capture_primary_authority(conn)
        provider = provider or captured[0]
        model_identity = model_identity or captured[1]
    provider, model_identity = str(provider).strip().lower(), str(model_identity).strip()
    if provider not in _ALLOWED_PROVIDERS or not model_identity:
        raise SettingsAuthorityError("baseline_authority_incomplete")
    from continuity.chunk_generation import ACCEPTED_PROMPT
    default_prompt = str(ACCEPTED_PROMPT)
    if prompt_body is None:
        prompt_body = default_prompt
    if persona_text is None:
        persona_text = _persona_from_runtime()
    persona = select_persona_sections(persona_text)
    persona_hash, prompt_hash = _hash(persona), _hash(prompt_body)
    prompt_revision = PROMPT_POLICY_VERSION if prompt_body == default_prompt else "prompt-" + prompt_hash[:16]
    policy = DEFAULT_SEALING_POLICY
    stamp, revision_id = str(now or _now()), "continuity-settings-baseline-v1"
    conn.execute(
        "INSERT INTO continuity_settings_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (revision_id, 1, stamp, "system", "baseline", "immutable",
         int(policy.target_logical_size), int(policy.max_completed_turns), provider, model_identity,
         prompt_body, prompt_hash, prompt_revision, PROMPT_POLICY_VERSION, persona, persona_hash,
         "persona:" + persona_hash[:24], PERSONA_POLICY_VERSION,
         str(policy.measurement_semantics), str(policy.version)),
    )
    conn.execute("INSERT INTO continuity_settings_authority VALUES(1,?,NULL,NULL,?)", (revision_id, stamp))
    conn.commit()
    return load_authority(conn)


def binding_for_revision(conn: sqlite3.Connection, revision_id: str) -> dict[str, Any]:
    row = _revision(conn, revision_id)
    if row is None:
        raise SettingsAuthorityError("settings_revision_not_found")
    if _hash(str(row["prompt_body"])) != str(row["prompt_hash"]):
        raise SettingsAuthorityError("frozen_prompt_hash_mismatch")
    if _hash(str(row["persona_body"])) != str(row["persona_hash"]):
        raise SettingsAuthorityError("frozen_persona_hash_mismatch")
    return {
        "settings_revision_id": str(row["revision_id"]),
        "target_logical_size": int(row["target_logical_size"]),
        "max_completed_turns": int(row["max_completed_turns"]),
        "provider": str(row["provider"]), "model_identity": str(row["model_identity"]),
        "prompt_body": str(row["prompt_body"]), "prompt_hash": str(row["prompt_hash"]),
        "prompt_revision": str(row["prompt_revision"]),
        "prompt_policy_version": str(row["prompt_policy_version"]),
        "persona_body": str(row["persona_body"]), "persona_hash": str(row["persona_hash"]),
        "persona_revision": str(row["persona_revision"]),
        "persona_policy_version": str(row["persona_policy_version"]),
        "measurement_semantics": str(row["measurement_semantics"]),
        "sealing_policy_version": str(row["sealing_policy_version"]),
    }


def sealing_policy_for_revision(revision: Mapping[str, Any]) -> SealingPolicy:
    revision_id = str(revision.get("revision_id") or "")
    if not revision_id:
        raise SettingsAuthorityError("settings_revision_id_missing")
    return SealingPolicy(
        version=f"{revision['sealing_policy_version']}@{revision_id}",
        target_logical_size=int(revision["target_logical_size"]),
        max_completed_turns=int(revision["max_completed_turns"]),
        measurement_semantics=str(revision["measurement_semantics"]),
    )


def save_revision(
    conn: sqlite3.Connection, payload: Mapping[str, Any], *,
    current_block: Mapping[str, Any], persona_text: str | None = None,
    created_by: str = "settings_api", now: str | None = None,
) -> dict[str, Any]:
    """Append an immutable revision and preserve an already-open block."""
    if current_block.get("available") is not True:
        raise SettingsAuthorityError("current_block_authority_unavailable")
    try:
        length = int(payload.get("target_logical_size", payload.get("length")))
        turns = int(payload.get("max_completed_turns", payload.get("turns")))
    except (TypeError, ValueError) as exc:
        raise SettingsAuthorityError("settings_measurements_invalid") from exc
    if not 1000 <= length <= 1_000_000 or not 1 <= turns <= 1000:
        raise SettingsAuthorityError("settings_measurements_out_of_range")
    provider = str(payload.get("provider") or "").strip().lower()
    model = str(payload.get("model_identity") or payload.get("model") or "").strip()
    raw_prompt = payload.get("prompt_body") if payload.get("prompt_body") is not None else payload.get("prompt")
    prompt = str(raw_prompt or "")
    if provider not in _ALLOWED_PROVIDERS or not model or not prompt.strip() or len(prompt) > 100_000:
        raise SettingsAuthorityError("settings_generation_binding_invalid")
    refs = tuple(str(x) for x in current_block.get("source_refs", ()) if str(x))
    revisions = tuple(str(x) for x in current_block.get("source_revisions", ()) if str(x))
    unclaimed = int(current_block.get("unclaimed_source_count", len(refs)) or 0)
    if len(refs) != len(revisions) or (unclaimed > 0 and not refs):
        raise SettingsAuthorityError("current_block_anchor_mismatch")
    persona = select_persona_sections(persona_text if persona_text is not None else _persona_from_runtime())
    persona_hash, prompt_hash = _hash(persona), _hash(prompt)
    from continuity.chunk_generation import ACCEPTED_PROMPT
    prompt_revision = PROMPT_POLICY_VERSION if prompt == ACCEPTED_PROMPT else "prompt-" + prompt_hash[:16]
    stamp = str(now or _now())
    conn.execute("BEGIN IMMEDIATE")
    try:
        authority = load_authority(conn)
        active = authority["active_revision"]
        seq = int(conn.execute(
            "SELECT COALESCE(MAX(revision_seq),0)+1 FROM continuity_settings_revisions"
        ).fetchone()[0])
        revision_id = f"continuity-settings-r{seq}-{_hash(prompt_hash + persona_hash + model)[:12]}"
        conn.execute(
            "INSERT INTO continuity_settings_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (revision_id, seq, stamp, str(created_by), "settings_save", "immutable",
             length, turns, provider, model, prompt, prompt_hash, prompt_revision,
             PROMPT_POLICY_VERSION, persona, persona_hash, "persona:" + persona_hash[:24],
             PERSONA_POLICY_VERSION, str(active["measurement_semantics"]),
             f"continuity_sealing_v1_{length}_{turns}"),
        )
        if unclaimed > 0:
            anchor = {
                "first_source_ref": refs[0], "first_source_revision": revisions[0],
                "active_revision_id": str(active["revision_id"]),
                "context_id": current_block.get("context_id"),
                "context_epoch": current_block.get("context_epoch"),
            }
            conn.execute(
                "UPDATE continuity_settings_authority SET pending_revision_id=?,pending_anchor_json=?,updated_at=? WHERE singleton_id=1",
                (revision_id, _json(anchor), stamp),
            )
        else:
            conn.execute(
                "UPDATE continuity_settings_authority SET active_revision_id=?,pending_revision_id=NULL,pending_anchor_json=NULL,updated_at=? WHERE singleton_id=1",
                (revision_id, stamp),
            )
        conn.commit()
        return load_authority(conn)
    except Exception:
        conn.rollback()
        raise


def promote_pending_after_seal(
    conn: sqlite3.Connection, *, candidate_id: str, generation_job_id: str,
    now: str | None = None,
) -> dict[str, Any] | None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        replay = conn.execute(
            "SELECT receipt_id,to_revision_id,candidate_id,generation_job_id "
            "FROM continuity_settings_promotion_receipts WHERE candidate_id=? OR generation_job_id=?",
            (candidate_id, generation_job_id),
        ).fetchone()
        if replay is not None:
            if str(replay[2]) != candidate_id or str(replay[3]) != generation_job_id:
                raise SettingsAuthorityError("promotion_evidence_conflict")
            conn.commit()
            return {"promoted": True, "idempotent_replay": True, "active_revision_id": str(replay[1]), "receipt_id": str(replay[0])}
        authority = load_authority(conn)
        active, pending, anchor = authority["active_revision"], authority["pending_revision"], authority["pending_anchor"]
        if pending is None:
            conn.commit()
            return None
        key = _hash(_json({"from": active["revision_id"], "to": pending["revision_id"],
                           "candidate_id": candidate_id, "generation_job_id": generation_job_id}))
        old = conn.execute(
            "SELECT receipt_id,to_revision_id FROM continuity_settings_promotion_receipts WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if old:
            conn.commit()
            return {"promoted": True, "idempotent_replay": True, "active_revision_id": str(old[1]), "receipt_id": str(old[0])}
        if not isinstance(anchor, dict) or anchor.get("active_revision_id") != active["revision_id"]:
            raise SettingsAuthorityError("pending_anchor_stale")
        candidate = conn.execute(
            "SELECT snapshot_id,source_revision,snapshot_source_hash,status,settings_revision_id FROM continuity_candidate_blocks WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        job = conn.execute(
            "SELECT candidate_id,settings_revision_id FROM continuity_generation_jobs WHERE generation_job_id=?",
            (generation_job_id,),
        ).fetchone()
        if candidate is None or job is None or str(candidate[4] or "") != str(active["revision_id"]):
            raise SettingsAuthorityError("promotion_candidate_binding_mismatch")
        if str(job[0]) != candidate_id or str(job[1] or "") != str(active["revision_id"]):
            raise SettingsAuthorityError("promotion_job_binding_mismatch")
        if str(candidate[3]) not in ("pending", "shadow"):
            raise SettingsAuthorityError("promotion_candidate_not_sealed")
        pairs = conn.execute(
            "SELECT source_ref,source_revision FROM continuity_candidate_members WHERE candidate_id=?",
            (candidate_id,),
        ).fetchall()
        if (str(anchor.get("first_source_ref") or ""), str(anchor.get("first_source_revision") or "")) not in {
            (str(x[0]), str(x[1])) for x in pairs
        }:
            raise SettingsAuthorityError("pending_anchor_not_in_candidate")
        source_pairs = conn.execute(
            "SELECT source_ref,source_revision FROM continuity_source_members WHERE snapshot_id=?",
            (str(candidate[0]),),
        ).fetchall()
        source_set = {(str(x[0]), str(x[1])) for x in source_pairs}
        member_set = {(str(x[0]), str(x[1])) for x in pairs}
        if not member_set or not member_set.issubset(source_set):
            raise SettingsAuthorityError("candidate_membership_not_in_snapshot")
        evidence = {
            "candidate_id": candidate_id, "generation_job_id": generation_job_id,
            "from_revision_id": active["revision_id"], "to_revision_id": pending["revision_id"],
            "candidate_source_revision": str(candidate[1]), "snapshot_source_hash": str(candidate[2]),
            "anchor": anchor, "member_count": len(pairs),
        }
        evidence_json = _json(evidence)
        evidence_hash = _hash(evidence_json)
        stamp, receipt_id = str(now or _now()), "continuity-promotion:" + key[:24]
        conn.execute(
            "INSERT INTO continuity_settings_promotion_receipts VALUES(?,?,?,?,?,?,?,?,?)",
            (receipt_id, key, active["revision_id"], pending["revision_id"], candidate_id,
             generation_job_id, stamp, evidence_hash, evidence_json),
        )
        conn.execute(
            "UPDATE continuity_settings_authority SET active_revision_id=?,pending_revision_id=NULL,pending_anchor_json=NULL,updated_at=? WHERE singleton_id=1",
            (pending["revision_id"], stamp),
        )
        conn.commit()
        return {"promoted": True, "idempotent_replay": False, "active_revision_id": pending["revision_id"], "receipt_id": receipt_id}
    except Exception:
        conn.rollback()
        raise
