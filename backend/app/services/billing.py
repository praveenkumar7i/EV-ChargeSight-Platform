"""Subscription plans, server-side price calculation, subscription lifecycle and the DEMO checkout.

Everything here is a demonstration of a subscription business, not a payment system:
* prices are illustrative PME-prototype assumptions (INR), editable centrally by a platform administrator;
* checkout is simulated through payments.DemoPaymentProvider: no card or bank data is collected, no
  money moves, and receipts are labelled DEMO and are not tax invoices;
* all totals are calculated here, on the server; the browser only ever sends a plan key and a period.

Subscription states and allowed transitions (`ALLOWED`) are enforced by `transition()`, which also
writes a SubscriptionEvent for every change.
"""
from __future__ import annotations

import calendar
import secrets
from decimal import ROUND_HALF_UP, Decimal
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import iso, wall_now
from ..models_saas import DemoCheckout, DemoReceipt, Organisation, Subscription, SubscriptionEvent, SubscriptionPlan
from . import settings_store

PERIODS = (1, 6, 12)
PERIOD_LABELS = {1: "Monthly", 6: "6 months", 12: "12 months"}

# --------------------------------------------------------------------------- catalogue (ASSUMPTIONS)
# Proposed PME-prototype prices (v9.1 catalogue), not validated market prices. This list only SEEDS the
# subscription_plans table; the table is the authoritative catalogue and a platform administrator edits it.
CATALOGUE_VERSION = "9.1"
FEATURE_LABELS = {
    "fleet_dashboard": "Fleet dashboard and uptime",
    "tickets": "Maintenance tickets and Raise a Ticket",
    "priority_engine": "Explainable fault prioritisation",
    "manual_assignment": "Technician registry and manual assignment",
    "auto_assignment": "Automatic technician assignment",
    "sla_tracking": "Basic SLA deadlines and overdue tracking",
    "sla_escalation": "SLA escalation and dispatch-needed alerts",
    "analytics_basic": "Basic analytics (faults, power, energy trends)",
    "analytics_advanced": "Advanced analytics (maintenance performance, site comparison)",
    "reports_standard": "Standard CSV exports",
    "reports_enhanced": "Maintenance performance reporting (downtime cost export)",
    "reports_expanded": "Expanded and historical reporting (printable operations summary)",
    "public_map": "Public charging-station map (Open Charge Map / OpenStreetMap listings)",
    "site_planning": "Site feasibility and charging-site planning",
    "policy_lab": "Repair policy tools (FIFO vs priority experiments)",
    "simulator": "Demo data: simulated fleet and scenarios",
    "data_import": "Supported data imports (operator CSV exports)",
    "integrations": "Configured integrations (operator API, OCPP)",
    "validation": "Validation workspace",
    "admin_enhanced": "Enhanced administration (organisation audit log)",
    "enterprise_admin": "Contract administration and custom onboarding",
    "audit_config": "Audit and retention configuration",
    "custom_reporting": "Custom reporting requirements",
}
_STARTER = ["fleet_dashboard", "tickets", "priority_engine", "manual_assignment", "sla_tracking", "analytics_basic",
            "reports_standard", "public_map", "simulator", "validation", "data_import"]
_GROWTH = _STARTER + ["integrations", "auto_assignment", "sla_escalation", "analytics_advanced", "reports_enhanced",
                      "site_planning", "policy_lab"]
_PROFESSIONAL = _GROWTH + ["reports_expanded", "admin_enhanced"]

DEFAULT_PLANS = [
    {"key": "demo", "name": "Demo / Sandbox", "monthly_price_paise": 0, "self_serve": False, "sort": 0, "recommended": False,
     "tagline": "Explore ChargeSight on a clearly labelled simulated fleet.",
     "target": "Evaluation, training and the PME presentation",
     "limits": {"chargers": 0, "users": 2, "sites": 0, "integrations": 0},
     "features": [f for f in _GROWTH if f not in ("integrations", "data_import")],
     "highlights": ["Simulated fleet only, labelled SIMULATED / DEMO", "Demo tickets and simulated technicians",
                    "Priority engine", "Sample analytics and reports", "Demo site planning",
                    "No operator credentials required", "No real operator telemetry is implied"],
     "support": "Self-service documentation"},
    {"key": "starter", "name": "Starter", "monthly_price_paise": 499_00, "self_serve": True, "sort": 1, "recommended": True,
     "tagline": "For a first site and a handful of chargers.", "target": "Small operators starting out",
     "limits": {"chargers": 5, "users": 2, "sites": 1, "integrations": 0},
     "features": list(_STARTER),
     "highlights": ["Up to 5 managed chargers", "Up to 2 organisation users", "1 site", "Basic fleet dashboard",
                    "Maintenance tickets and priority engine", "Manual technician assignment", "Basic SLA tracking",
                    "Basic analytics and CSV exports", "Public charging-station map", "Demo data and supported data imports"],
     "support": "Community / self-service support"},
    {"key": "growth", "name": "Growth", "monthly_price_paise": 1_499_00, "self_serve": True, "sort": 2, "recommended": False,
     "tagline": "For small networks across a few sites.", "target": "Growing networks with up to 3 sites",
     "limits": {"chargers": 25, "users": 5, "sites": 3, "integrations": 3},
     "features": list(_GROWTH),
     "highlights": ["Up to 25 managed chargers", "Up to 5 organisation users", "Up to 3 sites", "Everything in Starter",
                    "Automatic technician assignment", "SLA escalation", "Advanced analytics and maintenance reporting",
                    "Site feasibility and repair policy tools",
                    "Up to 3 configured integrations (subject to provider compatibility and operator authorisation)"],
     "support": "E-mail support (proposed service level)"},
    {"key": "professional", "name": "Professional", "monthly_price_paise": 4_999_00, "self_serve": True, "sort": 3,
     "recommended": False, "tagline": "For established multi-site operators.", "target": "Operators with up to 10 sites",
     "limits": {"chargers": 100, "users": 15, "sites": 10, "integrations": 10},
     "features": list(_PROFESSIONAL),
     "highlights": ["Up to 100 managed chargers", "Up to 15 organisation users", "Up to 10 sites", "Everything in Growth",
                    "Expanded reporting and operational analytics", "Up to 10 configured integrations",
                    "Enhanced administration (organisation audit log)", "Historical reporting"],
     "support": "Priority support (proposed service level)"},
    {"key": "enterprise", "name": "Enterprise", "monthly_price_paise": None, "self_serve": False, "sort": 4, "recommended": False,
     "tagline": "For large or multi-region charging networks.", "target": "Large networks with negotiated terms",
     "limits": {"chargers": None, "users": None, "sites": None, "integrations": None},
     "features": _PROFESSIONAL + ["enterprise_admin", "audit_config", "custom_reporting"],
     "highlights": ["Negotiated charger, user, site and integration limits", "Custom onboarding",
                    "Agreed support terms", "Audit and retention configuration", "Custom reporting requirements",
                    "Contract-specific features"],
     "support": "Agreed support terms (contract)"},
]
RECOMMENDED_NOTE = "Our recommended starting plan for small operators. A product recommendation, not a popularity claim."

