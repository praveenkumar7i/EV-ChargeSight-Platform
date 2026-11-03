"""Password hashing with Argon2id (argon2-cffi, the reference implementation of the PHC winner)."""
from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_ph = PasswordHasher()          # argon2-cffi defaults (RFC 9106 low-memory profile)
# A real hash of a random throw-away password: verifying against it when the e-mail is unknown keeps the
# response time similar, so timing does not reveal which e-mail addresses have accounts.
_DUMMY = _ph.hash("chargesight-timing-equaliser-not-a-password")

MIN_LENGTH = 10
MAX_LENGTH = 128
COMMON = {"password", "password1", "password123", "12345678", "123456789", "1234567890", "qwerty123", "iloveyou",
          "chargesight", "admin1234", "welcome123", "letmein123", "qwertyuiop"}


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _ph.verify(password_hash or _DUMMY, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


def password_problems(password: str, *, email: str = "", name: str = "") -> list[str]:
    """Strength rules (NIST SP 800-63B style: length first, block common and personal passwords)."""
    p = password or ""
    out = []
    if len(p) < MIN_LENGTH:
        out.append(f"Use at least {MIN_LENGTH} characters.")
    if len(p) > MAX_LENGTH:
        out.append(f"Use at most {MAX_LENGTH} characters.")
    classes = sum(bool(re.search(rx, p)) for rx in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    if len(p) < 16 and classes < 3:
        out.append("Mix at least three of: lower-case letters, upper-case letters, digits, symbols (or use 16+ characters).")
    low = p.lower()
    if low in COMMON or len(set(p)) <= 3:
        out.append("This password is too common or repetitive.")
    local = (email or "").split("@")[0].lower()
    if local and len(local) >= 4 and local in low:
        out.append("Do not include your e-mail address in the password.")
    first = (name or "").split(" ")[0].lower()
    if first and len(first) >= 4 and first in low:
        out.append("Do not include your name in the password.")
    return out
