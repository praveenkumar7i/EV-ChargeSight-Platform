"""Organisation profile, members and invitations, onboarding, workspace setup and integration connections.
Every query is scoped to the signed-in user's organisation (request.state.org_id, tenancy filter)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import iso, wall_now
from ..db import begin_immediate, get_db
from ..models import Charger, Site
from ..models_saas import AuditEvent, IntegrationConfig, Membership, OneTimeToken, Organisation, User
from ..security.guard import client_ip
from ..services import accounts, audit, entitlements
from ..services import integration_configs as ic
from ..services import onboarding as ob
from ..services import orgs as org_svc
from ..services import settings_store
from ..tenancy import nid

router = APIRouter(prefix="/api")


def _org(request: Request, db: Session) -> Organisation:
    return db.get(Organisation, request.state.org_id)


def _user(request: Request, db: Session) -> User:
    return db.get(User, request.state.user_id)


def _err(status: int, message: str, code: str = "invalid", **extra):
    return HTTPException(status, {"code": code, "message": message, **extra})


@router.get("/entitlements")
def get_entitlements(request: Request, db: Session = Depends(get_db)):
    return entitlements.summary(db, request.state.org_id)


# --------------------------------------------------------------------------- organisation profile
@router.get("/org")
def get_org(request: Request, db: Session = Depends(get_db)):
    org = _org(request, db)
    return {**org_svc.org_dict(org), "org_types": org_svc.ORG_TYPE_LABELS, "role": request.state.role}


class OrgIn(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=120)
    org_type: str | None = None
    country: str | None = Field(None, max_length=60)
    contact_phone: str | None = Field(None, max_length=40)
    website: str | None = Field(None, max_length=200)
    billing_address: str | None = Field(None, max_length=1000)
    tax_id: str | None = Field(None, max_length=40)
    primary_contact_name: str | None = Field(None, max_length=120)
    primary_contact_email: str | None = Field(None, max_length=254)
    sites_estimate: int | None = Field(None, ge=0, le=100000)
    chargers_estimate: int | None = Field(None, ge=0, le=1000000)
    alerts: dict | None = None
    retention_days: int | None = Field(None, ge=30, le=3650)


@router.put("/org")
def update_org(body: OrgIn, request: Request, db: Session = Depends(get_db)):
    org = _org(request, db)
    data = body.model_dump(exclude_unset=True)
    if "org_type" in data and data["org_type"] not in org_svc.ORG_TYPE_LABELS:
        raise _err(422, "Choose an organisation type.")
    if data.get("primary_contact_email") and not accounts.valid_email(accounts.normalise_email(data["primary_contact_email"])):
        raise _err(422, "Enter a valid contact e-mail address.")
    prefs = dict(org.preferences or {})
    if "alerts" in data:
        a = data.pop("alerts") or {}
        prefs["alerts"] = {"in_app": True, "critical_tickets": bool(a.get("critical_tickets", True)),
                           "sla_breaches": bool(a.get("sla_breaches", True)), "integration_failures": bool(a.get("integration_failures", True)),
                           "email": False, "sms": False, "note": "In-app alerts only; e-mail and SMS are not configured."}
    if "retention_days" in data:
        prefs["retention_days"] = data.pop("retention_days")
        prefs["retention_note"] = "Recorded preference. Automatic deletion is not implemented in this prototype; nothing is deleted."
    for k, v in data.items():
        if v is not None:
            setattr(org, k, v.strip() if isinstance(v, str) else v)
    org.preferences = prefs
    audit.record(db, "org.updated", org_id=org.id, actor=_user(request, db), target_type="organisation", target_id=org.id,
                 ip=client_ip(request), data={"fields": ",".join(sorted(body.model_dump(exclude_unset=True)))})
    db.commit()
    return org_svc.org_dict(org)


# --------------------------------------------------------------------------- members and invitations
@router.get("/org/members")
def members(request: Request, db: Session = Depends(get_db)):
    org_id = request.state.org_id
    out = []
    for m in db.scalars(select(Membership).where(Membership.org_id == org_id, Membership.status == "active").order_by(Membership.id)):
        u = db.get(User, m.user_id)
        out.append({"id": m.id, "user_id": u.id, "email": u.email, "full_name": u.full_name, "role": m.role,
                    "role_label": org_svc.ROLE_LABELS[m.role], "email_verified": u.email_verified_at is not None,
                    "technician_id": m.technician_id, "joined_at": iso(m.created_at), "you": u.id == request.state.user_id})
    invites = [{"id": t.id, "email": t.email, "role": t.role, "role_label": org_svc.ROLE_LABELS.get(t.role, t.role),
                "expires_at": iso(t.expires_at), "created_at": iso(t.created_at)}
               for t in db.scalars(select(OneTimeToken).where(OneTimeToken.org_id == org_id, OneTimeToken.purpose == "invite",
                                                               OneTimeToken.used_at.is_(None), OneTimeToken.revoked_at.is_(None),
                                                               OneTimeToken.expires_at > wall_now()))]
    return {"members": out, "invitations": invites, "roles": org_svc.ROLE_LABELS, "usage": entitlements.usage(db, org_id),
            "limits": entitlements.resolve(db, org_id)["limits"]}


class InviteIn(BaseModel):
    email: str = Field(max_length=300)
    role: str


@router.post("/org/invitations")
def invite(body: InviteIn, request: Request, db: Session = Depends(get_db)):
    begin_immediate(db)
    org = _org(request, db)
    try:
        link, delivery = accounts.invite(db, org, _user(request, db), request.state.role, body.email, body.role)
    except accounts.AccountError as e:
        db.rollback()
        raise HTTPException(e.status, {"code": e.code, "message": str(e), "fields": e.field_errors})
    except entitlements.EntitlementError as e:
        db.rollback()
        raise HTTPException(e.status, e.detail())
    db.commit()
    return {"ok": True, "invite_link": link, **delivery,
            "share_note": "Share this one-time link with the invitee yourself; it expires in 7 days."}


@router.post("/org/invitations/{iid}/revoke")
def revoke_invite(iid: int, request: Request, db: Session = Depends(get_db)):
    t = db.get(OneTimeToken, iid)
    if t is None or t.org_id != request.state.org_id or t.purpose != "invite":
        raise _err(404, "Invitation not found.", "not_found")
    t.revoked_at = wall_now()
    audit.record(db, "org.invite_revoked", org_id=t.org_id, actor=_user(request, db), target_type="invite", target_id=t.email)
    db.commit()
    return {"ok": True}


class MemberIn(BaseModel):
    role: str | None = None
    technician_id: str | None = Field(None, max_length=32)


def _member(db: Session, request: Request, mid: int) -> Membership:
    m = db.get(Membership, mid)
    if m is None or m.org_id != request.state.org_id or m.status != "active":
        raise _err(404, "Member not found.", "not_found")
    return m


@router.put("/org/members/{mid}")
def update_member(mid: int, body: MemberIn, request: Request, db: Session = Depends(get_db)):
    from ..models import Technician

    m = _member(db, request, mid)
    me = request.state.role
    if body.role is not None:
        if body.role not in org_svc.ROLE_LABELS:
            raise _err(422, "Unknown role.")
        if m.role == "owner" or body.role == "owner":
            raise _err(403, "Ownership cannot be changed here.", "forbidden")
        if me != "owner" and (m.role == "admin" or body.role == "admin"):
            raise _err(403, "Only the owner can grant or change administrator roles.", "forbidden")
        if m.user_id == request.state.user_id:
            raise _err(409, "You cannot change your own role.", "self")
        m.role = body.role
    if body.technician_id is not None:
        if body.technician_id and db.get(Technician, body.technician_id) is None:     # tenant-filtered lookup
            raise _err(404, "Technician record not found in this organisation.", "not_found")
        m.technician_id = body.technician_id or None
    audit.record(db, "org.member_updated", org_id=m.org_id, actor=_user(request, db), target_type="membership", target_id=m.id,
                 data={"role": m.role, "technician_id": m.technician_id})
    db.commit()
    return {"ok": True, "role": m.role, "technician_id": m.technician_id}


@router.post("/org/members/{mid}/remove")
def remove_member(mid: int, request: Request, db: Session = Depends(get_db)):
    from ..models_saas import AuthSession

    m = _member(db, request, mid)
    if m.role == "owner":
        raise _err(403, "The organisation owner cannot be removed.", "forbidden")
    if request.state.role != "owner" and m.role == "admin":
        raise _err(403, "Only the owner can remove an administrator.", "forbidden")
    if m.user_id == request.state.user_id:
        raise _err(409, "You cannot remove yourself.", "self")
    m.status = "removed"            # membership kept for the audit trail; the account itself is not deleted
    for s in db.scalars(select(AuthSession).where(AuthSession.user_id == m.user_id, AuthSession.org_id == m.org_id,
                                                  AuthSession.revoked_at.is_(None))):
        s.org_id = None
    audit.record(db, "org.member_removed", org_id=m.org_id, actor=_user(request, db), target_type="membership", target_id=m.id)
    db.commit()
    return {"ok": True}


@router.get("/org/audit")
def org_audit(request: Request, db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditEvent).where(AuditEvent.org_id == request.state.org_id).order_by(AuditEvent.id.desc()).limit(200))
    return [{"at": iso(a.at), "actor": a.actor_label, "action": a.action, "target": f"{a.target_type} {a.target_id}".strip(),
             "data": a.data} for a in rows]


# --------------------------------------------------------------------------- onboarding
@router.get("/org/onboarding")
def get_onboarding(request: Request, db: Session = Depends(get_db)):
    org = _org(request, db)
    return {"state": ob.state(org), "steps": ob.STEPS, "use_cases": ob.USE_CASES, "data_modes": ob.DATA_MODES,
            "checklist": ob.checklist(db, org), "organisation": org_svc.org_dict(org),
            "sla_hours": settings_store.sla_hours(db)}


class OnboardingIn(BaseModel):
    step: int | None = Field(None, ge=1, le=6)
    use_cases: list[str] | None = None
    data_mode: str | None = None
    complete: bool = False
    reopen: bool = False


@router.put("/org/onboarding")
def put_onboarding(body: OnboardingIn, request: Request, db: Session = Depends(get_db)):
    org = _org(request, db)
    try:
        st = ob.save(db, org, body.model_dump())
    except ValueError as e:
        raise _err(422, str(e))
    if body.complete:
        audit.record(db, "org.onboarding_completed", org_id=org.id, actor=_user(request, db), target_type="organisation", target_id=org.id)
    db.commit()
    return {"state": st, "checklist": ob.checklist(db, org)}


@router.post("/org/demo-workspace")
def demo_workspace(request: Request, db: Session = Depends(get_db)):
    from ..simulator import engine

    org = _org(request, db)
    begin_immediate(db)
    with engine.lock():
        res = org_svc.provision_demo_workspace(db, org)
    st = ob.state(org)
    st["data_mode"] = st.get("data_mode") or "demo"
    org.onboarding = st
    audit.record(db, "org.demo_workspace", org_id=org.id, actor=_user(request, db), target_type="organisation", target_id=org.id,
                 data={"created": res.get("created")})
    db.commit()
    return {"ok": True, "created": res.get("created"), "label": "SIMULATED",
            "message": "Private simulated demo fleet ready: 6 demo sites, 40 SIMULATED chargers, 4 DEMO technicians and a "
                       "6-hour scripted history. It is visible only to your organisation and is never mixed with real records."}


class SiteIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    code: str = Field(min_length=2, max_length=16, pattern=r"^[A-Za-z0-9-]+$")
    lat: float | None = Field(None, ge=-90, le=90)
    lng: float | None = Field(None, ge=-180, le=180)


@router.post("/org/sites")
def create_site(body: SiteIn, request: Request, db: Session = Depends(get_db)):
    code = nid(db, body.code.upper())
    if db.scalar(select(Site.id).where(Site.code == code)) is not None:
        raise _err(409, "A site with this code already exists.", "duplicate")
    begin_immediate(db)
    try:
        entitlements.check_new_managed_site(db, code)
    except entitlements.EntitlementError as e:
        db.rollback()
        raise HTTPException(e.status, e.detail())
    s = Site(code=code, name=body.name.strip(), lat=body.lat or 0.0, lng=body.lng or 0.0, daily_sessions=0, is_demo_location=False,
             source="operator_import")
    db.add(s)
    db.flush()
    audit.record(db, "org.site_registered", org_id=request.state.org_id, actor=_user(request, db), target_type="site", target_id=s.code)
    db.commit()
    return {"id": s.id, "code": s.code, "name": s.name, "source": "operator_import",
            "note": "Registered manually (shown under Imported data). No telemetry until data is imported or connected."}


class ChargerIn(BaseModel):
    site_id: int
    external_id: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9._:-]+$")
    connector_type: str = Field(min_length=2, max_length=40)
    power_kw: float = Field(gt=0, le=1000)


@router.post("/org/chargers")
def register_charger(body: ChargerIn, request: Request, db: Session = Depends(get_db)):
    site = db.get(Site, body.site_id)               # tenant-filtered: another organisation's site is "not found"
    if site is None or site.source == "simulation":
        raise _err(404, "Site not found (register real chargers on one of your registered sites).", "not_found")
    cid = nid(db, f"OPERATOR_IMPORT-{body.external_id}")[:64]
    if db.get(Charger, cid) is not None:
        raise _err(409, "A charger with this ID already exists.", "duplicate")
    begin_immediate(db)
    try:
        entitlements.check_limit(db, request.state.org_id, "chargers")
    except entitlements.EntitlementError as e:
        db.rollback()
        raise HTTPException(e.status, e.detail())
    c = Charger(id=cid, site_id=site.id, connector_type=body.connector_type, power_kw=body.power_kw, status="available",
                source="operator_import", external_id=body.external_id)
    db.add(c)
    audit.record(db, "org.charger_registered", org_id=request.state.org_id, actor=_user(request, db), target_type="charger", target_id=cid)
    db.commit()
    return {"id": c.id, "site_id": site.id, "source": "operator_import", "usage": entitlements.usage(db, request.state.org_id)}


class SlaIn(BaseModel):
    critical: float = Field(ge=0.5, le=168)
    major: float = Field(ge=0.5, le=720)
    minor: float = Field(ge=0.5, le=2160)


@router.put("/org/sla")
def set_sla(body: SlaIn, request: Request, db: Session = Depends(get_db)):
    if not (body.critical <= body.major <= body.minor):
        raise _err(422, "SLA hours must satisfy critical ≤ major ≤ minor.")
    settings_store.put(db, "sla_hours", body.model_dump())
    settings_store.put(db, "sla_policy_confirmed", {"at": iso(wall_now()), "by": request.state.user_email})
    audit.record(db, "org.sla_updated", org_id=request.state.org_id, actor=_user(request, db), data=body.model_dump())
    db.commit()
    return {"ok": True, "sla_hours": body.model_dump(),
            "note": "Applies to new tickets and to SLA deadlines computed from now on."}


# --------------------------------------------------------------------------- integration connections
def _cfg(db: Session, request: Request, iid: int) -> IntegrationConfig:
    cfg = db.get(IntegrationConfig, iid)
    if cfg is None or cfg.org_id != request.state.org_id:
        raise _err(404, "Connection not found.", "not_found")
    return cfg


def _ic_error(e: Exception):
    if isinstance(e, entitlements.EntitlementError):
        return HTTPException(e.status, e.detail())
    return HTTPException(getattr(e, "status", 422), {"code": getattr(e, "code", "integration"), "message": str(e)})


@router.get("/org/integrations")
def list_integrations(request: Request, db: Session = Depends(get_db)):
    from ..tenancy import LEGACY_ORG_ID

    org_id = request.state.org_id
    rows = db.scalars(select(IntegrationConfig).where(IntegrationConfig.org_id == org_id).order_by(IntegrationConfig.id))
    ent = entitlements.resolve(db, org_id)
    return {"connections": [ic.config_dict(db, c) for c in rows], "usage": entitlements.usage(db, org_id)["integrations"],
            "limit": ent["limits"].get("integrations"), "entitled": "integrations" in ent["features"],
            "states": ic.STATE_LABELS, "adapters": ic.ADAPTERS, "legacy_env_connection": org_id == LEGACY_ORG_ID,
            "requirements": {"operator_api": ["Written authorisation from the charging operator", "API base URL (https)",
                                              "A read-only API credential (sent as a Bearer token)",
                                              "Agreed data mapping to the ChargeSight event schema (docs/INTEGRATION.md)"],
                             "ocpp": ["Charge points that support OCPP 1.6J", "The operator's authorisation to point them at ChargeSight",
                                      "The organisation-specific endpoint URL shown here",
                                      "The central system process running (python -m app.integrations.ocpp_central)"]}}


class IntegrationIn(BaseModel):
    kind: str
    name: str = Field(max_length=80)


@router.post("/org/integrations")
def create_integration(body: IntegrationIn, request: Request, db: Session = Depends(get_db)):
    begin_immediate(db)
    try:
        cfg = ic.create(db, request.state.org_id, _user(request, db), body.kind, body.name)
    except (ic.IntegrationError, entitlements.EntitlementError) as e:
        db.rollback()
        raise _ic_error(e)
    audit.record(db, "integration.created", org_id=cfg.org_id, actor=_user(request, db), target_type="integration", target_id=cfg.id,
                 data={"kind": cfg.kind})
    db.commit()
    return ic.config_dict(db, cfg)


@router.get("/org/integrations/{iid}")
def get_integration(iid: int, request: Request, db: Session = Depends(get_db)):
    return ic.config_dict(db, _cfg(db, request, iid))


class IntegrationPatch(BaseModel):
    name: str | None = Field(None, max_length=80)
    base_url: str | None = Field(None, max_length=300)
    auth_method: str | None = None
    adapter: str | None = None
    credential: str | None = Field(None, max_length=500)          # write-only; never returned
    clear_credential: bool = False
    timeout_s: float | None = None
    poll_seconds: int | None = None
    mapping: dict | None = None
    operator_authorised: bool | None = None
    authorisation_note: str | None = Field(None, max_length=1000)
    ocpp_allowed_ids: list[str] | None = None


@router.put("/org/integrations/{iid}")
def update_integration(iid: int, body: IntegrationPatch, request: Request, db: Session = Depends(get_db)):
    cfg = _cfg(db, request, iid)
    try:
        ic.update(db, cfg, body.model_dump(exclude_unset=True))
    except ic.IntegrationError as e:
        db.rollback()
        raise _ic_error(e)
    audit.record(db, "integration.updated", org_id=cfg.org_id, actor=_user(request, db), target_type="integration", target_id=cfg.id,
                 data={"fields": ",".join(sorted(k for k in body.model_dump(exclude_unset=True) if k != "credential")),
                       "credential_changed": bool(body.credential) or body.clear_credential})
    db.commit()
    return ic.config_dict(db, cfg)


@router.post("/org/integrations/{iid}/test")
def test_integration(iid: int, request: Request, db: Session = Depends(get_db)):
    ok, retry = request.app.state.rate_limiter.hit(f"itest:{request.state.org_id}", 10, 600)
    if not ok:
        raise _err(429, f"Too many connection tests. Try again in {int(retry // 60) + 1} minute(s).", "rate_limited")
    cfg = _cfg(db, request, iid)
    try:
        res = ic.test_connection(db, cfg)
    except ic.IntegrationError as e:
        raise _ic_error(e)
    audit.record(db, "integration.tested", org_id=cfg.org_id, actor=_user(request, db), target_type="integration", target_id=cfg.id,
                 data={"ok": res["ok"], "state": res["state"]})
    db.commit()
    return {**res, "connection": ic.config_dict(db, cfg)}


@router.post("/org/integrations/{iid}/sync")
def sync_integration(iid: int, request: Request, db: Session = Depends(get_db)):
    cfg = _cfg(db, request, iid)
    try:
        res = ic.sync(db, cfg)
    except ic.IntegrationError as e:
        raise _ic_error(e)
    audit.record(db, "integration.synced", org_id=cfg.org_id, actor=_user(request, db), target_type="integration", target_id=cfg.id,
                 data={"accepted": res["accepted"]})
    db.commit()
    return {**res, "connection": ic.config_dict(db, cfg)}


@router.post("/org/integrations/{iid}/disable")
def disable_integration(iid: int, request: Request, db: Session = Depends(get_db)):
    cfg = _cfg(db, request, iid)
    ic.disable(cfg)
    audit.record(db, "integration.disabled", org_id=cfg.org_id, actor=_user(request, db), target_type="integration", target_id=cfg.id)
    db.commit()
    return ic.config_dict(db, cfg)


@router.post("/org/integrations/{iid}/enable")
def enable_integration(iid: int, request: Request, db: Session = Depends(get_db)):
    cfg = _cfg(db, request, iid)
    try:
        ic.enable(db, cfg)
    except (ic.IntegrationError, entitlements.EntitlementError) as e:
        db.rollback()
        raise _ic_error(e)
    audit.record(db, "integration.enabled", org_id=cfg.org_id, actor=_user(request, db), target_type="integration", target_id=cfg.id)
    db.commit()
    return ic.config_dict(db, cfg)
