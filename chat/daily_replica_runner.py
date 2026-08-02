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
class ReplicaPairResult:
    production: ReplicaVariantResult
    experiment: ReplicaVariantResult
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


def run_daily_replica_pair(
    *,
    conn: Any,
    plan: DailyReplicaPairPlan,
    full_system: str,
    env: Mapping[str, str],
    cwd: str,
    claude_home: str,
    allowed_tools: str,
    mcp_config_path: str,
    resident_factory: Optional[ResidentFactory] = None,
    seed_builder: Optional[NativeSeedBuilder] = None,
    session_id_factory: Optional[SessionIdFactory] = None,
    session_path_resolver: Optional[SessionPathResolver] = None,
) -> ReplicaPairResult:
    """Run A then B without installing either as the formal resident."""
    if not plan.contract_ok:
        raise ReplicaContractError(
            'replica material contract is not valid',
            error_code='REPLICA_CONTRACT_INVALID',
        )
    resident_factory = resident_factory or _default_resident_factory
    seed_builder = seed_builder or build_native_seed_from_db
    session_id_factory = session_id_factory or _default_session_id
    session_path_resolver = session_path_resolver or _default_session_path

    a_id = str(session_id_factory())
    b_id = str(session_id_factory())
    if not a_id or not b_id or a_id == b_id:
        raise ReplicaContractError(
            'replica session ids must be distinct',
            error_code='REPLICA_SESSION_ID_INVALID',
        )

    a_path = session_path_resolver(str(cwd), a_id, str(claude_home))
    seed: Optional[NativeSeed] = None
    resident_a = resident_factory(str(cwd), str(allowed_tools), str(mcp_config_path))
    resident_b = resident_factory(str(cwd), str(allowed_tools), str(mcp_config_path))
    try:
        seed = seed_builder(
            conn=conn,
            plan=plan,
            cwd=str(cwd),
            claude_home=str(claude_home),
            session_id=b_id,
        )
        if str(seed.session_id) != b_id:
            raise ReplicaContractError(
                'native seed session id changed',
                error_code='REPLICA_NATIVE_SESSION_MISMATCH',
            )

        resident_a.spawn_fresh_named(
            str(full_system), copy.deepcopy(dict(env)), session_id=a_id,
            reason='9a_replica_a',
        )
        result_a = _run_one(resident_a, plan.production)
        _kill_quietly(resident_a)

        resident_b.spawn_resumable(
            str(full_system), copy.deepcopy(dict(env)),
            resume_session_id=b_id,
            reason='9a_replica_b',
        )
        result_b = _run_one(resident_b, plan.experiment)
        _kill_quietly(resident_b)

        manifest = dict(plan.manifest)
        manifest.update({
            'runner_contract_ok': True,
            'formal_resident_reused': False,
            'formal_resident_swapped': False,
            'formal_db_written': False,
            'production_session_id': a_id,
            'experiment_session_id': b_id,
            'native_seed_sha256': seed.sha256,
            'native_seed_event_count': int(seed.event_count),
        })
        return ReplicaPairResult(
            production=result_a,
            experiment=result_b,
            manifest=manifest,
        )
    finally:
        _kill_quietly(resident_a)
        _kill_quietly(resident_b)
        _unlink_quietly(a_path)
        _unlink_quietly(seed.jsonl_path if seed is not None else None)

