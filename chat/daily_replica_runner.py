"""9A-R temporary twin-resident runner.

The runner consumes a validated :mod:`chat.daily_replica_ab` plan.  It never
updates the formal resident, chat database, Daily cursor, or context pointer.
Variant A starts a fresh named Claude session; variant B resumes a temporary
JSONL seed containing the exact native user/assistant history from the plan.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from chat.daily_replica_ab import (
    DailyReplicaPairPlan,
    ReplicaContractError,
    ReplicaVariantPlan,
)
from chat.daily_replica_session_start import (
    ReplicaSessionStartBundle,
    validate_session_start_against_a_hash,
    validate_session_start_bundle,
)


@dataclass(frozen=True)
class NativeSeed:
    session_id: str
    jsonl_path: Path
    sha256: str
    event_count: int


@dataclass(frozen=True)
class ReplicaVariantResult:
    variant: str
    content: str
    thinking: str
    usage: Mapping[str, Any]


@dataclass(frozen=True)
class ReplicaExecutionResult:
    result: ReplicaVariantResult
    manifest: Mapping[str, Any]


ResidentFactory = Callable[[str, str, str], Any]
NativeSeedBuilder = Callable[..., NativeSeed]
SessionIdFactory = Callable[[], str]
SessionPathResolver = Callable[[str, str, str], Path]


def _message_text(event: Mapping[str, Any]) -> str:
    message = event.get('message') or {}
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(
            str(block.get('text') or '')
            for block in content
            if isinstance(block, dict) and block.get('type') == 'text'
        )
    return ''


def build_native_seed_from_db(
    *,
    conn: Any,
    plan: DailyReplicaPairPlan,
    cwd: str,
    claude_home: str,
    session_id: str,
) -> NativeSeed:
    """Forge B from the frozen formal message IDs and verify exact contents."""
    from chat.context_window_forge import forge_target_session_from_db
    from tools.claude_forge_core import load_jsonl

    history = plan.experiment.native_history
    ids = [item.message_id for item in history]
    if any(mid is None for mid in ids):
        raise ReplicaContractError(
            'native history message_id missing',
            error_code='REPLICA_HISTORY_MESSAGE_ID_MISSING',
        )
    forged = forge_target_session_from_db(
        conn,
        selected_message_ids=[int(mid) for mid in ids if mid is not None],
        cwd=str(cwd),
        claude_home=Path(claude_home),
        target_session_id=str(session_id),
    )
    events = load_jsonl(forged.jsonl_path)
    actual = tuple(
        (str(event.get('type') or ''), _message_text(event))
        for event in events
        if str(event.get('type') or '') in ('user', 'assistant')
    )
    expected = tuple((item.role, item.content) for item in history)
    if actual != expected:
        try:
            forged.jsonl_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ReplicaContractError(
            'forged native history differs from frozen production history',
            error_code='REPLICA_NATIVE_HISTORY_MISMATCH',
        )
    return NativeSeed(
        session_id=forged.target_session_id,
        jsonl_path=forged.jsonl_path,
        sha256=forged.sha256,
        event_count=forged.event_count,
    )


def _default_resident_factory(cwd: str, allowed_tools: str, mcp_config_path: str) -> Any:
    import cc_resident

    return cc_resident.ResidentSession(cwd, allowed_tools, mcp_config_path)


def _default_session_id() -> str:
    from tools.claude_forge_core import new_uuid

    return str(new_uuid())


def _default_session_path(cwd: str, session_id: str, claude_home: str) -> Path:
    from tools.claude_forge_core import session_jsonl_path_for_cwd

    return Path(session_jsonl_path_for_cwd(
        str(cwd), str(session_id), claude_home=Path(claude_home),
    ))


def _kill_quietly(resident: Any) -> None:
    try:
        resident._kill(quiet=True)
    except Exception:
        pass


def _unlink_quietly(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _run_one(resident: Any, variant: ReplicaVariantPlan) -> ReplicaVariantResult:
    texts: list[str] = []
    thinking: list[str] = []
    usage: dict[str, Any] = {}
    completed = False
    for event, payload in resident.send_turn(variant.prompt, commit_meta={}):
        if event == 'tool_use':
            # Both variants expose the production tool surface.  A test prompt
            # may not execute a tool: kill at the first request and fail the
            # whole pair instead of accepting a side-effectful comparison.
            _kill_quietly(resident)
            raise ReplicaContractError(
                'replica requested a tool; pair aborted before acceptance',
                error_code='REPLICA_TOOL_CALL_BLOCKED',
            )
        if event == 'text':
            texts.append(str(payload))
        elif event == 'think':
            thinking.append(str(payload))
        elif event == 'done':
            if isinstance(payload, (list, tuple)) and len(payload) >= 3:
                usage = dict(payload[2] or {}) if isinstance(payload[2], dict) else {}
            completed = True
            break
    if not completed:
        raise ReplicaContractError(
            'replica ended without done',
            error_code='REPLICA_INCOMPLETE',
        )
    return ReplicaVariantResult(
        variant=variant.variant,
        content=''.join(texts).strip(),
        thinking=''.join(thinking).strip(),
        usage=usage,
    )


def _validated_runtime_dependencies(
    *,
    plan: DailyReplicaPairPlan,
    resident_factory: Optional[ResidentFactory] = None,
    session_id_factory: Optional[SessionIdFactory] = None,
    session_path_resolver: Optional[SessionPathResolver] = None,
) -> tuple[ResidentFactory, SessionIdFactory, SessionPathResolver]:
    if not plan.contract_ok:
        raise ReplicaContractError(
            'replica material contract is not valid',
            error_code='REPLICA_CONTRACT_INVALID',
        )
    return (
        resident_factory or _default_resident_factory,
        session_id_factory or _default_session_id,
        session_path_resolver or _default_session_path,
    )


def _spawn_cwd(cwd: str, session_start: Optional[ReplicaSessionStartBundle]) -> str:
    if session_start is not None:
        return str(session_start.replica_cwd)
    return str(cwd)


def _spawn_env(
    env: Mapping[str, str],
    session_start: Optional[ReplicaSessionStartBundle],
) -> dict[str, str]:
    if session_start is None:
        return dict(env)
    validate_session_start_bundle(session_start)
    return session_start.merge_env(env)


def _spawn_claude_home(
    claude_home: str,
    session_start: Optional[ReplicaSessionStartBundle],
) -> str:
    if session_start is not None:
        return str(session_start.claude_home)
    return str(claude_home)


def run_daily_replica_production(
    *,
    plan: DailyReplicaPairPlan,
    full_system: str,
    env: Mapping[str, str],
    cwd: str,
    claude_home: str,
    allowed_tools: str,
    mcp_config_path: str,
    tool_profile: str,
    session_start: Optional[ReplicaSessionStartBundle] = None,
    resident_factory: Optional[ResidentFactory] = None,
    session_id_factory: Optional[SessionIdFactory] = None,
    session_path_resolver: Optional[SessionPathResolver] = None,
) -> ReplicaExecutionResult:
    """Run only production replica A, then stop and clean its transcript."""
    resident_factory, session_id_factory, session_path_resolver = (
        _validated_runtime_dependencies(
            plan=plan,
            resident_factory=resident_factory,
            session_id_factory=session_id_factory,
            session_path_resolver=session_path_resolver,
        )
    )
    session_id = str(session_id_factory())
    if not session_id:
        raise ReplicaContractError(
            'replica session id is empty',
            error_code='REPLICA_SESSION_ID_INVALID',
        )
    spawn_cwd = _spawn_cwd(cwd, session_start)
    spawn_env = _spawn_env(env, session_start)
    spawn_home = _spawn_claude_home(claude_home, session_start)
    session_path = session_path_resolver(
        spawn_cwd, session_id, spawn_home,
    )
    resident = resident_factory(spawn_cwd, str(allowed_tools), str(mcp_config_path))
    try:
        resident.spawn_fresh_named(
            str(full_system), copy.deepcopy(spawn_env), session_id=session_id,
            tool_profile=str(tool_profile),
            reason='9a_replica_a',
        )
        result = _run_one(resident, plan.production)
        manifest = dict(plan.manifest)
        manifest.update({
            'runner_contract_ok': True,
            'variant_run': 'production_a',
            'tool_profile': str(tool_profile),
            'a_reproduction_gate': 'AWAITING_OWNER_CONFIRMATION',
            'experiment_b_started': False,
            'formal_resident_reused': False,
            'formal_resident_swapped': False,
            'formal_db_written': False,
            'session_start_isolation_ok': bool(
                session_start is not None and session_start.session_start_isolation_ok
            ),
        })
        if session_start is not None:
            manifest['frozen_session_start_sha256'] = session_start.frozen_sha256
            manifest['replica_settings_sha256'] = session_start.replica_settings_sha256
        return ReplicaExecutionResult(result=result, manifest=manifest)
    finally:
        _kill_quietly(resident)
        _unlink_quietly(session_path)


def run_daily_replica_experiment(
    *,
    conn: Any,
    plan: DailyReplicaPairPlan,
    a_reproduction_confirmed: bool,
    full_system: str,
    env: Mapping[str, str],
    cwd: str,
    claude_home: str,
    allowed_tools: str,
    mcp_config_path: str,
    tool_profile: str,
    session_start: Optional[ReplicaSessionStartBundle] = None,
    a_frozen_session_start_sha256: Optional[str] = None,
    resident_factory: Optional[ResidentFactory] = None,
    seed_builder: Optional[NativeSeedBuilder] = None,
    session_id_factory: Optional[SessionIdFactory] = None,
    session_path_resolver: Optional[SessionPathResolver] = None,
) -> ReplicaExecutionResult:
    """Run B only after an explicit owner decision that A reproduced 9A."""
    if not a_reproduction_confirmed:
        raise ReplicaContractError(
            'experiment B requires explicit confirmation that A reproduced 9A',
            error_code='REPLICA_A_GATE_REQUIRED',
        )
    resident_factory, session_id_factory, session_path_resolver = (
        _validated_runtime_dependencies(
            plan=plan,
            resident_factory=resident_factory,
            session_id_factory=session_id_factory,
            session_path_resolver=session_path_resolver,
        )
    )
    seed_builder = seed_builder or build_native_seed_from_db

    if session_start is not None:
        validate_session_start_against_a_hash(
            session_start,
            a_frozen_session_start_sha256,
        )

    session_id = str(session_id_factory())
    if not session_id:
        raise ReplicaContractError(
            'replica session id is empty',
            error_code='REPLICA_SESSION_ID_INVALID',
        )
    spawn_cwd = _spawn_cwd(cwd, session_start)
    spawn_env = _spawn_env(env, session_start)
    spawn_home = _spawn_claude_home(claude_home, session_start)
    seed: Optional[NativeSeed] = None
    resident = resident_factory(spawn_cwd, str(allowed_tools), str(mcp_config_path))
    try:
        seed = seed_builder(
            conn=conn,
            plan=plan,
            cwd=spawn_cwd,
            claude_home=spawn_home,
            session_id=session_id,
        )
        if str(seed.session_id) != session_id:
            raise ReplicaContractError(
                'native seed session id changed',
                error_code='REPLICA_NATIVE_SESSION_MISMATCH',
            )
        resident.spawn_resumable(
            str(full_system), copy.deepcopy(spawn_env),
            resume_session_id=session_id,
            tool_profile=str(tool_profile),
            reason='9a_replica_b',
        )
        result = _run_one(resident, plan.experiment)
        manifest = dict(plan.manifest)
        manifest.update({
            'runner_contract_ok': True,
            'variant_run': 'experiment_b',
            'tool_profile': str(tool_profile),
            'a_reproduction_gate': 'CONFIRMED_BY_OWNER',
            'experiment_b_started': True,
            'formal_resident_reused': False,
            'formal_resident_swapped': False,
            'formal_db_written': False,
            'session_start_isolation_ok': bool(
                session_start is not None and session_start.session_start_isolation_ok
            ),
            'native_seed_sha256': seed.sha256,
            'native_seed_event_count': int(seed.event_count),
        })
        if session_start is not None:
            manifest['frozen_session_start_sha256'] = session_start.frozen_sha256
            manifest['replica_settings_sha256'] = session_start.replica_settings_sha256
        return ReplicaExecutionResult(result=result, manifest=manifest)
    finally:
        _kill_quietly(resident)
        _unlink_quietly(seed.jsonl_path if seed is not None else None)
