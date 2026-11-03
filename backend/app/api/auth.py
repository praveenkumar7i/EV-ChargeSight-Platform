"""Sign-up, sign-in, sign-out, session information, verification, password reset and invitations."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import iso
from ..config import get_settings
from ..db import SessionLocal, begin_immediate
from ..models_saas import AuthSession, Organisation, User
from ..security.guard import CSRF_HEADER, SESSION_COOKIE, client_ip
from ..security.tokens import same
from ..services import accounts, audit, entitlements
from ..services import orgs as org_svc

router = APIRouter(prefix="/api/auth")


def _db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _limiter(request: Request):
    return request.app.state.rate_limiter


def _rate(request: Request, key: str, limit: int, window_s: float) -> None:
    ok, retry = _limiter(request).hit(key, limit, window_s)
    if not ok:
        raise HTTPException(429, {"code": "rate_limited", "message": f"Too many attempts. Try again in {int(retry // 60) + 1} minute(s).",
                                  "retry_after_s": int(retry)}, headers={"Retry-After": str(int(retry))})


def set_session_cookie(response: Response, token: str, remember: bool) -> None:
    s = get_settings()
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=s.secure_cookies, samesite="lax", path="/",
                        max_age=(s.session_remember_days * 86400) if remember else None)


def clear_session_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(SESSION_COOKIE, path="/", secure=s.secure_cookies, httponly=True, samesite="lax")


def _account_error(e: accounts.AccountError) -> HTTPException:
    return HTTPException(e.status, {"code": e.code, "message": str(e), "fields": e.field_errors})


def me_payload(db: Session, sess: AuthSession) -> dict:
    user = db.get(User, sess.user_id)
    orgs = []
    for m in org_svc.memberships_of(db, user.id):
        o = db.get(Organisation, m.org_id)
        orgs.append({"id": o.id, "name": o.name, "code": o.code, "role": m.role,
                     "role_label": org_svc.ROLE_LABELS[m.role], "is_system_demo": o.is_system_demo,
                     "workspace_kind": o.workspace_kind or "customer"})
    active = next((o for o in orgs if o["id"] == sess.org_id), None)
    out = {"authenticated": True, "user": accounts.user_dict(user), "csrf_token": sess.csrf_token,
           "organisations": orgs, "organisation": None, "role": None, "permissions": [],
           "session": {"expires_at": iso(sess.expires_at), "remember": sess.remember}}
    if active:
        org = db.get(Organisation, active["id"])
        ent = entitlements.summary(db, org.id)
        out.update({"organisation": org_svc.org_dict(org), "role": active["role"], "role_label": active["role_label"],
                    "permissions": org_svc.permissions_for(active["role"]), "entitlements": ent,
                    "onboarding": org.onboarding or {}})
    return out


# --------------------------------------------------------------------------- sign-up / sign-in
class SignupIn(BaseModel):
    full_name: str = Field("", max_length=200)
    email: str = Field("", max_length=300)
    password: str = Field("", max_length=200)
    confirm_password: str = Field("", max_length=200)
    organisation_name: str = Field("", max_length=200)
    organisation_type: str = Field("", max_length=40)
    country: str = Field("", max_length=80)
    contact_phone: str = Field("", max_length=40)
    website: str = Field("", max_length=200)
    accept_terms: bool = False
    remember: bool = False


@router.post("/signup")
def signup(body: SignupIn, request: Request, response: Response, db: Session = Depends(_db)):
    _rate(request, f"signup:{client_ip(request)}", get_settings().auth_signup_limit, 3600)
    begin_immediate(db)
    try:
        user, org, delivery = accounts.signup(db, body.model_dump(), ip=client_ip(request))
    except accounts.AccountError as e:
        db.rollback()
        raise _account_error(e)
    token, sess = accounts.create_session(db, user, remember=body.remember, user_agent=request.headers.get("user-agent", ""),
                                          org_id=org.id)
    db.commit()
    set_session_cookie(response, token, body.remember)
    return {"ok": True, "verification": delivery, "next": "/app#onboarding", **me_payload(db, sess)}


class SigninIn(BaseModel):
    email: str = Field("", max_length=300)
    password: str = Field("", max_length=200)
    remember: bool = False


@router.post("/signin")
def signin(body: SigninIn, request: Request, response: Response, db: Session = Depends(_db)):
    s = get_settings()
    email = accounts.normalise_email(body.email)
    ip = client_ip(request)
    _rate(request, f"signin-ip:{ip}", s.auth_signin_ip_limit, 900)
    _rate(request, f"signin:{email}:{ip}", s.auth_signin_limit, 900)
    begin_immediate(db)
    user = accounts.authenticate(db, email, body.password)
    if user is None:
        audit.record(db, "account.signin_failed", actor_label=email[:120] or "unknown", ip=ip)
        db.commit()
        raise HTTPException(401, {"code": "invalid_credentials", "message": accounts.GENERIC_SIGNIN_ERROR})
    old = request.cookies.get(SESSION_COOKIE)
    if old:                                     # session fixation: never reuse a pre-existing session
        prev = accounts.load_session(db, old, touch=False)
        if prev is not None:
            accounts.revoke_session(db, prev)
    token, sess = accounts.create_session(db, user, remember=body.remember, user_agent=request.headers.get("user-agent", ""))
    audit.record(db, "account.signin", org_id=sess.org_id, actor=user, target_type="user", target_id=user.id, ip=ip)
    db.commit()
    _limiter(request).reset(f"signin:{email}:{ip}")
    set_session_cookie(response, token, body.remember)
    payload = me_payload(db, sess)
    org = payload.get("organisation")
    nxt = "/app#admin" if (org is None and user.is_platform_admin) else (
        "/app#onboarding" if org and not (payload.get("onboarding") or {}).get("completed") else "/app")
    return {"ok": True, "next": nxt, **payload}


@router.post("/signout")
def signout(request: Request, response: Response, db: Session = Depends(_db)):
    token = request.cookies.get(SESSION_COOKIE)
    sess = accounts.load_session(db, token, touch=False) if token else None
    if sess is not None:
        if not same(request.headers.get(CSRF_HEADER, ""), sess.csrf_token):
            raise HTTPException(403, {"code": "csrf_failed", "message": "Reload the page and try again."})
        accounts.revoke_session(db, sess)
        audit.record(db, "account.signout", org_id=sess.org_id, actor_label=str(sess.user_id), target_type="user",
                     target_id=sess.user_id, ip=client_ip(request))
        db.commit()
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
def me(request: Request, db: Session = Depends(_db)):
    sess = accounts.load_session(db, request.cookies.get(SESSION_COOKIE))
    if sess is None:
        db.commit()
        raise HTTPException(401, {"code": "auth_required", "message": "Not signed in."})
    out = me_payload(db, sess)
    db.commit()
    return out


class SwitchIn(BaseModel):
    org_id: int


@router.post("/switch-org")
def switch_org(body: SwitchIn, request: Request, db: Session = Depends(_db)):
    sess = db.get(AuthSession, request.state.session_id)
    if org_svc.membership(db, sess.user_id, body.org_id) is None:
        raise HTTPException(404, {"code": "not_found", "message": "Organisation not found."})
    sess.org_id = body.org_id
    db.commit()
    return me_payload(db, sess)


@router.post("/presentation-demo")
def open_presentation_demo(request: Request, db: Session = Depends(_db)):
    """Open (creating once) this user's separate Presentation Demo workspace and switch the session to it.
    It holds only SIMULATED synthetic data and never receives records from any other organisation."""
    from ..services import presentation
    from ..simulator import engine

    sess = db.get(AuthSession, request.state.session_id)
    user = db.get(User, sess.user_id)
    begin_immediate(db)
    with engine.lock():
        org, info = presentation.ensure_for_user(db, user)
    sess.org_id = org.id
    audit.record(db, "org.presentation_demo_opened", org_id=org.id, actor=user, target_type="organisation", target_id=org.id,
                 data=info)
    db.commit()
    return {**me_payload(db, sess), "presentation": {**info, "label": "SIMULATED",
            "message": "Presentation Demo: 6 demo sites, 40 SIMULATED chargers, fictional technicians, 6 h of history and "
                       "8 scripted tickets. Everything here is synthetic and separate from your own workspace."}}


@router.post("/my-fleet")
def open_my_fleet(request: Request, db: Session = Depends(_db)):
    """Switch back to the user's own workspace (My Fleet)."""
    from ..services import presentation

    sess = db.get(AuthSession, request.state.session_id)
    home = presentation.home_org_for_user(db, sess.user_id)
    if home is None:
        raise HTTPException(404, {"code": "not_found", "message": "You are not a member of an operator workspace."})
    sess.org_id = home.id
    db.commit()
    return me_payload(db, sess)


class ProfileIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)


@router.put("/profile")
def update_profile(body: ProfileIn, request: Request, db: Session = Depends(_db)):
    u = db.get(User, request.state.user_id)
    u.full_name = body.full_name.strip()
    db.commit()
    return accounts.user_dict(u)


class ChangePasswordIn(BaseModel):
    current_password: str = Field("", max_length=200)
    new_password: str = Field("", max_length=200)
    confirm_password: str = Field("", max_length=200)


@router.post("/change-password")
def change_password(body: ChangePasswordIn, request: Request, db: Session = Depends(_db)):
    _rate(request, f"pwchange:{request.state.user_id}", 10, 900)
    u = db.get(User, request.state.user_id)
    try:
        accounts.change_password(db, u, body.current_password, body.new_password, body.confirm_password,
                                 keep_session_id=request.state.session_id)
    except accounts.AccountError as e:
        db.rollback()
        raise _account_error(e)
    db.commit()
    return {"ok": True, "message": "Password changed. Your other sessions were signed out."}


@router.post("/resend-verification")
def resend_verification(request: Request, db: Session = Depends(_db)):
    _rate(request, f"verify:{request.state.user_id}", 5, 3600)
    u = db.get(User, request.state.user_id)
    if u.email_verified_at:
        return {"ok": True, "message": "Your e-mail address is already verified."}
    info = accounts.send_verification(db, u)
    db.commit()
    return {"ok": True, **info}


class TokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


@router.post("/verify-email")
def verify_email(body: TokenIn, request: Request, db: Session = Depends(_db)):
    _rate(request, f"verify-ip:{client_ip(request)}", 30, 3600)
    try:
        u = accounts.verify_email(db, body.token)
    except accounts.AccountError as e:
        raise _account_error(e)
    db.commit()
    return {"ok": True, "email": u.email}


class ForgotIn(BaseModel):
    email: str = Field("", max_length=300)


@router.post("/forgot-password")
def forgot_password(body: ForgotIn, request: Request, db: Session = Depends(_db)):
    email = accounts.normalise_email(body.email)
    _rate(request, f"reset-ip:{client_ip(request)}", 20, 3600)
    _rate(request, f"reset:{email}", get_settings().auth_reset_limit, 3600)
    out = accounts.request_reset(db, email, ip=client_ip(request))
    db.commit()
    return out


class ResetIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field("", max_length=200)
    confirm_password: str = Field("", max_length=200)


@router.post("/reset-password")
def reset_password(body: ResetIn, request: Request, db: Session = Depends(_db)):
    _rate(request, f"reset-use:{client_ip(request)}", 20, 3600)
    try:
        accounts.reset_password(db, body.token, body.password, body.confirm_password)
    except accounts.AccountError as e:
        db.rollback()
        raise _account_error(e)
    db.commit()
    return {"ok": True, "message": "Password updated. Sign in with your new password."}


@router.get("/sessions")
def sessions(request: Request, db: Session = Depends(_db)):
    from ..clock import wall_now

    rows = db.scalars(select(AuthSession).where(AuthSession.user_id == request.state.user_id, AuthSession.revoked_at.is_(None),
                                                AuthSession.expires_at > wall_now()).order_by(AuthSession.last_seen_at.desc()))
    return [{"id": s.id, "created_at": iso(s.created_at), "last_seen_at": iso(s.last_seen_at), "expires_at": iso(s.expires_at),
             "remember": s.remember, "user_agent": s.user_agent, "current": s.id == request.state.session_id} for s in rows]


@router.post("/sessions/{sid}/revoke")
def revoke(sid: int, request: Request, db: Session = Depends(_db)):
    s = db.get(AuthSession, sid)
    if s is None or s.user_id != request.state.user_id:
        raise HTTPException(404, {"code": "not_found", "message": "Session not found."})
    accounts.revoke_session(db, s)
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- invitations
@router.get("/invitations/{token}")
def invitation(token: str, request: Request, db: Session = Depends(_db)):
    _rate(request, f"invite-ip:{client_ip(request)}", 60, 3600)
    try:
        row = accounts.invitation_for(db, token)
    except accounts.AccountError as e:
        raise _account_error(e)
    org = db.get(Organisation, row.org_id)
    has_account = db.scalar(select(User.id).where(User.email == row.email)) is not None
    return {"organisation": org.name, "email": row.email, "role": row.role, "role_label": org_svc.ROLE_LABELS[row.role],
            "expires_at": iso(row.expires_at), "has_account": has_account}


class InviteSignupIn(BaseModel):
    full_name: str = Field("", max_length=200)
    password: str = Field("", max_length=200)
    confirm_password: str = Field("", max_length=200)
    accept_terms: bool = False


@router.post("/invitations/{token}/signup")
def invitation_signup(token: str, body: InviteSignupIn, request: Request, response: Response, db: Session = Depends(_db)):
    _rate(request, f"signup:{client_ip(request)}", get_settings().auth_signup_limit, 3600)
    begin_immediate(db)
    try:
        user = accounts.signup_from_invite(db, token, body.full_name, body.password, body.confirm_password, body.accept_terms)
    except accounts.AccountError as e:
        db.rollback()
        raise _account_error(e)
    tok, sess = accounts.create_session(db, user, remember=False, user_agent=request.headers.get("user-agent", ""))
    db.commit()
    set_session_cookie(response, tok, False)
    return {"ok": True, "next": "/app", **me_payload(db, sess)}


@router.post("/invitations/{token}/accept")
def invitation_accept(token: str, request: Request, db: Session = Depends(_db)):
    begin_immediate(db)
    user = db.get(User, request.state.user_id)
    try:
        m = accounts.accept_invite(db, token, user)
    except accounts.AccountError as e:
        db.rollback()
        raise _account_error(e)
    sess = db.get(AuthSession, request.state.session_id)
    sess.org_id = m.org_id
    db.commit()
    return {"ok": True, "next": "/app", **me_payload(db, sess)}
