"""v9 plans, server-side pricing, demo checkout, subscription lifecycle, entitlements and limits."""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_v9_tenancy import make_org


TEST_UPI = {"method": "upi", "upi_app": "gpay", "upi_id": "success@test"}


def _pay(c, cid, details=None):
    """v9.2: the TEST payment step that must precede completion."""
    r = c.post(f"/api/billing/checkout/{cid}/payment-method", json=details or TEST_UPI)
    assert r.status_code == 200, r.text
    return r.json()


def _buy(c, plan="starter", months=1, key="idem-key-0001"):
    chk = c.post("/api/billing/checkout", json={"plan_key": plan, "billing_months": months})
    assert chk.status_code == 200, chk.text
    _pay(c, chk.json()["id"])
    r = c.post(f"/api/billing/checkout/{chk.json()['id']}/complete", json={"acknowledge_simulation": True}, headers={"Idempotency-Key": key})
    assert r.status_code == 200, r.text
    return chk.json(), r.json()


def test_plan_catalogue_returns_prices_limits_and_features(anon_client):
    d = anon_client.get("/api/public/plans").json()
    plans = {p["key"]: p for p in d["plans"]}
    assert list(plans) == ["demo", "starter", "growth", "professional", "enterprise"]
    assert plans["demo"]["free"] and plans["demo"]["monthly_price"] == "₹0.00"
    assert plans["starter"]["monthly_price_paise"] == 49900 and plans["starter"]["monthly_price"] == "₹499.00"
    assert plans["growth"]["monthly_price_paise"] == 149900 and plans["growth"]["monthly_price"] == "₹1,499.00"
    assert plans["professional"]["monthly_price_paise"] == 499900 and plans["professional"]["monthly_price"] == "₹4,999.00"
    assert plans["enterprise"]["custom_pricing"] and plans["enterprise"]["monthly_price_paise"] is None
    assert plans["demo"]["limits"] == {"chargers": 0, "users": 2, "sites": 0, "integrations": 0}
    assert plans["starter"]["limits"] == {"chargers": 5, "users": 2, "sites": 1, "integrations": 0}
    assert plans["growth"]["limits"] == {"chargers": 25, "users": 5, "sites": 3, "integrations": 3}
    assert plans["professional"]["limits"] == {"chargers": 100, "users": 15, "sites": 10, "integrations": 10}
    assert plans["enterprise"]["limits"] == {"chargers": None, "users": None, "sites": None, "integrations": None}   # negotiated
    f = {k: set(v["features"]) for k, v in plans.items()}
    assert {"tickets", "priority_engine", "manual_assignment", "sla_tracking", "analytics_basic", "reports_standard", "public_map",
            "simulator", "data_import"} <= f["starter"]
    assert not {"auto_assignment", "sla_escalation", "site_planning", "policy_lab", "integrations"} & f["starter"]
    assert {"auto_assignment", "sla_escalation", "analytics_advanced", "reports_enhanced", "site_planning", "policy_lab", "integrations"} <= f["growth"]
    assert f["growth"] < f["professional"] and {"reports_expanded", "admin_enhanced"} <= f["professional"]
    assert f["professional"] < f["enterprise"]
    assert "integrations" not in f["demo"] and "data_import" not in f["demo"] and "auto_assignment" in f["demo"]
    assert [k for k, v in plans.items() if v["recommended"]] == ["starter"]
    assert "not a popularity claim" in plans["starter"]["recommended_note"]
    assert "assumptions" in d["disclaimer"] and "no payment" in d["disclaimer"]
    assert "14,999" not in str(d) and "1499900" not in str(d)