# The v9.0 seed values. A plan row that still has exactly these values was never edited by an administrator
# and is moved to the v9.1 catalogue once; edited rows are left alone and reported (see upgrade_catalogue).
V9_0_PLAN_VALUES = {
    "demo": {"monthly_price_paise": 0, "limits": {"chargers": 0, "users": 3, "integrations": 0}},
    "starter": {"monthly_price_paise": 499900, "limits": {"chargers": 25, "users": 3, "integrations": 1}},
    "growth": {"monthly_price_paise": 1499900, "limits": {"chargers": 100, "users": 15, "integrations": 5}},
    "enterprise": {"monthly_price_paise": None, "limits": {"chargers": None, "users": None, "integrations": None}},
}
V9_0_FEATURES = {
    "starter": ["fleet_dashboard", "tickets", "priority_engine", "manual_assignment", "sla_tracking", "analytics_basic",
                "reports_standard", "public_map", "simulator", "validation", "integrations"],
}

DEFAULT_POLICY = {
    "discount_bps": {"1": 0, "6": 500, "12": 1000},     # 5 % for 6 months, 10 % for 12 months (prototype assumption)
    "grace_days": 7,                                     # past-due grace period before the subscription expires
    "checkout_ttl_min": 60,                              # a pending demo checkout expires after this
    "trial_days": 14,
    "currency": "INR",
    "tax_note": "Prices exclude GST. The demo checkout applies no tax and is not a tax invoice.",
}

# --------------------------------------------------------------------------- state machine
ALLOWED = {
    "demo": {"pending_checkout", "trial", "active", "suspended"},
    "trial": {"pending_checkout", "active", "expired", "suspended"},
    "pending_checkout": {"active", "demo", "trial", "expired", "cancelled", "suspended"},
    "active": {"active", "past_due", "cancelled", "suspended"},
    "past_due": {"active", "expired", "suspended"},
    "expired": {"pending_checkout", "active", "suspended"},
    "cancelled": {"pending_checkout", "active", "suspended"},
    "suspended": {"demo", "trial", "active", "past_due", "expired", "cancelled"},
}
FULL_ACCESS = ("demo", "trial", "pending_checkout", "active", "past_due")
READ_ONLY = ("expired", "cancelled", "suspended")
STATE_LABELS = {"demo": "Demo", "trial": "Trial", "pending_checkout": "Pending checkout", "active": "Active",
                "past_due": "Past due", "expired": "Expired", "cancelled": "Cancelled", "suspended": "Suspended"}


class BillingError(Exception):
    def __init__(self, message: str, status: int = 409, code: str = "billing"):
        super().__init__(message)
        self.status = status
        self.code = code
        self.field: str | None = None


# --------------------------------------------------------------------------- helpers
def format_inr(paise: int | None) -> str:
    """Indian digit grouping: 161989.20 -> ₹1,61,989.20."""
    if paise is None:
        return "Custom quotation"
    neg = paise < 0
    rupees, p = divmod(abs(int(paise)), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return f"{'-' if neg else ''}₹{s}.{p:02d}"


def round_half_up_div(n: int, d: int) -> int:
    return (2 * n + d) // (2 * d)


def add_months(dt: datetime, months: int) -> datetime:
    m = dt.month - 1 + months
    y, m = dt.year + m // 12, m % 12 + 1
    day = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=day)


def policy(db: Session) -> dict:
    stored = settings_store.get(db, "billing_policy", None) or {}
    out = {**DEFAULT_POLICY, **stored}
    out["discount_bps"] = {**DEFAULT_POLICY["discount_bps"], **(stored.get("discount_bps") or {})}
    return out


def plan_dict(p: SubscriptionPlan, pol: dict | None = None) -> dict:
    d = {"key": p.key, "name": p.name, "tagline": p.tagline, "target": p.target, "currency": p.currency,
         "monthly_price_paise": p.monthly_price_paise, "monthly_price": format_inr(p.monthly_price_paise),
         "custom_pricing": p.monthly_price_paise is None, "free": p.monthly_price_paise == 0,
         "limits": p.limits, "features": p.features, "highlights": p.highlights, "support": p.support,
         "self_serve": p.self_serve, "recommended": p.recommended,
         "recommended_note": RECOMMENDED_NOTE if p.recommended else None,
         "sort": p.sort, "active": p.active}
    if pol is not None and p.monthly_price_paise:
        d["quotes"] = {str(m): quote_for(p, m, pol) for m in PERIODS}
    return d


