"""9A-R: fail-closed birth plan for a production-equivalent A/B resident pair.

This module does not start Claude, touch the formal resident, or write the chat
database.  It freezes one Daily Runtime cold-birth material set and derives two
plans from it:

* A keeps production's packaged history prompt.
* B moves only ``current_day_history`` to native user/assistant turns.

Every other material is rendered by the same production formatter.  Callers
must verify ``contract_ok`` before creating either temporary resident.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence


VARIANT_PRODUCTION_PACKAGED = 'production_packaged'
VARIANT_NATIVE_ROLES = 'native_roles'
ALLOWED_DIFFERENCE = 'history_delivery'


class ReplicaContractError(ValueError):
    """The requested pair is not a valid single-variable production replica."""

    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class NativeHistoryMessage:
    role: str
    content: str
    message_id: Optional[int] = None


@dataclass(frozen=True)
class ReplicaVariantPlan:
    variant: str
    history_delivery: str
    native_history: tuple[NativeHistoryMessage, ...]
    prompt: str


@dataclass(frozen=True)
class DailyReplicaPairPlan:
    production: ReplicaVariantPlan
    experiment: ReplicaVariantPlan
    manifest: Mapping[str, Any]

    @property
    def contract_ok(self) -> bool:
        return bool(self.manifest.get('contract_ok'))


FormalFormatter = Callable[..., str]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value or '').encode('utf-8')).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )


def _sha256_json(value: Any) -> str:
    return _sha256_text(_canonical_json(value))


def _default_formal_formatter(**kwargs: Any) -> str:
    # Lazy import keeps the contract module independently testable while the
    # deployed path still uses Daily Runtime's sole production formatter.
    from chat.daily_runtime import format_resident_turn_content

    return str(format_resident_turn_content(**kwargs))


def _normalize_history(raw: Sequence[Mapping[str, Any]]) -> tuple[NativeHistoryMessage, ...]:
    normalized: list[NativeHistoryMessage] = []
    for index, item in enumerate(raw):
        role = str(item.get('role') or '').strip().lower()
        if role not in ('user', 'assistant'):
            raise ReplicaContractError(
                'history role must be user or assistant at index %d' % index,
                error_code='REPLICA_HISTORY_ROLE_INVALID',
            )
        content = str(item.get('content') or '')
        if not content.strip():
            raise ReplicaContractError(
                'history content is empty at index %d' % index,
                error_code='REPLICA_HISTORY_CONTENT_EMPTY',
            )
        message_id = item.get('message_id')
        normalized.append(NativeHistoryMessage(
            role=role,
            content=content,
            message_id=int(message_id) if message_id is not None else None,
        ))
    if not normalized:
        raise ReplicaContractError(
            'history packaging cannot be tested without formal history',
            error_code='REPLICA_HISTORY_EMPTY',
        )
    if normalized[-1].role != 'assistant':
        raise ReplicaContractError(
            'native seed must end at an assistant boundary',
            error_code='REPLICA_HISTORY_BOUNDARY_INVALID',
        )
    return tuple(normalized)


def _history_payload(history: Sequence[NativeHistoryMessage]) -> list[dict[str, Any]]:
    return [
        {
            'role': item.role,
            'content': item.content,
            'message_id': item.message_id,
        }
        for item in history
    ]


def _non_history_assembly(assembly: Mapping[str, Any]) -> dict[str, Any]:
    copied = copy.deepcopy(dict(assembly))
    copied['current_day_history'] = []
    return copied


def _material_payload(
    *,
    static_system_sha256: str,
    persona_sha256: str,
    provider: str,
    model: str,
    allowed_tools_sha256: str,
    mcp_config_sha256: str,
    assembly: Mapping[str, Any],
    history: Sequence[NativeHistoryMessage],
    user_content: str,
) -> dict[str, Any]:
    non_history = _non_history_assembly(assembly)
    return {
        'static_system_sha256': str(static_system_sha256 or ''),
        'persona_sha256': str(persona_sha256 or ''),
        'provider': str(provider or ''),
        'model': str(model or ''),
        'allowed_tools_sha256': str(allowed_tools_sha256 or ''),
        'mcp_config_sha256': str(mcp_config_sha256 or ''),
        'non_history_assembly_sha256': _sha256_json(non_history),
        'history_sha256': _sha256_json(_history_payload(history)),
        'current_user_sha256': _sha256_text(user_content),
    }


def build_daily_replica_pair(
    *,
    assembly: Mapping[str, Any],
    user_content: str,
    static_system_sha256: str,
    persona_sha256: str,
    provider: str,
    model: str,
    allowed_tools_sha256: str,
    mcp_config_sha256: str,
    formal_formatter: Optional[FormalFormatter] = None,
) -> DailyReplicaPairPlan:
    """Freeze one cold-birth material set and derive the A/B delivery plans.

    ``assembly`` must be the object built for the formal Daily Runtime path.
    The function deliberately has no DB path and performs no I/O.
    """
    current_user = str(user_content or '')
    if not current_user.strip():
        raise ReplicaContractError(
            'current user content is empty',
            error_code='REPLICA_CURRENT_USER_EMPTY',
        )
    formatter = formal_formatter or _default_formal_formatter
    frozen_assembly = copy.deepcopy(dict(assembly))
    history = _normalize_history(frozen_assembly.get('current_day_history') or [])
    non_history = _non_history_assembly(frozen_assembly)

    production_prompt = str(formatter(
        assembly=copy.deepcopy(frozen_assembly),
        user_content=current_user,
        is_cold=True,
        is_respawn=False,
    ))
    direct_prompt = str(formatter(
        assembly=copy.deepcopy(non_history),
        user_content=current_user,
        is_cold=True,
        is_respawn=False,
    ))
    if not production_prompt.strip() or not direct_prompt.strip():
        raise ReplicaContractError(
            'formal formatter returned an empty prompt',
            error_code='REPLICA_FORMATTER_EMPTY',
        )

    material = _material_payload(
        static_system_sha256=static_system_sha256,
        persona_sha256=persona_sha256,
        provider=provider,
        model=model,
        allowed_tools_sha256=allowed_tools_sha256,
        mcp_config_sha256=mcp_config_sha256,
        assembly=frozen_assembly,
        history=history,
        user_content=current_user,
    )
    common_sha = _sha256_json(material)
    assembly_manifest = dict(frozen_assembly.get('manifest') or {})
    manifest = {
        'contract_version': '9a-r1',
        'contract_ok': True,
        'allowed_differences': [ALLOWED_DIFFERENCE],
        'common_material_sha256': common_sha,
        'static_system_sha256': material['static_system_sha256'],
        'persona_sha256': material['persona_sha256'],
        'provider': material['provider'],
        'model': material['model'],
        'allowed_tools_sha256': material['allowed_tools_sha256'],
        'mcp_config_sha256': material['mcp_config_sha256'],
        'non_history_assembly_sha256': material['non_history_assembly_sha256'],
        'history_sha256': material['history_sha256'],
        'current_user_sha256': material['current_user_sha256'],
        'production_prompt_sha256': _sha256_text(production_prompt),
        'experiment_prompt_sha256': _sha256_text(direct_prompt),
        'history_message_count': len(history),
        'production': {
            'variant': VARIANT_PRODUCTION_PACKAGED,
            'history_delivery': 'packaged_prompt',
            'common_material_sha256': common_sha,
        },
        'experiment': {
            'variant': VARIANT_NATIVE_ROLES,
            'history_delivery': 'native_roles',
            'common_material_sha256': common_sha,
        },
        # Preserve the formal assembler's positive and negative injection
        # claims, including explicit legacy_* = False evidence.
        'formal_assembly_manifest': assembly_manifest,
    }
    return DailyReplicaPairPlan(
        production=ReplicaVariantPlan(
            variant=VARIANT_PRODUCTION_PACKAGED,
            history_delivery='packaged_prompt',
            native_history=(),
            prompt=production_prompt,
        ),
        experiment=ReplicaVariantPlan(
            variant=VARIANT_NATIVE_ROLES,
            history_delivery='native_roles',
            native_history=history,
            prompt=direct_prompt,
        ),
        manifest=manifest,
    )