@pytest.mark.parametrize("plan, months, subtotal, discount, total, monthly_eq, display_total", [
    ("starter", 1, 49900, 0, 49900, 49900, "₹499.00"),
    ("starter", 6, 299400, 14970, 284430, 47405, "₹2,844.30"),
    ("starter", 12, 598800, 59880, 538920, 44910, "₹5,389.20"),
    ("growth", 1, 149900, 0, 149900, 149900, "₹1,499.00"),
    ("growth", 6, 899400, 44970, 854430, 142405, "₹8,544.30"),
    ("growth", 12, 1798800, 179880, 1618920, 134910, "₹16,189.20"),
    ("professional", 1, 499900, 0, 499900, 499900, "₹4,999.00"),
    ("professional", 6, 2999400, 149970, 2849430, 474905, "₹28,494.30"),
    ("professional", 12, 5998800, 599880, 5398920, 449910, "₹53,989.20"),
])
def test_period_totals_are_calculated_on_the_server(anon_client, plan, months, subtotal, discount, total, monthly_eq, display_total):
    q = anon_client.get(f"/api/public/quote?plan={plan}&months={months}").json()
    assert (q["subtotal_paise"], q["discount_paise"], q["total_paise"], q["monthly_equivalent_paise"]) == (subtotal, discount, total, monthly_eq)
    assert q["display"]["total"] == display_total and q["amounts"]["total"] == f"{total / 100:.2f}"
    assert q["discount_percent"] == {1: 0, 6: 5, 12: 10}[months]
    assert "total payable" in q["calculation"] and q["tax"]["applied"] is False


def test_decimal_rounding_is_half_up_to_the_paisa():
    from app.models_saas import SubscriptionPlan
    from app.services.billing import DEFAULT_POLICY, quote_for

    p = SubscriptionPlan(key="x", name="X", monthly_price_paise=33333)      # ₹333.33
    q = quote_for(p, 6, DEFAULT_POLICY)                                   # 1999.98 × 5% = 99.999 -> 100.00
    assert (q["subtotal_paise"], q["discount_paise"], q["total_paise"]) == (199998, 10000, 189998)
    q = quote_for(p, 12, DEFAULT_POLICY)                                  # 3999.96 × 10% = 399.996 -> 400.00
    assert (q["discount_paise"], q["total_paise"], q["monthly_equivalent_paise"]) == (40000, 359996, 30000)
    with pytest.raises(Exception):
        quote_for(p, 3, DEFAULT_POLICY)


def test_enterprise_has_no_self_serve_price(anon_client):
    r = anon_client.get("/api/public/quote?plan=enterprise&months=12")
    assert r.status_code == 422 and "custom quotation" in r.json()["detail"]["message"]


def test_inr_formatting_uses_indian_grouping():
    from app.services.billing import format_inr

    assert format_inr(1618920) == "₹16,189.20" and format_inr(49900) == "₹499.00" and format_inr(0) == "₹0.00"
    assert format_inr(16198920) == "₹1,61,989.20"
    assert format_inr(123456789012) == "₹1,23,45,67,890.12"


def test_checkout_totals_cannot_be_manipulated_by_the_browser(client):
    b, org_b, _ = make_org(client.app, fleet=False)
    r = b.post("/api/billing/checkout", json={"plan_key": "growth", "billing_months": 12, "total_paise": 1, "discount_bps": 9999,
                                              "monthly_price_paise": 100, "state": "active"})
    assert r.status_code == 200 and r.json()["total_paise"] == 1618920 and r.json()["discount_bps"] == 1000
    assert b.get("/api/billing").json()["subscription"]["state"] == "pending_checkout"      # not active
    assert b.post("/api/billing/checkout", json={"plan_key": "enterprise", "billing_months": 12}).status_code == 422
    assert b.post("/api/billing/checkout", json={"plan_key": "growth", "billing_months": 3}).status_code == 422