def ensure_catalogue(db: Session) -> None:
    """Insert missing default plans and apply the v9.1 catalogue update once (see upgrade_catalogue).
    Never overwrites a plan an administrator has edited."""
    existing = {p.key for p in db.scalars(select(SubscriptionPlan))}
    for d in DEFAULT_PLANS:
        if d["key"] not in existing:
            db.add(SubscriptionPlan(**{k: v for k, v in d.items()}))
    db.flush()
    if existing:
        upgrade_catalogue(db)


def upgrade_catalogue(db: Session) -> dict | None:
    """Idempotent, non-destructive move from the v9.0 seed catalogue to v9.1.

    * A plan row still holding exactly its v9.0 seed price and limits (never edited) gets the v9.1 values;
      an edited row is left unchanged and reported in the `plan_catalogue` setting.
    * Completed checkouts and DEMO receipts keep their historical amounts (they are snapshots; never rewritten).
    * Policy for subscriptions active on a changed plan: the purchased limits and features are kept
      ("grandfathered" as contract values on the subscription) until the next checkout (renewal, upgrade or
      reactivation), which then uses the current catalogue. Nothing is deleted.
    * A consistent database backup is written before any existing row is changed."""
    info = settings_store.get(db, "plan_catalogue", None) or {}
    if info.get("version") == CATALOGUE_VERSION:
        return None
    to_update, customised = [], []
    for key, old in V9_0_PLAN_VALUES.items():
        p = db.get(SubscriptionPlan, key)
        if p is None:
            continue
        if p.monthly_price_paise == old["monthly_price_paise"] and (p.limits or {}) == old["limits"]:
            to_update.append(p)
        else:
            new = next(d for d in DEFAULT_PLANS if d["key"] == key)
            if p.monthly_price_paise != new["monthly_price_paise"] or (p.limits or {}) != new["limits"]:
                customised.append(key)
    backup = None
    if to_update:
        from ..db import backup_sqlite

        db.flush()
        backup = backup_sqlite("before-v9-1-plan-catalogue")
        for p in to_update:
            old_limits, old_features = dict(p.limits or {}), list(p.features or [])
            for sub in db.scalars(select(Subscription).where(Subscription.plan_key == p.key,
                                                            Subscription.state.in_(("active", "past_due", "trial")))):
                if p.key in ("starter", "growth") and not sub.custom_limits:
                    sub.custom_limits = old_limits
                    sub.custom_features = sorted(set(old_features) - set(next(d for d in DEFAULT_PLANS if d["key"] == p.key)["features"]))
                    event(db, sub, "catalogue_grandfathered", note=(
                        f"The {p.name} plan changed in catalogue {CATALOGUE_VERSION}. Your purchased limits and features "
                        "stay in place until your next checkout (renewal, upgrade or reactivation)."),
                        data={"limits": old_limits})
            d = next(d for d in DEFAULT_PLANS if d["key"] == p.key)
            for k, v in d.items():
                if k != "key":
                    setattr(p, k, v)
            p.updated_at = wall_now()
    settings_store.put(db, "plan_catalogue", {"version": CATALOGUE_VERSION, "updated": [p.key for p in to_update],
                                              "left_customised": customised, "backup": str(backup) if backup else None,
                                              "at": iso(wall_now())})
    from .entitlements import invalidate

    invalidate()
    db.flush()
    return {"updated": [p.key for p in to_update], "customised": customised, "backup": str(backup) if backup else None}


def catalogue(db: Session, *, include_inactive: bool = False) -> list[SubscriptionPlan]:
    q = select(SubscriptionPlan).order_by(SubscriptionPlan.sort)
    if not include_inactive:
        q = q.where(SubscriptionPlan.active.is_(True))
    return list(db.scalars(q))


def get_plan(db: Session, key: str) -> SubscriptionPlan:
    p = db.get(SubscriptionPlan, key)
    if p is None or not p.active:
        raise BillingError(f"Unknown plan '{key}'.", 404, "unknown_plan")
    return p


# --------------------------------------------------------------------------- quotes (server-side only)
def _money(paise) -> Decimal:
    return (Decimal(int(paise)) / 100).quantize(Decimal("0.01"))


def _paise(rupees: Decimal) -> int:
    return int((rupees * 100).to_integral_value(rounding=ROUND_HALF_UP))


