"""REST client for the private spicy-monopoly engine.

The engine owns all board truth.  This module deliberately has no Flask or
database dependency so calls can be tested without starting either service.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class EngineError(RuntimeError):
    def __init__(self, code: str, detail: str, *, status: int = 502, payload: Any = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        self.payload = payload


class EngineValidationError(EngineError):
    pass


class EngineUnavailable(EngineError):
    pass


class EngineMutationUncertain(EngineUnavailable):
    """A mutating request may have committed even though its response was lost."""


@dataclass(frozen=True)
class EngineReply:
    payload: dict
    reconciled: bool = False
    outcome_unknown: bool = False


class EngineClient:
    _UNCERTAIN_RESPONSE_CODES = {
        "ENGINE_UNAVAILABLE",
        "ENGINE_HTTP_ERROR",
        "ENGINE_BAD_RESPONSE",
    }

    ACTION_PATHS = {
        "skip": "/skip/{game_id}/{who}",
        "swap": "/swap/{game_id}/{who}",
        "duel_result": "/duel_result/{game_id}/{winner}",
        "pay_toll": "/pay_toll/{game_id}/{who}",
        "serve_toll": "/serve_toll/{game_id}/{who}",
        "buyout": "/buyout/{game_id}/{who}",
        "buy_card": "/buy_card/{game_id}/{who}",
        "use_card": "/use_card/{game_id}/{who}/{index}",
        "discard": "/discard/{game_id}/{who}/{index}",
        "reroll_identity": "/reroll_identity/{game_id}/{who}",
        "reroll_task": "/reroll_task/{game_id}/{who}",
        "id_event": "/id_event/{game_id}/{who}/{event}",
        "extra_task": "/extra_task/{game_id}/{who}",
        "guess_mark": "/guess_mark/{game_id}/{guesser}/{part}",
        "declare_persona": "/declare_persona/{game_id}/{who}",
    }

    def __init__(self, base_url: str = "http://127.0.0.1:8069", *, timeout: float = 15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {"detail": raw.decode("utf-8", errors="replace")}
            detail = payload.get("detail") if isinstance(payload, dict) else str(payload)
            if exc.code in (400, 422, 428):
                raise EngineValidationError(
                    "ENGINE_VALIDATION", str(detail or "引擎拒绝了动作"),
                    status=exc.code, payload=payload,
                ) from exc
            if exc.code == 404:
                raise EngineError("ENGINE_GAME_NOT_FOUND", str(detail or "棋局不存在"), status=404, payload=payload) from exc
            raise EngineError("ENGINE_HTTP_ERROR", str(detail or exc), status=502, payload=payload) from exc
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError) as exc:
            raise EngineUnavailable("ENGINE_UNAVAILABLE", str(exc), status=503) from exc

        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EngineError("ENGINE_BAD_RESPONSE", "引擎返回了非 JSON 响应", status=502) from exc
        if not isinstance(payload, dict):
            raise EngineError("ENGINE_BAD_RESPONSE", "引擎响应必须是对象", status=502, payload=payload)
        return payload

    @staticmethod
    def _q(value: Any) -> str:
        return urllib.parse.quote(str(value), safe="")

    def _state_after_uncertain_mutation(self, game_id: str | None) -> dict:
        if not game_id:
            return {}
        try:
            return self.state(game_id)
        except EngineError:
            return {}

    def _mutation_request(
        self,
        action: str,
        game_id: str | None,
        method: str,
        path: str,
        body: dict | None,
    ) -> dict:
        """Execute a mutation once and fail closed on any unverifiable response."""
        try:
            payload = self._request(method, path, body)
        except EngineError as exc:
            if exc.code not in self._UNCERTAIN_RESPONSE_CODES:
                raise
            raise EngineMutationUncertain(
                "ENGINE_MUTATION_UNCERTAIN",
                f"{action} may have committed before its response became unusable",
                status=503,
                payload={
                    "action": action,
                    "state": self._state_after_uncertain_mutation(game_id),
                    "cause": exc.code,
                },
            ) from exc
        if payload:
            return payload
        raise EngineMutationUncertain(
            "ENGINE_MUTATION_UNCERTAIN",
            f"{action} returned an empty response after a mutating request",
            status=503,
            payload={
                "action": action,
                "state": self._state_after_uncertain_mutation(game_id),
                "cause": "ENGINE_EMPTY_RESPONSE",
            },
        )

    def new_game(self, payload: dict) -> dict:
        return self._mutation_request("new_game", None, "POST", "/new_game", payload)

    def help(self) -> dict:
        return self._request("GET", "/help")

    def state(self, game_id: str) -> dict:
        return self._request("GET", f"/state/{self._q(game_id)}")

    def shop(self, game_id: str) -> dict:
        return self._request("GET", f"/shop/{self._q(game_id)}")

    def final_result(self, game_id: str) -> dict:
        path = f"/final_result/{self._q(game_id)}"
        last_error: EngineError | None = None
        for _attempt in range(2):
            try:
                payload = self._request("GET", path)
            except EngineError as exc:
                if exc.code not in self._UNCERTAIN_RESPONSE_CODES:
                    raise
                last_error = exc
                continue
            if payload:
                return payload
            last_error = EngineError(
                "ENGINE_BAD_RESPONSE",
                "final_result returned an empty response",
                status=502,
            )
        raise EngineMutationUncertain(
            "FINAL_RESULT_UNCERTAIN",
            "final_result remained unavailable after one safe retry",
            status=503,
            payload={
                "action": "final_result",
                "state": self._state_after_uncertain_mutation(game_id),
                "cause": last_error.code if last_error else "ENGINE_BAD_RESPONSE",
            },
        ) from last_error

    def delete_game(self, game_id: str, token: str) -> dict:
        query = urllib.parse.urlencode({"token": token})
        return self._request("DELETE", f"/game/{self._q(game_id)}?{query}")

    def roll(self, game_id: str, body: dict | None = None) -> EngineReply:
        """Roll exactly once; an ambiguous response is never retried.

        ``/state`` has no request id and an acceleration card may leave the
        same player active after a successful roll.  State comparison therefore
        cannot prove that a timed-out roll was not committed.
        """
        path = f"/roll/{self._q(game_id)}"
        try:
            return EngineReply(
                self._mutation_request("roll", game_id, "POST", path, body or {})
            )
        except EngineMutationUncertain as exc:
            after = exc.payload.get("state") if isinstance(exc.payload, dict) else {}
            if not isinstance(after, dict):
                after = {}
            return EngineReply(
                {
                    "state": after,
                    "reconciled_after_timeout": bool(after),
                    "cause": (exc.payload or {}).get("cause") if isinstance(exc.payload, dict) else None,
                },
                reconciled=bool(after),
                outcome_unknown=True,
            )

    def action(self, action: str, game_id: str, **params: Any) -> dict:
        template = self.ACTION_PATHS.get(action)
        if not template:
            raise ValueError(f"unsupported engine action: {action}")
        values = {"game_id": self._q(game_id)}
        values.update({key: self._q(value) for key, value in params.items()})
        try:
            path = template.format(**values)
        except KeyError as exc:
            raise ValueError(f"missing {exc.args[0]} for {action}") from exc
        body = {"persona": params["persona"]} if action == "declare_persona" else {}
        return self._mutation_request(action, game_id, "POST", path, body)

    def skip(self, game_id: str, who: str) -> dict:
        return self.action("skip", game_id, who=who)

    def swap(self, game_id: str, who: str) -> dict:
        return self.action("swap", game_id, who=who)

    def duel_result(self, game_id: str, winner: str) -> dict:
        return self.action("duel_result", game_id, winner=winner)

    def pay_toll(self, game_id: str, who: str) -> dict:
        return self.action("pay_toll", game_id, who=who)

    def serve_toll(self, game_id: str, who: str) -> dict:
        return self.action("serve_toll", game_id, who=who)

    def buyout(self, game_id: str, who: str) -> dict:
        return self.action("buyout", game_id, who=who)

    def buy_card(self, game_id: str, who: str) -> dict:
        return self.action("buy_card", game_id, who=who)

    def use_card(self, game_id: str, who: str, index: int) -> dict:
        return self.action("use_card", game_id, who=who, index=index)

    def discard(self, game_id: str, who: str, index: int) -> dict:
        return self.action("discard", game_id, who=who, index=index)

    def reroll_identity(self, game_id: str, who: str) -> dict:
        return self.action("reroll_identity", game_id, who=who)

    def reroll_task(self, game_id: str, who: str) -> dict:
        return self.action("reroll_task", game_id, who=who)

    def id_event(self, game_id: str, who: str, event: str) -> dict:
        return self.action("id_event", game_id, who=who, event=event)

    def extra_task(self, game_id: str, who: str) -> dict:
        return self.action("extra_task", game_id, who=who)

    def guess_mark(self, game_id: str, guesser: str, part: str) -> dict:
        return self.action("guess_mark", game_id, guesser=guesser, part=part)

    def declare_persona(self, game_id: str, who: str, persona: str) -> dict:
        return self.action("declare_persona", game_id, who=who, persona=persona)

