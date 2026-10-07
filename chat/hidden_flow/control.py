"""Pure parsing and privacy sanitizing for the hidden control protocol."""

from __future__ import annotations

import re
from typing import Optional

from .types import FlowControl


_CONTROL_NAME = "hidden_flow_control"
_CONTROL_TAG_RE = re.compile(
    r"<hidden_flow_control\b(?P<body>[^<>]*?)/>",
    re.IGNORECASE | re.DOTALL,
)
_ATTRIBUTE_RE = re.compile(r"(?P<name>[A-Za-z][A-Za-z0-9_-]*)\s*=\s*(?P<quote>[\"'])(?P<value>.*?)\2", re.DOTALL)
_VALID_ACTIONS = {"start", "hold", "continue", "stop"}


def _keys(raw: str) -> Optional[tuple[str, ...]]:
    values: list[str] = []
    for value in raw.split("|"):
        item = value.strip()
        if item and item not in values:
            values.append(item)
    if len(values) > 4:
        return None
    return tuple(values)


def parse_hidden_flow_control(raw: str, *, expected_flow_id: Optional[str] = None) -> Optional[FlowControl]:
    """Parse exactly one tail control tag; return ``None`` on ambiguity."""
    text = str(raw or "")
    matches = list(_CONTROL_TAG_RE.finditer(text))
    if len(matches) != 1 or matches[0].end() != len(text.rstrip()):
        return None
    match = matches[0]
    attributes: dict[str, str] = {}
    cursor = 0
    body = match.group("body")
    for item in _ATTRIBUTE_RE.finditer(body):
        if body[cursor:item.start()].strip():
            return None
        name = item.group("name").casefold()
        if name not in {"flow", "action", "keys"}:
            return None
        if name in attributes:
            return None
        attributes[name] = item.group("value").strip()
        cursor = item.end()
    if body[cursor:].strip():
        return None
    flow_id = attributes.get("flow", "").strip()
    if not flow_id or (expected_flow_id is not None and flow_id != str(expected_flow_id).strip()):
        return None
    action = attributes.get("action")
    if action is not None:
        action = action.strip().lower()
        if action not in _VALID_ACTIONS:
            return None
    keys = _keys(attributes.get("keys", ""))
    if keys is None:
        return None
    return FlowControl(flow_id=flow_id, action=action, keys=keys)


def sanitize_hidden_flow_text(raw: str) -> str:
    """Remove complete and trailing partial control payloads from visible text."""
    text = str(raw or "")
    output: list[str] = []
    cursor = 0
    folded = text.casefold()
    opener = "<" + _CONTROL_NAME
    while cursor < len(text):
        start = folded.find(opener, cursor)
        if start < 0:
            output.append(text[cursor:])
            break
        boundary = start + len(opener)
        if boundary < len(text) and (text[boundary].isalnum() or text[boundary] in "_-"):
            output.append(text[cursor:boundary])
            cursor = boundary
            continue
        output.append(text[cursor:start])
        close = text.find("/>", boundary)
        if close < 0:
            break
        cursor = close + 2
    result = "".join(output)
    folded_result = result.casefold()
    for index in range(len(result) - 1, -1, -1):
        suffix = folded_result[index:]
        if len(suffix) > 1 and _OPENER_PREFIX(suffix):
            result = result[:index]
            break
    return result


def _OPENER_PREFIX(value: str) -> bool:
    return ("<" + _CONTROL_NAME).casefold().startswith(value.casefold())

