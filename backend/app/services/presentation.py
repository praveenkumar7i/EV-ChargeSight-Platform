"""v9.2 Presentation Demo: a separate workspace per user that holds only SIMULATED synthetic data.

* "My Fleet" is the operator's own organisation. Nothing synthetic is ever added to it automatically.
* "Presentation Demo" is a separate organisation (``workspace_kind = "presentation"``) owned by the user who
  opened it. It is built by the existing synthetic generator (``demo_seed.build_demo``): 6 demo sites,
  40 SIMULATED chargers, 4 fictional technicians, 6 simulated hours of history, 8 scripted tickets in every
  workflow state, priority factors, the event timeline, two sample policy experiments and two sample
  investment sites.
* Because it is its own organisation, the normal tenant filter keeps its records apart from every customer
  workspace; nothing is copied from any other organisation.
* Opening it is idempotent: the same workspace is reused, and it is rebuilt only if its fleet is missing
  (e.g. an interrupted build or a misleading ``demo_seeded`` flag). Imported or live records are never deleted.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import wall_now
from ..models_saas import Membership, Organisation, User
from ..tenancy import as_org

PRESENTATION_NAME = "Presentation Demo (SIMULATED)"


def is_demo_workspace(org: Organisation) -> bool:
    """Workspaces where the SIMULATED demo dataset may be (re)built: the system demo workspace, a Presentation
    Demo, or a workspace whose owner explicitly chose Demo mode during onboarding."""
    return bool(org.is_system_demo or org.workspace_kind == "presentation" or org.demo_workspace_ready)


def find_for_user(db: Session, user_id: int) -> Organisation | None:
    return db.scalar(select(Organisation).where(Organisation.workspace_kind == "presentation",
                                                Organisation.presentation_owner_user_id == user_id)
                     .order_by(Organisation.id).limit(1))


def home_org_for_user(db: Session, user_id: int) -> Organisation | None:
    """The user's own workspace (My Fleet): their first active membership that is not a Presentation Demo."""
    rows = db.execute(select(Organisation).join(Membership, Membership.org_id == Organisation.id)
                      .where(Membership.user_id == user_id, Membership.status == "active",
                             Organisation.workspace_kind != "presentation")
                      .order_by(Membership.id)).scalars().all()
    return rows[0] if rows else None


def ensure_for_user(db: Session, user: User) -> tuple[Organisation, dict]:
    """Return the user's Presentation Demo workspace, creating and populating it if needed. Caller commits."""
    from . import billing
    from .demo_seed import has_demo_fleet
    from .orgs import ensure_platform, make_code, provision_demo_workspace
    from .seed import seed_reference_data

    org = find_for_user(db, user.id)
    created_org = False
    if org is None:
        ensure_platform(db)
        org = Organisation(name=PRESENTATION_NAME, code=make_code(db, "PRESENTATION"), org_type="other", country="India",
                           primary_contact_name=user.full_name, primary_contact_email=user.email,
                           onboarding={"step": 6, "completed": True, "data_mode": "demo",
                                       "note": "Presentation Demo: SIMULATED synthetic data only"},
                           preferences={"alerts": "in_app"}, created_at=wall_now(), created_by_user_id=user.id,
                           workspace_kind="presentation", presentation_owner_user_id=user.id)
        db.add(org)
        db.flush()
        db.add(Membership(org_id=org.id, user_id=user.id, role="owner", status="active"))
        billing.create_subscription(db, org, "demo", actor=user, note="Presentation Demo workspace (SIMULATED data only)")
        with as_org(db, org.id):
            seed_reference_data(db, with_fleet=False)
        db.flush()
        created_org = True
    with as_org(db, org.id):
        fleet_ok = has_demo_fleet(db)
    if not fleet_ok:
        org.demo_workspace_ready = False         # a flag without the records it describes is repaired, not trusted
    info = provision_demo_workspace(db, org)
    return org, {"created_workspace": created_org, "built_dataset": bool(info.get("created")),
                 "tickets": info.get("tickets")}
