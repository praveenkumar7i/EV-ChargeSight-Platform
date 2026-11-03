"""Plan entitlements and limits, enforced on the server.

* Features come from the organisation's plan (plus contract additions for Enterprise).
* Limits: managed chargers, managed sites, organisation users (members + pending invitations), configured integrations.
  "Managed chargers" are the organisation's real chargers (imported, operator API, OCPP). SIMULATED demo
  chargers and the local OCPP test simulator never count and are never presented as managed chargers.
* Access: full while Demo, Trial, Pending checkout, Active or Past due (grace period); read-only when
  Expired, Cancelled or Suspended. Read-only keeps every record readable (including safety history and
  billing) and still lets existing tickets be progressed and closed, so safety work is never blocked;
  other changes and paid features pause until the subscription is reactivated. Nothing is deleted.
* The plan is always read from the database. A client cannot change it by sending a plan name or state.
"""
from __future__ import annotations

import threading
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models_saas import IntegrationConfig, Membership, OneTimeToken, SubscriptionPlan


class EntitlementError(Exception):
    status = 403
    code = "entitlement"

    def __init__(self, message: str, **data):
        super().__init__(message)
        self.data = data

    def detail(self) -> dict:
        return {"code": self.code, "message": str(self), "upgrade": True, **self.data}


class LimitReached(EntitlementError):
    code = "plan_limit"


class FeatureNotInPlan(EntitlementError):
    code = "plan_feature"


class ReadOnlyAccess(EntitlementError):
    code = "subscription_read_only"


LIMIT_LABELS = {"chargers": "managed-charger", "users": "user", "integrations": "integration", "sites": "site"}
LIMIT_KEYS = ("chargers", "users", "sites", "integrations")


def is_placeholder_site_code(code: str) -> bool:
    """Grouping sites created automatically for rows without a site (UNASSIGNED-*) or for OCPP charge
    points (OCPP) are not real locations and never count as managed sites."""
    c = (code or "").upper()
    return "UNASSIGNED-" in c or c == "OCPP" or c.endswith("-OCPP")

_cache: dict[int, tuple[float, dict]] = {}
_lock = threading.Lock()
TTL_S = 5.0


def invalidate(org_id: int | None = None) -> None:
    with _lock:
        if org_id is None:
            _cache.clear()
        else:
            _cache.pop(org_id, None)


def usage(db: Session, org_id: int) -> dict:
    from ..models import Charger
    from ..tenancy import ALL_TENANTS

    opts = {ALL_TENANTS: True}
    chargers = db.scalar(select(func.count(Charger.id)).where(Charger.org_id == org_id, Charger.source != "simulation",
                                                              Charger.test_traffic.is_not(True)).execution_options(**opts)) or 0
    simulated = db.scalar(select(func.count(Charger.id)).where(Charger.org_id == org_id, Charger.source == "simulation")
                          .execution_options(**opts)) or 0
    from ..models import Site

    site_codes = db.scalars(select(Site.code).where(Site.org_id == org_id, Site.source != "simulation").execution_options(**opts)).all()
    sites = sum(1 for c in site_codes if not is_placeholder_site_code(c))
    members = db.scalar(select(func.count(Membership.id)).where(Membership.org_id == org_id, Membership.status == "active")) or 0
    from ..clock import wall_now

    invites = db.scalar(select(func.count(OneTimeToken.id)).where(
        OneTimeToken.org_id == org_id, OneTimeToken.purpose == "invite", OneTimeToken.used_at.is_(None),
        OneTimeToken.revoked_at.is_(None), OneTimeToken.expires_at > wall_now())) or 0
    integrations = db.scalar(select(func.count(IntegrationConfig.id)).where(
        IntegrationConfig.org_id == org_id, IntegrationConfig.state != "disabled")) or 0
    return {"chargers": chargers, "simulated_chargers": simulated, "sites": sites, "users": members + invites, "members": members,
            "pending_invites": invites, "integrations": integrations}


