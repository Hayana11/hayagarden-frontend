"""The sole production composition root for external MCP state.

This module owns production path validation, one SQLite connection, owner
construction order, and connection lifetime.  It deliberately has no
application startup hook and performs no work at import time.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from cryptography.fernet import Fernet

from .external_mcp_auth_binding import ExternalMcpAuthBindingRegistry
from .external_mcp_invocation import ExternalMcpInvocation
from .external_mcp_runtime import ExternalMcpRuntime
from .external_mcp_secret_materializer import ExternalMcpSecretMaterializer
from .external_secret_store import ExternalSecretStore
from .external_server_registry import ExternalServerRegistry
from .external_tool_execution_fence import ExternalToolExecutionFence
from .external_tool_registry import ExternalToolCandidateRegistry
from .external_tool_side_effect_policy import ExternalToolSideEffectPolicy


EXTERNAL_MCP_DB_PATH = "/var/lib/hayagarden/external-mcp.db"
EXTERNAL_MCP_KEY_FILE = "/etc/hayagarden/external-mcp-credentials.key"


class ExternalMcpProductionInitializationError(RuntimeError):
    """Stable, secret-free failure raised before production graph exposure."""


@dataclass(frozen=True)
class ExternalMcpProductionGraph:
    """All external MCP owners sharing one private SQLite connection."""

    server_registry: ExternalServerRegistry
    secret_store: ExternalSecretStore
    auth_binding_registry: ExternalMcpAuthBindingRegistry
    candidate_registry: ExternalToolCandidateRegistry
    side_effect_policy: ExternalToolSideEffectPolicy
    execution_fence: ExternalToolExecutionFence
    invocation: ExternalMcpInvocation
    materializer: ExternalMcpSecretMaterializer
    runtime: ExternalMcpRuntime
    _connection: sqlite3.Connection = field(repr=False, compare=False)

    def __repr__(self) -> str:
        return "ExternalMcpProductionGraph(<private connection>)"


def _initialization_error(code: str) -> ExternalMcpProductionInitializationError:
    return ExternalMcpProductionInitializationError(code)


def _lstat(path: str, *, code: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise _initialization_error(code) from exc


def _validate_external_key() -> None:
    metadata = _lstat(EXTERNAL_MCP_KEY_FILE, code="EXTERNAL_MCP_KEY_INVALID")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise _initialization_error("EXTERNAL_MCP_KEY_INVALID")
    if (
        metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise _initialization_error("EXTERNAL_MCP_KEY_INVALID")
    try:
        raw = Path(EXTERNAL_MCP_KEY_FILE).read_bytes().strip()
        if len(raw) != 44:
            raise ValueError
        Fernet(raw)
    except (OSError, TypeError, ValueError) as exc:
        raise _initialization_error("EXTERNAL_MCP_KEY_INVALID") from exc


def _validate_db_parent() -> None:
    metadata = _lstat("/var/lib/hayagarden", code="EXTERNAL_MCP_DB_PARENT_INVALID")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise _initialization_error("EXTERNAL_MCP_DB_PARENT_INVALID")
    if metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise _initialization_error("EXTERNAL_MCP_DB_PARENT_INVALID")


def _validate_existing_db() -> None:
    metadata = _lstat(EXTERNAL_MCP_DB_PATH, code="EXTERNAL_MCP_DB_INVALID")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise _initialization_error("EXTERNAL_MCP_DB_INVALID")


def _create_db_exclusively() -> None:
    """Ensure the exact DB file exists without taking ownership of cleanup."""
    try:
        metadata = os.lstat(EXTERNAL_MCP_DB_PATH)
    except FileNotFoundError:
        metadata = None
    except OSError as exc:
        raise _initialization_error("EXTERNAL_MCP_DB_INVALID") from exc

    if metadata is not None:
        _validate_existing_db()
        return

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(EXTERNAL_MCP_DB_PATH, flags, 0o600)
    except FileExistsError:
        _validate_existing_db()
        return
    except OSError as exc:
        raise _initialization_error("EXTERNAL_MCP_DB_CREATE_FAILED") from exc
    try:
        os.fsync(fd)
        metadata = os.fstat(fd)
    finally:
        os.close(fd)
    _validate_existing_db()
    return


def _build_graph(connection: sqlite3.Connection) -> ExternalMcpProductionGraph:
    server_registry = ExternalServerRegistry(connection)
    secret_store = ExternalSecretStore(
        connection,
        registry=server_registry,
        key_file=EXTERNAL_MCP_KEY_FILE,
    )
    auth_binding_registry = ExternalMcpAuthBindingRegistry(
        connection,
        server_registry=server_registry,
        secret_store=secret_store,
    )
    candidate_registry = ExternalToolCandidateRegistry(
        connection,
        server_registry=server_registry,
    )
    side_effect_policy = ExternalToolSideEffectPolicy(
        connection,
        candidate_registry=candidate_registry,
        server_registry=server_registry,
    )
    execution_fence = ExternalToolExecutionFence(
        connection,
        server_registry=server_registry,
        candidate_registry=candidate_registry,
        side_effect_policy=side_effect_policy,
    )
    invocation = ExternalMcpInvocation(
        connection,
        server_registry=server_registry,
        candidate_registry=candidate_registry,
        side_effect_policy=side_effect_policy,
        execution_fence=execution_fence,
        auth_binding_registry=auth_binding_registry,
    )
    materializer = ExternalMcpSecretMaterializer(
        secret_store,
        key_file=EXTERNAL_MCP_KEY_FILE,
    )
    runtime = ExternalMcpRuntime(
        invocation,
        server_registry=server_registry,
        auth_binding_registry=auth_binding_registry,
        secret_store=secret_store,
        materializer=materializer,
    )
    return ExternalMcpProductionGraph(
        server_registry=server_registry,
        secret_store=secret_store,
        auth_binding_registry=auth_binding_registry,
        candidate_registry=candidate_registry,
        side_effect_policy=side_effect_policy,
        execution_fence=execution_fence,
        invocation=invocation,
        materializer=materializer,
        runtime=runtime,
        _connection=connection,
    )


@contextlib.contextmanager
def open_external_mcp_production() -> Iterator[ExternalMcpProductionGraph]:
    """Open the one production external-MCP authority graph."""
    _validate_external_key()
    _validate_db_parent()
    _create_db_exclusively()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(EXTERNAL_MCP_DB_PATH, timeout=5.0)
        graph = _build_graph(connection)
        yield graph
    finally:
        if connection is not None:
            connection.close()


__all__ = [
    "EXTERNAL_MCP_DB_PATH",
    "EXTERNAL_MCP_KEY_FILE",
    "ExternalMcpProductionGraph",
    "ExternalMcpProductionInitializationError",
    "open_external_mcp_production",
]