def test_repeated_checkout_completion_does_not_duplicate_anything(client):
    from app import db as D
    from app.models_saas import DemoReceipt, SubscriptionEvent

    b, org_b, _ = make_org(client.app, fleet=False)
    chk = b.post("/api/billing/checkout", json={"plan_key": "starter", "billing_months": 6}).json()
    _pay(b, chk["id"])
    url = f"/api/billing/checkout/{chk['id']}/complete"
    first = b.post(url, json={"acknowledge_simulation": True}, headers={"Idempotency-Key": "abc-12345678"})
    again = b.post(url, json={"acknowledge_simulation": True}, headers={"Idempotency-Key": "abc-12345678"})
    other_key = b.post(url, json={"acknowledge_simulation": True}, headers={"Idempotency-Key": "zzz-87654321"})
    assert first.status_code == again.status_code == other_key.status_code == 200
    assert not first.json()["replayed"] and again.json()["replayed"] and other_key.json()["replayed"]
    assert first.json()["receipt"]["number"] == again.json()["receipt"]["number"]
    with D.session_scope() as s:
        assert s.scalar(select(func.count(DemoReceipt.id)).where(DemoReceipt.org_id == org_b)) == 1
        assert s.scalar(select(func.count(SubscriptionEvent.id)).where(SubscriptionEvent.org_id == org_b,
                                                                       SubscriptionEvent.kind.like("activated%"))) == 1
    sub = b.get("/api/billing").json()["subscription"]
    assert sub["state"] == "active" and sub["plan_key"] == "starter" and sub["billing_months"] == 6 and sub["mode"] == "DEMO"
    # a used idempotency key cannot complete a different checkout
    chk2 = b.post("/api/billing/checkout", json={"plan_key": "growth", "billing_months": 6}).json()
    _pay(b, chk2["id"])
    r = b.post(f"/api/billing/checkout/{chk2['id']}/complete", json={"acknowledge_simulation": True}, headers={"Idempotency-Key": "abc-12345678"})
    assert r.status_code == 409


def test_completion_requires_acknowledgement_and_an_idempotency_key(client):
    b, _, _ = make_org(client.app, fleet=False)
    chk = b.post("/api/billing/checkout", json={"plan_key": "starter", "billing_months": 1}).json()
    _pay(b, chk["id"])
    url = f"/api/billing/checkout/{chk['id']}/complete"
    assert b.post(url, json={}, headers={"Idempotency-Key": "abc-12345678"}).status_code == 422
    assert b.post(url, json={"acknowledge_simulation": True}).status_code == 422


def test_demo_receipts_are_never_presented_as_real_payments(client):
    b, _, _ = make_org(client.app, fleet=False)
    chk, res = _buy(b, "growth", 12)
    rec = res["receipt"]
    assert rec["is_demo"] and rec["payment_taken"] is False and rec["tax_invoice"] is False
    assert rec["number"].startswith("DEMO-RCPT-") and "No payment was taken" in rec["label"] and "Not a tax invoice" in rec["label"]
    assert "Simulation only" in chk["simulation_notice"]
    assert b.get(f"/api/billing/receipts/{rec['number']}").json()["total"] == "₹16,189.20"


def test_subscription_state_transitions_are_validated(client):
    from app import db as D
    from app.services import billing

    b, org_b, _ = make_org(client.app, fleet=False)
    with D.session_scope() as s:
        sub = billing.current(s, org_b)
        with pytest.raises(billing.BillingError):
            billing.transition(s, sub, "past_due", kind="test")          # demo -> past_due is not allowed
        with pytest.raises(billing.BillingError):
            billing.transition(s, sub, "cancelled", kind="test")


def test_expiry_grace_and_read_only_entitlements_keep_data_available(client):
    b, org_b, ticket = make_org(client.app)
    _buy(b, "growth", 1)
    n_tickets = len(b.get("/api/tickets").json())
    # renewal date reached -> past due (full access during grace)
    r = client.post(f"/api/admin/organisations/{org_b}/simulate-time", json={"action": "reach_renewal_date", "reason": "demo of renewal"})
    assert r.json()["subscription"]["state"] == "past_due"
    assert b.post("/api/sim/step", json={"n": 1}).status_code == 200
    # grace ends -> expired -> read-only
    r = client.post(f"/api/admin/organisations/{org_b}/simulate-time", json={"action": "end_grace_period", "reason": "demo of expiry"})
    assert r.json()["subscription"]["state"] == "expired"
    w = b.post("/api/sim/step", json={"n": 1})
    assert w.status_code == 403 and w.json()["detail"]["code"] == "subscription_read_only"
    assert b.post("/api/technicians", json={"name": "New tech"}).status_code == 403
    # data stays readable, including safety history; tickets can still be progressed; billing stays usable
    assert len(b.get("/api/tickets").json()) == n_tickets
    assert b.get(f"/api/tickets/{ticket}").status_code == 200
    assert b.post(f"/api/tickets/{ticket}/notes", json={"note": "Inspection notes"}).status_code == 200
    assert b.get("/api/billing").json()["subscription"]["access"] == "read_only"
    _buy(b, "starter", 1, key="reactivate-0001")                       # reactivation through a new demo checkout
    assert b.post("/api/sim/step", json={"n": 1}).status_code == 200


