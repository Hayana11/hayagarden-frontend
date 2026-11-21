from flask import jsonify, request


def brain_emotions(get_db):
    """情绪状态 - 返回真实emotion_state + 历史趋势"""
    try:
        import emotion_engine as _ee
        state = _ee.get_state()
        longing = _ee.get_longing()
        # 历史：取最近chat_messages里情绪相关的节点（以updated_at粒度）
        conn = get_db()
        hist = conn.execute(
            "SELECT updated_at, pa, na, valence, arousal, mood_word FROM emotion_state WHERE id=1"
        ).fetchall()
        conn.close()
        current = {
            'pa': state.get('pa', 0.5),
            'na': state.get('na', 0.2),
            'valence': state.get('valence', 0.6),
            'arousal': state.get('arousal', 0.3),
            'mood_word': state.get('mood_word', '平静'),
            'longing': longing,
            'updated_at': state.get('updated_at', ''),
        }
        return jsonify({'ok': True, 'current': current})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


def brain_dreams(get_db):
    """梦 - 返回存储的梦（支持 limit/before 分页）"""
    try:
        from tools.dream_meta import fetch_dream_page

        try:
            limit = int(request.args.get('limit', 20))
        except (TypeError, ValueError):
            limit = 20
        before_raw = request.args.get('before')
        before = None
        if before_raw not in (None, ''):
            try:
                before = int(before_raw)
            except (TypeError, ValueError):
                return jsonify({'ok': False, 'error': 'invalid before'}), 400

        conn = get_db()
        page = fetch_dream_page(conn, limit=limit, before=before, include_id=False)
        conn.close()
        return jsonify({
            'ok': True,
            'items': page['items'],
            'has_more': page['has_more'],
            'next_before': page['next_before'],
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


def _brain_posts_page(get_db, type_name: str, *, limit_default=20, limit_max=50, order_by='id'):
    """通用 posts 分页：limit + before(id)。返回 (rows, has_more, next_before, err)。"""
    try:
        limit = int(request.args.get('limit', limit_default))
    except (TypeError, ValueError):
        limit = limit_default
    limit = max(1, min(limit, limit_max))
    before_raw = request.args.get('before')
    before = None
    if before_raw not in (None, ''):
        try:
            before = int(before_raw)
        except (TypeError, ValueError):
            return None, None, None, 'invalid before'
    conn = get_db()
    if order_by == 'created_at':
        if before is not None:
            rows = conn.execute(
                "SELECT id, author, content, created_at FROM posts "
                "WHERE type=? AND id < ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (type_name, before, limit + 1),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, author, content, created_at FROM posts "
                "WHERE type=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (type_name, limit + 1),
            ).fetchall()
    else:
        if before is not None:
            rows = conn.execute(
                "SELECT id, author, content, created_at FROM posts "
                "WHERE type=? AND id < ? ORDER BY id DESC LIMIT ?",
                (type_name, before, limit + 1),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, author, content, created_at FROM posts "
                "WHERE type=? ORDER BY id DESC LIMIT ?",
                (type_name, limit + 1),
            ).fetchall()
    conn.close()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_before = int(rows[-1]['id']) if has_more and rows else None
    return rows, has_more, next_before, None


def brain_thoughts(get_db):
    """深夜想法 - posts THOUGHT（支持 limit/before 分页）"""
    try:
        rows, has_more, next_before, err = _brain_posts_page(get_db, 'THOUGHT', limit_default=20)
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        items = []
        for r in rows:
            created_at = r['created_at'] or ''
            items.append({
                'id': int(r['id']),
                'author': (r['author'] or 'fyodor').strip() or 'fyodor',
                'created_at': created_at,
                'time': created_at[11:16] if created_at else '—',
                'content': (r['content'] or ''),
            })
        return jsonify({
            'ok': True,
            'items': items,
            'has_more': bool(has_more and next_before is not None),
            'next_before': next_before,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


def brain_diary(get_db):
    """日摘要 - 费奥多尔的日记归档（支持 limit/before 分页）"""
    try:
        rows, has_more, next_before, err = _brain_posts_page(
            get_db, 'DAILY_SUMMARY', limit_default=20, order_by='created_at',
        )
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        items = []
        seen = set()
        for r in rows:
            c = (r['content'] or '').strip()
            if not c or c in seen:
                continue
            seen.add(c)
            created_at = r['created_at'] or ''
            items.append({
                'id': int(r['id']),
                'author': (r['author'] or 'fyodor').strip() or 'fyodor',
                'created_at': created_at,
                'date': created_at[:10] if created_at else '—',
                'content': c,
            })
        return jsonify({
            'ok': True,
            'items': items,
            'has_more': bool(has_more and next_before is not None),
            'next_before': next_before,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
