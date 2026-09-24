"""Xiaomi consumer API request signing and response decryption helpers."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from typing import Any, Mapping


class CryptoError(Exception):
    """Secret-free cryptographic protocol error."""


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    try:
        return base64.b64decode("".join(str(value).split()), validate=True)
    except (ValueError, TypeError) as exc:
        raise CryptoError("invalid Xiaomi response encoding") from exc


def rc4_drop(key: bytes, data: bytes, *, drop: int = 1024) -> bytes:
    if not key:
        raise CryptoError("invalid Xiaomi encryption key")
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 0xFF
        state[i], state[j] = state[j], state[i]
    i = 0
    j = 0
    def next_byte() -> int:
        nonlocal i, j
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        return state[(state[i] + state[j]) & 0xFF]
    for _ in range(drop):
        next_byte()
    return bytes(byte ^ next_byte() for byte in data)


def generate_nonce(*, now_ms: int | None = None, random_bytes: bytes | None = None) -> str:
    random_part = random_bytes if random_bytes is not None else os.urandom(8)
    if len(random_part) != 8:
        raise CryptoError("invalid Xiaomi nonce")
    minute = int((now_ms if now_ms is not None else time.time() * 1000) // 60000)
    return _b64(random_part + (minute & 0xFFFFFFFF).to_bytes(4, "big"))


def signed_nonce(ssecurity: str, nonce: str) -> bytes:
    return hashlib.sha256(_unb64(ssecurity) + _unb64(nonce)).digest()


def _signature_text(method: str, path: str, params: Mapping[str, Any], key: bytes) -> str:
    signed = _b64(key)
    entries = [str(method).upper(), path if path.startswith("/") else f"/{path}"]
    entries.extend(f"{name}={params[name]}" for name in sorted(params))
    entries.append(signed)
    return "&".join(entries)


def build_encrypted_params(
    method: str,
    path: str,
    ssecurity: str,
    params: Mapping[str, Any] | None = None,
    *,
    nonce: str | None = None,
    now_ms: int | None = None,
) -> dict[str, str]:
    nonce = nonce or generate_nonce(now_ms=now_ms)
    key = signed_nonce(ssecurity, nonce)
    raw: dict[str, str] = {}
    if params:
        raw["data"] = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    raw["rc4_hash__"] = _b64(hashlib.sha1(_signature_text(method, path, raw, key).encode()).digest())
    encrypted: dict[str, str] = {}
    ordered = sorted(raw.items())
    encoded_values = [value.encode("utf-8") for _, value in ordered]
    stream = rc4_drop(key, b"".join(encoded_values))
    offset = 0
    for (name, _), value in zip(ordered, encoded_values):
        encrypted[name] = _b64(stream[offset:offset + len(value)])
        offset += len(value)
    encrypted["signature"] = _b64(hashlib.sha1(_signature_text(method, path, encrypted, key).encode()).digest())
    encrypted["_nonce"] = nonce
    return encrypted


def decrypt_response(ssecurity: str, nonce: str, ciphertext: str) -> Any:
    plain = rc4_drop(signed_nonce(ssecurity, nonce), _unb64(ciphertext)).decode("utf-8")
    if plain.startswith("&&&START&&&"):
        plain = plain[len("&&&START&&&"):]
    try:
        return json.loads(plain)
    except json.JSONDecodeError as exc:
        raise CryptoError("invalid Xiaomi response payload") from exc
