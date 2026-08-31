"""SQLite owner for external MCP server connection state."""

from __future__ import annotations

import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

TRANSPORT_STREAMABLE_HTTP = "streamable_http"
CONNECTED_STATE = "CONNECTED"
DISCONNECTED_STATE = "DISCONNECTED"
REVOKED_STATE = "REVOKED"
LIFECYCLE_STATES = frozenset({CONNECTED_STATE, DISCONNECTED_STATE, REVOKED_STATE})
_SECRET_LIKE_FIELDS = frozenset({"token", "api_key", "key", "secret", "authorization", "cookie", "headers", "credential", "password", "bearer", "secret_ref"})
_SECRET_LITERAL_PATTERN = re.compile(r"(?:api[_-]?key|authorization|bearer|cookie|credential|password|secret(?:[_-]?ref)?|token|(?:^|[^a-z])key(?:$|[^a-z]))", re.I)
_REGISTRATION_FIELDS = frozenset({"display_name", "transport", "endpoint", "provenance"})


class RegistryError(Exception):
    code = "REGISTRY_ERROR"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        if code is not None: self.code = code


class RegistryValidationError(RegistryError): code = "INVALID_REGISTRATION"
class DuplicateEndpointError(RegistryError): code = "DUPLICATE_ACTIVE_ENDPOINT"
class UnknownServerError(RegistryError): code = "UNKNOWN_SERVER"
class InvalidStateTransitionError(RegistryError): code = "INVALID_STATE_TRANSITION"


@dataclass(frozen=True)
class ExternalServerRecord:
    server_id: str
    display_name: str
    transport: str
    endpoint: str
    lifecycle_state: str
    registration_provenance: str
    created_at: str
    updated_at: str
    revision: int

    def to_public_dict(self) -> dict[str, object]:
        return {"server_id": self.server_id, "display_name": self.display_name, "transport": self.transport, "endpoint": self.endpoint, "lifecycle_state": self.lifecycle_state, "registration_provenance": self.registration_provenance, "created_at": self.created_at, "updated_at": self.updated_at, "revision": self.revision}


def _timestamp(value: Optional[datetime] = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None: current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str): raise RegistryValidationError(f"{field} must be text", code=f"INVALID_{field.upper()}")
    value = value.strip()
    if not value or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value): raise RegistryValidationError(f"{field} is invalid", code=f"INVALID_{field.upper()}")
    return value


def _reject_secret_literal(value: str, field: str) -> None:
    if _SECRET_LITERAL_PATTERN.search(value): raise RegistryValidationError(f"{field} contains secret-like text", code="SECRET_LITERAL_FORBIDDEN")


def _provenance(value: object) -> str:
    value = _text(value, "provenance", 200).lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value): raise RegistryValidationError("provenance is invalid", code="INVALID_PROVENANCE")
    _reject_secret_literal(value, "provenance")
    return value


def _transport(value: object) -> str:
    value = _text(value, "transport", 64).lower()
    if value != TRANSPORT_STREAMABLE_HTTP: raise RegistryValidationError("unsupported transport", code="UNSUPPORTED_TRANSPORT")
    return value