def test_cancel_renewal_ends_at_period_end_without_external_claims(client):
    b, org_b, _ = make_org(client.app, fleet=False)
    _buy(b, "starter", 1)
    r = b.post("/api/billing/cancel-renewal", json={"confirm": True, "reason": "testing"})
    assert r.status_code == 200 and r.json()["cancel_at_period_end"] and "No e-mail" in r.json()["message"]
    assert r.json()["access"] == "full"
    r = client.post(f"/api/admin/organisations/{org_b}/simulate-time", json={"action": "reach_renewal_date", "reason": "end of period"})
    assert r.json()["subscription"]["state"] == "cancelled"


def test_upgrade_credits_unused_time_and_downgrade_waits_for_renewal(client):
    b, org_b, _ = make_org(client.app, fleet=False)
    _buy(b, "starter", 12)
    up = b.post("/api/billing/checkout", json={"plan_key": "growth", "billing_months": 12}).json()
    assert up["kind"] == "upgrade" and 0 < up["credit_paise"] <= 538920 and up["total_paise"] == 1618920 - up["credit_paise"]
    b.post(f"/api/billing/checkout/{up['id']}/cancel")
    down = b.post("/api/billing/checkout", json={"plan_key": "starter", "billing_months": 1})
    assert down.status_code == 409                                     # same plan, shorter period: at renewal only
    r = b.post("/api/billing/schedule-change", json={"plan_key": "starter", "billing_months": 1, "confirm": True})
    assert r.status_code == 200 and r.json()["scheduled_change"]["billing_months"] == 1


def test_plan_features_are_enforced_on_the_server(client):
    b, org_b, ticket = make_org(client.app)
    _buy(b, "starter", 1)
    r = b.get("/api/planning/preview?lat=12.9&lng=77.6")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "plan_feature" and r.json()["detail"]["upgrade"]
    assert b.get("/api/analytics/maintenance").status_code == 403
    assert b.get("/api/analytics/faults").status_code == 200
    assert b.post(f"/api/tickets/{ticket}/auto-assign").status_code == 403
    assert b.get("/api/reports/downtime_cost.csv").status_code == 403
    assert b.get("/api/reports/tickets.csv").status_code == 200


