"""Organisations (tenants), roles and permissions, and demo-workspace provisioning."""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import wall_now
from ..models_saas import Membership, Organisation, Subscription, User
from ..tenancy import LEGACY_ORG_ID, as_org

ORG_TYPE_LABELS = {"cpo": "Charging Point Operator", "fleet_operator": "EV fleet operator",
                   "service_provider": "Charging infrastructure service provider", "site_owner": "Property / site owner",
                   "other": "Other"}
ROLE_LABELS = {"owner": "Organisation Owner", "admin": "Organisation Admin", "ops_manager": "Operations Manager",
               "technician": "Technician", "viewer": "Read-only Viewer"}

# Least-privilege permission matrix, enforced in the backend (security/guard.py maps every route).
PERMISSIONS = {
    "read.ops":          {"owner", "admin", "ops_manager", "technician", "viewer"},   # dashboard, chargers, tickets, map
    "read.analysis":     {"owner", "admin", "ops_manager", "viewer"},                 # analytics, planning, reports, lab, validation
    "compute":           {"owner", "admin", "ops_manager", "viewer"},                 # what-if calculations that store nothing
    "tickets.raise":     {"owner", "admin", "ops_manager", "technician"},
    "tickets.work":      {"owner", "admin", "ops_manager", "technician"},             # technicians: only tickets assigned to them
    "dispatch":          {"owner", "admin", "ops_manager"},
    "technicians.manage": {"owner", "admin", "ops_manager"},
    "simulator.control": {"owner", "admin", "ops_manager"},
    "planning.manage":   {"owner", "admin", "ops_manager"},
    "validation.manage": {"owner", "admin", "ops_manager"},
    "settings.manage":   {"owner", "admin"},                                          # priority weights, SLA, assignment policy
    "integrations.manage": {"owner", "admin"},
    "users.manage":      {"owner", "admin"},
    "org.manage":        {"owner", "admin"},
    "billing.view":      {"owner", "admin"},
    "billing.manage":    {"owner"},
    "platform.admin":    set(),                                                       # platform administrators only
}


LEGACY_FEATURES = ("integrations", "data_import", "reports_expanded", "admin_enhanced")
LEGACY_LIMITS = {"chargers": 500, "users": 25, "sites": 100, "integrations": 5}


def can(role: str | None, permission: str) -> bool:
    return bool(role) and role in PERMISSIONS.get(permission, set())


def permissions_for(role: str | None) -> list[str]:
    return sorted(p for p, roles in PERMISSIONS.items() if role in roles)


# --------------------------------------------------------------------------- platform bootstrap
def ensure_platform(db: Session) -> Organisation:
    """Plan catalogue + the explicit system demo workspace (organisation 1) that owns the prototype data."""
    from . import billing

    billing.ensure_catalogue(db)
    org = db.get(Organisation, LEGACY_ORG_ID)
    if org is None:
        org = Organisation(id=LEGACY_ORG_ID, name="ChargeSight Demo Workspace", code="DEMO", org_type="other",
                           country="India", is_system_demo=True, demo_workspace_ready=True,
                           onboarding={"completed": True, "data_mode": "demo", "note": "System demo workspace"},
                           created_at=wall_now())
        db.add(org)
        db.flush()
    sub = db.scalar(select(Subscription).where(Subscription.org_id == LEGACY_ORG_ID))
    if sub is None:
        sub = billing.create_subscription(db, org, "demo", note="System demo workspace (prototype data before v9)")
    # The system demo workspace keeps every capability the prototype had before v9 (.env operator API, CSV import,
    # OCPP, printable summary, audit log) with generous contract limits. Applied idempotently on every start.
    want_features = sorted(set(sub.custom_features or []) | set(LEGACY_FEATURES))
    want_limits = {**LEGACY_LIMITS, **{k: v for k, v in (sub.custom_limits or {}).items() if v is not None}}
    if sub.custom_features != want_features:
        sub.custom_features = want_features
    if sub.custom_limits != want_limits:
        sub.custom_limits = want_limits
    return org


# --------------------------------------------------------------------------- organisations
def make_code(db: Session, name: str) -> str:
    letters = re.sub(r"[^A-Z]", "", (name or "").upper())[:4] or "ORG"
    n = (db.scalar(select(func.max(Organisation.id))) or 1) + 1
    code = f"{letters}{n}"
    while db.scalar(select(Organisation.id).where(Organisation.code == code)) is not None:
        n += 1
        code = f"{letters}{n}"
    return code[:12]


def create_org(db: Session, *, name: str, org_type: str, country: str, owner: User, contact_phone: str = "",
               website: str = "") -> Organisation:
    """New tenant: organisation, owner membership, Demo/Sandbox subscription and default settings.
    No existing prototype data is attached; a private simulated fleet is created only when the owner
    chooses Demo mode in onboarding."""
    from . import billing
    from .seed import seed_reference_data

    ensure_platform(db)
    org = Organisation(name=name.strip()[:120], code=make_code(db, name), org_type=org_type, country=country.strip()[:60] or "India",
                       contact_phone=contact_phone[:40], website=website[:200], primary_contact_name=owner.full_name,
                       primary_contact_email=owner.email, onboarding={"step": 1, "completed": False}, created_at=wall_now(),
                       created_by_user_id=owner.id)
    db.add(org)
    db.flush()
    db.add(Membership(org_id=org.id, user_id=owner.id, role="owner", status="active"))
    billing.create_subscription(db, org, "demo", actor=owner, note="Organisation registered; Demo / Sandbox plan")
    with as_org(db, org.id):
        seed_reference_data(db, with_fleet=False)      # simulator state, settings, connector health; no fleet
    db.flush()
    return org


def provision_demo_workspace(db: Session, org: Organisation) -> dict:
    """Private simulated fleet for this organisation only: 6 demo sites, 40 SIMULATED chargers, 4 DEMO
    technicians and the 6-hour scripted demo history. Idempotent."""
    from .demo_seed import build_demo, is_seeded

    from .demo_seed import has_demo_fleet

    with as_org(db, org.id):
        if org.demo_workspace_ready and is_seeded(db) and has_demo_fleet(db):
            return {"created": False}
        info = build_demo(db, commit_every=240)
    org.demo_workspace_ready = True
    return {"created": True, **info}


def membership(db: Session, user_id: int, org_id: int) -> Membership | None:
    return db.scalar(select(Membership).where(Membership.user_id == user_id, Membership.org_id == org_id,
                                              Membership.status == "active"))


def memberships_of(db: Session, user_id: int) -> list[Membership]:
    return list(db.scalars(select(Membership).where(Membership.user_id == user_id, Membership.status == "active")
                           .order_by(Membership.id)))


def org_dict(org: Organisation) -> dict:
    from ..clock import iso

    return {"id": org.id, "name": org.name, "code": org.code, "org_type": org.org_type,
            "org_type_label": ORG_TYPE_LABELS.get(org.org_type, org.org_type), "country": org.country,
            "contact_phone": org.contact_phone, "website": org.website, "billing_address": org.billing_address,
            "tax_id": org.tax_id, "primary_contact_name": org.primary_contact_name,
            "primary_contact_email": org.primary_contact_email, "sites_estimate": org.sites_estimate,
            "chargers_estimate": org.chargers_estimate, "is_system_demo": org.is_system_demo,
            "demo_workspace_ready": org.demo_workspace_ready, "created_at": iso(org.created_at),
            "workspace_kind": org.workspace_kind or "customer",
            "preferences": org.preferences or {}}
