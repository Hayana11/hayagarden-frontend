"""Flask routes that expose a deliberately small WeRead data contract."""

import os

from flask import Blueprint, jsonify

from weread_client import WereadClient, WereadError


def _pick(obj, *keys, default=None):
    if not isinstance(obj, dict):
        return default
    for key in keys:
        value = obj.get(key)
        if value is not None and value != "":
            return value
    return default


def _unwrap(value):
    current = value
    for _ in range(4):
        if not isinstance(current, dict):
            return current
        nested = next(
            (
                current[key]
                for key in ("data", "result", "response", "payload")
                if isinstance(current.get(key), (dict, list))
            ),
            None,
        )
        if nested is None:
            return current
        current = nested
    return current


def _items(value, *keys):
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    for key in keys:
        if isinstance(value.get(key), list):
            return value[key]
    return []


def _bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _categories(value):
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if value is None:
        return []
    value = str(value).strip()
    return [value] if value else []


def _book(item):
    book_id = _pick(item, "bookId", "book_id", "id")
    if book_id is None:
        return None
    return {
        "bookId": str(book_id),
        "title": str(_pick(item, "title", "bookTitle", default="")),
        "author": str(_pick(item, "author", "authorName", "author_name", default="")),
        "cover": _pick(item, "cover", "coverUrl", "cover_url", "bookCover"),
        "category": _categories(_pick(item, "category", "categories", "tags")),
        "readUpdateTime": _pick(item, "readUpdateTime", "read_update_time", "lastReadTime"),
        "finishReading": _bool(_pick(item, "finishReading", "finish_reading", default=False)),
        "isTop": _bool(_pick(item, "isTop", "is_top", default=False)),
        "secret": _bool(_pick(item, "secret", default=False)),
        "deepLink": _pick(item, "deepLink", "deep_link", "url"),
    }


def _album(item):
    # Official /shelf/sync albums are split between albumInfo and
    # albumInfoExtra.  A flat album object is not a valid contract.
    if not isinstance(item, dict):
        return None
    info = item.get("albumInfo")
    extra = item.get("albumInfoExtra")
    if not isinstance(info, dict):
        return None
    if not isinstance(extra, dict):
        extra = {}
    album_id = _pick(info, "albumId")
    if album_id is None:
        return None
    return {
        "albumId": str(album_id),
        "title": str(_pick(info, "name", default="")),
        "author": str(_pick(info, "authorName", default="")),
        "cover": _pick(info, "cover"),
        "trackCount": _int(_pick(info, "trackCount", default=0)),
        "finish": _bool(_pick(info, "finish", default=False)),
        "secret": _bool(_pick(extra, "secret", default=False)),
        "lectureReadUpdateTime": _pick(extra, "lectureReadUpdateTime"),
        "isTop": _bool(_pick(extra, "isTop", default=False)),
    }


def normalize_shelf(raw):
    data = _unwrap(raw)
    if not isinstance(data, dict):
        data = {}
    books = [item for item in (_book(x) for x in _items(data, "books", "bookList", "book_list")) if item]
    albums = [item for item in (_album(x) for x in _items(data, "albums")) if item]
    mp = _pick(data, "mp", "articleCollection", "article_collection")
    return {
        "books": books,
        "albums": albums,
        "hasArticleCollection": bool(mp),
        "totalVisibleCount": len(books) + len(albums) + (1 if mp else 0),
    }


def normalize_progress(book_id, raw):
    data = _unwrap(raw)
    if isinstance(data, dict) and isinstance(data.get("book"), dict):
        data = data["book"]
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        data = {}
    progress = max(0, min(100, _int(_pick(data, "progress", "percent", default=0))))
    return {
        "bookId": str(_pick(data, "bookId", "book_id", default=book_id)),
        "chapterUid": _pick(data, "chapterUid", "chapter_uid"),
        "chapterOffset": _int(_pick(data, "chapterOffset", "chapter_offset", default=0)),
        "progress": progress,
        "updateTime": _pick(data, "updateTime", "update_time"),
        "recordReadingTime": _pick(data, "recordReadingTime", "record_reading_time"),
        "finishTime": _pick(data, "finishTime", "finish_time"),
        "isStartReading": _bool(_pick(data, "isStartReading", "is_start_reading", default=False)),
    }


