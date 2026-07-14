"""Small encrypted-at-rest vault for relay console credentials.

The master key lives outside the repository and SQLite database. Database backups therefore
contain only authenticated ciphertext; losing the separate key intentionally makes the saved
credentials unrecoverable.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat

from cryptography.fernet import Fernet, InvalidToken


DEFAULT_KEY_FILE = "/etc/hayagarden/relay-credentials.key"


class CredentialVaultError(RuntimeError):
    pass


def _key_path(path: str | None = None) -> Path:
    return Path(path or os.environ.get("HAYAGARDEN_RELAY_VAULT_KEY_FILE") or DEFAULT_KEY_FILE)


def _load_fernet(path: str | None = None) -> Fernet:
    key_path = _key_path(path)
    try:
        metadata = key_path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise CredentialVaultError("凭据保险箱密钥不是普通文件")
        if os.name == "posix" and metadata.st_mode & 0o077:
            raise CredentialVaultError("凭据保险箱密钥权限必须是 600")
        key = key_path.read_bytes().strip()
        return Fernet(key)
    except CredentialVaultError:
        raise
    except Exception as exc:
        raise CredentialVaultError("凭据保险箱尚未配置") from exc


def encrypt_secret(secret: str, *, key_file: str | None = None) -> str:
    raw = str(secret or "")
    if not raw:
        raise CredentialVaultError("不能保存空凭据")
    return _load_fernet(key_file).encrypt(raw.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str, *, key_file: str | None = None) -> str:
    try:
        return _load_fernet(key_file).decrypt(str(ciphertext or "").encode("ascii")).decode("utf-8")
    except CredentialVaultError:
        raise
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise CredentialVaultError("已保存的控制台凭据无法解密") from exc
