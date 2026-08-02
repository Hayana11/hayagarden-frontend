"""Owner-only lifecycle for the one-shot 9A A/B Daily replica."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from chat.daily_replica_ab import ReplicaContractError
from chat.daily_replica_runner import (
    ReplicaExecutionResult,
    run_daily_replica_experiment,
    run_daily_replica_production,
)
from chat.daily_replica_snapshot import (
    DailyReplicaSnapshot,
    create_daily_replica_snapshot,
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value or '').encode('utf-8')).hexdigest()


def _sha256_file_or_empty(path: str) -> str:
    target = Path(path)
    if not target.is_file():
        return _sha256_text('')
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _public_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): item
        for key, item in dict(value).items()
        if not str(key).endswith('_session_id')
    }


@dataclass
class ActiveDailyReplica:
    experiment_id: str
    snapshot: DailyReplicaSnapshot
    full_system: str
    env: Mapping[str, str]
    a_result: ReplicaExecutionResult
    created_at: float


class DailyReplicaManager:
    """Keep one frozen A result until the owner either authorizes B or closes."""

    def __init__(
        self,
        *,
        source_db_path: str,
        cwd: str,
        claude_home: str,
        allowed_tools: str,
        mcp_config_path: str,
        cc_token: str,
        get_provider: Callable[[], str],
        get_model: Callable[[], str],
        build_static_parts: Callable[[], Mapping[str, str]],
        snapshot_factory: Callable[..., DailyReplicaSnapshot] = create_daily_replica_snapshot,
        run_a: Callable[..., ReplicaExecutionResult] = run_daily_replica_production,
        run_b: Callable[..., ReplicaExecutionResult] = run_daily_replica_experiment,
        id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        clock: Callable[[], float] = time.time,
    ):
        self.source_db_path = str(source_db_path)
        self.cwd = str(cwd)
        self.claude_home = str(claude_home)
        self.allowed_tools = str(allowed_tools)
        self.mcp_config_path = str(mcp_config_path)
        self.cc_token = str(cc_token)
        self.get_provider = get_provider
        self.get_model = get_model
        self.build_static_parts = build_static_parts
        self.snapshot_factory = snapshot_factory
        self.run_a = run_a
        self.run_b = run_b
        self.id_factory = id_factory
        self.clock = clock
        self._lock = threading.RLock()
        self._active: Optional[ActiveDailyReplica] = None

    def _runtime_material(self) -> tuple[str, str, dict[str, str], str, str]:
        provider = str(self.get_provider() or '')
        if provider != 'claude_code':
            raise ReplicaContractError(
                'formal provider is not claude_code',
                error_code='REPLICA_PROVIDER_MISMATCH',
            )
        model = str(self.get_model() or '')
        parts = dict(self.build_static_parts())
        full_system = str(parts.get('full_system') or '')
        persona = str(parts.get('persona') or '')
        if not full_system or not persona:
            raise ReplicaContractError(
                'formal static system or persona is empty',
                error_code='REPLICA_STATIC_MATERIAL_EMPTY',
            )
        env = dict(os.environ)
        env['CLAUDE_CODE_OAUTH_TOKEN'] = self.cc_token
        env.pop('ANTHROPIC_API_KEY', None)
        return full_system, persona, env, provider, model

    def start_a(self, *, user_message_id: int) -> dict[str, Any]:
        with self._lock:
            if self._active is not None:
                raise ReplicaContractError(
                    'another Daily replica is awaiting an owner decision',
                    error_code='REPLICA_ALREADY_ACTIVE',
                )
            full_system, persona, env, provider, model = self._runtime_material()
            snapshot = self.snapshot_factory(
                source_db_path=self.source_db_path,
                user_message_id=int(user_message_id),
                static_system=full_system,
                static_system_sha256=_sha256_text(full_system),
                persona_sha256=_sha256_text(persona),
                provider=provider,
                model=model,
                tool_profile='text_only',
                allowed_tools_sha256=_sha256_text(self.allowed_tools),
                mcp_config_sha256=_sha256_file_or_empty(self.mcp_config_path),
            )
            try:
                result = self.run_a(
                    plan=snapshot.plan,
                    full_system=full_system,
                    env=env,
                    cwd=self.cwd,
                    claude_home=self.claude_home,
                    allowed_tools=self.allowed_tools,
                    mcp_config_path=self.mcp_config_path,
                    tool_profile='text_only',
                )
            except Exception:
                snapshot.close()
                raise
            experiment_id = str(self.id_factory())
            self._active = ActiveDailyReplica(
                experiment_id=experiment_id,
                snapshot=snapshot,
                full_system=full_system,
                env=env,
                a_result=result,
                created_at=float(self.clock()),
            )
            return {
                'ok': True,
                'experiment_id': experiment_id,
                'variant': 'A',
                'thinking': result.result.thinking,
                'content': result.result.content,
                'snapshot_manifest': _public_manifest(snapshot.manifest),
                'runner_manifest': _public_manifest(result.manifest),
                'next': 'CONFIRM_A_REPRODUCED_OR_CLOSE',
            }

    def run_experiment_b(
        self,
        *,
        experiment_id: str,
        a_reproduction_confirmed: bool,
    ) -> dict[str, Any]:
        with self._lock:
            active = self._active
            if active is None or active.experiment_id != str(experiment_id):
                raise ReplicaContractError(
                    'Daily replica experiment not found',
                    error_code='REPLICA_NOT_FOUND',
                )
            if not a_reproduction_confirmed:
                raise ReplicaContractError(
                    'B remains locked until the owner confirms A reproduced 9A',
                    error_code='REPLICA_A_GATE_REQUIRED',
                )
            try:
                conn = sqlite3.connect(str(active.snapshot.db_path))
                conn.row_factory = sqlite3.Row
                try:
                    result = self.run_b(
                        conn=conn,
                        plan=active.snapshot.plan,
                        a_reproduction_confirmed=True,
                        full_system=active.full_system,
                        env=active.env,
                        cwd=self.cwd,
                        claude_home=self.claude_home,
                        allowed_tools=self.allowed_tools,
                        mcp_config_path=self.mcp_config_path,
                        tool_profile='text_only',
                    )
                finally:
                    conn.close()
                return {
                    'ok': True,
                    'experiment_id': active.experiment_id,
                    'variant': 'B',
                    'thinking': result.result.thinking,
                    'content': result.result.content,
                    'runner_manifest': _public_manifest(result.manifest),
                    'next': 'COMPARE_A_AND_B',
                }
            finally:
                active.snapshot.close()
                self._active = None

    def close(self, *, experiment_id: str) -> dict[str, Any]:
        with self._lock:
            active = self._active
            if active is None or active.experiment_id != str(experiment_id):
                raise ReplicaContractError(
                    'Daily replica experiment not found',
                    error_code='REPLICA_NOT_FOUND',
                )
            active.snapshot.close()
            self._active = None
            return {'ok': True, 'experiment_id': str(experiment_id), 'closed': True}
