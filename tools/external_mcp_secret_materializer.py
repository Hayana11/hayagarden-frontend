"""Materialize one frozen external MCP auth binding for a transport call.

This module is intentionally a narrow runtime primitive.  It never chooses an
auth binding, looks up a slot, or exposes a plaintext accessor on the store.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, Optional

from .external_secret_store import (
    ACTIVE_STATE,
    CredentialVaultError,
    ExternalSecretStore,
    SecretStoreError,
    decrypt_secret,
)


MAX_MATERIALIZED_CREDENTIAL_BYTES = 16 * 1024
NONE = "none"
BEARER = "bearer"
SUPPORTED_AUTH_SCHEMES = frozenset({NONE, BEARER})
AUTH_BINDING_INVALID = "AUTH_BINDING_INVALID"
AUTH_SCHEME_UNSUPPORTED = "AUTH_SCHEME_UNSUPPORTED"
AUTH_SECRET_UNAVAILABLE = "AUTH_SECRET_UNAVAILABLE"
_AUTH_FIELDS = frozenset({"server_id", "auth_scheme", "secret_ref", "credential_slot"})


class ExternalMcpSecretMaterializerError(Exception):
    """Stable, secret-free materialization failure."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MaterializedExternalMcpAuth:
    scheme: str
    credential: Optional[str] = field(default=None, repr=False)


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _auth_fields(
    auth: Optional[Mapping[str, Any]],
    *,
    server_id: Any,
    auth_scheme: Any,
    secret_ref: Any,
    credential_slot: Any,
) -> dict[str, Any]:
    if auth is not None:
        if not isinstance(auth, Mapping) or set(auth) != _AUTH_FIELDS:
            raise ExternalMcpSecretMaterializerError(
                "frozen auth binding fields are invalid", code=AUTH_BINDING_INVALID
            )
        if any(value is not None for value in (server_id, auth_scheme, secret_ref, credential_slot)):
            raise ExternalMcpSecretMaterializerError(
                "auth binding was supplied twice", code=AUTH_BINDING_INVALID
            )
        return dict(auth)
    if auth_scheme is None:
        raise ExternalMcpSecretMaterializerError(
            "auth_scheme is required", code=AUTH_BINDING_INVALID
        )
    return {
        "server_id": server_id,
        "auth_scheme": auth_scheme,
        "secret_ref": secret_ref,
        "credential_slot": credential_slot,
    }


class ExternalMcpSecretMaterializer:
    """Owner of transient bearer credential materialization."""

    def __init__(self, secret_store: ExternalSecretStore, *, key_file: object):
        if not isinstance(secret_store, ExternalSecretStore):
            raise TypeError("secret_store must be an ExternalSecretStore")
        try:
            key_path = os.fspath(key_file)
        except TypeError as exc:
            raise ValueError("an explicit external MCP key file is required") from exc
        if not key_path:
            raise ValueError("an explicit external MCP key file is required")
        self._secret_store = secret_store
        self._key_file = key_path

    def materialize(
        self,
        auth: Optional[Mapping[str, Any]] = None,
        *,
        server_id: Any = None,
        auth_scheme: Any = None,
        secret_ref: Any = None,
        credential_slot: Any = None,
    ) -> MaterializedExternalMcpAuth:
        fields = _auth_fields(
            auth,
            server_id=server_id,
            auth_scheme=auth_scheme,
            secret_ref=secret_ref,
            credential_slot=credential_slot,
        )
        scheme = fields["auth_scheme"]
        if scheme not in SUPPORTED_AUTH_SCHEMES:
            raise ExternalMcpSecretMaterializerError(
                "auth scheme is unsupported", code=AUTH_SCHEME_UNSUPPORTED
            )
        if scheme == NONE:
            if fields["secret_ref"] is not None or fields["credential_slot"] is not None:
                raise ExternalMcpSecretMaterializerError(
                    "none auth cannot carry secret fields", code=AUTH_BINDING_INVALID
                )
            return MaterializedExternalMcpAuth(scheme=NONE)

        if not all(
            _nonempty_text(fields[name])
            for name in ("server_id", "secret_ref", "credential_slot")
        ):
            raise ExternalMcpSecretMaterializerError(
                "bearer auth binding is incomplete", code=AUTH_BINDING_INVALID
            )

        try:
            runtime_record = self._secret_store._load_runtime_record(fields["secret_ref"])
            if (
                runtime_record.lifecycle != ACTIVE_STATE
                or runtime_record.secret_ref != fields["secret_ref"]
                or runtime_record.server_id != fields["server_id"]
                or runtime_record.credential_slot != fields["credential_slot"]
                or not runtime_record.ciphertext
            ):
                raise ValueError("runtime secret record is unavailable")
            credential = decrypt_secret(runtime_record.ciphertext, key_file=self._key_file)
            if not isinstance(credential, str) or not credential:
                raise ValueError("decrypted credential is unavailable")
            encoded = credential.encode("utf-8")
            if len(encoded) > MAX_MATERIALIZED_CREDENTIAL_BYTES or any(
                ord(char) < 32 or ord(char) == 127 for char in credential
            ):
                raise ValueError("decrypted credential is unavailable")
        except ExternalMcpSecretMaterializerError:
            raise
        except (
            CredentialVaultError,
            SecretStoreError,
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
            AttributeError,
        ) as exc:
            raise ExternalMcpSecretMaterializerError(
                "external MCP credential is unavailable", code=AUTH_SECRET_UNAVAILABLE
            ) from exc
        return MaterializedExternalMcpAuth(scheme=BEARER, credential=credential)


__all__ = [
    "AUTH_BINDING_INVALID",
    "AUTH_SCHEME_UNSUPPORTED",
    "AUTH_SECRET_UNAVAILABLE",
    "BEARER",
    "ExternalMcpSecretMaterializer",
    "ExternalMcpSecretMaterializerError",
    "MAX_MATERIALIZED_CREDENTIAL_BYTES",
    "MaterializedExternalMcpAuth",
    "NONE",
]
