"""User accounts: sign-up, sign-in, server-side sessions, e-mail verification, password reset, invitations.

E-mail delivery is NOT configured in this prototype. Messages that would be e-mailed (verification,
password reset, invitation) are written to a local development outbox (data/dev_outbox/*.txt on the
server) when ENVIRONMENT is not production, and the UI says exactly that. Nothing claims an e-mail was sent.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..clock import iso, wall_now
from ..config import PROJECT_ROOT, get_settings
from ..models_saas import AuthSession, Membership, OneTimeToken, Organisation, User
from ..security.passwords import hash_password, needs_rehash, password_problems, verify_password
from ..security.tokens import new_token, token_hash
from . import audit
from .orgs import ROLE_LABELS, create_org, memberships_of

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
TERMS_VERSION = "prototype-2026-10"
VERIFY_TTL = timedelta(hours=48)
RESET_TTL = timedelta(minutes=30)
INVITE_TTL = timedelta(days=7)
GENERIC_SIGNIN_ERROR = "The e-mail address or password is incorrect."


class AccountError(Exception):
    def __init__(self, message: str, status: int = 422, field_errors: dict | None = None, code: str = "account"):
        super().__init__(message)
        self.status = status
        self.field_errors = field_errors or {}
        self.code = code


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    return len(email) <= 254 and bool(EMAIL_RE.match(email))


def outbox_dir() -> Path:
    return PROJECT_ROOT / "data" / "dev_outbox"


def base_url() -> str:
    s = get_settings()
    return (s.public_base_url or f"http://{s.host}:{s.port}").rstrip("/")


def write_outbox(to: str, subject: str, body: str) -> str | None:
    """Development outbox (a file-based stand-in for e-mail). Returns the file name, or None when disabled."""
    if not get_settings().outbox_active:
        return None
    d = outbox_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = wall_now().strftime("%Y%m%dT%H%M%S%f")
    safe = re.sub(r"[^a-z0-9]+", "-", to.lower())[:40]
    path = d / f"{stamp}-{safe}.txt"
    path.write_text(f"DEVELOPMENT OUTBOX - THIS MESSAGE WAS NOT E-MAILED\nTo: {to}\nSubject: {subject}\n\n{body}\n",
                    encoding="utf-8")
    return path.name


def delivery_info(outbox_file: str | None) -> dict:
    if outbox_file:
        return {"delivery": "dev_outbox", "email_sent": False,
                "message": ("E-mail delivery is not configured. The message was written to the server's development "
                            f"outbox (data/dev_outbox/{outbox_file}); no e-mail was sent.")}
    return {"delivery": "none", "email_sent": False,
            "message": "E-mail delivery is not configured, so no message was sent. Ask a platform administrator for help."}


# --------------------------------------------------------------------------- sign-up
def validate_signup(db: Session, data: dict) -> dict:
    errors = {}
    name = (data.get("full_name") or "").strip()
    email = normalise_email(data.get("email"))
    pw = data.get("password") or ""
    if not (2 <= len(name) <= 120):
        errors["full_name"] = "Enter your full name (2-120 characters)."
    if not valid_email(email):
        errors["email"] = "Enter a valid work e-mail address."
    probs = password_problems(pw, email=email, name=name)
    if probs:
        errors["password"] = " ".join(probs)
    if pw != (data.get("confirm_password") or ""):
        errors["confirm_password"] = "The passwords do not match."
    org = (data.get("organisation_name") or "").strip()
    if not (2 <= len(org) <= 120):
        errors["organisation_name"] = "Enter your organisation's name (2-120 characters)."
    if data.get("organisation_type") not in ("cpo", "fleet_operator", "service_provider", "site_owner", "other"):
        errors["organisation_type"] = "Choose an organisation type."
    if not (data.get("country") or "").strip():
        errors["country"] = "Choose a country."
    if not data.get("accept_terms"):
        errors["accept_terms"] = "Please accept the prototype terms and privacy notice."
    if not errors.get("email") and db.scalar(select(User.id).where(User.email == email)) is not None:
        errors["email"] = "An account with this e-mail already exists. Sign in instead, or reset your password."
    return errors


def signup(db: Session, data: dict, *, ip: str = "") -> tuple[User, Organisation, dict]:
    errors = validate_signup(db, data)
    if errors:
        raise AccountError("Please correct the highlighted fields.", 422, errors, "validation")
    now = wall_now()
    user = User(email=normalise_email(data["email"]), full_name=data["full_name"].strip(), password_hash=hash_password(data["password"]),
                created_at=now, password_changed_at=now, terms_accepted_at=now, terms_version=TERMS_VERSION)
    db.add(user)
    db.flush()
    org = create_org(db, name=data["organisation_name"], org_type=data["organisation_type"], country=data["country"],
                     owner=user, contact_phone=(data.get("contact_phone") or "").strip(), website=(data.get("website") or "").strip())
    audit.record(db, "account.signup", org_id=org.id, actor=user, target_type="user", target_id=user.id, ip=ip,
                 data={"organisation": org.name})
    info = send_verification(db, user)
    return user, org, info


def send_verification(db: Session, user: User) -> dict:
    token = new_token()
    db.add(OneTimeToken(purpose="verify_email", token_hash=token_hash(token), user_id=user.id, email=user.email,
                        created_at=wall_now(), expires_at=wall_now() + VERIFY_TTL))
    f = write_outbox(user.email, "Verify your ChargeSight e-mail address",
                     f"Hello {user.full_name},\n\nConfirm your e-mail address for ChargeSight:\n{base_url()}/verify-email?token={token}\n\n"
                     "The link expires in 48 hours.")
    return delivery_info(f)


def verify_email(db: Session, token: str) -> User:
    row = _consume(db, "verify_email", token)
    user = db.get(User, row.user_id)
    if user is None or normalise_email(user.email) != row.email:
        raise AccountError("This verification link is not valid.", 400, code="invalid_token")
    user.email_verified_at = wall_now()
    audit.record(db, "account.email_verified", actor=user, target_type="user", target_id=user.id)
    return user


def _consume(db: Session, purpose: str, token: str) -> OneTimeToken:
    row = db.scalar(select(OneTimeToken).where(OneTimeToken.token_hash == token_hash(token or ""), OneTimeToken.purpose == purpose))
    if row is None or row.used_at is not None or row.revoked_at is not None or row.expires_at <= wall_now():
        raise AccountError("This link is invalid or has expired.", 400, code="invalid_token")
    row.used_at = wall_now()
    return row


# --------------------------------------------------------------------------- sign-in and sessions
def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == normalise_email(email)))
    ok = verify_password(password or "", user.password_hash if user else None)
    if not ok or user is None or not user.is_active:
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    return user


def default_org_id(db: Session, user: User) -> int | None:
    ms = memberships_of(db, user.id)
    owned = [m for m in ms if m.role == "owner"]
    return (owned or ms)[0].org_id if ms else None


def create_session(db: Session, user: User, *, remember: bool, user_agent: str = "", org_id: int | None = None) -> tuple[str, AuthSession]:
    s = get_settings()
    now = wall_now()
    token = new_token()
    expires = now + (timedelta(days=s.session_remember_days) if remember else timedelta(hours=s.session_absolute_hours))
    sess = AuthSession(token_hash=token_hash(token), user_id=user.id, org_id=org_id or default_org_id(db, user),
                       csrf_token=new_token(24), created_at=now, last_seen_at=now, expires_at=expires, remember=remember,
                       user_agent=(user_agent or "")[:200])
    db.add(sess)
    user.last_login_at = now
    return token, sess


def load_session(db: Session, token: str | None, *, touch: bool = True) -> AuthSession | None:
    """Valid session for a cookie token, or None (unknown, revoked, expired or idle too long)."""
    if not token or len(token) > 200:
        return None
    sess = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(token)))
    if sess is None or sess.revoked_at is not None:
        return None
    now = wall_now()
    s = get_settings()
    idle = timedelta(minutes=s.session_idle_minutes) if not sess.remember else timedelta(days=s.session_remember_days)
    if sess.expires_at <= now or now - sess.last_seen_at > idle:
        if sess.revoked_at is None:
            sess.revoked_at = now
        return None
    user = db.get(User, sess.user_id)
    if user is None or not user.is_active:
        return None
    if touch and (now - sess.last_seen_at).total_seconds() > 60:
        sess.last_seen_at = now
    return sess


def revoke_session(db: Session, sess: AuthSession) -> None:
    sess.revoked_at = wall_now()


def revoke_all(db: Session, user_id: int, *, except_id: int | None = None) -> int:
    q = update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
    if except_id is not None:
        q = q.where(AuthSession.id != except_id)
    return db.execute(q.values(revoked_at=wall_now())).rowcount or 0


# --------------------------------------------------------------------------- passwords
def request_reset(db: Session, email: str, *, ip: str = "") -> dict:
    """Always returns the same generic response, whether or not the account exists."""
    user = db.scalar(select(User).where(User.email == normalise_email(email)))
    generic = {"message": ("If an account exists for that address, a reset link has been prepared. "
                           "E-mail delivery is not configured in this prototype: "
                           + ("in development the link is written to the server's data/dev_outbox folder; "
                              if get_settings().outbox_active else "")
                           + "no e-mail is sent."), "email_sent": False}
    if user is None or not user.is_active:
        return generic
    db.execute(update(OneTimeToken).where(OneTimeToken.user_id == user.id, OneTimeToken.purpose == "reset_password",
                                          OneTimeToken.used_at.is_(None)).values(revoked_at=wall_now()))
    token = new_token()
    db.add(OneTimeToken(purpose="reset_password", token_hash=token_hash(token), user_id=user.id, email=user.email,
                        created_at=wall_now(), expires_at=wall_now() + RESET_TTL))
    write_outbox(user.email, "Reset your ChargeSight password",
                 f"Hello {user.full_name},\n\nA password reset was requested for your ChargeSight account.\n"
                 f"{base_url()}/reset-password?token={token}\n\nThe link expires in 30 minutes. If you did not ask for this, ignore it.")
    audit.record(db, "account.reset_requested", actor=user, target_type="user", target_id=user.id, ip=ip)
    return generic


def reset_password(db: Session, token: str, password: str, confirm: str) -> User:
    if password != confirm:
        raise AccountError("The passwords do not match.", 422, {"confirm_password": "The passwords do not match."}, "validation")
    row = db.scalar(select(OneTimeToken).where(OneTimeToken.token_hash == token_hash(token or ""),
                                               OneTimeToken.purpose == "reset_password"))
    if row is None or row.used_at or row.revoked_at or row.expires_at <= wall_now():
        raise AccountError("This reset link is invalid or has expired. Request a new one.", 400, code="invalid_token")
    user = db.get(User, row.user_id)
    probs = password_problems(password, email=user.email, name=user.full_name)
    if probs:
        raise AccountError("Choose a stronger password.", 422, {"password": " ".join(probs)}, "validation")
    row.used_at = wall_now()
    user.password_hash = hash_password(password)
    user.password_changed_at = wall_now()
    revoke_all(db, user.id)
    audit.record(db, "account.password_reset", actor=user, target_type="user", target_id=user.id)
    return user


def change_password(db: Session, user: User, current: str, new: str, confirm: str, *, keep_session_id: int) -> None:
    if not verify_password(current or "", user.password_hash):
        raise AccountError("The current password is incorrect.", 422, {"current_password": "The current password is incorrect."}, "validation")
    if new != confirm:
        raise AccountError("The new passwords do not match.", 422, {"confirm_password": "The passwords do not match."}, "validation")
    probs = password_problems(new, email=user.email, name=user.full_name)
    if probs:
        raise AccountError("Choose a stronger password.", 422, {"new_password": " ".join(probs)}, "validation")
    user.password_hash = hash_password(new)
    user.password_changed_at = wall_now()
    revoke_all(db, user.id, except_id=keep_session_id)
    audit.record(db, "account.password_changed", actor=user, target_type="user", target_id=user.id)


# --------------------------------------------------------------------------- invitations
def invite(db: Session, org: Organisation, inviter: User, inviter_role: str, email: str, role: str) -> tuple[str, dict]:
    from .entitlements import check_limit

    email = normalise_email(email)
    if not valid_email(email):
        raise AccountError("Enter a valid e-mail address.", 422, {"email": "Enter a valid e-mail address."}, "validation")
    if role not in ROLE_LABELS or role == "owner":
        raise AccountError("Choose a role (ownership cannot be granted by invitation).", 422, {"role": "Invalid role."}, "validation")
    if inviter_role != "owner" and role == "admin":
        raise AccountError("Only the organisation owner can invite administrators.", 403, code="forbidden")
    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None and db.scalar(select(Membership.id).where(Membership.org_id == org.id, Membership.user_id == existing.id,
                                                                      Membership.status == "active")):
        raise AccountError("This person is already a member.", 409, {"email": "Already a member."}, "duplicate")
    pending = db.scalar(select(OneTimeToken).where(OneTimeToken.org_id == org.id, OneTimeToken.email == email,
                                                   OneTimeToken.purpose == "invite", OneTimeToken.used_at.is_(None),
                                                   OneTimeToken.revoked_at.is_(None), OneTimeToken.expires_at > wall_now()))
    if pending is not None:
        raise AccountError("An invitation for this address is already pending. Revoke it to send a new one.", 409,
                           {"email": "Invitation already pending."}, "duplicate")
    check_limit(db, org.id, "users")
    token = new_token()
    db.add(OneTimeToken(purpose="invite", token_hash=token_hash(token), org_id=org.id, email=email, role=role,
                        created_at=wall_now(), expires_at=wall_now() + INVITE_TTL, created_by_user_id=inviter.id))
    link = f"{base_url()}/accept-invite?token={token}"
    f = write_outbox(email, f"Invitation to {org.name} on ChargeSight",
                     f"{inviter.full_name} invited you to join {org.name} on ChargeSight as {ROLE_LABELS[role]}.\n{link}\n\n"
                     "The invitation expires in 7 days.")
    audit.record(db, "org.member_invited", org_id=org.id, actor=inviter, target_type="invite", target_id=email, data={"role": role})
    return link, delivery_info(f)


def invitation_for(db: Session, token: str) -> OneTimeToken:
    row = db.scalar(select(OneTimeToken).where(OneTimeToken.token_hash == token_hash(token or ""), OneTimeToken.purpose == "invite"))
    if row is None or row.used_at or row.revoked_at or row.expires_at <= wall_now():
        raise AccountError("This invitation is invalid, has expired or was already used.", 400, code="invalid_token")
    return row


def accept_invite(db: Session, token: str, user: User) -> Membership:
    row = invitation_for(db, token)
    if normalise_email(user.email) != row.email:
        raise AccountError("This invitation was issued to a different e-mail address. Sign in with that address.", 403,
                           code="wrong_account")
    m = db.scalar(select(Membership).where(Membership.org_id == row.org_id, Membership.user_id == user.id))
    if m is None:
        m = Membership(org_id=row.org_id, user_id=user.id, role=row.role, status="active", invited_by_user_id=row.created_by_user_id)
        db.add(m)
    else:
        m.status, m.role = "active", row.role
    row.used_at = wall_now()
    if user.email_verified_at is None:
        user.email_verified_at = wall_now()       # the invitation link proves control of the address
    audit.record(db, "org.member_joined", org_id=row.org_id, actor=user, target_type="user", target_id=user.id, data={"role": row.role})
    return m


def signup_from_invite(db: Session, token: str, full_name: str, password: str, confirm: str, accept_terms: bool) -> User:
    row = invitation_for(db, token)
    errors = {}
    if not (2 <= len((full_name or "").strip()) <= 120):
        errors["full_name"] = "Enter your full name."
    probs = password_problems(password, email=row.email, name=full_name)
    if probs:
        errors["password"] = " ".join(probs)
    if password != confirm:
        errors["confirm_password"] = "The passwords do not match."
    if not accept_terms:
        errors["accept_terms"] = "Please accept the prototype terms and privacy notice."
    if db.scalar(select(User.id).where(User.email == row.email)) is not None:
        errors["email"] = "An account already exists for this address; sign in to accept the invitation."
    if errors:
        raise AccountError("Please correct the highlighted fields.", 422, errors, "validation")
    now = wall_now()
    user = User(email=row.email, full_name=full_name.strip(), password_hash=hash_password(password), created_at=now,
                password_changed_at=now, terms_accepted_at=now, terms_version=TERMS_VERSION)
    db.add(user)
    db.flush()
    accept_invite(db, token, user)
    return user


def user_dict(u: User) -> dict:
    return {"id": u.id, "email": u.email, "full_name": u.full_name, "email_verified": u.email_verified_at is not None,
            "is_platform_admin": u.is_platform_admin, "created_at": iso(u.created_at), "last_login_at": iso(u.last_login_at)}


def create_user_direct(db: Session, *, email: str, full_name: str, password: str, platform_admin: bool = False,
                       verified: bool = True) -> User:
    """CLI / tests: create a user without the public sign-up flow (still hashed, still validated)."""
    email = normalise_email(email)
    if not valid_email(email):
        raise AccountError("Invalid e-mail address.")
    probs = password_problems(password, email=email, name=full_name)
    if probs:
        raise AccountError(" ".join(probs))
    if db.scalar(select(User.id).where(User.email == email)):
        raise AccountError("A user with this e-mail already exists.", 409)
    now = wall_now()
    u = User(email=email, full_name=full_name, password_hash=hash_password(password), is_platform_admin=platform_admin,
             email_verified_at=now if verified else None, created_at=now, password_changed_at=now)
    db.add(u)
    db.flush()
    return u


def grant(db: Session, user: User, org_id: int, role: str) -> Membership:
    m = db.scalar(select(Membership).where(Membership.org_id == org_id, Membership.user_id == user.id))
    if m is None:
        m = Membership(org_id=org_id, user_id=user.id, role=role, status="active")
        db.add(m)
    else:
        m.role, m.status = role, "active"
    db.flush()
    return m