def quote_for(plan: SubscriptionPlan, months: int, pol: dict, *, monthly_price_paise: int | None = None,
              credit_paise: int = 0) -> dict:
    """Decimal arithmetic in rupees, rounded half-up to the paisa; amounts are returned as integer paise
    (exact) plus formatted strings. Browser-supplied amounts are never used."""
    if months not in PERIODS:
        raise BillingError("Billing period must be 1, 6 or 12 months.", 422, "bad_period")
    monthly = plan.monthly_price_paise if monthly_price_paise is None else monthly_price_paise
    if monthly is None:
        raise BillingError("Enterprise pricing is a custom quotation. Contact sales.", 422, "custom_pricing")
    bps = int(pol["discount_bps"].get(str(months), 0))
    rate = Decimal(bps) / Decimal(10_000)
    m = _money(monthly)
    sub_r = (m * months).quantize(Decimal("0.01"))
    disc_r = (sub_r * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    after_r = sub_r - disc_r
    subtotal, discount, after = _paise(sub_r), _paise(disc_r), _paise(after_r)
    credit = max(0, min(int(credit_paise), after))
    total = after - credit
    meq = _paise((after_r / months).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {
        "plan_key": plan.key, "plan_name": plan.name, "billing_months": months, "period_label": PERIOD_LABELS[months],
        "currency": "INR", "monthly_price_paise": monthly, "subtotal_paise": subtotal, "discount_bps": bps,
        "discount_percent": bps / 100, "discount_paise": discount, "credit_paise": credit, "total_paise": total,
        "monthly_equivalent_paise": meq,
        "amounts": {"monthly_price": str(m), "subtotal": str(sub_r), "discount": str(disc_r), "credit": str(_money(credit)),
                    "total": str(_money(total)), "monthly_equivalent": str(_money(meq))},
        "display": {"monthly_price": format_inr(monthly), "subtotal": format_inr(subtotal), "discount": format_inr(discount),
                    "credit": format_inr(credit), "total": format_inr(total), "monthly_equivalent": format_inr(meq)},
        "calculation": (f"{format_inr(monthly)} × {months} month{'s' if months > 1 else ''} = {format_inr(subtotal)}"
                        + (f"; − {bps / 100:g}% commitment discount {format_inr(discount)}" if discount else "")
                        + (f"; − unused-period credit {format_inr(credit)}" if credit else "")
                        + f"; total payable {format_inr(total)}"
                        + (f" (monthly equivalent {format_inr(meq)})" if months > 1 else "")),
        "tax": {"applied": False, "note": pol["tax_note"]},
        "tax_note": pol["tax_note"],
        "commitment_note": ("Monthly plans renew every month." if months == 1 else
                            f"A {months}-month commitment is billed in advance for the whole period. "
                            "Cancelling stops the next renewal; the current period is not refunded."),
    }


def quote(db: Session, plan_key: str, months: int) -> dict:
    return quote_for(get_plan(db, plan_key), months, policy(db))


# --------------------------------------------------------------------------- subscriptions
def subscription_for(db: Session, org_id: int) -> Subscription | None:
    return db.scalar(select(Subscription).where(Subscription.org_id == org_id))


def event(db: Session, sub: Subscription, kind: str, *, from_state=None, to_state=None, actor=None,
          actor_label: str | None = None, note: str = "", data: dict | None = None) -> SubscriptionEvent:
    ev = SubscriptionEvent(org_id=sub.org_id, at=wall_now(), kind=kind, from_state=from_state, to_state=to_state,
                           plan_key=sub.plan_key, actor_user_id=getattr(actor, "id", None),
                           actor_label=(actor_label or getattr(actor, "email", None) or "system")[:80], note=note,
                           data=data or {})
    db.add(ev)
    return ev


def transition(db: Session, sub: Subscription, to: str, *, kind: str, actor=None, actor_label: str | None = None,
               note: str = "", data: dict | None = None) -> None:
    frm = sub.state
    if to not in ALLOWED.get(frm, set()):
        raise BillingError(f"A subscription cannot move from {STATE_LABELS.get(frm, frm)} to {STATE_LABELS.get(to, to)}.",
                           409, "invalid_transition")
    if to != frm:
        sub.prev_state = frm
    sub.state = to
    sub.updated_at = wall_now()
    event(db, sub, kind, from_state=frm, to_state=to, actor=actor, actor_label=actor_label, note=note, data=data)
    from .entitlements import invalidate

    invalidate(sub.org_id)


def create_subscription(db: Session, org: Organisation, plan_key: str = "demo", *, state: str = "demo",
                        actor=None, note: str = "Organisation created") -> Subscription:
    now = wall_now()
    sub = Subscription(org_id=org.id, plan_key=plan_key, billing_months=1, state=state, started_at=now,
                       current_period_start=now, current_period_end=None, auto_renew=False, mode="DEMO", updated_at=now)
    db.add(sub)
    db.flush()
    event(db, sub, "created", to_state=state, actor=actor, note=note)
    return sub


def apply_due(db: Session, sub: Subscription, now: datetime | None = None) -> list[str]:
    """Apply time-based transitions that are due (idempotent). Returns the event kinds written."""
    now = now or wall_now()
    pol = policy(db)
    done = []
    for chk in db.scalars(select(DemoCheckout).where(DemoCheckout.org_id == sub.org_id, DemoCheckout.status == "pending",
                                                    DemoCheckout.expires_at <= now)):
        chk.status = "expired"
        event(db, sub, "checkout_expired", note=f"Demo checkout {chk.id} expired before completion.", data={"checkout": chk.id})
        done.append("checkout_expired")
    if sub.state == "pending_checkout" and not _open_checkout(db, sub.org_id):
        transition(db, sub, sub.prev_state if sub.prev_state in ALLOWED["pending_checkout"] else "demo",
                   kind="checkout_abandoned", note="No open checkout; previous state restored.")
        done.append("checkout_abandoned")
    if sub.state == "trial" and sub.current_period_end and now >= sub.current_period_end:
        transition(db, sub, "expired", kind="trial_ended", note="Trial period ended.")
        sub.ended_at = sub.current_period_end
        done.append("trial_ended")
    if sub.state == "active" and sub.current_period_end and now >= sub.current_period_end:
        if sub.cancel_at_period_end or not sub.auto_renew:
            transition(db, sub, "cancelled", kind="ended_at_period_end",
                       note="Renewal had been cancelled; the subscription ended at the end of the paid period.")
            sub.ended_at = sub.current_period_end
            done.append("cancelled")
        else:
            sub.grace_until = sub.current_period_end + timedelta(days=int(pol["grace_days"]))
            transition(db, sub, "past_due", kind="renewal_due",
                       note=("Renewal date reached. DEMO mode has no payment method on file and cannot charge one, "
                             f"so the subscription is past due until a demo renewal checkout is completed "
                             f"(grace period until {iso(sub.grace_until)})."))
            done.append("past_due")
    if sub.state == "past_due" and sub.grace_until and now >= sub.grace_until:
        transition(db, sub, "expired", kind="grace_ended",
                   note="Grace period ended without renewal. Data is kept and stays readable; changes are paused.")
        sub.ended_at = sub.grace_until
        done.append("expired")
    return done


def current(db: Session, org_id: int, now: datetime | None = None) -> Subscription:
    sub = subscription_for(db, org_id)
    if sub is None:
        org = db.get(Organisation, org_id)
        if org is None:
            raise BillingError("Unknown organisation.", 404, "unknown_org")
        sub = create_subscription(db, org)
    apply_due(db, sub, now)
    return sub


def _open_checkout(db: Session, org_id: int) -> DemoCheckout | None:
    return db.scalar(select(DemoCheckout).where(DemoCheckout.org_id == org_id, DemoCheckout.status == "pending")
                     .order_by(DemoCheckout.created_at.desc()).limit(1))


def effective_monthly_price(db: Session, sub: Subscription) -> int | None:
    if sub.custom_monthly_price_paise is not None:
        return sub.custom_monthly_price_paise
    p = db.get(SubscriptionPlan, sub.plan_key)
    return p.monthly_price_paise if p else None


def _last_paid_checkout(db: Session, sub: Subscription) -> DemoCheckout | None:
    if sub.current_period_start is None:
        return None
    return db.scalar(select(DemoCheckout).where(DemoCheckout.org_id == sub.org_id, DemoCheckout.status == "completed",
                                                DemoCheckout.completed_at >= sub.current_period_start - timedelta(seconds=1))
                     .order_by(DemoCheckout.completed_at.desc()).limit(1))


def unused_credit(db: Session, sub: Subscription, now: datetime) -> int:
    """Pro-rata value of the unused part of the current paid period (by time), used for upgrades."""
    if sub.state != "active" or not sub.current_period_start or not sub.current_period_end:
        return 0
    paid = _last_paid_checkout(db, sub)
    if paid is None or paid.total_paise <= 0:
        return 0
    total_s = (sub.current_period_end - sub.current_period_start).total_seconds()
    left_s = max(0.0, (sub.current_period_end - now).total_seconds())
    if total_s <= 0:
        return 0
    return int(round(paid.total_paise * left_s / total_s))


def classify_change(db: Session, sub: Subscription, plan: SubscriptionPlan, months: int) -> str:
    """new | renewal | reactivation | upgrade | downgrade | same."""
    if sub.state in ("demo", "trial"):
        return "new"
    if sub.state == "pending_checkout":
        return "reactivation" if sub.prev_state in ("expired", "cancelled") else "new"
    if sub.state in ("expired", "cancelled"):
        return "reactivation"
    if sub.state == "past_due":
        return "renewal"
    if sub.state != "active":
        return "new"
    cur = effective_monthly_price(db, sub) or 0
    new = plan.monthly_price_paise or 0
    if plan.key == sub.plan_key and months == sub.billing_months:
        return "same"
    if new > cur or (plan.key == sub.plan_key and months > sub.billing_months):
        return "upgrade"
    return "downgrade"


# --------------------------------------------------------------------------- demo checkout
@dataclass
class CheckoutInput:
    plan_key: str
    billing_months: int
    billing_name: str = ""
    billing_email: str = ""
    billing_address: str = ""


def start_checkout(db: Session, org: Organisation, user, data: CheckoutInput, now: datetime | None = None) -> DemoCheckout:
    now = now or wall_now()
    sub = current(db, org.id, now)
    if sub.state == "suspended":
        raise BillingError("This organisation is suspended by the platform administrator; checkout is unavailable.", 403, "suspended")
    plan = get_plan(db, data.plan_key)
    if not plan.self_serve:
        raise BillingError("This plan cannot be bought through checkout. Contact sales for Enterprise.", 422, "not_self_serve")
    kind = classify_change(db, sub, plan, data.billing_months)
    if kind == "same":
        raise BillingError("You are already on this plan and billing period.", 409, "same_plan")
    if kind == "downgrade":
        raise BillingError("Downgrades and shorter billing periods take effect at renewal. Use 'Schedule change' instead.",
                           409, "downgrade_at_renewal")
    credit = unused_credit(db, sub, now) if kind == "upgrade" else 0
    q = quote_for(plan, data.billing_months, policy(db), credit_paise=credit)
    for chk in db.scalars(select(DemoCheckout).where(DemoCheckout.org_id == org.id, DemoCheckout.status == "pending")):
        chk.status = "cancelled"          # only one open checkout per organisation
        event(db, sub, "checkout_replaced", note=f"Checkout {chk.id} replaced by a new checkout.", data={"checkout": chk.id})
    chk = DemoCheckout(
        id=f"chk_{secrets.token_urlsafe(12)}", org_id=org.id, created_by_user_id=user.id, kind=kind, plan_key=plan.key,
        billing_months=data.billing_months, monthly_price_paise=q["monthly_price_paise"], subtotal_paise=q["subtotal_paise"],
        discount_bps=q["discount_bps"], discount_paise=q["discount_paise"], credit_paise=q["credit_paise"],
        total_paise=q["total_paise"], currency="INR", status="pending",
        billing_name=(data.billing_name or org.name)[:120], billing_email=(data.billing_email or user.email)[:254],
        billing_address=(data.billing_address or org.billing_address or "")[:1000], provider="demo",
        created_at=now, expires_at=now + timedelta(minutes=int(policy(db)["checkout_ttl_min"])))
    db.add(chk)
    db.flush()
    if sub.state in ("demo", "trial", "expired", "cancelled"):
        transition(db, sub, "pending_checkout", kind="checkout_started", actor=user,
                   note=f"Demo checkout started for {plan.name} ({PERIOD_LABELS[data.billing_months]}).", data={"checkout": chk.id})
    else:
        event(db, sub, "checkout_started", actor=user, note=f"Demo checkout started for {plan.name} "
              f"({PERIOD_LABELS[data.billing_months]}, {kind}).", data={"checkout": chk.id})
    return chk


def cancel_checkout(db: Session, org: Organisation, user, checkout_id: str) -> DemoCheckout:
    chk = db.get(DemoCheckout, checkout_id)
    if chk is None or chk.org_id != org.id:
        raise BillingError("Checkout not found.", 404, "not_found")
    if chk.status != "pending":
        raise BillingError(f"This checkout is already {chk.status}.", 409, "not_pending")
    chk.status = "cancelled"
    sub = current(db, org.id)
    event(db, sub, "checkout_cancelled", actor=user, note=f"Checkout {chk.id} cancelled by the user.", data={"checkout": chk.id})
    if sub.state == "pending_checkout":
        transition(db, sub, sub.prev_state if sub.prev_state in ALLOWED["pending_checkout"] else "demo",
                   kind="checkout_abandoned", actor=user, note="Checkout cancelled; previous state restored.")
    return chk


def _next_receipt_number(db: Session) -> str:
    n = int(settings_store.get(db, "receipt_counter", 0) or 0) + 1
    settings_store.put(db, "receipt_counter", n)
    return f"DEMO-RCPT-{n:06d}"


def complete_checkout(db: Session, org: Organisation, user, checkout_id: str, idempotency_key: str,
                      now: datetime | None = None, provider=None) -> dict:
    """Idempotent: repeating the request (same or different key) after success returns the same result
    and never creates a second activation, receipt or event."""
    from .payments import DemoPaymentProvider

    now = now or wall_now()
    key = (idempotency_key or "").strip()[:80]
    if len(key) < 8:
        raise BillingError("An Idempotency-Key (8-80 characters) is required to complete a checkout.", 422, "idempotency_key")
    chk = db.get(DemoCheckout, checkout_id)
    if chk is None or chk.org_id != org.id:
        raise BillingError("Checkout not found.", 404, "not_found")
    if chk.status == "completed":
        return {"replayed": True, **checkout_result(db, chk)}
    other = db.scalar(select(DemoCheckout).where(DemoCheckout.completion_key == key))
    if other is not None:
        raise BillingError("This Idempotency-Key was already used for a different checkout.", 409, "idempotency_conflict")
    if chk.status != "pending":
        raise BillingError(f"This checkout is {chk.status}. Start a new checkout.", 409, "not_pending")
    sub = current(db, org.id, now)
    if chk.expires_at <= now:
        chk.status = "expired"
        apply_due(db, sub, now)
        raise BillingError("This demo checkout expired. Start a new checkout.", 409, "expired")
    if sub.state == "suspended":
        raise BillingError("This organisation is suspended; the checkout cannot be completed.", 403, "suspended")
    # re-verify the price on the server against the current catalogue and subscription
    plan = get_plan(db, chk.plan_key)
    kind = classify_change(db, sub, plan, chk.billing_months)
    credit = unused_credit(db, sub, now) if kind == "upgrade" else 0
    fresh = quote_for(plan, chk.billing_months, policy(db), credit_paise=credit)
    tolerance = 100 if credit else 0     # the pro-rata credit shrinks by the second; allow ₹1 drift
    if fresh["subtotal_paise"] != chk.subtotal_paise or fresh["discount_paise"] != chk.discount_paise or \
            abs(fresh["total_paise"] - chk.total_paise) > tolerance or kind != chk.kind:
        chk.status = "cancelled"
        event(db, sub, "checkout_price_changed", actor=user, note="Price or subscription changed since checkout started.",
              data={"checkout": chk.id, "was": chk.total_paise, "now": fresh["total_paise"]})
        if sub.state == "pending_checkout":
            transition(db, sub, sub.prev_state or "demo", kind="checkout_abandoned", note="Checkout invalidated.")
        raise BillingError("The price or your subscription changed since this checkout started. Please review and start again.",
                           409, "price_changed")
    if not chk.payment_method:
        # v9.2: a paid plan is never activated without the payment step (no "claim" shortcut)
        raise BillingError("Choose a payment method and enter the test payment details before confirming.", 409,
                           "payment_method_required")
    result = (provider or DemoPaymentProvider()).confirm(chk)
    if not result.verified:
        if chk.payment_test_outcome == "decline":
            declined_with = chk.payment_display
            chk.payment_method, chk.payment_display, chk.payment_attached_at, chk.payment_test_outcome = None, "", None, None
            event(db, sub, "test_payment_declined", actor=user,
                  note=f"Test payment declined (simulated): {declined_with}. No money moved; the plan was not changed.",
                  data={"checkout": chk.id})
            raise BillingError("The test payment was declined (simulated). No money moved and your plan was not changed. "
                               "Choose another test payment method.", 402, "test_payment_declined")
        raise BillingError("The payment provider did not confirm this checkout.", 402, "payment_unconfirmed")
    chk.status = "completed"
    chk.completed_at = now
    chk.completion_key = key
    chk.provider = result.provider
    chk.provider_reference = result.reference
    receipt = DemoReceipt(number=_next_receipt_number(db), org_id=org.id, checkout_id=chk.id, issued_at=now,
                          total_paise=chk.total_paise, currency="INR",
                          billed_to={"name": chk.billing_name, "email": chk.billing_email, "address": chk.billing_address,
                                     "organisation": org.name,
                                     "payment": {"method": chk.payment_method, "label": chk.payment_display, "test": True,
                                                 "money_moved": False}},
                          lines=_receipt_lines(chk, plan))
    db.add(receipt)
    chk.receipt_number = receipt.number
    frm = sub.state
    sub.plan_key = plan.key
    sub.billing_months = chk.billing_months
    sub.current_period_start = now
    sub.current_period_end = add_months(now, chk.billing_months)
    sub.grace_until = None
    sub.auto_renew = True
    sub.cancel_at_period_end = False
    sub.cancel_requested_at = None
    sub.ended_at = None
    sub.scheduled_plan_key = None
    sub.scheduled_billing_months = None
    sub.custom_monthly_price_paise = None
    if not db.get(Organisation, org.id).is_system_demo:
        sub.custom_limits = {}             # a new checkout always uses the current catalogue (ends any grandfathering)
        sub.custom_features = []
    sub.mode = "DEMO"
    transition(db, sub, "active", kind=f"activated_{chk.kind}", actor=user,
               note=(f"{plan.name}, {PERIOD_LABELS[chk.billing_months]}, activated in DEMO/TEST mode through the simulated "
                     f"checkout ({chk.payment_display}). No payment was taken. Receipt {receipt.number}."),
               data={"checkout": chk.id, "receipt": receipt.number, "total_paise": chk.total_paise, "from_state": frm,
                     "provider": result.provider, "simulated": result.simulated})
    db.flush()
    return {"replayed": False, **checkout_result(db, chk)}


def _receipt_lines(chk: DemoCheckout, plan: SubscriptionPlan) -> list[dict]:
    lines = [{"description": f"{plan.name} plan, {PERIOD_LABELS[chk.billing_months]} "
                             f"({format_inr(chk.monthly_price_paise)} per month)", "amount_paise": chk.subtotal_paise}]
    if chk.discount_paise:
        lines.append({"description": f"Commitment discount {chk.discount_bps / 100:g}%", "amount_paise": -chk.discount_paise})
    if chk.credit_paise:
        lines.append({"description": "Credit for the unused part of the previous plan", "amount_paise": -chk.credit_paise})
    return lines


def checkout_dict(chk: DemoCheckout) -> dict:
    return {"id": chk.id, "kind": chk.kind, "plan_key": chk.plan_key, "billing_months": chk.billing_months,
            "period_label": PERIOD_LABELS.get(chk.billing_months), "status": chk.status, "currency": chk.currency,
            "monthly_price_paise": chk.monthly_price_paise, "subtotal_paise": chk.subtotal_paise,
            "discount_bps": chk.discount_bps, "discount_paise": chk.discount_paise, "credit_paise": chk.credit_paise,
            "total_paise": chk.total_paise,
            "display": {"monthly_price": format_inr(chk.monthly_price_paise), "subtotal": format_inr(chk.subtotal_paise),
                        "discount": format_inr(chk.discount_paise), "credit": format_inr(chk.credit_paise),
                        "total": format_inr(chk.total_paise)},
            "billing_name": chk.billing_name, "billing_email": chk.billing_email, "billing_address": chk.billing_address,
            "created_at": iso(chk.created_at), "expires_at": iso(chk.expires_at), "completed_at": iso(chk.completed_at),
            "receipt_number": chk.receipt_number, "provider": chk.provider, "mode": "DEMO",
            "provider_reference": chk.provider_reference,
            "payment_method": chk.payment_method, "payment_display": chk.payment_display,
            "payment_ready": bool(chk.payment_method),
            "record_type": "TEST",
            "record_note": ("DEMO/TEST checkout: no payment provider was involved and no money moved."
                            if chk.payment_method or chk.status != "completed" else
                            "DEMO/TEST checkout from before v9.2 (no payment-method step). No payment provider was involved."),
            "provider_reference_label": "Test reference (not a bank, card or UPI transaction ID)",
            "simulation_notice": "Simulation only — no real payment will be taken. Only test payment details are accepted."}


def attach_payment(db: Session, org: Organisation, user, checkout_id: str, details: dict, now: datetime | None = None) -> DemoCheckout:
    """Store the TEST payment method for a pending checkout (method + masked label only)."""
    from .demo_payment_methods import PaymentDetailsError, validate

    now = now or wall_now()
    chk = db.get(DemoCheckout, checkout_id)
    if chk is None or chk.org_id != org.id:
        raise BillingError("Checkout not found.", 404, "not_found")
    if chk.status != "pending":
        raise BillingError(f"This checkout is {chk.status}. Start a new checkout.", 409, "not_pending")
    if chk.expires_at <= now:
        raise BillingError("This demo checkout expired. Start a new checkout.", 409, "expired")
    try:
        choice = validate(details, now)
    except PaymentDetailsError as e:
        err = BillingError(str(e), 422, "payment_details")
        err.field = e.field
        raise err from None
    chk.payment_method, chk.payment_display = choice.method, choice.display
    chk.payment_attached_at, chk.payment_test_outcome = now, choice.outcome
    return chk


def receipt_dict(r: DemoReceipt) -> dict:
    return {"number": r.number, "issued_at": iso(r.issued_at), "checkout_id": r.checkout_id, "currency": r.currency,
            "total_paise": r.total_paise, "total": format_inr(r.total_paise),
            "lines": [{**ln, "amount": format_inr(ln["amount_paise"])} for ln in r.lines], "billed_to": r.billed_to,
            "label": r.label, "is_demo": True, "payment_taken": False, "tax_invoice": False, "record_type": "TEST",
            "payment_label": ((r.billed_to or {}).get("payment") or {}).get("label") or
                             "No payment method recorded (DEMO/TEST checkout from before v9.2)"}


def checkout_result(db: Session, chk: DemoCheckout) -> dict:
    sub = subscription_for(db, chk.org_id)
    rec = db.scalar(select(DemoReceipt).where(DemoReceipt.checkout_id == chk.id))
    return {"checkout": checkout_dict(chk), "receipt": receipt_dict(rec) if rec else None,
            "subscription": subscription_dict(db, sub) if sub else None}


# --------------------------------------------------------------------------- self-service changes
def cancel_renewal(db: Session, org: Organisation, user, reason: str = "") -> Subscription:
    sub = current(db, org.id)
    if sub.state not in ("active", "past_due", "trial"):
        raise BillingError("Only an active, past-due or trial subscription has a renewal to cancel.", 409, "nothing_to_cancel")
    if sub.state == "past_due":
        transition(db, sub, "expired", kind="cancelled_while_past_due", actor=user,
                   note=f"Cancellation requested while past due. {reason}".strip())
        sub.ended_at = wall_now()
        return sub
    if sub.cancel_at_period_end:
        raise BillingError("Renewal is already cancelled.", 409, "already_cancelled")
    sub.cancel_at_period_end = True
    sub.auto_renew = False
    sub.cancel_requested_at = wall_now()
    sub.updated_at = wall_now()
    event(db, sub, "renewal_cancelled", actor=user,
          note=("Renewal cancelled in the app. Access continues until the end of the current period; the current period "
                f"is not refunded. Nothing was sent outside ChargeSight. {reason}").strip())
    return sub


def resume_renewal(db: Session, org: Organisation, user) -> Subscription:
    sub = current(db, org.id)
    if sub.state != "active" or not sub.cancel_at_period_end:
        raise BillingError("There is no cancelled renewal to resume.", 409, "nothing_to_resume")
    sub.cancel_at_period_end = False
    sub.auto_renew = True
    sub.cancel_requested_at = None
    sub.updated_at = wall_now()
    event(db, sub, "renewal_resumed", actor=user, note="Automatic renewal turned back on.")
    return sub


def schedule_change(db: Session, org: Organisation, user, plan_key: str, months: int) -> Subscription:
    """Downgrades and shorter periods apply at the next renewal (no mid-period refunds)."""
    sub = current(db, org.id)
    if sub.state != "active":
        raise BillingError("Changes at renewal are only available for an active subscription.", 409, "not_active")
    plan = get_plan(db, plan_key)
    if not plan.self_serve:
        raise BillingError("Contact sales for Enterprise.", 422, "not_self_serve")
    kind = classify_change(db, sub, plan, months)
    if kind == "same":
        sub.scheduled_plan_key = None
        sub.scheduled_billing_months = None
        event(db, sub, "scheduled_change_cleared", actor=user, note="Scheduled change removed.")
        return sub
    if kind != "downgrade":
        raise BillingError("Upgrades take effect immediately through checkout.", 409, "use_checkout")
    from .entitlements import usage

    u = usage(db, org.id)
    lim = plan.limits or {}
    over = [k for k in ("chargers", "users", "sites", "integrations") if lim.get(k) is not None and u.get(k, 0) > lim[k]]
    sub.scheduled_plan_key = plan.key
    sub.scheduled_billing_months = months
    sub.updated_at = wall_now()
    event(db, sub, "change_scheduled", actor=user,
          note=f"{plan.name} ({PERIOD_LABELS[months]}) scheduled for the next renewal.",
          data={"over_limits": over})
    return sub


def subscription_dict(db: Session, sub: Subscription) -> dict:
    plan = db.get(SubscriptionPlan, sub.plan_key)
    monthly = effective_monthly_price(db, sub)
    nxt = None
    renew_plan = db.get(SubscriptionPlan, sub.scheduled_plan_key) if sub.scheduled_plan_key else plan
    renew_months = sub.scheduled_billing_months or sub.billing_months
    if sub.state in ("active", "past_due") and not sub.cancel_at_period_end and renew_plan and renew_plan.monthly_price_paise:
        q = quote_for(renew_plan, renew_months, policy(db))
        nxt = {"total_paise": q["total_paise"], "total": q["display"]["total"], "plan": renew_plan.name,
               "period_label": PERIOD_LABELS[renew_months], "due_at": iso(sub.current_period_end),
               "note": "Estimated. DEMO mode never charges automatically; renewal needs a demo checkout."}
    return {"org_id": sub.org_id, "plan_key": sub.plan_key, "plan_name": plan.name if plan else sub.plan_key,
            "state": sub.state, "state_label": STATE_LABELS.get(sub.state, sub.state), "prev_state": sub.prev_state,
            "billing_months": sub.billing_months, "period_label": PERIOD_LABELS.get(sub.billing_months),
            "started_at": iso(sub.started_at), "current_period_start": iso(sub.current_period_start),
            "current_period_end": iso(sub.current_period_end), "grace_until": iso(sub.grace_until),
            "auto_renew": sub.auto_renew, "cancel_at_period_end": sub.cancel_at_period_end,
            "cancel_requested_at": iso(sub.cancel_requested_at), "ended_at": iso(sub.ended_at),
            "suspended_reason": sub.suspended_reason or None,
            "scheduled_change": ({"plan_key": sub.scheduled_plan_key, "billing_months": sub.scheduled_billing_months,
                                  "plan_name": renew_plan.name if renew_plan else None} if sub.scheduled_plan_key else None),
            "monthly_price_paise": monthly, "monthly_price": format_inr(monthly) if monthly is not None else "Custom",
            "custom_limits": sub.custom_limits or {}, "mode": sub.mode, "next_payment": nxt,
            "access": "full" if sub.state in FULL_ACCESS else "read_only",
            "current_period_paid": ({"total_paise": paid.total_paise, "total": format_inr(paid.total_paise),
                                     "receipt_number": paid.receipt_number, "billing_months": paid.billing_months}
                                    if (paid := _last_paid_checkout(db, sub)) else None),
            "grandfathered": bool(sub.custom_limits) and sub.plan_key in ("starter", "growth", "professional"),
            "price_policy": ("The amount you paid for the current period is fixed and its receipt is never rewritten. "
                             "Renewals and plan changes use the catalogue price at the time of checkout. If a plan's limits "
                             "change, your purchased limits stay until your next checkout."),
            "time_basis": "UTC timestamps; shown in your local time"}