def _note(item, kind, index):
    if not isinstance(item, dict):
        return None

    source = item
    if kind == "review":
        source = item.get("review")
        if not isinstance(source, dict):
            return None
        note_id = _pick(source, "reviewId")
        quote = _pick(source, "abstract", default="")
        text = _pick(source, "content", default="")
    else:
        note_id = _pick(source, "bookmarkId")
        quote = _pick(source, "markText", default="")
        text = _pick(source, "note", "content", "comment", "text", default="")

    if note_id is None:
        note_id = kind + "-" + str(index)
    position = _pick(source, "range", "position")
    if position is None:
        start = _pick(source, "start", "startPos", "start_pos", "begin")
        end = _pick(source, "end", "endPos", "end_pos")
        if start is not None or end is not None:
            position = {"start": start, "end": end}

    note = {
        "id": str(note_id),
        "type": kind,
        "who": "haya",
        "quote": str(quote or ""),
        "text": str(text or ""),
        "chapterUid": _pick(source, "chapterUid", "chapter_uid"),
        "range": position,
        "createdAt": _pick(source, "createTime", "createdAt", "created_at", "updateTime", "update_time"),
    }
    if kind == "bookmark":
        note["bookId"] = _pick(source, "bookId", "book_id")
        note["colorStyle"] = _pick(source, "colorStyle")
    else:
        note.update({
            "chapterIdx": _pick(source, "chapterIdx", "chapter_idx"),
            "chapterName": _pick(source, "chapterName", "chapter_name"),
            "star": _pick(source, "star"),
            "isFinish": _bool(_pick(source, "isFinish", "is_finish", default=False)),
        })
    return note


def normalize_notes(book_id, bookmarks, reviews):
    notes = []
    seen = set()
    for kind, rows in (("bookmark", bookmarks), ("review", reviews)):
        for index, item in enumerate(rows or []):
            note = _note(item, kind, index)
            if not note:
                continue
            key = (note["type"], note["id"])
            if key in seen:
                continue
            seen.add(key)
            notes.append(note)
    notes.sort(key=lambda x: str(x.get("createdAt") or ""))
    return {"bookId": str(book_id), "notes": notes}


def _error_response(error):
    body = {"ok": False, "code": error.code}
    if error.code == "WEREAD_UPGRADE_REQUIRED":
        body["upgradeRequired"] = True
    return jsonify(body), error.status


def create_weread_blueprint(client_factory=None):
    bp = Blueprint("weread", __name__, url_prefix="/api/weread")

    def get_client():
        api_key = os.environ.get("WEREAD_API_KEY", "").strip()
        if not api_key:
            raise WereadError("WEREAD_NOT_CONFIGURED", 503)
        return client_factory(api_key) if client_factory else WereadClient(api_key=api_key)

    @bp.get("/shelf")
    def shelf():
        try:
            return jsonify(normalize_shelf(get_client().call("/shelf/sync")))
        except WereadError as error:
            return _error_response(error)

    @bp.get("/books/<book_id>/progress")
    def progress(book_id):
        try:
            raw = get_client().call("/book/getprogress", {"bookId": book_id})
            return jsonify(normalize_progress(book_id, raw))
        except WereadError as error:
            return _error_response(error)

    @bp.get("/books/<book_id>/notes")
    def notes(book_id):
        try:
            client = get_client()
            bookmarks = client.call("/book/bookmarklist", {"bookId": book_id})
            reviews = client.call("/review/list/mine", {"bookid": book_id})
            bookmarks = _unwrap(bookmarks)
            reviews = _unwrap(reviews)
            return jsonify(normalize_notes(
                book_id,
                _items(bookmarks, "updated"),
                _items(reviews, "reviews"),
            ))
        except WereadError as error:
            return _error_response(error)

    return bp
