"""
relay_sanitize.py — outbound payload sanitizer for relay/manager.py.

Scrub API keys, bearer tokens, sensitive paths, and unknown IPs from the
full outbound JSON payload before it reaches a third-party relay. Operates
on a deep copy; the caller's payload is never modified.

PR 1 scope: outbound replacement only. [KEY_n] refs are stable within a
single sanitize call but are not persisted for shell_exec round-trip.
"""

from __future__ import annotations

import copy
import os
import re
from typing import Any

MIN_SECRET_LITERAL_LEN = 12

_PASSTHROUGH_IPS = {"127.0.0.1", "0.0.0.0"}


def _load_host_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for pair in os.environ.get("HOST_ALIASES", "").split(","):
        ip, _, name = pair.partition("=")
        if ip.strip() and name.strip():
            aliases[ip.strip()] = name.strip()
    return aliases


HOST_ALIASES = _load_host_aliases()

_KEY_PREFIX = re.compile(
    r"\b(?:sk-(?:ant-|or-|cp-)?|wrk-|tvly-(?:dev-)?|fc-)[A-Za-z0-9_\-]{8,}"
)
_BEARER = re.compile(r"(Bearer\s+)([A-Za-z0-9_\-.]{8,})", re.IGNORECASE)
_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_URL_PARAM = re.compile(
    r"(?<=[?&])((?:key|token|secret|api_key|apiKey|access_token))=([^&\s\"']{6,})"
)
_ENV_ASSIGN = re.compile(
    r"(?:^|(?<=\s))((?:export\s+)?(?:[A-Z][A-Z0-9_]{0,64}_)?(?:KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL))\s*=\s*(\S+)",
    re.MULTILINE,
)
_NOT_A_SECRET = re.compile(
    r"""^(?:\[[A-Z0-9_]+\]|\$\{?[A-Z][A-Z0-9_]*(?::-?[^}]*)?\}?|None|null|''|"")$"""
)
_SSH_FILES = re.compile(
    r"\b(?:id_(?:ed25519|rsa|ecdsa)[\w.]*|authorized_keys|known_hosts)\b"
)
_SENSITIVE_PATH = re.compile(
    r"""(?:[^\s"']{0,128}\.ssh/[^\s"']+|/[^\s"']{0,128}\.env\b|[^\s"']{0,128}vault\.db\b)"""
)
_IPV4 = re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3})(:\d{2,5})?\b")

_CREDENTIAL_ENV_KEYS = [
    name.strip()
    for name in os.environ.get(
        "CREDENTIAL_ENV_WHITELIST",
        "ANTHROPIC_API_KEY,API_KEY,DEEPSEEK_API_KEY,TAVILY_API_KEY,"
        "GITHUB_TOKEN,BOARD_TOKEN_FYODOR,CLAUDE_CODE_OAUTH_TOKEN",
    ).split(",")
    if name.strip()
]


class _RefVault:
    """In-memory stable refs for one sanitize pass."""

    def __init__(self) -> None:
        self._pairs: dict[str, str] = {}
        self._counter = 0

    def put(self, value: str, kind: str = "key") -> str:
        if _NOT_A_SECRET.match(value):
            return value
        if value in self._pairs:
            return self._pairs[value]
        self._counter += 1
        ref = f"[KEY_{self._counter}]"
        self._pairs[value] = ref
        return ref

    def all_pairs(self) -> list[tuple[str, str]]:
        return [(value, ref) for value, ref in self._pairs.items()]


def _sub_ip(m: re.Match) -> str:
    ip, port = m.group(1), m.group(2) or ""
    if ip in _PASSTHROUGH_IPS:
        return m.group(0)
    alias = HOST_ALIASES.get(ip)
    return f"{alias}{port}" if alias else "[IP]"


def _scrub(text: str, vault: _RefVault) -> str:
    text = _PEM.sub(lambda m: vault.put(m.group(0), "pem"), text)
    text = _KEY_PREFIX.sub(lambda m: vault.put(m.group(0), "key"), text)
    text = _BEARER.sub(lambda m: m.group(1) + vault.put(m.group(2), "token"), text)
    text = _URL_PARAM.sub(
        lambda m: f"{m.group(1)}={vault.put(m.group(2), 'token')}", text
    )
    text = _ENV_ASSIGN.sub(
        lambda m: f"{m.group(1)}={vault.put(m.group(2), 'env')}", text
    )

    text = _SSH_FILES.sub("[REDACTED]", text)
    text = _SENSITIVE_PATH.sub("[PATH]", text)
    text = _IPV4.sub(_sub_ip, text)

    for real_value, ref in vault.all_pairs():
        if len(real_value) >= MIN_SECRET_LITERAL_LEN and real_value in text:
            text = text.replace(real_value, ref)
    for env_name in _CREDENTIAL_ENV_KEYS:
        value = os.environ.get(env_name)
        if value and len(value) >= MIN_SECRET_LITERAL_LEN and value in text:
            text = text.replace(value, f"[{env_name}]")
    return text


def _sanitize_content(content: Any, vault: _RefVault) -> Any:
    if isinstance(content, str):
        return _scrub(content, vault)
    if isinstance(content, list):
        return [_sanitize_content(item, vault) for item in content]
    if isinstance(content, dict):
        return {k: _sanitize_content(v, vault) for k, v in content.items()}
    return content


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy the payload and recursively sanitize every string value."""
    vault = _RefVault()
    return _sanitize_content(copy.deepcopy(payload), vault)
