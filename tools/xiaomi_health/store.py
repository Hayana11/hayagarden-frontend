"""Root-only atomic credential bundle for the Xiaomi Health provider."""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


DEFAULT_CREDENTIAL_PATH = "/opt/frontend/.xiaomi-health.env"
CANARY_CREDENTIAL_PATH = "/run/hayagarden/xiaomi-health-canary.env"
SOURCE = "xiaomi_fitness_cloud"
AUTH_STATES = frozenset({"valid", "auth_expired"})
ERROR_CODES = frozenset({"auth_expired", "timeout", "api_error", "malformed_response", "unavailable"})
SECRET_FIELDS = frozenset({"user_id", "c_user_id", "service_token", "ssecurity", "pass_token", "device_id"})
METADATA_FIELDS = frozenset({"auth_state", "updated_at", "last_checked_at", "last_success_at", "last_error"})
ALLOWED_FIELDS = SECRET_FIELDS | METADATA_FIELDS


class SecretStoreError(Exception):
    """Stable error whose message never includes a credential or file content."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def empty_status() -> dict[str, Any]:
    return {
        "connected": False,
        "provider": SOURCE,
        "auth_state": "missing",
        "last_success_at": None,
        "last_error": None,
    }


def _secure_owner_mode(st: os.stat_result, *, directory: bool = False, require_root: bool = True) -> bool:
    if not stat.S_ISDIR(st.st_mode) if directory else not stat.S_ISREG(st.st_mode):
        return False
    if require_root and (
        (hasattr(st, "st_uid") and st.st_uid != 0)
        or (hasattr(st, "st_gid") and st.st_gid != 0)
    ):
        return False
    if (not directory and stat.S_IMODE(st.st_mode) != 0o600) or (directory and st.st_mode & 0o002):
        return False
    return True


class XiaomiCredentialStore:
    """Provider-private file reader/writer. The file is never sourced as shell."""

    def __init__(self, path: str = DEFAULT_CREDENTIAL_PATH, *, require_root: bool = True):
        self.path = Path(path)
        self.require_root = require_root

    def _check_parent(self, *, create: bool = False) -> None:
        parent = self.path.parent
        if create:
            try:
                parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                raise SecretStoreError("credential directory unavailable") from exc
        try:
            st = parent.stat(follow_symlinks=False)
        except OSError as exc:
            raise SecretStoreError("credential directory unavailable") from exc
        if not stat.S_ISDIR(st.st_mode) or (self.require_root and hasattr(st, "st_uid") and st.st_uid != 0) or st.st_mode & 0o002:
            raise SecretStoreError("credential directory permissions invalid")

    @contextmanager
    def _locked(self, *, exclusive: bool, create: bool = False) -> Iterator[None]:
        self._check_parent(create=create)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        try:
            fd = os.open(self.path.parent, flags)
        except OSError as exc:
            raise SecretStoreError("credential lock unavailable") from exc
        try:
            lock_st = os.fstat(fd)
            if not _secure_owner_mode(lock_st, directory=True, require_root=self.require_root):
                raise SecretStoreError("credential directory permissions invalid")
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _read_locked(self) -> dict[str, Any] | None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SecretStoreError("credential file unavailable") from exc
        try:
            st = os.fstat(fd)
            if not _secure_owner_mode(st, require_root=self.require_root):
                raise SecretStoreError("credential file permissions invalid")
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
                lines = stream.read(64 * 1024 + 1)
            if len(lines) > 64 * 1024:
                raise SecretStoreError("credential file size invalid")
            data: dict[str, Any] = {}
            for line in lines.splitlines():
                if not line:
                    continue
                key, separator, value = line.partition("=")
                if not separator or not key.startswith("XIAOMI_HEALTH_"):
                    raise SecretStoreError("credential format invalid")
                field = key.removeprefix("XIAOMI_HEALTH_").lower()
                if field not in ALLOWED_FIELDS or field in data:
                    raise SecretStoreError("credential format invalid")
                try:
                    data[field] = json.loads(value)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise SecretStoreError("credential format invalid") from exc
            self._validate(data)
            return data
        finally:
            os.close(fd)

    @staticmethod
    def _validate(data: Mapping[str, Any]) -> None:
        if set(data) != ALLOWED_FIELDS:
            raise SecretStoreError("credential bundle incomplete")
        for field in SECRET_FIELDS:
            if not isinstance(data.get(field), str):
                raise SecretStoreError("credential field invalid")
        if not data["user_id"].isdigit() or not data["service_token"] or not data["ssecurity"] or not data["device_id"]:
            raise SecretStoreError("credential bundle incomplete")
        if data["auth_state"] not in AUTH_STATES:
            raise SecretStoreError("credential state invalid")
        for field in ("updated_at", "last_checked_at", "last_success_at"):
            if data[field] is not None and (not isinstance(data[field], str) or len(data[field]) > 40):
                raise SecretStoreError("credential metadata invalid")
        if data["last_error"] is not None and data["last_error"] not in ERROR_CODES:
            raise SecretStoreError("credential metadata invalid")

    def load(self) -> dict[str, Any] | None:
        with self._locked(exclusive=False):
            return self._read_locked()

    def status(self) -> dict[str, Any]:
        try:
            data = self.load()
        except SecretStoreError:
            return {**empty_status(), "auth_state": "unavailable", "last_error": "unavailable"}
        if data is None:
            return empty_status()
        connected = data["auth_state"] == "valid"
        return {
            "connected": connected,
            "provider": SOURCE,
            "auth_state": data["auth_state"],
            "last_success_at": data["last_success_at"],
            "last_error": data["last_error"],
        }

    def _write_locked(self, data: Mapping[str, Any]) -> None:
        self._validate(data)
        self._check_parent(create=True)
        content = "".join(
            f"XIAOMI_HEALTH_{field.upper()}={json.dumps(data[field], ensure_ascii=False, separators=(',', ':'))}\n"
            for field in sorted(ALLOWED_FIELDS)
        ).encode("utf-8")
        fd = -1
        temp_path: str | None = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=f".{secrets.token_hex(6)}.tmp", dir=str(self.path.parent))
            os.fchmod(fd, 0o600)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                os.fchown(fd, 0, 0)
            with os.fdopen(fd, "wb", closefd=True) as stream:
                fd = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
            dir_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, ValueError) as exc:
            raise SecretStoreError("credential update failed") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            if temp_path:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    def save(self, data: Mapping[str, Any]) -> None:
        with self._locked(exclusive=True, create=True):
            self._write_locked(data)

    def update_status(self, *, auth_state: str | None = None, error: str | None = None, success: bool = False) -> None:
        with self._locked(exclusive=True):
            data = self._read_locked()
            if data is None:
                return
            now = utc_now()
            data["last_checked_at"] = now
            data["last_error"] = error if error in ERROR_CODES else None
            if auth_state in AUTH_STATES:
                data["auth_state"] = auth_state
            if success:
                data["auth_state"] = "valid"
                data["last_success_at"] = now
                data["last_error"] = None
            self._write_locked(data)

    def delete(self) -> None:
        with self._locked(exclusive=True):
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise SecretStoreError("credential cleanup failed") from exc
            dir_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