def resolve(db: Session, org_id: int, *, fresh: bool = False) -> dict:
    """Entitlements of an organisation (cached for a few seconds; invalidated on subscription changes)."""
    now = time.monotonic()
    if not fresh:
        with _lock:
            hit = _cache.get(org_id)
        if hit and now - hit[0] < TTL_S:
            return hit[1]
    from . import billing

    sub = billing.current(db, org_id)
    plan = db.get(SubscriptionPlan, sub.plan_key)
    limits = dict(plan.limits or {}) if plan else {}
    limits.setdefault("sites", None if plan and plan.key == "enterprise" else limits.get("sites", 0))
    limits.update({k: v for k, v in (sub.custom_limits or {}).items() if k in LIMIT_KEYS})
    features = sorted(set(plan.features if plan else []) | set(sub.custom_features or []))
    access = "full" if sub.state in billing.FULL_ACCESS else "read_only"
    ent = {"org_id": org_id, "plan_key": sub.plan_key, "plan_name": plan.name if plan else sub.plan_key,
           "state": sub.state, "access": access, "features": features, "limits": limits,
           "negotiated": [k for k, v in limits.items() if v is None]}
    with _lock:
        _cache[org_id] = (now, ent)
    return ent


def has_feature(db: Session, org_id: int, feature: str) -> bool:
    ent = resolve(db, org_id)
    return feature in ent["features"] and ent["access"] == "full"


def require_feature(db: Session, org_id: int, feature: str) -> None:
    from .billing import FEATURE_LABELS

    ent = resolve(db, org_id)
    if feature not in ent["features"]:
        raise FeatureNotInPlan(f"{FEATURE_LABELS.get(feature, feature)} is not included in your {ent['plan_name']} plan. "
                               "Upgrade your subscription or contact us to discuss it.", feature=feature, plan=ent["plan_key"])


def require_full_access(db: Session, org_id: int) -> None:
    from .billing import STATE_LABELS

    ent = resolve(db, org_id)
    if ent["access"] != "full":
        raise ReadOnlyAccess(f"Your subscription is {STATE_LABELS.get(ent['state'], ent['state']).lower()}, so the workspace is "
                             "read-only: your data stays available, but changes are paused. Reactivate the subscription in "
                             "Subscription & Billing (owners) to continue.", state=ent["state"])


def check_limit(db: Session, org_id: int, key: str, adding: int = 1) -> None:
    ent = resolve(db, org_id, fresh=True)
    limit = ent["limits"].get(key)
    if limit is None:
        if ent["plan_key"] == "enterprise":
            return          # negotiated per contract; a platform administrator records the agreed limit
        return
    used = usage(db, org_id)[key]
    if used + adding > limit:
        label = LIMIT_LABELS[key]
        if key in ("chargers", "sites") and limit == 0 and ent["plan_key"] == "demo":
            msg = (f"Your Demo / Sandbox plan uses the simulated fleet only and cannot register real {label}s. "
                   "Choose Starter or above to import or connect real chargers and sites.")
        elif key == "integrations" and limit == 0:
            msg = (f"Configured integrations are not included in your {ent['plan_name']} plan (CSV imports are). "
                   "Upgrade to Growth or above to add an operator API or OCPP connection.")
        else:
            msg = (f"You have reached your plan's {limit}-{label} limit ({used} in use). Upgrade your subscription or "
                   "contact us to discuss a higher limit.")
        raise LimitReached(msg, limit=limit, used=used, resource=key, plan=ent["plan_key"])


def check_new_managed_charger(db: Session) -> None:
    from ..tenancy import effective_org

    check_limit(db, effective_org(db), "chargers")


def check_new_managed_site(db: Session, code: str) -> None:
    from ..tenancy import effective_org

    if not is_placeholder_site_code(code):
        check_limit(db, effective_org(db), "sites")


def summary(db: Session, org_id: int) -> dict:
    from .billing import FEATURE_LABELS

    ent = resolve(db, org_id, fresh=True)
    u = usage(db, org_id)
    return {**ent, "usage": u, "feature_labels": {f: FEATURE_LABELS.get(f, f) for f in FEATURE_LABELS},
            "managed_charger_note": "Managed chargers and sites are your real imported, registered, operator-API and OCPP "
                                    "records. Simulated demo chargers and sites, the local OCPP test simulator and automatic "
                                    "grouping sites (UNASSIGNED, OCPP) are not counted."}
