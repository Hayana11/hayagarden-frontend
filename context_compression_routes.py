"""Production read-only Continuity HTTP/BFF for the context-compression page.

GET /dash/__continuity/blocks
GET /dash/__continuity/blocks/<candidate_id>
GET /dash/__continuity/current

This blueprint does not open the producer, does not expose settings writes,
and does not mutate resident state. Unknown paths under the prefix return
JSON 404 so the SPA catch-all never serves HTML to the page client.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from flask import Blueprint, jsonify, request

from continuity.read_surface import get_block_detail, get_current, list_blocks

logger = logging.getLogger(__name__)

_ALL_METHODS = ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS')


def create_context_compression_blueprint(
    *,
    db_path: str,
    window_identity_reader: Callable[[Any, str], Mapping[str, Any]] | None = None,
) -> Blueprint:
    blueprint = Blueprint(
        'context_compression',
        __name__,
        url_prefix='/dash/__continuity',
    )

    def _no_store(response):
        response.headers['Cache-Control'] = 'no-store'
        return response

    def _method_not_allowed():
        response = jsonify({'ok': False, 'error': 'method_not_allowed'})
        response.status_code = 405
        response.headers['Allow'] = 'GET, HEAD'
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
            return _json_ok(get_current(
                db_path=db_path,
                window_identity_reader=window_identity_reader,
            ))
        except Exception:
            logger.exception('context-compression/current failed')
            return _json_error('continuity_read_failed', 500)

    @blueprint.route('/', defaults={'rest': ''}, methods=_ALL_METHODS)
    @blueprint.route('/<path:rest>', methods=_ALL_METHODS)
    def continuity_unknown(rest: str):
        return _json_error('not_found', 404)

    return blueprint
