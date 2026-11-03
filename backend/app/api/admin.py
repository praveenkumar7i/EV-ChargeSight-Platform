"""Platform administration (platform administrators only; created with scripts/manage.py, never hard-coded).
Every action is audited with the administrator and a reason."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import iso, wall_now
from ..db import get_system_db
from ..models import Charger, Ticket
from ..models_saas import (AuditEvent, EnterpriseEnquiry, IntegrationConfig, Membership, Organisation, Subscription,
                           SubscriptionEvent, SubscriptionPlan, User)
from ..services import audit, billing, entitlements
from ..services import integration_configs as ic
from ..services import orgs as org_svc

router = APIRouter(prefix="/api/admin")


def _admin(request: Request, db: Session) -> User:
    return db.get(User, request.state.user_id)


def _err(status: int, message: str, code: str = "invalid"):
    return HTTPException(status, {"code": code, "message": message})


def _org_row(db: Session, org: Organisation) -> dict:
    sub = billing.current(db, org.id)
    plan = db.get(SubscriptionPlan, sub.plan_key)
    owner = db.scalar(select(User).join(Membership, Membership.user_id == User.id)
                      .where(Membership.org_id == org.id, Membership.role == "owner", Membership.status == "active").limit(1))
    cfgs = list(db.scalars(select(IntegrationConfig).where(IntegrationConfig.org_id == org.id)))
    u = entitlements.usage(db, org.id)
    return {"id": org.id, "name": org.name, "code": org.code, "org_type": org_svc.ORG_TYPE_LABELS.get(org.org_type, org.org_type),
            "country": org.country, "created_at": iso(org.created_at), "is_system_demo": org.is_system_demo,
            "owner": owner.email if owner else None, "plan": plan.name if plan else sub.plan_key, "plan_key": sub.plan_key,
            "state": sub.state, "state_label": billing.STATE_LABELS[sub.state], "current_period_end": iso(sub.current_period_end),
            "suspended_reason": sub.suspended_reason or None, "usage": u,
            "data_modes": {"demo": org.demo_workspace_ready or org.is_system_demo,
                           "live_connections": [{"kind": c.kind, "name": c.name, "state": ic.effective_state(c)} for c in cfgs]},
            "onboarding_completed": bool((org.onboarding or {}).get("completed"))}


@router.get("/overview")
def overview(db: Session = Depends(get_system_db)):
    by_state = dict(db.execute(select(Subscription.state, func.count()).group_by(Subscription.state)).all())
    return {"organisations": db.scalar(select(func.count(Organisation.id))), "users": db.scalar(select(func.count(User.id))),
            "subscriptions_by_state": by_state, "enquiries_new": db.scalar(select(func.count(EnterpriseEnquiry.id))
                                                                             .where(EnterpriseEnquiry.status == "new")),
            "verified_connections": db.scalar(select(func.count(IntegrationConfig.id)).where(IntegrationConfig.state == "connected")),
            "note": "Platform view across organisations. Organisation data itself is not shown here."}


@router.get("/organisations")
def organisations(db: Session = Depends(get_system_db)):
    rows = [_org_row(db, o) for o in db.scalars(select(Organisation).order_by(Organisation.id))]
    db.commit()
    return rows


@router.get("/organisations/{oid}")
def organisation(oid: int, db: Session = Depends(get_system_db)):
    org = db.get(Organisation, oid)
    if org is None:
        raise _err(404, "Organisation not found.", "not_found")
    events = db.scalars(select(SubscriptionEvent).where(SubscriptionEvent.org_id == oid).order_by(SubscriptionEvent.id.desc()).limit(100))
    acct = db.scalars(select(AuditEvent).where(AuditEvent.org_id == oid).order_by(AuditEvent.id.desc()).limit(100))
    return {**_org_row(db, org),
            "subscription": billing.subscription_dict(db, billing.current(db, oid)),
            "subscription_events": [{"at": iso(e.at), "kind": e.kind, "from": e.from_state, "to": e.to_state, "actor": e.actor_label,
                                     "note": e.note} for e in events],
            "audit": [{"at": iso(a.at), "action": a.action, "actor": a.actor_label, "target": f"{a.target_type} {a.target_id}".strip()}
                      for a in acct]}


class ReasonIn(BaseModel):
    reason: str = Field(min_length=5, max_length=500)


@router.post("/organisations/{oid}/suspend")
def suspend(oid: int, body: ReasonIn, request: Request, db: Session = Depends(get_system_db)):
    org = db.get(Organisation, oid)
    if org is None:
        raise _err(404, "Organisation not found.", "not_found")
    sub = billing.current(db, oid)
    if sub.state == "suspended":
        raise _err(409, "Already suspended.", "already")
    admin = _admin(request, db)
    try:
        billing.transition(db, sub, "suspended", kind="suspended_by_admin", actor=admin, note=body.reason)
    except billing.BillingError as e:
        raise _err(e.status, str(e), e.code)
    sub.suspended_reason = body.reason
    audit.record(db, "admin.org_suspended", org_id=oid, actor=admin, target_type="organisation", target_id=oid, data={"reason": body.reason})
    db.commit()
    return _org_row(db, org)


@router.post("/organisations/{oid}/reactivate")
def reactivate(oid: int, body: ReasonIn, request: Request, db: Session = Depends(get_system_db)):
    org = db.get(Organisation, oid)
    if org is None:
        raise _err(404, "Organisation not found.", "not_found")
    sub = billing.current(db, oid)
    if sub.state != "suspended":
        raise _err(409, "The organisation is not suspended.", "not_suspended")
    admin = _admin(request, db)
    target = sub.prev_state if sub.prev_state in billing.ALLOWED["suspended"] else "demo"
    if target in ("active", "trial") and sub.current_period_end and sub.current_period_end <= wall_now():
        target = "expired"
    billing.transition(db, sub, target, kind="reactivated_by_admin", actor=admin, note=body.reason)
    sub.suspended_reason = ""
    audit.record(db, "admin.org_reactivated", org_id=oid, actor=admin, target_type="organisation", target_id=oid, data={"reason": body.reason})
    billing.apply_due(db, sub)
    db.commit()
    return _org_row(db, org)


class AdminSubIn(BaseModel):
    action: str = Field(pattern="^(grant_trial|assign_enterprise|set_demo)$")
    reason: str = Field(min_length=5, max_length=500)
    trial_days: int | None = Field(None, ge=1, le=60)
    limits: dict | None = None                    # Enterprise: negotiated chargers / users / integrations
    monthly_price_paise: int | None = Field(None, ge=0, le=10**10)
    months: int | None = None


@router.post("/organisations/{oid}/subscription")
def admin_subscription(oid: int, body: AdminSubIn, request: Request, db: Session = Depends(get_system_db)):
    org = db.get(Organisation, oid)
    if org is None:
        raise _err(404, "Organisation not found.", "not_found")
    sub = billing.current(db, oid)
    admin = _admin(request, db)
    now = wall_now()
    try:
        if body.action == "grant_trial":
            days = body.trial_days or int(billing.policy(db)["trial_days"])
            sub.plan_key = "growth"
            sub.billing_months = 1
            sub.current_period_start, sub.current_period_end = now, now + timedelta(days=days)
            sub.auto_renew = False
            billing.transition(db, sub, "trial", kind="trial_granted", actor=admin, note=f"{days}-day Growth trial. {body.reason}")
        elif body.action == "assign_enterprise":
            lim = body.limits or {}
            clean = {k: int(lim[k]) for k in ("chargers", "users", "sites", "integrations") if lim.get(k) is not None}
            if set(clean) != {"chargers", "users", "sites", "integrations"} or min(clean.values()) < 0:
                raise _err(422, "Enterprise needs negotiated limits for chargers, users, sites and integrations (no unlimited plans).")
            months = body.months or 12
            if months not in billing.PERIODS:
                raise _err(422, "Billing period must be 1, 6 or 12 months.")
            sub.plan_key = "enterprise"
            sub.custom_limits = clean
            sub.custom_monthly_price_paise = body.monthly_price_paise
            sub.billing_months = months
            sub.current_period_start, sub.current_period_end = now, billing.add_months(now, months)
            sub.auto_renew, sub.cancel_at_period_end = True, False
            billing.transition(db, sub, "active", kind="enterprise_assigned", actor=admin,
                               note=f"Enterprise contract recorded by a platform administrator (DEMO). {body.reason}",
                               data={"limits": clean, "monthly_price_paise": body.monthly_price_paise})
        else:
            sub.plan_key = "demo"
            sub.custom_limits, sub.custom_monthly_price_paise = ({} if not org.is_system_demo else sub.custom_limits), None
            sub.current_period_end = None
            billing.transition(db, sub, "demo", kind="set_demo_by_admin", actor=admin, note=body.reason)
    except billing.BillingError as e:
        db.rollback()
        raise _err(e.status, str(e), e.code)
    entitlements.invalidate(oid)
    audit.record(db, f"admin.subscription_{body.action}", org_id=oid, actor=admin, target_type="subscription", target_id=sub.id,
                 data={"reason": body.reason})
    db.commit()
    return billing.subscription_dict(db, sub)


class TimeIn(BaseModel):
    action: str = Field(pattern="^(reach_renewal_date|end_grace_period|expire_checkouts)$")
    reason: str = Field(min_length=5, max_length=500)


@router.post("/organisations/{oid}/simulate-time")
def simulate_time(oid: int, body: TimeIn, request: Request, db: Session = Depends(get_system_db)):
    """Demonstration aid: move a subscription's dates to now so expiry and grace behaviour can be shown live.
    Recorded as an administrator action; no payment state is invented."""
    org = db.get(Organisation, oid)
    if org is None:
        raise _err(404, "Organisation not found.", "not_found")
    sub = billing.current(db, oid)
    admin = _admin(request, db)
    now = wall_now()
    if body.action == "reach_renewal_date":
        if sub.state not in ("active", "trial"):
            raise _err(409, "Only an active or trial subscription has a renewal date to reach.")
        sub.current_period_end = now - timedelta(seconds=1)
    elif body.action == "end_grace_period":
        if sub.state != "past_due":
            raise _err(409, "Only a past-due subscription has a grace period.")
        sub.grace_until = now - timedelta(seconds=1)
    else:
        from ..models_saas import DemoCheckout
        for c in db.scalars(select(DemoCheckout).where(DemoCheckout.org_id == oid, DemoCheckout.status == "pending")):
            c.expires_at = now - timedelta(seconds=1)
    billing.event(db, sub, "admin_time_simulation", actor=admin, note=f"{body.action}: {body.reason}")
    done = billing.apply_due(db, sub, now)
    entitlements.invalidate(oid)
    audit.record(db, "admin.simulate_time", org_id=oid, actor=admin, target_type="subscription", target_id=sub.id,
                 data={"action": body.action, "result": ",".join(done)})
    db.commit()
    return {"applied": done, "subscription": billing.subscription_dict(db, sub)}


@router.get("/plans")
def plans(db: Session = Depends(get_system_db)):
    pol = billing.policy(db)
    return {"plans": [billing.plan_dict(p, pol) for p in billing.catalogue(db, include_inactive=True)], "policy": pol,
            "feature_labels": billing.FEATURE_LABELS}


class PlanIn(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=60)
    tagline: str | None = Field(None, max_length=200)
    monthly_price_paise: int | None = Field(None, ge=0, le=10**10)
    limits: dict | None = None
    features: list[str] | None = None
    highlights: list[str] | None = None
    support: str | None = Field(None, max_length=200)
    active: bool | None = None
    recommended: bool | None = None
    reason: str = Field(min_length=5, max_length=500)


@router.put("/plans/{key}")
def update_plan(key: str, body: PlanIn, request: Request, db: Session = Depends(get_system_db)):
    p = db.get(SubscriptionPlan, key)
    if p is None:
        raise _err(404, "Unknown plan.", "not_found")
    data = body.model_dump(exclude_unset=True, exclude={"reason"})
    if "features" in data:
        bad = [f for f in data["features"] if f not in billing.FEATURE_LABELS]
        if bad:
            raise _err(422, f"Unknown feature(s): {', '.join(bad)}")
    if "limits" in data:
        lim = data["limits"]
        if set(lim) - {"chargers", "users", "sites", "integrations"} or any(v is not None and (not isinstance(v, int) or v < 0) for v in lim.values()):
            raise _err(422, "Limits must be chargers / users / sites / integrations as non-negative integers (null = negotiated).")
    if key == "enterprise" and data.get("monthly_price_paise") is not None:
        raise _err(422, "Enterprise is priced per contract.")
    if key == "demo" and data.get("monthly_price_paise"):
        raise _err(422, "The Demo / Sandbox plan is free.")
    before = {k: getattr(p, k) for k in data}
    for k, v in data.items():
        setattr(p, k, v)
    p.updated_at = wall_now()
    entitlements.invalidate()
    audit.record(db, "admin.plan_updated", actor=_admin(request, db), target_type="plan", target_id=key,
                 data={"reason": body.reason, "fields": ",".join(sorted(data)), "before": str(before)[:400]})
    db.commit()
    return billing.plan_dict(p, billing.policy(db))


class PolicyIn(BaseModel):
    discount_bps: dict | None = None
    grace_days: int | None = Field(None, ge=0, le=60)
    checkout_ttl_min: int | None = Field(None, ge=5, le=1440)
    trial_days: int | None = Field(None, ge=1, le=60)
    reason: str = Field(min_length=5, max_length=500)


@router.put("/billing-policy")
def update_policy(body: PolicyIn, request: Request, db: Session = Depends(get_system_db)):
    from ..services import settings_store

    pol = billing.policy(db)
    data = body.model_dump(exclude_unset=True, exclude={"reason"})
    if "discount_bps" in data:
        d = {str(k): int(v) for k, v in data["discount_bps"].items()}
        if set(d) - {"1", "6", "12"} or any(not (0 <= v <= 5000) for v in d.values()):
            raise _err(422, "Discounts are basis points (0-5000) for periods 1, 6 and 12.")
        data["discount_bps"] = {**pol["discount_bps"], **d}
    stored = {k: v for k, v in {**pol, **data}.items() if k in billing.DEFAULT_POLICY}
    settings_store.put(db, "billing_policy", stored)
    audit.record(db, "admin.billing_policy_updated", actor=_admin(request, db), target_type="policy", target_id="billing",
                 data={"reason": body.reason, "fields": ",".join(sorted(data))})
    db.commit()
    return billing.policy(db)


@router.get("/enquiries")
def enquiries(db: Session = Depends(get_system_db)):
    return [{"id": e.id, "reference": f"ENQ-{e.id:05d}", "created_at": iso(e.created_at), "kind": e.kind, "org_name": e.org_name,
             "contact_name": e.contact_name, "contact_email": e.contact_email, "phone": e.phone, "charger_count": e.charger_count,
             "site_count": e.site_count, "integration_requirements": e.integration_requirements, "message": e.message,
             "status": e.status} for e in db.scalars(select(EnterpriseEnquiry).order_by(EnterpriseEnquiry.id.desc()).limit(200))]


@router.get("/events")
def events(db: Session = Depends(get_system_db)):
    acc = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(300))
    return [{"at": iso(a.at), "org_id": a.org_id, "action": a.action, "actor": a.actor_label,
             "target": f"{a.target_type} {a.target_id}".strip()} for a in acc]
