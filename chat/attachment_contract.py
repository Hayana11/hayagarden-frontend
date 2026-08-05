"""Narrow validation helpers for chat file and image attachments."""
from __future__ import annotations

import io
import json
import os
import re
import warnings
from html import escape
from pathlib import Path
from typing import BinaryIO, Optional
from urllib.parse import unquote, urlsplit


CHAT_FILE_URL_PREFIX = '/static/uploads/files/'
ALLOWED_TEXT_FILE_EXTENSIONS = frozenset({
    '.md', '.txt', '.html', '.htm', '.py', '.js', '.json', '.csv', '.css',
    '.xml', '.yaml', '.yml', '.log', '.ini', '.sh',
})
MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024

MAX_IMAGE_INPUT_BYTES = 10 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8192
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_STORED_DIMENSION = 2048
MAX_IMAGE_OUTPUT_BYTES = 8 * 1024 * 1024


class AttachmentValidationError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def sandbox_preview_shell(content_url: str) -> str:
    """Return a fixed preview shell; untrusted HTML is assigned only to srcdoc."""
    url_json = json.dumps(str(content_url))
    return '''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>安全预览</title><style>html,body,iframe{width:100%;height:100%;margin:0;border:0}
body{background:#fff}#error{padding:24px;font-family:sans-serif;color:#8b3340}</style></head>
<body><iframe id="preview" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe>
<div id="error" hidden>预览加载失败</div><script>
fetch(__CONTENT_URL__,{credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error();return r.json()})
.then(function(data){document.getElementById('preview').srcdoc=String(data.content||'')})
.catch(function(){document.getElementById('preview').hidden=true;document.getElementById('error').hidden=false});
</script></body></html>'''.replace('__CONTENT_URL__', url_json)


def render_markdown_preview_page(title: str, markdown_text: str) -> str:
    """Render Markdown as a complete page for sandboxed ``iframe.srcdoc``."""
    import markdown as markdown_lib

    html_body = markdown_lib.markdown(
        str(markdown_text),
        extensions=['fenced_code', 'tables'],
    )
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>' + escape(str(title)) + '</title>'
        '<style>body{font-family:-apple-system,"PingFang SC",sans-serif;max-width:720px;'
        'margin:40px auto;padding:0 20px;line-height:1.7;color:#2a2020}'
        'h1,h2,h3{color:#5a4a6a}pre{background:#f5f0e8;padding:12px;border-radius:8px;overflow-x:auto}'
        'code{background:#f5f0e8;padding:1px 5px;border-radius:4px}'
        'table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:6px 10px}</style>'
        '</head><body>' + html_body + '</body></html>'
    )


def _is_relative_to(candidate: Path, base: Path) -> bool:
    try:
        candidate.relative_to(base)
        return True
    except ValueError:
        return False


def safe_child_path(base_dir: str, filename: str) -> Optional[Path]:
    """Resolve one basename below base_dir; traversal and siblings fail closed."""
    name = str(filename or '')
    if (
        not name or name in {'.', '..'} or '/' in name or '\\' in name
        or Path(name).name != name
    ):
        return None
    base = Path(base_dir).resolve()
    candidate = (base / name).resolve()
    return candidate if _is_relative_to(candidate, base) else None


def resolve_uploaded_file_url(file_url: str, files_dir: str) -> Optional[Path]:
    """Resolve only canonical chat-upload URLs to one file inside files_dir."""
    parsed = urlsplit(str(file_url or ''))
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    if not parsed.path.startswith(CHAT_FILE_URL_PREFIX):
        return None
    encoded_name = parsed.path[len(CHAT_FILE_URL_PREFIX):]
    if not encoded_name or '/' in encoded_name or '\\' in encoded_name:
        return None
    name = unquote(encoded_name)
    return safe_child_path(files_dir, name)


def validate_uploaded_file_reference(
    file_url: str,
    file_name: str,
    files_dir: str,
) -> Optional[tuple[Path, str]]:
    """Validate a client-provided upload reference without trusting its label."""
    path = resolve_uploaded_file_url(file_url, files_dir)
    label = os.path.basename(str(file_name or '').strip())
    if path is None or not path.is_file() or not label:
        return None
    if not re.fullmatch(r'[\w\u4e00-\u9fff.\-]{1,255}', label):
        return None
    storage_prefix, separator, stored_label = path.name.partition('_')
    if (
        not separator or len(storage_prefix) != 8
        or any(ch not in '0123456789abcdef' for ch in storage_prefix.lower())
        or stored_label != label
    ):
        return None
    suffix = path.suffix.lower()
    if suffix not in ALLOWED_TEXT_FILE_EXTENSIONS or Path(label).suffix.lower() != suffix:
        return None
    try:
        if path.stat().st_size > MAX_TEXT_FILE_BYTES:
            return None
    except OSError:
        return None
    return path, label


def read_limited_upload(stream: BinaryIO, max_bytes: int = MAX_IMAGE_INPUT_BYTES) -> bytes:
    data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise AttachmentValidationError('图片超过 10MB', 413)
    if not data:
        raise AttachmentValidationError('图片为空')
    return data


def write_limited_text_upload(
    stream: BinaryIO,
    destination: str | os.PathLike,
    max_bytes: int = MAX_TEXT_FILE_BYTES,
) -> int:
    """Bound the application read before creating a stored text attachment."""
    data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise AttachmentValidationError('文件超过 2MB', 413)
    with open(destination, 'wb') as output:
        output.write(data)
    return len(data)


def reencode_chat_image(data: bytes) -> tuple[bytes, str, str]:
    """Decode, bound, resize and metadata-strip JPEG/PNG/WebP bytes."""
    from PIL import Image, ImageOps

    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                source_format = str(source.format or '').upper()
                if source_format not in {'JPEG', 'PNG', 'WEBP'}:
                    raise AttachmentValidationError('只支持真实的 JPG、PNG 或 WebP 图片')
                width, height = source.size
                if (
                    width <= 0 or height <= 0
                    or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION
                    or width * height > MAX_IMAGE_PIXELS
                ):
                    raise AttachmentValidationError('图片尺寸或像素数过大', 413)
                if getattr(source, 'is_animated', False):
                    raise AttachmentValidationError('暂不支持动图')
                source.load()
                image = ImageOps.exif_transpose(source).copy()
    except AttachmentValidationError:
        raise
    except Exception as exc:
        raise AttachmentValidationError('图片解码失败') from exc

    if max(image.size) > MAX_IMAGE_STORED_DIMENSION:
        image.thumbnail(
            (MAX_IMAGE_STORED_DIMENSION, MAX_IMAGE_STORED_DIMENSION),
            Image.Resampling.LANCZOS,
        )

    has_alpha = image.mode in {'RGBA', 'LA'} or (
        image.mode == 'P' and 'transparency' in image.info
    )
    out = io.BytesIO()
    if source_format == 'PNG':
        image = image.convert('RGBA' if has_alpha else 'RGB')
        image.save(out, format='PNG', optimize=True)
        extension, mime = '.png', 'image/png'
    elif source_format == 'WEBP':
        image = image.convert('RGBA' if has_alpha else 'RGB')
        image.save(out, format='WEBP', quality=82, method=4)
        extension, mime = '.webp', 'image/webp'
    else:
        image = image.convert('RGB')
        image.save(out, format='JPEG', quality=85, optimize=True)
        extension, mime = '.jpg', 'image/jpeg'

    encoded = out.getvalue()
    if len(encoded) > MAX_IMAGE_OUTPUT_BYTES:
        raise AttachmentValidationError('压缩后的图片仍超过 8MB', 413)
    return encoded, extension, mime
