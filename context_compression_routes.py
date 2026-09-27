"""Production Continuity HTTP/BFF for the context-compression page.

History/current reads and the single Compression Settings Authority share this
blueprint. It never opens the producer or mutates resident state.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from flask import Blueprint, jsonify, request

from continuity.read_surface import get_block_detail, get_current, list_blocks, open_read_only

logger = logging.getLogger(__name__)
_ALL_METHODS = ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS')


def _settings_tables_ready(db_path: str) -> bool:
    try:
        conn = open_read_only(db_path)
    except (OSError, sqlite3.Error):
        return False
    try:
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
            "('continuity_settings_revisions','continuity_settings_authority')"
        ).fetchone()[0]
        if tables != 2:
            return False
        return conn.execute(
            'SELECT 1 FROM continuity_settings_authority WHERE singleton_id=1'
        ).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _load_settings_authority(db_path: str) -> dict[str, Any]:
    from continuity.settings import load_authority
    from continuity.store import open_continuity_read_only

    conn = open_continuity_read_only(db_path)
    try:
        return load_authority(conn)
    finally:
        conn.close()


def _load_default_settings_revision(db_path: str) -> dict[str, Any]:
    from continuity.store import open_continuity_read_only

    conn = open_continuity_read_only(db_path)
    try:
        row = conn.execute(
            'SELECT * FROM continuity_settings_revisions ORDER BY revision_seq LIMIT 1'
        ).fetchone()
        if row is None:
            raise RuntimeError('settings_default_revision_missing')
        return dict(row)
    finally:
        conn.close()


def _config_from_revision(revision: Mapping[str, Any]) -> dict[str, Any]:
    identity = str(revision.get('model_identity') or '')
    model = identity[len('explicit:'):] if identity.startswith('explicit:') else identity
    return {
        'revision_id': revision.get('revision_id'),
        'source': revision.get('source'),
        'length': int(revision['target_logical_size']),
        'turns': int(revision['max_completed_turns']),
        'provider': str(revision['provider']),
        'model': model,
        'model_identity': identity,
        'prompt': str(revision['prompt_body']),
        'prompt_hash': str(revision['prompt_hash']),
        'prompt_revision': str(revision['prompt_revision']),
        'prompt_policy_version': str(revision['prompt_policy_version']),
        'persona_hash': str(revision['persona_hash']),
        'persona_revision': str(revision['persona_revision']),
        'persona_policy_version': str(revision['persona_policy_version']),
        'measurement_semantics': str(revision['measurement_semantics']),
        'sealing_policy_version': str(revision['sealing_policy_version']),
    }


def _provider_registry() -> dict[str, Any]:
    from chat.cc_model import get_cc_model_catalog

    catalog = get_cc_model_catalog()
    models = []
    for row in catalog.get('models', ()):
        model_id = str(row.get('id') or '').strip()
        if not model_id:
            continue
        enabled = bool(row.get('runtime_compatible'))
        models.append({
            'id': model_id,
            'label': str(row.get('label') or model_id),
            'enabled': enabled,
            'disabled_reason': None if enabled else '当前 Claude Code runtime 不支持此模型',
        })
    runtime_enabled = any(bool(model['enabled']) for model in models)
    models.insert(0, {
        'id': 'default', 'label': 'Claude Code 默认模型', 'enabled': runtime_enabled,
        'disabled_reason': None if runtime_enabled else 'Claude Code runtime 不可用',
    })
    return {'providers': [
        {'id': 'claude_code', 'label': 'Claude', 'enabled': runtime_enabled,
         'disabled_reason': None if runtime_enabled else 'Claude Code runtime 不可用', 'models': models},
        {'id': 'gpt', 'label': 'GPT', 'enabled': False,
         'disabled_reason': '暂未接入连续压缩任务', 'models': []},
        {'id': 'deepseek', 'label': 'DeepSeek', 'enabled': False,
         'disabled_reason': '暂未接入连续压缩任务', 'models': []},
    ]}


def _settings_payload(db_path: str) -> dict[str, Any]:
    authority = _load_settings_authority(db_path)
    active = authority['active_revision']
    pending = authority['pending_revision']
    defaults = _config_from_revision(_load_default_settings_revision(db_path))
    return {
        'ok': True,
        'authority': {
            'active_revision': _config_from_revision(active),
            'pending_revision': _config_from_revision(pending) if pending else None,
            'current_block': _config_from_revision(active),
        },
        'defaults': defaults,
        'registry': _provider_registry(),
    }


def create_context_compression_blueprint(
    *,
    db_path: str,
    window_identity_reader: Callable[[Any, str], Mapping[str, Any]] | None = None,
) -> Blueprint:
    blueprint = Blueprint('context_compression', __name__, url_prefix='/dash/__continuity')

    def _no_store(response):
        response.headers['Cache-Control'] = 'no-store'
        return response

    def _method_not_allowed(allow: str = 'GET, HEAD'):
        response = jsonify({'ok': False, 'error': 'method_not_allowed'})
        response.status_code = 405
        response.headers['Allow'] = allow
        return _no_store(response)

    def _json_error(message: str, status: int):
        response = jsonify({'ok': False, 'error': message})
        response.status_code = status
        return _no_store(response)

    def _json_ok(payload: dict[str, Any], status: int = 200):
        response = jsonify(payload)
        response.status_code = status
        return _no_store(response)

    @blueprint.route('/blocks', methods=_ALL_METHODS)
    def continuity_blocks():
        if request.method not in ('GET', 'HEAD'):
            return _method_not_allowed()
        try:
            return _json_ok(list_blocks(db_path=db_path))
        except Exception:
            logger.exception('context-compression/blocks failed')
            return _json_error('continuity_read_failed', 500)

    @blueprint.route('/blocks/<candidate_id>', methods=_ALL_METHODS)
    def continuity_block_detail(candidate_id: str):
        if request.method not in ('GET', 'HEAD'):
            return _method_not_allowed()
        try:
            payload = get_block_detail(db_path=db_path, candidate_id=candidate_id)
        except Exception:
            logger.exception('context-compression/blocks/%s failed', candidate_id)
            return _json_error('continuity_read_failed', 500)
        if payload is None:
            return _json_error('not_found', 404)
        return _json_ok(payload)

    @blueprint.route('/current', methods=_ALL_METHODS)
    def continuity_current():
        if request.method not in ('GET', 'HEAD'):
            return _method_not_allowed()
        try:
            kwargs: dict[str, Any] = {
                'db_path': db_path,
                'window_identity_reader': window_identity_reader,
            }
            settings_ready = _settings_tables_ready(db_path)
            if settings_ready:
                from continuity.settings import sealing_policy_for_revision
                authority = _load_settings_authority(db_path)
                active = authority['active_revision']
                kwargs['policy'] = sealing_policy_for_revision(active)
            current = get_current(**kwargs)
            if settings_ready:
                current['settings_revision_id'] = str(active['revision_id'])
            return _json_ok(current)
        except Exception:
            logger.exception('context-compression/current failed')
            return _json_error('continuity_read_failed', 500)

    @blueprint.route('/settings', methods=_ALL_METHODS)
    def continuity_settings():
        if request.method not in ('GET', 'HEAD', 'POST'):
            return _method_not_allowed('GET, HEAD, POST')
        if not _settings_tables_ready(db_path):
            return _json_error('not_found', 404)
        if request.method in ('GET', 'HEAD'):
            try:
                return _json_ok(_settings_payload(db_path))
            except Exception:
                logger.exception('context-compression/settings read failed')
                return _json_error('settings_unavailable', 503)
        payload = request.get_json(silent=True)
        if not isinstance(payload, Mapping):
            return _json_error('invalid_settings', 400)
        provider = str(payload.get('provider') or '').strip().lower()
        model = str(payload.get('model') or '').strip()
        if provider != 'claude_code':
            return _json_error('compression_provider_unavailable', 400)
        if model == 'default':
            provider_entry = next(
                (row for row in _provider_registry()['providers'] if row['id'] == provider), None
            )
            default_entry = next(
                (row for row in (provider_entry or {}).get('models', ()) if row['id'] == 'default'), None
            )
            if not provider_entry or not provider_entry['enabled'] or not default_entry or not default_entry['enabled']:
                return _json_error('compression_model_unavailable', 400)
        else:
            from chat.cc_model import cc_model_runtime_compatibility, is_allowed_cc_model
            if not is_allowed_cc_model(model) or not re.fullmatch(r'claude-[a-z0-9]+(?:-[a-z0-9]+)*', model):
                return _json_error('compression_model_invalid', 400)
            compatible, _requirement = cc_model_runtime_compatibility(model)
            if not compatible:
                return _json_error('compression_model_unavailable', 400)
        from continuity.settings import save_revision, sealing_policy_for_revision
        try:
            authority = _load_settings_authority(db_path)
            current = get_current(
                db_path=db_path,
                window_identity_reader=window_identity_reader,
                policy=sealing_policy_for_revision(authority['active_revision']),
            )
            context_available = current.get('context_id') is not None and current.get('context_epoch') is not None
            if (not context_available or current.get('materialization_error')):
                return _json_error('current_block_authority_unavailable', 409)
            block = {
                'available': True,
                'source_refs': current.get('source_refs') or [],
                'source_revisions': current.get('source_revisions') or [],
                'unclaimed_source_count': int(current.get('source_count') or 0),
                'context_id': current.get('context_id'),
                'context_epoch': current.get('context_epoch'),
            }
            write = sqlite3.connect(db_path, timeout=10.0)
            write.row_factory = sqlite3.Row
            write.execute('PRAGMA foreign_keys=ON')
            write.execute('PRAGMA busy_timeout=10000')
            try:
                frozen_payload = dict(payload)
                frozen_payload['model_identity'] = 'default' if model == 'default' else 'explicit:' + model
                save_revision(
                    write, frozen_payload,
                    current_block=block,
                    created_by='context_compression_settings_api',
                )
            finally:
                write.close()
            return _json_ok(_settings_payload(db_path))
        except (ValueError, RuntimeError, sqlite3.Error):
            logger.exception('context-compression/settings save failed')
            return _json_error('settings_save_rejected', 409)
        except Exception:
            logger.exception('context-compression/settings save failed')
            return _json_error('settings_unavailable', 503)

    @blueprint.route('/', defaults={'rest': ''}, methods=_ALL_METHODS)
    @blueprint.route('/<path:rest>', methods=_ALL_METHODS)
    def continuity_unknown(rest: str):
        return _json_error('not_found', 404)

    return blueprint
