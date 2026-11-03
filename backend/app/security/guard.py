"""Authentication, CSRF, role permissions, plan entitlements and tenant binding for every API route.

Installed as an application-wide FastAPI dependency. Every `/api` route must have an entry in
`RULES` (method + route template). A route without an entry is refused (deny by default), and a test
checks that every registered route is covered.

Per request the guard:
1. resolves the session from the HttpOnly `cs_session` cookie (server-side session, see accounts.py);
2. rejects missing / expired sessions with 401 on protected routes;
3. requires the `X-CSRF-Token` header to match the session's CSRF token on state-changing requests;
4. resolves the active organisation and the user's role in it (memberships are checked on every request);
5. checks the role permission and, for technicians, that a ticket action targets a ticket assigned to them;
6. checks plan features and read-only access (expired / cancelled / suspended subscriptions);
7. binds the request's database session to the organisation (tenancy.set_org) via request.state.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import HTTPException, Request
from sqlalchemy import select

from ..db import SessionLocal
from ..services import orgs as org_svc

SESSION_COOKIE = "cs_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


@dataclass(frozen=True)
class Rule:
    perm: str | None = None            # None = public (no session required)
    feature: str | None = None         # plan feature required
    ro_ok: bool = False                # allowed while the subscription is read-only
    tech_scope: bool = False           # technicians: the {code} ticket must be assigned to them
    optional_auth: bool = False        # public, but uses the session when present
    no_org: bool = False               # session required, but no organisation membership needed
    feature_fn: object = field(default=None, compare=False)


PUBLIC = Rule(perm=None, optional_auth=True)
R = Rule

REPORT_FEATURES = {"fleet": "reports_standard", "tickets": "reports_standard", "priority": "reports_standard",
                   "events": "reports_standard", "integrations": "reports_standard", "public_stations": "public_map",
                   "validation": "validation", "investment": "site_planning", "experiment": "policy_lab",
                   "sensitivity": "policy_lab", "downtime_cost": "reports_enhanced"}


def _report_feature(params: dict) -> str:
    return REPORT_FEATURES.get(params.get("name", ""), "reports_standard")


RULES: dict[tuple[str, str], Rule] = {
    # --- public ------------------------------------------------------------------
    ("GET", "/api/health"): PUBLIC,
    ("GET", "/api/public/plans"): PUBLIC,
    ("GET", "/api/public/quote"): PUBLIC,
    ("POST", "/api/public/enquiries"): PUBLIC,
    ("POST", "/api/auth/signup"): PUBLIC,
    ("POST", "/api/auth/signin"): PUBLIC,
    ("POST", "/api/auth/signout"): PUBLIC,
    ("GET", "/api/auth/me"): PUBLIC,
    ("POST", "/api/auth/forgot-password"): PUBLIC,
    ("POST", "/api/auth/reset-password"): PUBLIC,
    ("POST", "/api/auth/verify-email"): PUBLIC,
    ("GET", "/api/auth/invitations/{token}"): PUBLIC,
    ("POST", "/api/auth/invitations/{token}/signup"): PUBLIC,
    # --- account (session, no organisation needed) ----------------------------------
    ("POST", "/api/auth/invitations/{token}/accept"): R("account", no_org=True, ro_ok=True),
    ("POST", "/api/auth/switch-org"): R("account", no_org=True, ro_ok=True),
    ("POST", "/api/auth/presentation-demo"): R("account", no_org=True, ro_ok=True),
    ("POST", "/api/auth/my-fleet"): R("account", no_org=True, ro_ok=True),
    ("POST", "/api/auth/change-password"): R("account", no_org=True, ro_ok=True),
    ("POST", "/api/auth/resend-verification"): R("account", no_org=True, ro_ok=True),
    ("GET", "/api/auth/sessions"): R("account", no_org=True, ro_ok=True),
    ("POST", "/api/auth/sessions/{sid}/revoke"): R("account", no_org=True, ro_ok=True),
    ("PUT", "/api/auth/profile"): R("account", no_org=True, ro_ok=True),
    # --- meta / reports ------------------------------------------------------------------
    ("GET", "/api/config"): R("read.ops"),
    ("GET", "/api/entitlements"): R("read.ops"),
    ("GET", "/api/reports"): R("read.analysis", "reports_standard"),
    ("GET", "/api/reports/full.html"): R("read.analysis", "reports_expanded"),
    ("GET", "/api/reports/{name}.csv"): R("read.analysis", feature_fn=_report_feature),
    # --- operations ------------------------------------------------------------------------
    ("GET", "/api/fleet/summary"): R("read.ops", "fleet_dashboard"),
    ("GET", "/api/sources/status"): R("read.ops"),
    ("GET", "/api/sources"): R("read.ops"),
    ("GET", "/api/sites"): R("read.ops"),
    ("GET", "/api/chargers"): R("read.ops"),
    ("GET", "/api/chargers/{charger_id}"): R("read.ops"),
    ("GET", "/api/events"): R("read.ops"),
    ("GET", "/api/sim/state"): R("read.ops"),
    ("POST", "/api/sim/start"): R("simulator.control", "simulator"),
    ("POST", "/api/sim/resume"): R("simulator.control", "simulator"),
    ("POST", "/api/sim/pause"): R("simulator.control", "simulator", ro_ok=True),
    ("POST", "/api/sim/reset"): R("simulator.control", "simulator"),
    ("POST", "/api/sim/step"): R("simulator.control", "simulator"),
    ("PATCH", "/api/sim/config"): R("simulator.control", "simulator"),
    ("POST", "/api/sim/inject"): R("simulator.control", "simulator"),
    ("GET", "/api/sim/imported/status"): R("read.ops"),
    ("POST", "/api/sim/imported/start"): R("simulator.control", "simulator"),
    ("POST", "/api/sim/imported/reset"): R("simulator.control", "simulator"),
    ("GET", "/api/catalog"): R("read.ops"),
    ("GET", "/api/technicians"): R("read.ops"),
    ("POST", "/api/technicians"): R("technicians.manage", "manual_assignment"),
    ("PUT", "/api/technicians/{tech_id}"): R("technicians.manage", "manual_assignment"),
    ("GET", "/api/assignment/policy"): R("read.ops"),
    ("PUT", "/api/assignment/policy"): R("settings.manage", "manual_assignment"),
    ("GET", "/api/tickets/meta"): R("read.ops"),
    ("POST", "/api/tickets"): R("tickets.raise", "tickets"),
    ("GET", "/api/tickets"): R("read.ops"),
    ("GET", "/api/tickets/metrics"): R("read.ops"),
    ("GET", "/api/tickets/{code}"): R("read.ops"),
    ("GET", "/api/tickets/{code}/eligible-technicians"): R("read.ops"),
    ("POST", "/api/tickets/{code}/assign"): R("dispatch", "manual_assignment", ro_ok=True),
    ("POST", "/api/tickets/{code}/unassign"): R("dispatch", "manual_assignment", ro_ok=True),
    ("POST", "/api/tickets/{code}/auto-assign"): R("dispatch", "auto_assignment"),
    ("POST", "/api/tickets/{code}/notes"): R("tickets.work", "tickets", ro_ok=True, tech_scope=True),
    ("POST", "/api/tickets/{code}/transition"): R("tickets.work", "tickets", ro_ok=True, tech_scope=True),
    ("GET", "/api/priority/config"): R("read.ops"),
    ("PUT", "/api/priority/config"): R("settings.manage", "priority_engine"),
    ("GET", "/api/priority/ranking"): R("read.ops"),
    ("POST", "/api/priority/preview"): R("compute", "priority_engine", ro_ok=True),
    # --- insights / demo ------------------------------------------------------------------------
    ("GET", "/api/analytics/timeseries"): R("read.analysis", "analytics_basic"),
    ("GET", "/api/analytics/faults"): R("read.analysis", "analytics_basic"),
    ("GET", "/api/analytics/maintenance"): R("read.analysis", "analytics_advanced"),
    ("GET", "/api/analytics/sites"): R("read.analysis", "analytics_advanced"),
    ("GET", "/api/chargers/{charger_id}/readings"): R("read.ops"),
    ("GET", "/api/timeline"): R("read.ops"),
    ("GET", "/api/demo/status"): R("read.ops"),
    ("POST", "/api/demo/restore"): R("simulator.control", "simulator"),
    ("GET", "/api/demo/scenarios"): R("read.ops"),
    ("POST", "/api/demo/scenarios/{key}"): R("simulator.control", "simulator"),
    # --- analysis: policy lab and investment -------------------------------------------------------
    ("GET", "/api/experiments/defaults"): R("read.analysis", "policy_lab"),
    ("POST", "/api/experiments/run"): R("planning.manage", "policy_lab"),
    ("POST", "/api/experiments/sensitivity"): R("planning.manage", "policy_lab"),
    ("GET", "/api/experiments"): R("read.analysis", "policy_lab"),
    ("GET", "/api/experiments/{run_id}"): R("read.analysis", "policy_lab"),
    ("DELETE", "/api/experiments/{run_id}"): R("planning.manage", "policy_lab"),
    ("GET", "/api/investment/defaults"): R("read.analysis", "site_planning"),
    ("POST", "/api/investment/evaluate"): R("compute", "site_planning", ro_ok=True),
    ("GET", "/api/investment/sites"): R("read.analysis"),
    ("POST", "/api/investment/sites"): R("planning.manage", "site_planning"),
    ("PUT", "/api/investment/sites/{site_id}"): R("planning.manage", "site_planning"),
    ("DELETE", "/api/investment/sites/{site_id}"): R("planning.manage", "site_planning"),
    ("POST", "/api/investment/compare"): R("compute", "site_planning", ro_ok=True),
    # --- data integrations (legacy endpoints + v9 connection manager) --------------------------------
    ("GET", "/api/integrations/health"): R("read.analysis"),
    ("GET", "/api/integrations/runs"): R("read.analysis"),
    ("POST", "/api/integrations/operator/test"): R("integrations.manage", "integrations"),
    ("POST", "/api/integrations/operator/sync"): R("integrations.manage", "integrations"),
    ("POST", "/api/integrations/operator/diagnose"): R("integrations.manage", "integrations"),
    ("GET", "/api/integrations/operator/status"): R("read.analysis"),
    ("POST", "/api/integrations/import/events"): R("integrations.manage"),
    ("POST", "/api/integrations/historical/preview"): R("read.ops"),
    ("POST", "/api/integrations/historical/import"): R("integrations.manage"),
    ("POST", "/api/integrations/stations/preview"): R("read.ops"),
    ("POST", "/api/integrations/stations/import"): R("integrations.manage"),
    ("GET", "/api/historical/sessions"): R("read.ops"),
    ("GET", "/api/historical/summary"): R("read.ops"),
    ("GET", "/api/imported-stations"): R("read.ops"),
    ("GET", "/api/imported-stations/summary"): R("read.ops"),
    ("GET", "/api/integrations/samples/{name}"): R("read.ops"),
    ("POST", "/api/integrations/import/sample"): R("integrations.manage"),
    ("POST", "/api/integrations/maps/status"): R("read.ops", ro_ok=True),
    ("GET", "/api/org/integrations"): R("read.analysis"),
    ("POST", "/api/org/integrations"): R("integrations.manage", "integrations"),
    ("GET", "/api/org/integrations/{iid}"): R("read.analysis"),
    ("PUT", "/api/org/integrations/{iid}"): R("integrations.manage", "integrations"),
    ("POST", "/api/org/integrations/{iid}/test"): R("integrations.manage", "integrations"),
    ("POST", "/api/org/integrations/{iid}/sync"): R("integrations.manage", "integrations"),
    ("POST", "/api/org/integrations/{iid}/disable"): R("integrations.manage", ro_ok=True),
    ("POST", "/api/org/integrations/{iid}/enable"): R("integrations.manage", "integrations"),
    # --- planning ----------------------------------------------------------------------------------
    ("GET", "/api/planning/preview"): R("read.analysis", "site_planning"),
    ("GET", "/api/planning/candidates"): R("read.analysis"),
    ("POST", "/api/planning/candidates"): R("planning.manage", "site_planning"),
    ("GET", "/api/planning/candidates/{cid}"): R("read.analysis"),
    ("PUT", "/api/planning/candidates/{cid}"): R("planning.manage", "site_planning"),
    ("DELETE", "/api/planning/candidates/{cid}"): R("planning.manage", "site_planning"),
    ("POST", "/api/planning/candidates/{cid}/context"): R("planning.manage", "site_planning"),
    ("POST", "/api/planning/candidates/{cid}/link"): R("planning.manage", "site_planning"),
    ("GET", "/api/planning/candidates/{cid}/prefill"): R("read.analysis", "site_planning"),
    ("GET", "/api/planning/method"): R("read.analysis"),
    ("POST", "/api/planning/evaluate"): R("compute", "site_planning", ro_ok=True),
    ("POST", "/api/planning/compare-finance"): R("compute", "site_planning", ro_ok=True),
    # --- public listings (shared reference data) -----------------------------------------------------
    ("GET", "/api/public-stations"): R("read.ops", "public_map"),
    ("GET", "/api/public-stations/localities"): R("read.ops", "public_map"),
    ("GET", "/api/public-stations/inventory"): R("read.ops", "public_map"),
    ("POST", "/api/public-stations/refresh"): R("platform.admin", no_org=True, ro_ok=True),
    ("GET", "/api/public-stations/refresh/status"): R("read.ops", "public_map"),
    ("POST", "/api/public-stations/reload-snapshot"): R("platform.admin", no_org=True, ro_ok=True),
    ("GET", "/api/public-stations/google"): R("platform.admin", no_org=True, ro_ok=True),
    # --- validation ------------------------------------------------------------------------------------
    ("GET", "/api/validation/interviews"): R("read.analysis", "validation"),
    ("POST", "/api/validation/interviews"): R("validation.manage", "validation"),
    ("DELETE", "/api/validation/interviews/{iid}"): R("validation.manage", "validation"),
    ("GET", "/api/validation/datasets"): R("read.analysis", "validation"),
    ("POST", "/api/validation/datasets"): R("validation.manage", "validation"),
    ("GET", "/api/validation/datasets/{ds_id}"): R("read.analysis", "validation"),
    ("DELETE", "/api/validation/datasets/{ds_id}"): R("validation.manage", "validation"),
    ("POST", "/api/validation/datasets/{ds_id}/compare"): R("compute", "validation", ro_ok=True),
    ("POST", "/api/validation/datasets/{ds_id}/replay"): R("compute", "validation", ro_ok=True),
    ("GET", "/api/validation/financial-references"): R("read.analysis", "validation"),
    ("POST", "/api/validation/financial-references"): R("validation.manage", "validation"),
    ("DELETE", "/api/validation/financial-references/{ref_id}"): R("validation.manage", "validation"),
    ("GET", "/api/validation/financial/{site_id}"): R("read.analysis", "validation"),
    ("GET", "/api/validation/financial-metrics"): R("read.analysis", "validation"),
    ("GET", "/api/validation/tracks"): R("read.analysis", "validation"),
    ("PUT", "/api/validation/tracks/{track}"): R("validation.manage", "validation"),
    # --- organisation, users, onboarding --------------------------------------------------------------------
    ("GET", "/api/org"): R("read.ops", ro_ok=True),
    ("PUT", "/api/org"): R("org.manage", ro_ok=True),
    ("GET", "/api/org/members"): R("users.manage", ro_ok=True),
    ("POST", "/api/org/invitations"): R("users.manage", ro_ok=True),
    ("POST", "/api/org/invitations/{iid}/revoke"): R("users.manage", ro_ok=True),
    ("PUT", "/api/org/members/{mid}"): R("users.manage", ro_ok=True),
    ("POST", "/api/org/members/{mid}/remove"): R("users.manage", ro_ok=True),
    ("GET", "/api/org/audit"): R("org.manage", "admin_enhanced", ro_ok=True),
    ("GET", "/api/org/onboarding"): R("read.ops", ro_ok=True),
    ("PUT", "/api/org/onboarding"): R("org.manage"),
    ("POST", "/api/org/demo-workspace"): R("org.manage", "simulator"),
    ("POST", "/api/org/sites"): R("org.manage", "data_import"),
    ("POST", "/api/org/chargers"): R("org.manage", "data_import"),
    ("PUT", "/api/org/sla"): R("settings.manage", "sla_tracking"),
    # --- subscription & billing (always available to owners, also when read-only) ---------------------------
    ("GET", "/api/billing"): R("billing.view", ro_ok=True),
    ("GET", "/api/billing/quote"): R("billing.view", ro_ok=True),
    ("POST", "/api/billing/checkout"): R("billing.manage", ro_ok=True),
    ("GET", "/api/billing/checkout/{cid}"): R("billing.view", ro_ok=True),
    ("POST", "/api/billing/checkout/{cid}/payment-method"): R("billing.manage", ro_ok=True),
    ("POST", "/api/billing/checkout/{cid}/complete"): R("billing.manage", ro_ok=True),
    ("POST", "/api/billing/checkout/{cid}/cancel"): R("billing.manage", ro_ok=True),
    ("POST", "/api/billing/cancel-renewal"): R("billing.manage", ro_ok=True),
    ("POST", "/api/billing/resume-renewal"): R("billing.manage", ro_ok=True),
    ("POST", "/api/billing/schedule-change"): R("billing.manage", ro_ok=True),
    ("GET", "/api/billing/receipts/{number}"): R("billing.view", ro_ok=True),
    # --- platform administration ----------------------------------------------------------------------------
    ("GET", "/api/admin/overview"): R("platform.admin", no_org=True, ro_ok=True),
    ("GET", "/api/admin/organisations"): R("platform.admin", no_org=True, ro_ok=True),
    ("GET", "/api/admin/organisations/{oid}"): R("platform.admin", no_org=True, ro_ok=True),
    ("POST", "/api/admin/organisations/{oid}/suspend"): R("platform.admin", no_org=True, ro_ok=True),
    ("POST", "/api/admin/organisations/{oid}/reactivate"): R("platform.admin", no_org=True, ro_ok=True),
    ("POST", "/api/admin/organisations/{oid}/subscription"): R("platform.admin", no_org=True, ro_ok=True),
    ("POST", "/api/admin/organisations/{oid}/simulate-time"): R("platform.admin", no_org=True, ro_ok=True),
    ("GET", "/api/admin/plans"): R("platform.admin", no_org=True, ro_ok=True),
    ("PUT", "/api/admin/plans/{key}"): R("platform.admin", no_org=True, ro_ok=True),
    ("PUT", "/api/admin/billing-policy"): R("platform.admin", no_org=True, ro_ok=True),
    ("GET", "/api/admin/enquiries"): R("platform.admin", no_org=True, ro_ok=True),
    ("GET", "/api/admin/events"): R("platform.admin", no_org=True, ro_ok=True),
}


def _deny(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, **extra})


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def guard(request: Request) -> None:
    path = request.url.path
    if not path.startswith("/api/"):
        return                                    # frontend pages and static files
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    rule = RULES.get((request.method, template)) or (RULES.get(("GET", template)) if request.method == "HEAD" else None)
    if rule is None:
        raise _deny(403, "route_not_permitted", "This endpoint is not available.")
    token = request.cookies.get(SESSION_COOKIE)
    if rule.perm is None and not (rule.optional_auth and token):
        return
    from ..models_saas import Organisation, User
    from ..services import accounts, entitlements

    db = SessionLocal()
    try:
        sess = accounts.load_session(db, token)
        if sess is None:
            db.commit()                           # records revocation of an expired session
            if rule.perm is None:
                return
            raise _deny(401, "auth_required", "Please sign in to continue.")
        user = db.get(User, sess.user_id)
        request.state.user_id = user.id
        request.state.user_email = user.email
        request.state.user_name = user.full_name
        request.state.is_platform_admin = bool(user.is_platform_admin)
        request.state.session_id = sess.id
        request.state.csrf_token = sess.csrf_token
        if request.method not in SAFE_METHODS and rule.perm is not None:
            from .tokens import same

            if not same(request.headers.get(CSRF_HEADER, ""), sess.csrf_token):
                raise _deny(403, "csrf_failed", "Your session security token is missing or out of date. Reload the page and try again.")
        m = org_svc.membership(db, user.id, sess.org_id) if sess.org_id else None
        if m is None and sess.org_id:
            sess.org_id = accounts.default_org_id(db, user)           # removed from that organisation
            m = org_svc.membership(db, user.id, sess.org_id) if sess.org_id else None
        if m is not None:
            request.state.org_id = m.org_id
            request.state.role = m.role
            request.state.membership_id = m.id
            request.state.technician_id = m.technician_id
            org = db.get(Organisation, m.org_id)
            request.state.org_name = org.name if org else ""
        else:
            request.state.org_id = None
            request.state.role = None
        if rule.perm is None:
            db.commit()
            return
        if rule.perm == "platform.admin":
            if not user.is_platform_admin:
                raise _deny(403, "forbidden", "Platform administrators only.")
            db.commit()
            return
        if rule.no_org:
            db.commit()
            return
        if m is None:
            raise _deny(403, "no_organisation", "Your account is not a member of an active organisation.")
        if not org_svc.can(m.role, rule.perm):
            raise _deny(403, "forbidden", f"Your role ({org_svc.ROLE_LABELS.get(m.role, m.role)}) does not allow this action.")
        if rule.tech_scope and m.role == "technician":
            _check_technician_scope(db, request, m)
        feature = rule.feature_fn(request.path_params) if rule.feature_fn else rule.feature
        try:
            if feature:
                entitlements.require_feature(db, m.org_id, feature)
            if request.method not in SAFE_METHODS and not rule.ro_ok:
                entitlements.require_full_access(db, m.org_id)
        except entitlements.EntitlementError as e:
            raise HTTPException(e.status, e.detail())
        db.commit()
    except HTTPException:
        db.commit()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _check_technician_scope(db, request: Request, m) -> None:
    from ..models import Ticket
    from ..tenancy import set_org

    code = request.path_params.get("code")
    set_org(db, m.org_id)
    t = db.scalar(select(Ticket).where(Ticket.code == code)) if code else None
    if t is None:
        return                                    # the endpoint answers 404 itself
    if not m.technician_id or t.technician_id != m.technician_id:
        raise _deny(403, "forbidden", "Technicians can only update tickets assigned to them.")


def require_role_in_endpoint(request: Request, permission: str) -> None:
    """For endpoints whose required permission depends on the request body."""
    role = getattr(request.state, "role", None)
    if role is not None and not org_svc.can(role, permission):
        raise _deny(403, "forbidden", f"Your role ({org_svc.ROLE_LABELS.get(role, role)}) does not allow this action.")
