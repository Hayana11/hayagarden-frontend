"""Claude Code chat model authority (MODEL-1B).

Config key: CC_CHAT_MODEL
  ""        → default → no --model argv
  "<id>"    → explicit → ["--model", "<id>"]

Never reads MODEL / ACTIVE_RELAY / relay catalog.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

import config_store

CC_CHAT_MODEL_KEY = 'CC_CHAT_MODEL'
CC_MODEL_NOT_ALLOWED = 'CC_MODEL_NOT_ALLOWED'
CC_MODEL_RUNTIME_INCOMPATIBLE = 'CC_MODEL_RUNTIME_INCOMPATIBLE'

# Curated Claude Code fallback IDs; separate from relay models.json.
CC_FALLBACK_MODEL_CATALOG: list[dict[str, Any]] = [
    {
        'id': 'claude-sonnet-5',
        'label': 'Sonnet 5',
        'desc': '主力均衡',
        'primary': True,
        'dot': '#6a8a7c',
    },
    {
        'id': 'claude-opus-5-5',
        'label': 'Opus 5.5',
        'desc': '新一代旗舰',
        'primary': True,
        'min_claude_code_version': '2.1.280',
        'dot': '#8a5a72',
    },
    {
        'id': 'claude-opus-5',
        'label': 'Opus 5',
        'desc': '新一代旗舰',
        'primary': True,
        'dot': '#8a5a72',
    },
    {
        'id': 'claude-opus-4-8',
        'label': 'Opus 4.8',
        'desc': '上一代旗舰',
        'primary': True,
        'dot': '#8a6a7c',
    },
    {
        'id': 'claude-opus-4-6',
        'label': 'Opus 4.6',
        'desc': '深度推理',
        'primary': True,
        'dot': '#7c6a8a',
    },
    {
        'id': 'claude-sonnet-4-6',
        'label': 'Sonnet 4.6',
        'desc': '上一代均衡',
        'primary': False,
        'dot': '#8a7c6a',
    },
    {
        'id': 'claude-haiku-4-5-20251001',
        'label': 'Haiku 4.5',
        'desc': '更快更轻',
        'primary': False,
        'dot': '#6a7c8a',
    },
]

# Deprecated compatibility alias; in-tree runtime code uses the explicit fallback name.
CC_MODEL_CATALOG = CC_FALLBACK_MODEL_CATALOG

_CC_MODEL_ID_RE = re.compile(r'^claude-[a-z0-9]+(?:-[a-z0-9]+)*$')


def _is_well_formed_cc_model_id(model_id: str) -> bool:
    return bool(_CC_MODEL_ID_RE.fullmatch(str(model_id or '').strip()))


def _native_model_catalog_adapter(*, force: bool = False) -> dict[str, Any] | None:
    """Future adapter seam for a stable, machine-readable subscription catalog.

    Claude Code has no supported account-catalog API.
    Do not infer one from the interactive /model picker, TUI, HTML, errors,
    or gateway /v1/models. Implement this adapter only after an official,
    non-generative subscription catalog contract is verified.
    """
    return None


def _normalized_model_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for candidate in value:
        if not isinstance(candidate, dict):
            continue
        model_id = str(candidate.get('id') or '').strip()
        if not _is_well_formed_cc_model_id(model_id):
            continue
        row: dict[str, Any] = {'id': model_id}
        for key in ('label', 'desc', 'thinking', 'primary', 'dot', 'min_claude_code_version'):
            if key in candidate:
                row[key] = candidate[key]
        rows.append(row)
    return rows


def _active_runtime_version_for_catalog() -> str | None:
    try:
        from chat.cc_runtime import require_managed_claude_runtime
        return require_managed_claude_runtime(timeout=10.0)
    except Exception:
        return None


def annotate_runtime_compatibility(
    models: list[dict[str, Any]], *, runtime_version: str | None = None,
) -> list[dict[str, Any]]:
    from chat.cc_runtime import MINIMUM_CLAUDE_CODE_VERSION, version_tuple

    version = runtime_version if runtime_version is not None else _active_runtime_version_for_catalog()
    try:
        runtime_ready = bool(version) and (
            version_tuple(version) >= version_tuple(MINIMUM_CLAUDE_CODE_VERSION)
        )
    except Exception:
        runtime_ready = False
    annotated = []
    for source in models:
        row = dict(source)
        required = str(row.get('min_claude_code_version') or '').strip() or None
        compatible = runtime_ready
        if compatible and required:
            try:
                compatible = version_tuple(version) >= version_tuple(required)
            except Exception:
                compatible = False
        row['runtime_compatible'] = compatible
        row['runtime_requirement'] = required or MINIMUM_CLAUDE_CODE_VERSION
        annotated.append(row)
    return annotated


def cc_model_runtime_compatibility(model_id: str) -> tuple[bool, str | None]:
    row = next(
        (item for item in get_cc_model_catalog()['models'] if item.get('id') == str(model_id or '').strip()),
        None,
    )
    if row is None:
        return False, None
    decorated = annotate_runtime_compatibility([row])[0]
    return bool(decorated.get('runtime_compatible')), decorated.get('runtime_requirement')


def get_cc_model_catalog(*, force: bool = False) -> dict[str, Any]:
    """Return a selection catalog annotated against the verified active runtime.

    Catalog availability is not account-entitlement proof. Runtime compatibility
    is independent and comes only from the managed active-version authority.
    """
    catalog_error = None
    try:
        native = _native_model_catalog_adapter(force=force)
    except Exception:
        native = None
        catalog_error = 'native_discovery_failed'
    if isinstance(native, dict):
        models = _normalized_model_rows(native.get('models'))
        if models:
            return {
                'models': annotate_runtime_compatibility(models),
                'catalog_source': 'native',
                'catalog_ready': True,
                'catalog_error': None,
                'catalog_refreshed_at': str(native.get('refreshed_at') or '') or None,
            }
        catalog_error = 'invalid_native_catalog'
    return {
        'models': annotate_runtime_compatibility(deepcopy(CC_FALLBACK_MODEL_CATALOG)),
        'catalog_source': 'fallback',
        'catalog_ready': True,
        'catalog_error': catalog_error,
        'catalog_refreshed_at': None,
    }


def get_cc_chat_model() -> str:
    """Return stripped CC_CHAT_MODEL, or '' for default."""
    return str(config_store.get(CC_CHAT_MODEL_KEY, '') or '').strip()


def cc_catalog_ids() -> frozenset[str]:
    return frozenset(
        str(row.get('id') or '').strip()
        for row in get_cc_model_catalog()['models']
        if str(row.get('id') or '').strip()
    )


def is_allowed_cc_model(model_id: str) -> bool:
    """True only for current native/fallback CC catalog ids; relay aliases never pass."""
    return str(model_id or '').strip() in cc_catalog_ids()


def cc_model_snapshot(model: str | None = None) -> tuple[str, str, list[str]]:
    """One read of CC_CHAT_MODEL → (raw_model, identity, argv_fragment).

    Spawn paths must use this so argv and stored identity cannot diverge
    across two separate DB reads.
    """
    value = get_cc_chat_model() if model is None else str(model or '').strip()
    if not value:
        return '', 'default', []
    return value, 'explicit:%s' % value, ['--model', value]


def cc_model_mode(model: str | None = None) -> str:
    value = get_cc_chat_model() if model is None else str(model or '').strip()
    return 'explicit' if value else 'default'


def cc_model_identity(model: str | None = None) -> str:
    """Stable identity for resident reuse checks."""
    _value, identity, _args = cc_model_snapshot(model)
    return identity


def cc_model_args(model: str | None = None) -> list[str]:
    """CLI argv fragment: [] or ['--model', '<id>']."""
    _value, _identity, args = cc_model_snapshot(model)
    return args


def cc_model_args_from_identity(identity: str) -> list[str]:
    """Convert a frozen CC authority identity to argv without reading config."""
    model = cc_model_from_identity(identity)
    return ['--model', model] if model else []


def cc_model_from_identity(identity: str) -> str:
    """Return frozen explicit model, or '' for default, without reading config."""
    identity = str(identity or '').strip()
    if identity == 'default':
        return ''
    prefix = 'explicit:'
    if not identity.startswith(prefix):
        raise ValueError('invalid CC model identity: %s' % (identity or '<empty>'))
    model = identity[len(prefix):].strip()
    # Frozen authorities must not be revalidated against a refreshable catalog:
    # a list change must not invalidate an already captured background task.
    if not _is_well_formed_cc_model_id(model):
        raise ValueError('invalid CC model identity: %s' % identity)
    return model


def describe_cc_model_state() -> dict[str, Any]:
    model = get_cc_chat_model()
    mode = cc_model_mode(model)
    configured = model or None
    return {
        'provider': 'claude_code',
        'model_mode': mode,
        'configured_model': configured,
        'model': configured,
    }


def set_cc_chat_model(model: str | None) -> dict[str, Any]:
    """Write CC_CHAT_MODEL. None/'' clears to default.

    Non-empty values must be in the current native/fallback CC catalog.
    Relay aliases and other free-form strings are rejected without mutation.
    """
    if model is None:
        value = ''
    else:
        value = str(model).strip()
    if value and not is_allowed_cc_model(value):
        state = describe_cc_model_state()
        state['ok'] = False
        state['error'] = CC_MODEL_NOT_ALLOWED
        state['rejected_model'] = value
        state['scope'] = 'cc_chat_model'
        return state
    if value:
        compatible, requirement = cc_model_runtime_compatibility(value)
        if not compatible:
            state = describe_cc_model_state()
            state['ok'] = False
            state['error'] = CC_MODEL_RUNTIME_INCOMPATIBLE
            state['runtime_requirement'] = requirement
            state['rejected_model'] = value
            state['scope'] = 'cc_chat_model'
            return state
    config_store.set(CC_CHAT_MODEL_KEY, value)
    state = describe_cc_model_state()
    state['ok'] = True
    state['effective_from'] = 'next_turn'
    state['scope'] = 'cc_chat_model'
    return state


def cc_catalog_label(model_id: str) -> str:
    mid = str(model_id or '').strip()
    for row in get_cc_model_catalog()['models']:
        if row.get('id') == mid:
            return str(row.get('label') or mid)
    return mid
