"""Validate and re-encode moments cover uploads."""

from __future__ import annotations

import io
from typing import BinaryIO

from PIL import Image

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_DIMENSION = 8192
_OUTPUT_EXT = '.jpg'


def read_bounded(stream: BinaryIO, max_bytes: int = MAX_BYTES) -> bytes:
    limit = max_bytes + 1
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = stream.read(min(65536, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    data = b''.join(chunks)
    if len(data) > max_bytes:
        raise ValueError(f'image exceeds {max_bytes} bytes')
    if not data:
        raise ValueError('empty file')
    return data


def _validate_image_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ValueError('invalid image dimensions')
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise ValueError('image dimensions too large')
    if width * height > MAX_PIXELS:
        raise ValueError('image resolution too large')


def encode_cover_image(data: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as img:
            _validate_image_dimensions(*img.size)
            img.load()
            if getattr(img, 'is_animated', False):
                img.seek(0)
                _validate_image_dimensions(*img.size)
            if img.mode in ('RGBA', 'LA'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                rgb = background
            elif img.mode == 'P':
                rgb = img.convert('RGBA')
                background = Image.new('RGB', rgb.size, (255, 255, 255))
                background.paste(rgb, mask=rgb.split()[-1])
                rgb = background
            elif img.mode != 'RGB':
                rgb = img.convert('RGB')
            else:
                rgb = img
            buf = io.BytesIO()
            rgb.save(buf, format='JPEG', quality=88, optimize=True)
            encoded = buf.getvalue()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('invalid image') from exc

    if not encoded:
        raise ValueError('invalid image')
    if len(encoded) > MAX_BYTES:
        raise ValueError(f'encoded image exceeds {MAX_BYTES} bytes')
    return encoded


def output_extension() -> str:
    return _OUTPUT_EXT
