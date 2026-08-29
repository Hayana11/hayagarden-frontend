"""Transactional, metadata-only authority for external MCP authentication."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from tools.external_secret_store import (
    ACTIVE_STATE,
    REVOKED_SECRET_STATE,
    ExternalSecretStore,
    SecretNotFoundError,
)
from tools.external_server_registry import (
    REVOKED_STATE,
    ExternalServerRegistry,
    UnknownServerError,
)

AUTH_NONE = "none"
AUTH_BEARER = "bearer"
AUTH_SCHEMES = frozenset({AUTH_NONE, AUTH_BEARER})


class ExternalMcpAuthBindingError(Exception):
    code = "AUTH_BINDING_ERROR"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        if code:
            self.code = code


class ExternalMcpAuthBindingInitializationError(ExternalMcpAuthBindingError):
    code = "AUTH_BINDING_INITIALIZATION_ERROR"


@dataclass(frozen=True)
class ExternalMcpAuthBindingRecord:
    server_id: str
    auth_scheme: str
    secret_ref: Optional[str]
    credential_slot: Optional[str]
    revision: int
    created_at: str
    updated_at: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "server_id": self.server_id,
            "auth_scheme": self.auth_scheme,
            "secret_ref": self.secret_ref,
            "credential_slot": self.credential_slot,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _timestamp(value: Optional[datetime]) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


class ExternalMcpAuthBindingRegistry:
    """One binding per server; all trust invalidation is one SQLite transaction."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        server_registry: ExternalServerRegistry,
        secret_store: ExternalSecretStore,
    ) -> None:
        if (
            not isinstance(connection, sqlite3.Connection)
            or not isinstance(server_registry, ExternalServerRegistry)
            or not isinstance(secret_store, ExternalSecretStore)
            or getattr(server_registry, "_connection", None) is not connection
            or getattr(secret_store, "_connection", None) is not connection
            or getattr(secret_store, "_registry", None) is not server_registry
        ):
            raise ExternalMcpAuthBindingInitializationError(
                "auth binding owners must share one connection and registry"
            )
        self._connection = connection
        self._server_registry = server_registry
        self._secret_store = secret_store
        self.initialize()

    def initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS external_mcp_auth_bindings (
                server_id TEXT PRIMARY KEY,
                auth_scheme TEXT NOT NULL CHECK (auth_scheme IN ('none', 'bearer')),
                secret_ref TEXT,
                credential_slot TEXT,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK ((auth_scheme = 'none' AND secret_ref IS NULL AND credential_slot IS NULL)
                    OR (auth_scheme = 'bearer' AND secret_ref IS NOT NULL AND credential_slot IS NOT NULL))
            );
            """
        )
        self._connection.commit()

    def get_binding(self, server_id: str) -> Optional[ExternalMcpAuthBindingRecord]:
        row = self._connection.execute(
            "SELECT server_id, auth_scheme, secret_ref, credential_slot, revision, created_at, updated_at "
            "FROM external_mcp_auth_bindings WHERE server_id=?", (server_id,)
        ).fetchone()
        return self._record(row) if row else None

    get = get_binding

    def set_binding(
        self,
        server_id: object,
        auth_scheme: object,
        *,
        secret_ref: object = None,
        credential_slot: object = None,
        now: Optional[datetime] = None,
    ) -> ExternalMcpAuthBindingRecord:
        if auth_scheme not in AUTH_SCHEMES:
            raise ExternalMcpAuthBindingError("unsupported auth scheme", code="AUTH_SCHEME_UNSUPPORTED")
        if not isinstance(server_id, str) or not server_id:
            raise ExternalMcpAuthBindingError("server identity is invalid", code="UNKNOWN_SERVER")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            try:
                server = self._server_registry.get(server_id)
            except UnknownServerError as exc:
                raise ExternalMcpAuthBindingError("server does not exist", code="UNKNOWN_SERVER") from exc
            if server.lifecycle_state == REVOKED_STATE:
                raise ExternalMcpAuthBindingError("server is revoked", code="SERVER_REVOKED")
            selected_ref: Optional[str] = None
            selected_slot: Optional[str] = None
            if auth_scheme == AUTH_NONE:
                if secret_ref is not None or credential_slot is not None:
                    raise ExternalMcpAuthBindingError("none binding cannot carry secret metadata", code="AUTH_METADATA_FORBIDDEN")
            else:
                if not isinstance(secret_ref, str) or not secret_ref:
                    raise ExternalMcpAuthBindingError("bearer binding requires secret reference", code="AUTH_SECRET_REQUIRED")
                try:
                    metadata = self._secret_store.get_metadata(secret_ref)
                except SecretNotFoundError as exc:
                    raise ExternalMcpAuthBindingError("secret reference does not exist", code="AUTH_SECRET_UNKNOWN") from exc
                if metadata.lifecycle_state == REVOKED_SECRET_STATE:
                    raise ExternalMcpAuthBindingError("secret is revoked", code="AUTH_SECRET_REVOKED")
                if metadata.lifecycle_state != ACTIVE_STATE:
                    raise ExternalMcpAuthBindingError("secret is not active", code="AUTH_SECRET_INACTIVE")
                if metadata.server_id != server_id:
                    raise ExternalMcpAuthBindingError("secret belongs to another server", code="AUTH_SERVER_MISMATCH")
                selected_ref = metadata.secret_ref
                selected_slot = metadata.credential_slot
                if credential_slot is not None and credential_slot != selected_slot:
                    raise ExternalMcpAuthBindingError("credential slot does not match secret metadata", code="AUTH_SLOT_MISMATCH")
            old = self.get_binding(server_id)
            if old and (old.auth_scheme, old.secret_ref, old.credential_slot) == (auth_scheme, selected_ref, selected_slot):
                self._connection.commit()
                return old
            revision = old.revision + 1 if old else 1
            timestamp = _timestamp(now)
            self._connection.execute(
                "INSERT INTO external_mcp_auth_bindings (server_id, auth_scheme, secret_ref, credential_slot, revision, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(server_id) DO UPDATE SET auth_scheme=excluded.auth_scheme, secret_ref=excluded.secret_ref, credential_slot=excluded.credential_slot, revision=excluded.revision, updated_at=excluded.updated_at",
                (server_id, auth_scheme, selected_ref, selected_slot, revision, old.created_at if old else timestamp, timestamp),
            )
            self._server_registry.mark_auth_binding_changed_in_transaction(server_id, now=now)
            self._connection.commit()
            result = self.get_binding(server_id)
            assert result is not None
            return result
        except Exception:
            self._connection.rollback()
            raise

    bind = set_binding

    @staticmethod
    def _record(row) -> ExternalMcpAuthBindingRecord:
        return ExternalMcpAuthBindingRecord(*tuple(row))


__all__ = [
    "AUTH_NONE", "AUTH_BEARER", "AUTH_SCHEMES", "ExternalMcpAuthBindingError",
    "ExternalMcpAuthBindingInitializationError", "ExternalMcpAuthBindingRecord",
    "ExternalMcpAuthBindingRegistry",
]