def test_managed_charger_and_site_limits_for_registration_and_import(client):
    b, org_b, _ = make_org(client.app, fleet=False)
    r = b.post("/api/org/sites", json={"name": "Depot", "code": "DEP"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "plan_feature"        # demo plan: simulated only
    _buy(b, "starter", 1)
    site = b.post("/api/org/sites", json={"name": "Depot", "code": "DEP"})
    assert site.status_code == 200
    r = b.post("/api/org/sites", json={"name": "Second", "code": "SEC"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "plan_limit" and "1-site limit" in r.json()["detail"]["message"]
    for i in range(4):
        r = b.post("/api/org/chargers", json={"site_id": site.json()["id"], "external_id": f"C{i}", "connector_type": "CCS2", "power_kw": 60})
        assert r.status_code == 200, r.text
    # import: a 5th charger at a NEW site is refused by the 1-site limit (the charger limit still has room)
    csv = ("external_event_id,external_charger_id,timestamp,event_type,status,fault_category,site_code\n"
           "e1,NEWCH,2026-10-01T10:00:00Z,status_change,Available,,NEWSITE\n")
    run = b.post("/api/integrations/import/events", files={"file": ("e.csv", csv, "text/csv")}, data={"dataset_label": "t1"}).json()
    assert run["accepted"] == 0 and run["errors"][0]["code"] == "plan_limit" and "site" in run["errors"][0]["error"]
    r = b.post("/api/org/chargers", json={"site_id": site.json()["id"], "external_id": "C4", "connector_type": "CCS2", "power_kw": 60})
    assert r.status_code == 200
    r = b.post("/api/org/chargers", json={"site_id": site.json()["id"], "external_id": "C99", "connector_type": "CCS2", "power_kw": 60})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "plan_limit"
    assert "5-managed-charger limit" in r.json()["detail"]["message"] and r.json()["detail"]["upgrade"]
    csv = ("external_event_id,external_charger_id,timestamp,event_type,status,fault_category,site_code\n"
           "e2,NEWCH2,2026-10-01T10:00:00Z,status_change,Available,,DEP\n")
    run = b.post("/api/integrations/import/events", files={"file": ("e.csv", csv, "text/csv")}, data={"dataset_label": "t2"}).json()
    assert run["accepted"] == 0 and run["errors"][0]["code"] == "plan_limit" and "charger" in run["errors"][0]["error"]
    usage = b.get("/api/entitlements").json()["usage"]
    assert usage["chargers"] == 5 and usage["sites"] == 1                      # nothing deleted, nothing over the limit
    # Growth raises both limits; existing records are kept
    _buy(b, "growth", 1, key="idem-upgrade-02")
    assert b.post("/api/org/sites", json={"name": "Second", "code": "SEC"}).status_code == 200
    assert b.post("/api/org/chargers", json={"site_id": site.json()["id"], "external_id": "C99", "connector_type": "CCS2", "power_kw": 60}).status_code == 200


def test_ocpp_and_operator_sync_respect_the_charger_limit(client):
    """Limits apply to every way a charger can appear: here an ingest of OCPP/operator-API events."""
    from app import db as D
    from app.integrations.ingest import ingest
    from app.integrations.schema import ExternalEvent
    from app.tenancy import as_org

    b, org_b, _ = make_org(client.app, fleet=False)
    _buy(b, "starter", 1)
    evs = [ExternalEvent(external_event_id=f"x{i}", external_charger_id=f"CP{i}", timestamp="2026-10-01T10:00:00Z",
                         event_type="heartbeat", site_code="OCPP") for i in range(7)]
    with D.session_scope() as s, as_org(s, org_b):
        run = ingest(s, evs, source="ocpp", connector="ocpp")
        assert run.accepted == 5 and run.rejected == 2 and all(e["code"] == "plan_limit" for e in run.errors)
        test_point = ingest(s, [ExternalEvent(external_event_id="t1", external_charger_id="SIMOCPP-9", timestamp="2026-10-01T10:00:00Z",
                                              event_type="heartbeat", site_code="OCPP")], source="ocpp", connector="ocpp")
        assert test_point.accepted == 1                                      # the local test point is never counted
    assert b.get("/api/entitlements").json()["usage"]["chargers"] == 5


def test_user_and_integration_limits(client):
    b, org_b, _ = make_org(client.app, fleet=False)
    assert b.post("/api/org/invitations", json={"email": "p0@voltway.example", "role": "viewer"}).status_code == 200
    r = b.post("/api/org/invitations", json={"email": "p9@voltway.example", "role": "viewer"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "plan_limit"            # 2 users incl. the owner
    assert b.post("/api/org/integrations", json={"kind": "operator_api", "name": "X"}).status_code == 403   # demo: none
    _buy(b, "starter", 1)
    r = b.post("/api/org/integrations", json={"kind": "operator_api", "name": "X"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "plan_feature"          # Starter: CSV imports, no configured integrations
    _buy(b, "growth", 1, key="idem-upgrade-03")
    for i in range(3):
        assert b.post("/api/org/integrations", json={"kind": "operator_api", "name": f"Feed {i}"}).status_code == 200
    r = b.post("/api/org/integrations", json={"kind": "ocpp", "name": "Fourth"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "plan_limit"
    for i in range(1, 4):
        assert b.post("/api/org/invitations", json={"email": f"g{i}@voltway.example", "role": "viewer"}).status_code == 200
    assert b.post("/api/org/invitations", json={"email": "g9@voltway.example", "role": "viewer"}).status_code == 403   # 5 users


def test_professional_unlocks_expanded_reporting_and_audit_log(client):
    b, org_b, _ = make_org(client.app, fleet=False)
    _buy(b, "growth", 1)
    assert b.get("/api/reports/full.html").status_code == 403 and b.get("/api/org/audit").status_code == 403
    assert b.get("/api/reports/downtime_cost.csv").status_code == 200       # maintenance reporting is in Growth
    _buy(b, "professional", 1, key="idem-upgrade-04")
    assert b.get("/api/reports/full.html").status_code == 200 and b.get("/api/org/audit").status_code == 200
    assert b.get("/api/entitlements").json()["limits"] == {"chargers": 100, "users": 15, "sites": 10, "integrations": 10}


def test_downgrade_from_professional_is_scheduled_and_keeps_data(client):
    b, org_b, _ = make_org(client.app, fleet=False)
    _buy(b, "professional", 12)
    for code in ("A1", "A2"):
        assert b.post("/api/org/sites", json={"name": code, "code": code}).status_code == 200
    assert b.post("/api/billing/checkout", json={"plan_key": "starter", "billing_months": 12}).status_code == 409
    r = b.post("/api/billing/schedule-change", json={"plan_key": "starter", "billing_months": 12, "confirm": True})
    assert r.status_code == 200 and r.json()["scheduled_change"]["plan_key"] == "starter"
    assert b.get("/api/billing").json()["subscription"]["next_payment"]["total"] == "₹5,389.20"
    assert b.get("/api/entitlements").json()["usage"]["sites"] == 2           # nothing removed by scheduling


def test_client_cannot_spoof_plan_or_state(client):
    b, org_b, _ = make_org(client.app, fleet=False)
    r = b.get("/api/planning/preview?lat=12.9&lng=77.6&plan=growth", headers={"X-Plan": "enterprise"})
    assert r.status_code == 200          # demo plan includes the site-planning demonstration
    _buy(b, "starter", 1)
    r = b.get("/api/planning/preview?lat=12.9&lng=77.6&plan=growth", headers={"X-Plan": "growth"})
    assert r.status_code == 403


def test_admin_suspension_is_audited_and_read_only(client):
    from app import db as D
    from app.models_saas import AuditEvent

    b, org_b, ticket = make_org(client.app)
    assert client.post(f"/api/admin/organisations/{org_b}/suspend", json={"reason": "abuse report check"}).status_code == 200
    assert b.post("/api/sim/step", json={"n": 1}).json()["detail"]["code"] == "subscription_read_only"
    assert b.get(f"/api/tickets/{ticket}").status_code == 200
    assert client.post(f"/api/admin/organisations/{org_b}/reactivate", json={"reason": "check complete"}).json()["state"] == "demo"
    with D.session_scope() as s:
        actions = set(s.scalars(select(AuditEvent.action).where(AuditEvent.org_id == org_b)))
    assert {"admin.org_suspended", "admin.org_reactivated"} <= actions


def test_enterprise_requires_negotiated_limits(client):
    b, org_b, _ = make_org(client.app, fleet=False)
    r = client.post(f"/api/admin/organisations/{org_b}/subscription", json={"action": "assign_enterprise", "reason": "contract signed"})
    assert r.status_code == 422
    r = client.post(f"/api/admin/organisations/{org_b}/subscription",
                    json={"action": "assign_enterprise", "reason": "contract signed", "limits": {"chargers": 500, "users": 40, "sites": 30, "integrations": 8}})
    assert r.status_code == 200 and r.json()["plan_key"] == "enterprise"
    assert b.get("/api/entitlements").json()["limits"] == {"chargers": 500, "users": 40, "sites": 30, "integrations": 8}


def test_enterprise_enquiry_is_stored_before_success_is_shown(anon_client):
    from app import db as D
    from app.models_saas import EnterpriseEnquiry

    r = anon_client.post("/api/public/enquiries", json={"org_name": "Big Network", "contact_email": "cto@big.example", "charger_count": 800,
                                                        "site_count": 60, "integration_requirements": "OCPP 1.6J and REST"})
    assert r.status_code == 200 and r.json()["stored"] and r.json()["reference"].startswith("ENQ-")
    with D.session_scope() as s:
        e = s.scalar(select(EnterpriseEnquiry))
        assert e.charger_count == 800 and e.contact_email == "cto@big.example"
    bad = anon_client.post("/api/public/enquiries", json={"org_name": "X Org", "contact_email": "nope"})
    assert bad.status_code == 422
