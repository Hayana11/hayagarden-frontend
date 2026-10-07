"""Stateful privacy-first filtering for provider text deltas."""

from __future__ import annotations


_OPENER = "<hidden_flow_control"


class HiddenFlowStreamFilter:
    """Emit ordinary text promptly while withholding protocol candidates."""

    def __init__(self) -> None:
        self._pending = ""
        self._hidden = ""
        self._quote: str | None = None
        self._finished = False

    @staticmethod
    def _is_prefix(value: str) -> bool:
        return _OPENER.casefold().startswith(value.casefold())

    @staticmethod
    def _suffix_prefix(value: str) -> int:
        folded = value.casefold()
        for length in range(min(len(value), len(_OPENER)), 0, -1):
            if folded[-length:] == _OPENER[:length].casefold():
                return length
        return 0

    def _release_mismatch(self, value: str) -> str:
        keep = self._suffix_prefix(value)
        if keep:
            self._pending = value[-keep:]
            return value[:-keep]
        self._pending = ""
        return value

    def _feed_normal(self, value: str) -> str:
        visible: list[str] = []
        for index, char in enumerate(value):
            if not self._pending:
                if char == "<":
                    self._pending = char
                else:
                    visible.append(char)
                continue
            candidate = self._pending + char
            if self._is_prefix(candidate):
                self._pending = candidate
                if candidate.casefold() == _OPENER.casefold():
                    self._hidden = candidate
                    self._pending = ""
                    self._quote = None
                    return "".join(visible) + self._feed_hidden(value[index + 1 :])
                continue
            visible.append(self._release_mismatch(candidate))
        return "".join(visible)

    def _feed_hidden(self, value: str) -> str:
        for index, char in enumerate(value):
            self._hidden += char
            if self._quote is not None:
                if char == self._quote:
                    self._quote = None
                continue
            if char in {"\"", "'"}:
                self._quote = char
                continue
            if char == ">" and self._hidden.endswith("/>"):
                self._hidden = ""
                self._quote = None
                self._pending = ""
                return self.feed(value[index + 1 :])
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
            self._hidden = ""
            self._pending = ""
            return ""
        pending = self._pending
        self._pending = ""
        if len(pending) > 1 and self._is_prefix(pending):
            return ""
        return pending

    def close(self) -> str:
        return self.finish()

