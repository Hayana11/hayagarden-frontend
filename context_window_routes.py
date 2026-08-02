"""HTTP routes for manual context window (default disabled).

Canonical paths — independent of legacy ``/api/daily-context/*`` daily resolver.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from flask import Blueprint, jsonify, request

from chat.context_window import (
    CLOSE_REASON_MANUAL,
    CarryoverMessageUnforgeableError,
    FirstTurnFinalizePendingError,
    IdempotencyMismatchError,
    NoOpenContextWindowError,
    StaleSourceContextError,
    SwitchFailedError,
    SwitchHooksRequiredError,
    SwitchInProgressError,
    WindowBusyError,
    current_window_summary,
    enabled,
    list_context_window_carryover_rounds,
    parse_strict_json_carryover_count,
    parse_strict_json_positive_int,
    parse_strict_query_positive_int,
    switch_context_window,
)
from chat.context_window_preview import (
    PreviewDbUnavailableError,
    parse_thinking_policy,
    preview_context_window,
)
from chat.daily_context import DEFAULT_CHAT_ID
from daily_context_routes import _default_token_from_env, _reject_non_default_chat_id

logger = logging.getLogger(__name__)


def create_context_window_blueprint(
    *,
    db_path: str,
    token_getter: Optional[Callable[[], str]] = None,
) -> Blueprint:
    blueprint = Blueprint('context_window', __name__)
    get_token = token_getter or _default_token_from_env

    def _auth_error():
        if not enabled():
            return jsonify({'ok': False, 'error': 'disabled'}), 404
        import hmac
        expected = str(get_token() or '').strip()
        if not expected:
            return jsonify({'ok': False, 'error': 'daily soft window token not configured'}), 503
        header = request.headers.get('Authorization', '')
        supplied = header[7:].strip() if header.startswith('Bearer ') else ''
        if not supplied or not hmac.compare_digest(supplied, expected):
            response = jsonify({'ok': False, 'error': 'unauthorized'})
            response.status_code = 401
            response.headers['WWW-Authenticate'] = 'Bearer'
            return response
        return None

    @blueprint.route('/api/context-window/current', methods=['GET'])
    def context_window_current():
        err = _auth_error()
        if err:
            return err
        chat_id = str(request.args.get('chat_id') or DEFAULT_CHAT_ID)
        cid_err = _reject_non_default_chat_id(chat_id)
        if cid_err:
            return cid_err
        try:
            data = current_window_summary(chat_id=chat_id, db_path=db_path)
            return jsonify({'ok': True, **data})
        except NoOpenContextWindowError as exc:
            return jsonify({'ok': False, 'error': str(exc), 'code': 'no_open_context'}), 409
        except ValueError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('context-window/current failed')
            return jsonify({'ok': False, 'error': str(exc)}), 500

    @blueprint.route('/api/context-window/carryover-candidates', methods=['GET'])
    def context_window_carryover_candidates():
        err = _auth_error()
        if err:
            return err
        chat_id = str(request.args.get('chat_id') or DEFAULT_CHAT_ID)
        cid_err = _reject_non_default_chat_id(chat_id)
        if cid_err:
            return cid_err
        try:
            source_context_id = parse_strict_query_positive_int(
                'source_context_id', request.args.get('source_context_id'),
            )
            source_context_epoch = parse_strict_query_positive_int(
                'source_context_epoch', request.args.get('source_context_epoch'),
            )
        except ValueError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400
        try:
            data = list_context_window_carryover_rounds(
                source_context_id,
                source_context_epoch,
                chat_id=chat_id,
                db_path=db_path,
            )
            return jsonify({'ok': True, **data})
        except StaleSourceContextError as exc:
            return jsonify({'ok': False, 'error': str(exc), 'code': 'stale_source_context'}), 409
        except WindowBusyError as exc:
            return jsonify({
                'ok': False,
                'error': str(exc),
                'code': 'window_busy',
                'retryable': True,
            }), 423
        except ValueError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('context-window/carryover-candidates failed')
            return jsonify({'ok': False, 'error': str(exc)}), 500

    @blueprint.route('/api/context-window/preview', methods=['POST'])
    def context_window_preview():
        err = _auth_error()
        if err:
            return err
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'ok': False, 'error': 'invalid preview payload'}), 400
        chat_id = str(data.get('chat_id') or request.args.get('chat_id') or DEFAULT_CHAT_ID)
        cid_err = _reject_non_default_chat_id(chat_id)
        if cid_err:
            return cid_err
        try:
            source_context_id = parse_strict_json_positive_int(
                'source_context_id', data.get('source_context_id'),
            )
            source_context_epoch = parse_strict_json_positive_int(
                'source_context_epoch', data.get('source_context_epoch'),
            )
            count = parse_strict_json_carryover_count(data.get('count'))
            preview_id = str(data['preview_id'])
            thinking_policy = parse_thinking_policy(data.get('thinking_policy'))
        except (KeyError, TypeError):
            return jsonify({'ok': False, 'error': 'invalid preview payload'}), 400
        except ValueError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400
        try:
            result = preview_context_window(
                source_context_id=source_context_id,
                source_context_epoch=source_context_epoch,
                count=count,
                preview_id=preview_id,
                thinking_policy=thinking_policy,
                chat_id=chat_id,
                db_path=db_path,
            )
            return jsonify(result), 200
        except StaleSourceContextError as exc:
            return jsonify({
                'ok': False, 'error': str(exc), 'code': 'stale_source_context',
            }), 409
        except NoOpenContextWindowError as exc:
            return jsonify({
                'ok': False, 'error': str(exc), 'code': 'no_open_context',
            }), 409
        except WindowBusyError as exc:
            return jsonify({
                'ok': False,
                'error': str(exc),
                'code': 'window_busy',
                'retryable': True,
            }), 423
        except SwitchInProgressError as exc:
            return jsonify({
                'ok': False,
                'error': str(exc),
                'code': 'switch_in_progress',
                'retryable': True,
            }), 423
        except PreviewDbUnavailableError as exc:
            return jsonify({
                'ok': False,
                'error': str(exc),
                'code': exc.error_code,
            }), 503
        except ValueError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400
        except Exception:
            logger.exception('context-window/preview failed')
            return jsonify({
                'ok': False,
                'error': 'preview failed',
                'code': 'preview_internal_error',
            }), 500

    @blueprint.route('/api/context-window/switch', methods=['POST'])
    def context_window_switch():
        err = _auth_error()
        if err:
            return err
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'ok': False, 'error': 'invalid switch payload'}), 400
        chat_id = str(data.get('chat_id') or request.args.get('chat_id') or DEFAULT_CHAT_ID)
        cid_err = _reject_non_default_chat_id(chat_id)
        if cid_err:
            return cid_err
        try:
            source_context_id = parse_strict_json_positive_int(
                'source_context_id', data.get('source_context_id'),
            )
            source_context_epoch = parse_strict_json_positive_int(
                'source_context_epoch', data.get('source_context_epoch'),
            )
            count = parse_strict_json_carryover_count(data.get('count'))
            request_id = str(data['request_id'])
        except (KeyError, TypeError):
            return jsonify({'ok': False, 'error': 'invalid switch payload'}), 400
        except ValueError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400
        try:
            result = switch_context_window(
                source_context_id=source_context_id,
                source_context_epoch=source_context_epoch,
                count=count,
                request_id=request_id,
                chat_id=chat_id,
                close_reason=CLOSE_REASON_MANUAL,
                db_path=db_path,
            )
            return jsonify({'ok': True, **result})
        except SwitchHooksRequiredError as exc:
            return jsonify({
                'ok': False,
                'error': str(exc),
                'code': 'switch_hooks_required',
            }), 503
        except IdempotencyMismatchError as exc:
            return jsonify({'ok': False, 'error': str(exc), 'code': 'idempotency_mismatch'}), 409
        except StaleSourceContextError as exc:
            return jsonify({'ok': False, 'error': str(exc), 'code': 'stale_source_context'}), 409
        except CarryoverMessageUnforgeableError as exc:
            return jsonify({
                'ok': False,
                'error': str(exc),
                'code': 'carryover_message_unforgeable',
            }), 409
        except SwitchFailedError as exc:
            return jsonify({'ok': False, 'error': str(exc), 'code': exc.error_code}), 409
        except SwitchInProgressError as exc:
            return jsonify({
                'ok': False,
                'error': str(exc),
                'code': 'switch_in_progress',
                'retryable': True,
            }), 423
        except FirstTurnFinalizePendingError as exc:
            return jsonify({
                'ok': False,
                'error': str(exc),
                'code': 'FIRST_TURN_FINALIZE_PENDING',
                'retryable': True,
            }), 423
        except WindowBusyError as exc:
            return jsonify({
                'ok': False,
                'error': str(exc),
                'code': 'window_busy',
                'retryable': True,
            }), 423
        except ValueError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('context-window/switch failed')
            return jsonify({'ok': False, 'error': str(exc)}), 500

    return blueprint
