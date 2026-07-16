"""HTTP routes for the unified moments feed."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

import moments_store


def create_moments_blueprint(
    *,
    memories_db_path: str,
    gallery_db_path: str | None = None,
) -> Blueprint:
    blueprint = Blueprint('moments', __name__)

    @blueprint.route('/api/moments/feed', methods=['GET'])
    def moments_feed():
        try:
            limit = int(request.args.get('limit', 20))
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid limit'}), 400
        cursor = request.args.get('cursor') or None
        feed_type = request.args.get('type', 'all')
        try:
            payload = moments_store.get_feed(
                memories_db_path=memories_db_path,
                gallery_db_path=gallery_db_path,
                cursor=cursor,
                limit=limit,
                feed_type=feed_type,
            )
            return jsonify(payload)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400

    return blueprint
