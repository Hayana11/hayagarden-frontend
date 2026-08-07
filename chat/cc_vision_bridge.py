"""Claude Code stream-json vision input bridge (P0).

Pure helpers: resolve a chat image reference → validated bytes → the exact
content-block shape accepted by Claude Code 2.1.x stream-json user messages:

    {"type": "image", "source": {
        "type": "base64", "media_type": "image/png|jpeg|webp", "data": "<b64>"
    }}

No permanent base64 storage. Fail closed before any empty/invalid image block
is produced.
"""
from __future__ import annotations

import base64
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from chat.attachment_contract import (
    MAX_IMAGE_OUTPUT_BYTES,
    safe_child_path,
)

ALLOWED_IMAGE_MIMES = frozenset({'image/png', 'image/jpeg', 'image/webp'})
_ATTACHMENT_URI_RE = re.compile(r'^attachment://([0-9a-fA-F]{8})$')
_ATTACHMENT_API_RE = re.compile(r'^/api/attachments/([0-9a-fA-F]{8})$')
_STATIC_UPLOAD_RE = re.compile(r'^/static/uploads/([^/]+)$')
_IMAGE_PLACEHOLDER = '[image]'

# Defaults match production layout; tests inject overrides.
DEFAULT_UPLOAD_DIR = '/opt/frontend/static/uploads'
DEFAULT_ATTACH_DIR = '/opt/frontend/attachments'


class VisionBridgeError(RuntimeError):
    """Raised when an image cannot be safely turned into Claude vision input."""

    def __init__(self, message: str, *, code: str = 'vision_bridge_error'):
        super().__init__(message)
        self.code = code


def _norm_mime(mime: str, *, filename: str = '') -> str:
    m = str(mime or '').strip().lower()
    if m == 'image/jpg':
        m = 'image/jpeg'
    if m in ALLOWED_IMAGE_MIMES:
        return m
    guessed, _ = mimetypes.guess_type(filename or '')
    g = str(guessed or '').lower()
    if g == 'image/jpg':
        g = 'image/jpeg'
    if g in ALLOWED_IMAGE_MIMES:
        return g
    raise VisionBridgeError('unsupported image mime: %s' % (mime or guessed or '?'),
                            code='invalid_mime')


def parse_image_ref(ref: str) -> tuple[str, str]:
    """Return ``(kind, token)`` where kind is ``attachment`` or ``static_upload``."""
    raw = str(ref or '').strip()
    if not raw:
        raise VisionBridgeError('empty image reference', code='missing_attachment')
    m = _ATTACHMENT_URI_RE.match(raw)
    if m:
        return 'attachment', m.group(1).lower()
    m = _ATTACHMENT_API_RE.match(raw)
    if m:
        return 'attachment', m.group(1).lower()
    m = _STATIC_UPLOAD_RE.match(raw)
    if m:
        return 'static_upload', m.group(1)
    raise VisionBridgeError('unrecognized image reference', code='bad_image_ref')


def _is_safe_regular_file(path: Path, *, root: Path) -> bool:
    try:
        if path.is_symlink():
            return False
        resolved = path.resolve()
        root_resolved = root.resolve()
        if not str(resolved).startswith(str(root_resolved) + os.sep) and resolved != root_resolved:
            return False
        return resolved.is_file() and not resolved.is_symlink()
    except OSError:
        return False


def resolve_image_bytes(
    ref: str,
    *,
    upload_dir: str = DEFAULT_UPLOAD_DIR,
    attach_dir: str = DEFAULT_ATTACH_DIR,
    get_attachment: Optional[Callable[[str], Optional[dict]]] = None,
    max_bytes: int = MAX_IMAGE_OUTPUT_BYTES,
) -> tuple[bytes, str]:
    """Resolve a chat image reference to ``(bytes, mime)``. Fail closed."""
    kind, token = parse_image_ref(ref)
    if kind == 'attachment':
        if get_attachment is None:
            import attachment_store
            get_attachment = attachment_store.get
            attach_dir = getattr(attachment_store, 'ATTACH_DIR', attach_dir)
        row = get_attachment(token)
        if not row:
            raise VisionBridgeError('attachment not found: %s' % token,
                                    code='missing_attachment')
        filename = str(row.get('filename') or '')
        path = safe_child_path(attach_dir, filename)
        if path is None or not _is_safe_regular_file(path, root=Path(attach_dir)):
            raise VisionBridgeError('attachment file missing or unsafe',
                                    code='missing_file')
        mime = _norm_mime(str(row.get('mime') or ''), filename=filename)
    else:
        path = safe_child_path(upload_dir, token)
        if path is None or not _is_safe_regular_file(path, root=Path(upload_dir)):
            raise VisionBridgeError('upload file missing or unsafe',
                                    code='missing_file')
        mime = _norm_mime('', filename=token)

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise VisionBridgeError('cannot stat image file', code='missing_file') from exc
    if size <= 0:
        raise VisionBridgeError('empty image file', code='empty_image')
    if size > int(max_bytes):
        raise VisionBridgeError('image exceeds size limit', code='image_too_large')
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise VisionBridgeError('cannot read image file', code='missing_file') from exc
    if not data:
        raise VisionBridgeError('empty image file', code='empty_image')
    if len(data) > int(max_bytes):
        raise VisionBridgeError('image exceeds size limit', code='image_too_large')
    return data, mime