def normalize_external_endpoint(value: object) -> str:
    endpoint = _text(value, "endpoint", 2048)
    if any(c.isspace() for c in endpoint): raise RegistryValidationError("endpoint must not contain whitespace", code="INVALID_ENDPOINT")
    try:
        parsed = urlsplit(endpoint); port = parsed.port
    except ValueError as exc: raise RegistryValidationError("endpoint is not a valid URL", code="INVALID_ENDPOINT") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname: raise RegistryValidationError("external endpoints must use HTTPS", code="HTTPS_REQUIRED")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc: raise RegistryValidationError("endpoint userinfo is not accepted", code="ENDPOINT_USERINFO_FORBIDDEN")
    if parsed.query or parsed.fragment or any(c in parsed.path for c in ("%", "=", ";", "&", "\\", ":")): raise RegistryValidationError("endpoint contains unsupported syntax", code="INVALID_ENDPOINT")
    _reject_secret_literal(parsed.path, "endpoint")
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["): host = f"[{host}]"
    if port is not None and port != 443: host = f"{host}:{port}"
    return urlunsplit(("https", host, parsed.path or "", "", ""))


class ExternalServerRegistry:
    def __init__(self, connection: sqlite3.Connection, *, id_factory: Optional[Callable[[], str]] = None) -> None:
        if not isinstance(connection, sqlite3.Connection): raise TypeError("connection must be an sqlite3.Connection")
        self._connection = connection; self._id_factory = id_factory or (lambda: secrets.token_hex(16)); self.initialize()

    def initialize(self) -> None:
        tables = {r[0] for r in self._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "external_server_registry" not in tables: self._create_schema()
        else:
            columns = {r[1] for r in self._connection.execute("PRAGMA table_info(external_server_registry)")}
            if "master_" + "state" in columns: self._migrate_legacy_schema()
        self._connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_external_server_active_endpoint ON external_server_registry(endpoint) WHERE lifecycle_state <> 'REVOKED'")
        self._connection.commit()

    def _create_schema(self) -> None:
        self._connection.executescript("""
        CREATE TABLE IF NOT EXISTS external_server_registry (
            server_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, transport TEXT NOT NULL,
            endpoint TEXT NOT NULL, lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('CONNECTED','DISCONNECTED','REVOKED')),
            registration_provenance TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1)
        );
        """)

    def _migrate_legacy_schema(self) -> None:
        self._connection.execute("ALTER TABLE external_server_registry RENAME TO external_server_registry_legacy")
        self._create_schema()
        rows = self._connection.execute("SELECT server_id, display_name, transport, endpoint, lifecycle_state, registration_provenance, created_at, updated_at, revision FROM external_server_registry_legacy").fetchall()
        for row in rows:
            state = REVOKED_STATE if row[4] == REVOKED_STATE else DISCONNECTED_STATE
            self._connection.execute("INSERT INTO external_server_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (*row[:4], state, *row[5:]))
        self._connection.execute("DROP TABLE external_server_registry_legacy")

    def register(self, *, display_name: object, endpoint: object, provenance: object, transport: object = TRANSPORT_STREAMABLE_HTTP, now: Optional[datetime] = None, **unsupported_fields: object) -> ExternalServerRecord:
        self._reject_unsupported_fields(unsupported_fields)
        name, endpoint, transport, provenance = _text(display_name, "display_name", 200), normalize_external_endpoint(endpoint), _transport(transport), _provenance(provenance)
        timestamp = _timestamp(now); self._begin_write()
        try:
            for _ in range(8):
                server_id = self._id_factory()
                if not isinstance(server_id, str) or not server_id or ":" in server_id: raise RegistryValidationError("unsafe generated ID", code="UNSAFE_SERVER_ID")
                try:
                    self._connection.execute("INSERT INTO external_server_registry (server_id, display_name, transport, endpoint, lifecycle_state, registration_provenance, created_at, updated_at, revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)", (server_id, name, transport, endpoint, DISCONNECTED_STATE, provenance, timestamp, timestamp))
                    record = self._record(self._select_row(server_id)); self._connection.commit(); return record
                except sqlite3.IntegrityError as exc:
                    if "endpoint" in str(exc): raise DuplicateEndpointError("an active server already owns this endpoint") from exc
                    if "server_id" not in str(exc): raise
            raise RegistryError("server ID collision budget exhausted", code="ID_COLLISION")
        except Exception: self._connection.rollback(); raise

    register_server = register

    def register_record(self, record: Mapping[str, object]) -> ExternalServerRecord:
        if not isinstance(record, Mapping): raise RegistryValidationError("registration must be a mapping")
        self._reject_unknown_mapping_fields(record, _REGISTRATION_FIELDS)
        return self.register(display_name=record.get("display_name"), endpoint=record.get("endpoint"), provenance=record.get("provenance"), transport=record.get("transport", TRANSPORT_STREAMABLE_HTTP))

    def get(self, server_id: object) -> ExternalServerRecord:
        server_id = self._require_id(server_id); record = self._record(self._select_row(server_id), missing_ok=True)
        if record is None: raise UnknownServerError("server does not exist")
        return record

    get_server = get

    def list(self) -> tuple[ExternalServerRecord, ...]:
        return tuple(self._record(row) for row in self._connection.execute("SELECT server_id, display_name, transport, endpoint, lifecycle_state, registration_provenance, created_at, updated_at, revision FROM external_server_registry ORDER BY created_at, server_id"))

    def rename(self, server_id: object, display_name: object, *, now: Optional[datetime] = None) -> ExternalServerRecord:
        current = self._editable(server_id); name = _text(display_name, "display_name", 200)
        if name == current.display_name: return current
        self._begin_write()
        try:
            self._connection.execute("UPDATE external_server_registry SET display_name=?, updated_at=?, revision=revision+1 WHERE server_id=?", (name, _timestamp(now), current.server_id)); record = self._record(self._select_row(current.server_id)); self._connection.commit(); return record
        except Exception: self._connection.rollback(); raise

    rename_server = rename

    def update_connection(self, server_id: object, *, endpoint: Optional[object] = None, transport: Optional[object] = None, now: Optional[datetime] = None) -> ExternalServerRecord:
        current = self._editable(server_id)
        if endpoint is None and transport is None: raise RegistryValidationError("endpoint or transport is required", code="NO_CONNECTION_CHANGE")
        endpoint = normalize_external_endpoint(endpoint) if endpoint is not None else current.endpoint; transport = _transport(transport) if transport is not None else current.transport
        if endpoint == current.endpoint and transport == current.transport: return current
        self._begin_write()
        try:
            self._connection.execute("UPDATE external_server_registry SET endpoint=?, transport=?, lifecycle_state=?, updated_at=?, revision=revision+1 WHERE server_id=?", (endpoint, transport, DISCONNECTED_STATE, _timestamp(now), current.server_id)); record = self._record(self._select_row(current.server_id)); self._connection.commit(); return record
        except sqlite3.IntegrityError as exc:
            self._connection.rollback()
            if "endpoint" in str(exc): raise DuplicateEndpointError("an active server already owns this endpoint") from exc
            raise
        except Exception: self._connection.rollback(); raise

    def update_transport(self, server_id: object, transport: object, *, now: Optional[datetime] = None) -> ExternalServerRecord:
        return self.update_connection(server_id, transport=transport, now=now)

    def revoke(self, server_id: object, *, now: Optional[datetime] = None) -> ExternalServerRecord:
        current = self.get(server_id)
        if current.lifecycle_state == REVOKED_STATE: return current
        self._begin_write()
        try:
            self._connection.execute("UPDATE external_server_registry SET lifecycle_state=?, updated_at=?, revision=revision+1 WHERE server_id=?", (REVOKED_STATE, _timestamp(now), current.server_id)); record = self._record(self._select_row(current.server_id)); self._connection.commit(); return record
        except Exception: self._connection.rollback(); raise

    revoke_server = revoke

    def mark_connected(self, server_id: object, expected_revision: object, *, now: Optional[datetime] = None) -> ExternalServerRecord:
        return self._mark_state(server_id, expected_revision, CONNECTED_STATE, now=now)

    def mark_disconnected(self, server_id: object, expected_revision: object, *, now: Optional[datetime] = None) -> ExternalServerRecord:
        return self._mark_state(server_id, expected_revision, DISCONNECTED_STATE, now=now)

    def _mark_state(self, server_id: object, expected_revision: object, state: str, *, now: Optional[datetime]) -> ExternalServerRecord:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1: raise InvalidStateTransitionError("expected revision is invalid", code="REVISION_MISMATCH")
        current = self.get(server_id)
        if current.revision != expected_revision: raise InvalidStateTransitionError("server revision changed", code="REVISION_MISMATCH")
        if current.lifecycle_state == REVOKED_STATE: raise InvalidStateTransitionError("revoked server cannot change state", code="SERVER_REVOKED")
        self._begin_write()
        try:
            result = self._connection.execute("UPDATE external_server_registry SET lifecycle_state=?, updated_at=? WHERE server_id=? AND revision=? AND lifecycle_state <> 'REVOKED'", (state, _timestamp(now), current.server_id, expected_revision))
            if result.rowcount != 1: raise InvalidStateTransitionError("server revision changed", code="REVISION_MISMATCH")
            record = self._record(self._select_row(current.server_id)); self._connection.commit(); return record
        except Exception: self._connection.rollback(); raise

    def mark_auth_binding_changed_in_transaction(self, server_id: object, *, now: Optional[datetime] = None) -> ExternalServerRecord:
        if not self._connection.in_transaction: raise InvalidStateTransitionError("auth binding change requires an active transaction", code="AUTH_BINDING_TRANSACTION_REQUIRED")
        current = self.get(server_id)
        if current.lifecycle_state == REVOKED_STATE: raise InvalidStateTransitionError("revoked server cannot change auth", code="SERVER_REVOKED")
        self._connection.execute("UPDATE external_server_registry SET lifecycle_state=?, updated_at=?, revision=revision+1 WHERE server_id=?", (DISCONNECTED_STATE, _timestamp(now), current.server_id))
        return self._record(self._select_row(current.server_id))

    def _editable(self, server_id: object) -> ExternalServerRecord:
        record = self.get(server_id)
        if record.lifecycle_state == REVOKED_STATE: raise InvalidStateTransitionError("revoked servers cannot be changed", code="SERVER_REVOKED")
        return record

    def _begin_write(self) -> None: self._connection.execute("BEGIN IMMEDIATE")
    def _select_row(self, server_id: str): return self._connection.execute("SELECT server_id, display_name, transport, endpoint, lifecycle_state, registration_provenance, created_at, updated_at, revision FROM external_server_registry WHERE server_id=?", (server_id,)).fetchone()

    @staticmethod
    def _record(row, missing_ok: bool = False) -> Optional[ExternalServerRecord]:
        if row is None:
            if missing_ok: return None
            raise UnknownServerError("server does not exist")
        return ExternalServerRecord(*tuple(row))

    @staticmethod
    def _require_id(value: object) -> str:
        if not isinstance(value, str) or not value or ":" in value: raise UnknownServerError("server identity is invalid")
        return value

    @staticmethod
    def _reject_unknown_mapping_fields(record: Mapping[str, object], allowed: Iterable[str]) -> None:
        unknown = set(record) - set(allowed)
        if unknown: ExternalServerRegistry._reject_unsupported_fields({str(k): record[k] for k in unknown})

    @staticmethod
    def _reject_unsupported_fields(fields: Mapping[str, object]) -> None:
        if not fields: return
        names = {str(k) for k in fields}; code = "SECRET_FIELD_FORBIDDEN" if names & _SECRET_LIKE_FIELDS else "UNSUPPORTED_FIELD"
        raise RegistryValidationError("registration contains unsupported fields", code=code)


__all__ = ["CONNECTED_STATE", "DISCONNECTED_STATE", "DuplicateEndpointError", "ExternalServerRecord", "ExternalServerRegistry", "InvalidStateTransitionError", "LIFECYCLE_STATES", "REVOKED_STATE", "RegistryError", "RegistryValidationError", "TRANSPORT_STREAMABLE_HTTP", "UnknownServerError", "normalize_external_endpoint"]
