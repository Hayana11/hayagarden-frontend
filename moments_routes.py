"""HTTP routes for the unified moments feed."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

import moments_social
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

    @blueprint.route('/api/moments/chat-collections/<int:collection_id>', methods=['GET'])
    def get_chat_collection(collection_id: int):
        item = moments_store.get_chat_collection(
            collection_id, memories_db_path=memories_db_path
        )
        if not item:
            return jsonify({'error': 'not found'}), 404
        return jsonify(item)

    @blueprint.route('/api/moments/chat-collections/<int:collection_id>', methods=['DELETE'])
    def delete_chat_collection(collection_id: int):
        deleted = moments_store.delete_chat_collection(
            collection_id, memories_db_path=memories_db_path
        )
        if not deleted:
            return jsonify({'error': 'not found'}), 404
        return jsonify({'deleted': True})

    @blueprint.route('/api/moments/react', methods=['POST'])
    def moments_react():
        payload = request.get_json() or {}
        item_key = (payload.get('item_key') or '').strip()
        reaction = (payload.get('reaction') or '').strip().lower()
        if not item_key:
            return jsonify({'error': 'item_key required'}), 400
        try:
            social = moments_social.toggle_reaction(
                item_key,
                reaction,
                memories_db_path=memories_db_path,
                gallery_db_path=gallery_db_path,
            )
            return jsonify({'ok': True, 'social': social})
        except LookupError as exc:
            return jsonify({'error': str(exc)}), 404
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400

    @blueprint.route('/api/moments/comments', methods=['GET'])
    def moments_comments_list():
        item_key = (request.args.get('item_key') or '').strip()
        if not item_key:
            return jsonify({'error': 'item_key required'}), 400
        try:
            limit = int(request.args.get('limit', 50))
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid limit'}), 400
        try:
            comments = moments_social.list_comments(
                item_key,
                memories_db_path=memories_db_path,
                gallery_db_path=gallery_db_path,
                limit=limit,
            )
            return jsonify({'items': comments})
        except LookupError as exc:
            return jsonify({'error': str(exc)}), 404
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400

    @blueprint.route('/api/moments/comments', methods=['POST'])
    def moments_comments_create():
        payload = request.get_json() or {}
        item_key = (payload.get('item_key') or '').strip()
        content = payload.get('content', '') or ''
        if not item_key:
            return jsonify({'error': 'item_key required'}), 400
        try:
            result = moments_social.add_comment(
                item_key,
                content,
                memories_db_path=memories_db_path,
                gallery_db_path=gallery_db_path,
            )
            return jsonify({'ok': True, **result})
        except LookupError as exc:
            return jsonify({'error': str(exc)}), 404
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400

    @blueprint.route('/api/moments/collect-intent', methods=['POST'])
    def collect_intent():
        payload = request.get_json() or {}
        turn_key = (payload.get('turn_key') or '').strip() or None
        conversation_id = (payload.get('conversation_id') or 'hayana-chat').strip() or 'hayana-chat'
        try:
            previous_turns = int(payload.get('previous_turns', 0) or 0)
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid previous_turns'}), 400
        caption = payload.get('caption', '') or ''
        try:
            import moments_turn
            moments_turn.collect_chat_moment(
                memories_db_path,
                turn_key=turn_key,
                conversation_id=conversation_id,
                previous_turns=previous_turns,
                caption=caption,
            )
            return jsonify({'ok': True})
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400

    return blueprint