def build_image_content_block(data: bytes, mime: str) -> dict[str, Any]:
    """Build the Claude Code–accepted image content block. Never empty."""
    if not data:
        raise VisionBridgeError('refusing empty image block', code='empty_image')
    media_type = _norm_mime(mime)
    b64 = base64.standard_b64encode(data).decode('ascii')
    if not b64:
        raise VisionBridgeError('refusing empty image block', code='empty_image')
    return {
        'type': 'image',
        'source': {
            'type': 'base64',
            'media_type': media_type,
            'data': b64,
        },
    }


def _strip_trailing_image_placeholder(text: str) -> str:
    t = str(text or '')
    return re.sub(r'(?:\r?\n){0,2}\[image\]\s*$', '', t)


def build_claude_user_content(
    text: str = '',
    image_refs: Optional[Sequence[str]] = None,
    *,
    resolve_fn: Optional[Callable[[str], tuple[bytes, str]]] = None,
) -> Any:
    """Build Claude Code user ``content`` (str or multimodal list).

    text-only → plain ``str`` (legacy-compatible)
    text+image / image-only → ``list`` of content blocks
    """
    refs = [str(r).strip() for r in (image_refs or []) if str(r or '').strip()]
    if not refs:
        return str(text or '')

    resolve = resolve_fn or (lambda r: resolve_image_bytes(r))
    blocks: list[dict[str, Any]] = []
    text_part = _strip_trailing_image_placeholder(str(text or ''))
    if text_part.strip() == _IMAGE_PLACEHOLDER:
        text_part = ''
    if text_part:
        blocks.append({'type': 'text', 'text': text_part})

    for ref in refs:
        data, mime = resolve(ref)
        blocks.append(build_image_content_block(data, mime))

    if not blocks:
        raise VisionBridgeError('image-only turn produced no content',
                                code='empty_vision_turn')
    return blocks


def assert_claude_user_content_safe(content: Any) -> None:
    """Fail closed before stdin write if content contains invalid image blocks."""
    if isinstance(content, str):
        return
    if not isinstance(content, list):
        raise VisionBridgeError('user content must be str or list',
                                code='bad_content_type')
    for block in content:
        if not isinstance(block, dict):
            raise VisionBridgeError('invalid content block', code='bad_content_block')
        btype = block.get('type')
        if btype == 'text':
            continue
        if btype == 'image':
            src = block.get('source')
            if not isinstance(src, dict):
                raise VisionBridgeError('invalid image source', code='bad_image_block')
            if src.get('type') != 'base64':
                raise VisionBridgeError('unsupported image source type',
                                        code='bad_image_block')
            data = src.get('data')
            mime = str(src.get('media_type') or '')
            if not data or not isinstance(data, str):
                raise VisionBridgeError('refusing empty image block',
                                        code='empty_image')
            if mime not in ALLOWED_IMAGE_MIMES and mime != 'image/jpg':
                raise VisionBridgeError('unsupported image mime in block',
                                        code='invalid_mime')
            continue
        # Other block types (should not appear on the user turn we build).
        raise VisionBridgeError('unexpected content block type: %s' % btype,
                                code='bad_content_block')
    if not content:
        raise VisionBridgeError('empty user content', code='empty_vision_turn')


def summarize_content_shape(content: Any) -> dict[str, Any]:
    """Diagnostic summary without dumping base64."""
    if isinstance(content, str):
        return {'kind': 'text', 'chars': len(content)}
    if not isinstance(content, list):
        return {'kind': type(content).__name__}
    parts = []
    for b in content:
        if not isinstance(b, dict):
            parts.append({'type': '?'})
            continue
        if b.get('type') == 'text':
            parts.append({'type': 'text', 'chars': len(str(b.get('text') or ''))})
        elif b.get('type') == 'image':
            src = b.get('source') or {}
            parts.append({
                'type': 'image',
                'media_type': src.get('media_type'),
                'data_len': len(src.get('data') or ''),
            })
        else:
            parts.append({'type': b.get('type')})
    return {'kind': 'multimodal', 'blocks': parts}
