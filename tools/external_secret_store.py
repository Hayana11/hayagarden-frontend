"""Administrative external-secret storage for the M5-02 boundary.

The store owns only external secret records.  It accepts an injected SQLite
connection and an explicit key file; it has no production database path,
environment fallback, network, runtime, or plaintext-returning API.
"""

from __future__ import annotations

import os
import importlib.util
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

from tools.external_server_registry import (
    ExternalServerRegistry,
    REVOKED_STATE,
    UnknownServerError,
)


def _load_credential_vault_primitive():
    """Load relay's Fernet primitive without importing relay package state."""
    path = Path(__file__).resolve().parents[1] / "relay" / "credential_vault.py"
    spec = importlib.util.spec_from_file_location(
        "_hayagarden_external_credential_vault", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("credential vault primitive is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CREDENTIAL_VAULT = _load_credential_vault_primitive()
CredentialVaultError = _CREDENTIAL_VAULT.CredentialVaultError
encrypt_secret = _CREDENTIAL_VAULT.encrypt_secret


ACTIVE_STATE = "ACTIVE"
REVOKED_SECRET_STATE = "REVOKED"
MAX_SECRET_BYTES = 4096
MAX_SLOT_LENGTH = 32
MAX_SECRET_REF_LENGTH = 200
_SLOT_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,31}$")
_REF_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_SECRET_LITERAL_PATTERN = re.compile(
    r"(?:api[_-]?key|authorization|bearer|cookie|credential|password|"
    r"secret(?:[_-]?ref)?|token|(?:^|[^a-z])key(?:$|[^a-z]))",
    re.IGNORECASE,
)


class SecretStoreError(Exception):
    code = "SECRET_STORE_ERROR"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class SecretStoreValidationError(SecretStoreError):
    code = "INVALID_SECRET_INPUT"


class ActiveSlotConflictError(SecretStoreError):
    code = "ACTIVE_SLOT_CONFLICT"


class SecretNotFoundError(SecretStoreError):
    code = "SECRET_NOT_FOUND"


@dataclass(frozen=True)
class ExternalSecretRecord:
    """Metadata only; ciphertext is deliberately absent from this type."""

    secret_ref: str
    server_id: str
    credential_slot: str
    lifecycle_state: str
    configured: bool
    created_at: str
    updated_at: str
    revision: int

    def to_public_dict(self) -> dict[str, object]:
        return {
            "secret_ref": self.secret_ref,
            "server_id": self.server_id,
            "credential_slot": self.credential_slot,
            "lifecycle_state": self.lifecycle_state,
            "configured": self.configured,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class _RuntimeSecretRecord:
    """Private transport seam; never expose this through public metadata APIs."""

    secret_ref: str
    server_id: str
    credential_slot: str
    lifecycle: str
    revision: int
    ciphertext: Optional[str] = field(repr=False)


def _timestamp(value: Optional[datetime]) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _reject_secret_literal(value: str, field: str) -> None:
    if _SECRET_LITERAL_PATTERN.search(value):
        raise SecretStoreValidationError(
            f"{field} contains a forbidden secret marker",
            code=f"{field.upper()}_SECRET_LITERAL_FORBIDDEN",
        )


def _slot(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_SLOT_LENGTH:
        raise SecretStoreValidationError("credential slot is invalid", code="INVALID_SLOT")
    if not _SLOT_PATTERN.fullmatch(value) or _SECRET_LITERAL_PATTERN.search(value):
        raise SecretStoreValidationError("credential slot is invalid", code="INVALID_SLOT")
    return value


def _secret(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise SecretStoreValidationError("secret value is invalid", code="INVALID_SECRET")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SecretStoreValidationError(
            "secret value is invalid", code="INVALID_SECRET"
        ) from exc
    if len(encoded) > MAX_SECRET_BYTES or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise SecretStoreValidationError("secret value is invalid", code="INVALID_SECRET")
    return value


def _secret_ref(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SECRET_REF_LENGTH
        or not _REF_PATTERN.fullmatch(value)
    ):
        raise SecretStoreValidationError("secret reference is invalid", code="INVALID_SECRET_REF")
    return value


class ExternalSecretStore:
    """External secret store whose server owner is the M5-01 registry."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        key_file: object,
        registry: ExternalServerRegistry,
        id_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be an sqlite3.Connection")
        if not isinstance(registry, ExternalServerRegistry):
            raise TypeError("registry must be an ExternalServerRegistry")
        if getattr(registry, "_connection", None) is not connection:
            raise ValueError("registry and secret store must share one connection")
        try:
            key_path = os.fspath(key_file)
        except TypeError as exc:
            raise ValueError("an explicit external key file is required") from exc
        if not key_path:
            raise ValueError("an explicit external key file is required")
        self._connection = connection
        self._key_file = key_path
        self._registry = registry
        self._id_factory = id_factory or (
            lambda: "extsecret_" + secrets.token_urlsafe(24)
        )
        self.initialize()

    def initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS external_secret_records (
                secret_ref TEXT PRIMARY KEY,
                server_id TEXT NOT NULL,
                credential_slot TEXT NOT NULL,
                ciphertext TEXT,
                lifecycle_state TEXT NOT NULL CHECK (
                    lifecycle_state IN ('ACTIVE', 'REVOKED')
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_external_secret_active_slot
                ON external_secret_records(server_id, credential_slot)
                WHERE lifecycle_state = 'ACTIVE';
            """
        )
        self._connection.commit()

    @staticmethod
    def _reject_unsupported(fields: Mapping[str, object]) -> None:
        if not fields:
            return
        if "secret_ref" in fields:
            raise SecretStoreValidationError(
                "secret reference is backend-generated",
                code="CALLER_SECRET_REF_FORBIDDEN",
            )
        raise SecretStoreValidationError(
            "unsupported secret-store field", code="UNSUPPORTED_FIELD"
        )

    def _require_live_server(self, server_id: object) -> str:
        if not isinstance(server_id, str) or not server_id:
            raise SecretStoreValidationError("server ID is invalid", code="INVALID_SERVER_ID")
        try:
            server = self._registry.get(server_id)
        except UnknownServerError as exc:
            raise SecretStoreError("server is not registered", code="UNKNOWN_SERVER") from exc
        if server.lifecycle_state == REVOKED_STATE:
            raise SecretStoreError("server is revoked", code="SERVER_REVOKED")
        return server.server_id

    def _generated_ref(self) -> str:
        try:
            value = self._id_factory()
        except Exception as exc:
            raise SecretStoreError("secret reference generation failed", code="ID_GENERATION_FAILED") from exc
        return _secret_ref(value)

    @staticmethod
    def _begin_write(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _record(row: tuple[object, ...]) -> ExternalSecretRecord:
        return ExternalSecretRecord(
            secret_ref=str(row[0]),
            server_id=str(row[1]),
            credential_slot=str(row[2]),
            lifecycle_state=str(row[4]),
            configured=row[4] == ACTIVE_STATE,
            created_at=str(row[5]),
            updated_at=str(row[6]),
            revision=int(row[7]),
        )

    def _select(self, secret_ref: str) -> Optional[tuple[object, ...]]:
        return self._connection.execute(
            "SELECT secret_ref, server_id, credential_slot, ciphertext, "
            "lifecycle_state, created_at, updated_at, revision "
            "FROM external_secret_records WHERE secret_ref = ?",
            (secret_ref,),
        ).fetchone()

    def _load_runtime_record(self, secret_ref: object) -> _RuntimeSecretRecord:
        """Load one exact encrypted record for the authenticated runtime seam."""
        ref = _secret_ref(secret_ref)
        row = self._select(ref)
        if row is None:
            raise SecretNotFoundError("secret reference does not exist")
        return _RuntimeSecretRecord(
            secret_ref=str(row[0]),
            server_id=str(row[1]),
            credential_slot=str(row[2]),
            lifecycle=str(row[4]),
            revision=int(row[7]),
            ciphertext=None if row[3] is None else str(row[3]),
        )

    def create(
        self,
        *,
        server_id: object,
        credential_slot: object = None,
        secret: object = None,
        slot: object = None,
        now: Optional[datetime] = None,
        **unsupported_fields: object,
    ) -> ExternalSecretRecord:
        self._reject_unsupported(unsupported_fields)
        if credential_slot is not None and slot is not None:
            raise SecretStoreValidationError("credential slot is duplicated", code="INVALID_SLOT")
        owner = self._require_live_server(server_id)
        selected_slot = _slot(credential_slot if credential_slot is not None else slot)
        raw_secret = _secret(secret)
        timestamp = _timestamp(now)
        self._begin_write(self._connection)
        try:
            active = self._connection.execute(
                "SELECT 1 FROM external_secret_records WHERE server_id = ? "
                "AND credential_slot = ? AND lifecycle_state = 'ACTIVE'",
                (owner, selected_slot),
            ).fetchone()
            if active is not None:
                raise ActiveSlotConflictError("credential slot already has an active secret")
            try:
                ciphertext = encrypt_secret(raw_secret, key_file=self._key_file)
            except (CredentialVaultError, OSError, ValueError) as exc:
                raise SecretStoreError(
                    "external secret encryption failed", code="ENCRYPTION_FAILED"
                ) from exc
            for _ in range(8):
                ref = self._generated_ref()
                try:
                    self._connection.execute(
                        "INSERT INTO external_secret_records ("
                        "secret_ref, server_id, credential_slot, ciphertext, "
                        "lifecycle_state, created_at, updated_at, revision) "
                        "VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, 1)",
                        (ref, owner, selected_slot, ciphertext, timestamp, timestamp),
                    )
                    row = self._select(ref)
                    self._connection.commit()
                    assert row is not None
                    return self._record(row)
                except sqlite3.IntegrityError as exc:
                    if "external_secret_records.secret_ref" not in str(exc):
                        raise ActiveSlotConflictError(
                            "credential slot already has an active secret"
                        ) from exc
            raise SecretStoreError(
                "secret reference collision budget exhausted", code="ID_COLLISION"
            )
        except Exception:
            self._connection.rollback()
            raise

    create_secret = create

    def create_record(self, record: Mapping[str, object]) -> ExternalSecretRecord:
        if not isinstance(record, Mapping):
            raise SecretStoreValidationError("secret record must be a mapping")
        allowed = {"server_id", "credential_slot", "slot", "secret"}
        self._reject_unsupported({key: value for key, value in record.items() if key not in allowed})
        return self.create(
            server_id=record.get("server_id"),
            credential_slot=record.get("credential_slot"),
            slot=record.get("slot"),
            secret=record.get("secret"),
        )

    def rotate(
        self,
        *,
        server_id: object = None,
        credential_slot: object = None,
        secret: object = None,
        slot: object = None,
        secret_ref: object = None,
        now: Optional[datetime] = None,
        **unsupported_fields: object,
    ) -> ExternalSecretRecord:
        self._reject_unsupported(unsupported_fields)
        if secret_ref is not None and any(
            value is not None for value in (server_id, credential_slot, slot)
        ):
            raise SecretStoreValidationError(
                "rotation reference is ambiguous", code="INVALID_ROTATION_TARGET"
            )
        if secret_ref is None and credential_slot is not None and slot is not None:
            raise SecretStoreValidationError("credential slot is duplicated", code="INVALID_SLOT")
        raw_secret = _secret(secret)
        timestamp = _timestamp(now)
        self._begin_write(self._connection)
        try:
            if secret_ref is not None:
                ref = _secret_ref(secret_ref)
                row = self._connection.execute(
                    "SELECT secret_ref, server_id, credential_slot, lifecycle_state "
                    "FROM external_secret_records WHERE secret_ref = ?",
                    (ref,),
                ).fetchone()
                if row is not None:
                    owner = self._require_live_server(row[1])
                    selected_slot = str(row[2])
            else:
                owner = self._require_live_server(server_id)
                selected_slot = _slot(credential_slot if credential_slot is not None else slot)
                row = self._connection.execute(
                    "SELECT secret_ref, server_id, credential_slot, lifecycle_state "
                    "FROM external_secret_records WHERE server_id = ? "
                    "AND credential_slot = ? AND lifecycle_state = 'ACTIVE'",
                    (owner, selected_slot),
                ).fetchone()
            if row is None:
                raise SecretNotFoundError("active secret does not exist")
            if row[3] != ACTIVE_STATE:
                raise SecretNotFoundError("active secret does not exist")
            try:
                ciphertext = encrypt_secret(raw_secret, key_file=self._key_file)
            except (CredentialVaultError, OSError, ValueError) as exc:
                raise SecretStoreError(
                    "external secret encryption failed", code="ENCRYPTION_FAILED"
                ) from exc
            self._connection.execute(
                "UPDATE external_secret_records SET ciphertext = ?, updated_at = ?, "
                "revision = revision + 1 WHERE server_id = ? AND credential_slot = ? "
                "AND lifecycle_state = 'ACTIVE'",
                (ciphertext, timestamp, owner, selected_slot),
            )
            updated = self._select(str(row[0]))
            self._connection.commit()
            assert updated is not None
            return self._record(updated)
        except Exception:
            self._connection.rollback()
            raise

    rotate_secret = rotate

    def revoke(self, secret_ref: object, *, now: Optional[datetime] = None) -> ExternalSecretRecord:
        ref = _secret_ref(secret_ref)
        self._begin_write(self._connection)
        try:
            row = self._select(ref)
            if row is None:
                raise SecretNotFoundError("secret reference does not exist")
            if row[4] == REVOKED_SECRET_STATE:
                self._connection.commit()
                return self._record(row)
            self._connection.execute(
                "UPDATE external_secret_records SET ciphertext = NULL, "
                "lifecycle_state = 'REVOKED', updated_at = ?, revision = revision + 1 "
                "WHERE secret_ref = ?",
                (_timestamp(now), ref),
            )
            revoked = self._select(ref)
            self._connection.commit()
            assert revoked is not None
            return self._record(revoked)
        except Exception:
            self._connection.rollback()
            raise

    revoke_secret = revoke

    def get_metadata(self, secret_ref: object) -> ExternalSecretRecord:
        ref = _secret_ref(secret_ref)
        row = self._select(ref)
        if row is None:
            raise SecretNotFoundError("secret reference does not exist")
        return self._record(row)

    get = get_metadata

    def list_metadata(
        self, server_id: object, credential_slot: object = None
    ) -> tuple[ExternalSecretRecord, ...]:
        if not isinstance(server_id, str) or not server_id:
            raise SecretStoreValidationError("server ID is invalid", code="INVALID_SERVER_ID")
        self._registry.get(server_id)
        query = (
            "SELECT secret_ref, server_id, credential_slot, ciphertext, "
            "lifecycle_state, created_at, updated_at, revision "
            "FROM external_secret_records WHERE server_id = ?"
        )
        params: list[object] = [server_id]
        if credential_slot is not None:
            query += " AND credential_slot = ?"
            params.append(_slot(credential_slot))
        query += " ORDER BY credential_slot, created_at, secret_ref"
        return tuple(self._record(row) for row in self._connection.execute(query, params).fetchall())

    list = list_metadata
