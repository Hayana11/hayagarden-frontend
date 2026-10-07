"""Stateful privacy-first filtering for all private provider protocol deltas."""

from __future__ import annotations


_OPENERS = (
    ("control", "<hidden_flow_control"),
    ("guidance", "<hidden_flow_guidance"),
    ("available", "<available_hidden_flows"),
)
_CLOSERS = {
    "guidance": "</hidden_flow_guidance>",
    "available": "</available_hidden_flows>",
}
_BOUNDARY = frozenset(" \t\r\n/>")


class HiddenFlowStreamFilter:
    """Emit ordinary text promptly while withholding private protocol candidates."""

    def __init__(self) -> None:
        self._pending = ""
        self._hidden = ""
        self._hidden_kind: str | None = None
        self._hidden_quote: str | None = None
        self._hidden_open_complete = False
        self._finished = False

    @staticmethod
    def _opener_kind(value: str) -> str | None:
        folded = value.casefold()
        for kind, opener in _OPENERS:
            if folded == opener.casefold():
                return kind
        return None

    @staticmethod
    def _is_prefix(value: str) -> bool:
        folded = value.casefold()
        return any(opener.casefold().startswith(folded) for _kind, opener in _OPENERS)

    @staticmethod
    def _suffix_prefix(value: str) -> int:
        folded = value.casefold()
        longest = 0
        for _kind, opener in _OPENERS:
            for length in range(min(len(value), len(opener)), 0, -1):
                if folded[-length:] == opener[:length].casefold():
                    longest = max(longest, length)
                    break
        return longest

    def _release_mismatch(self, value: str) -> str:
        keep = self._suffix_prefix(value)
        if keep:
            self._pending = value[-keep:]
            return value[:-keep]
        self._pending = ""
        return value

    def _start_hidden(self, kind: str, seed: str) -> None:
        self._hidden = seed
        self._hidden_kind = kind
        self._hidden_quote = None
        self._hidden_open_complete = (
            kind in _CLOSERS and seed.endswith(">")
        )
        self._pending = ""

    def _reset_hidden(self) -> None:
        self._hidden = ""
        self._hidden_kind = None
        self._hidden_quote = None
        self._hidden_open_complete = False
        self._pending = ""

    def _feed_normal(self, value: str) -> str:
        visible: list[str] = []
        index = 0
        while index < len(value):
            char = value[index]
            if not self._pending:
                if char == "<":
                    self._pending = char
                else:
                    visible.append(char)
                index += 1
                continue

            pending_kind = self._opener_kind(self._pending)
            if pending_kind is not None:
                if char in _BOUNDARY:
                    self._start_hidden(pending_kind, self._pending + char)
                    visible.append(self._feed_hidden(value[index + 1:]))
                    return "".join(visible)
                visible.append(self._release_mismatch(self._pending + char))
                index += 1
                continue

            candidate = self._pending + char
            if self._is_prefix(candidate):
                self._pending = candidate
                index += 1
                continue
            visible.append(self._release_mismatch(candidate))
            index += 1
        return "".join(visible)

    def _feed_hidden(self, value: str) -> str:
        kind = self._hidden_kind
        if kind is None:
            return self._feed_normal(value)
        if kind == "control":
            for index, char in enumerate(value):
                self._hidden += char
                if self._hidden_quote is not None:
                    if char == self._hidden_quote:
                        self._hidden_quote = None
                    continue
                if char in {"\"", "'"}:
                    self._hidden_quote = char
                    continue
                if self._hidden.endswith("/>"):
                    self._reset_hidden()
                    return self._feed_normal(value[index + 1:])
            return ""

        for char in value:
            self._hidden += char
            if not self._hidden_open_complete:
                if self._hidden_quote is not None:
                    if char == self._hidden_quote:
                        self._hidden_quote = None
                elif char in {"\"", "'"}:
                    self._hidden_quote = char
                elif char == ">":
                    self._hidden_open_complete = True
                continue
            close = _CLOSERS[kind]
            close_index = self._hidden.casefold().find(close.casefold())
            if close_index >= 0:
                remainder = self._hidden[close_index + len(close):]
                self._reset_hidden()
                return self._feed_normal(remainder)
        return ""

    def feed(self, delta: str) -> str:
        if self._finished:
            return ""
        value = str(delta or "")
        if not value:
            return ""
        if self._hidden:
            return self._feed_hidden(value)
        return self._feed_normal(value)

    def finish(self) -> str:
        if self._finished:
            return ""
        self._finished = True
        if self._hidden:
            self._reset_hidden()
            return ""
        pending = self._pending
        self._pending = ""
        if len(pending) > 1 and self._is_prefix(pending):
            return ""
        return pending

    def close(self) -> str:
        return self.finish()
