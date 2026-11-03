"""Encryption of stored integration credentials (Fernet: AES-128-CBC + HMAC-SHA256, `cryptography`).

The key is held OUTSIDE the database:
* `INTEGRATION_ENCRYPTION_KEY` environment variable (recommended for any shared deployment), or
* a key file created on first use at data/keys/integration.key (local development).
Losing the key makes stored credentials unreadable (they must be re-entered); it never exposes them.
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..config import PROJECT_ROOT, get_settings


class SecretUnavailable(Exception):
    pass


def _key_file() -> Path:
    return Path(get_settings().integration_key_file or (PROJECT_ROOT / "data" / "keys" / "integration.key"))


def _fernet() -> Fernet:
    key = get_settings().integration_encryption_key
    if not key:
        path = _key_file()
        if path.exists():
            key = path.read_text(encoding="ascii").strip()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key().decode("ascii")
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(key)
    return Fernet(key.encode("ascii") if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as e:
        raise SecretUnavailable("The stored credential cannot be decrypted with the current key; re-enter it.") from e
