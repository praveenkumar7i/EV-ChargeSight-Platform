"""v9 tenant isolation and role permissions.

Organisation A = the legacy demo workspace (the `client` fixture, owner + platform admin).
Organisation B = a newly registered operator with its own private simulated fleet.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import ensure_user, sign_in

PW = "Tenant-B-Pass-2026!"


def make_org(app, email="ops@voltway.example", name="Volt Way", fleet=True):
    """Sign up a new organisation and give it a small private simulated fleet with one open ticket."""
    from app import db as D
    from app.models_saas import Organisation
    from app.services.seed import seed_reference_data
    from app.simulator import engine
    from app.tenancy import as_org

    c = TestClient(app)
    r = c.post("/api/auth/signup", json=dict(full_name="Bina Shah", email=email, password=PW, confirm_password=PW,
                                             organisation_name=name, organisation_type="cpo", country="India", accept_terms=True))
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    org_id = r.json()["organisation"]["id"]
    code = None
    if fleet:
        with D.session_scope() as s:
            with as_org(s, org_id):
                seed_reference_data(s, with_fleet=True)
                prefix = s.get(Organisation, org_id).code
                out = engine.inject_fault(s, f"{prefix}-SIM-HSR-01", "overcurrent")
                code = out["ticket"]
    return c, org_id, code


@pytest.fixture
def two_orgs(client):
    b, org_b, ticket_b = make_org(client.app)
    a_ticket = client.post("/api/sim/inject", json={"charger_id": "SIM-WFD-02", "category": "high_temperature"}).json()["ticket"]
    return client, b, org_b, ticket_b, a_ticket


def test_one_organisation_cannot_read_another_organisations_tickets(two_orgs):
    a, b, org_b, ticket_b, ticket_a = two_orgs
    assert ticket_b.startswith("TKT-VOLT") and ticket_a.startswith("TKT-0")
    assert a.get(f"/api/tickets/{ticket_b}").status_code == 404
    assert b.get(f"/api/tickets/{ticket_a}").status_code == 404
    a_codes = {t["code"] for t in a.get("/api/tickets").json()}
    b_codes = {t["code"] for t in b.get("/api/tickets").json()}
    assert ticket_a in a_codes and ticket_b not in a_codes
    assert b_codes == {ticket_b}
    assert all(c["id"].startswith("VOLT") for c in b.get("/api/chargers").json())
    assert not any(c["id"].startswith("VOLT") for c in a.get("/api/chargers").json())
    ev = b.get("/api/events?limit=500").json()
    rows = ev if isinstance(ev, list) else ev.get("items", ev.get("events", []))
    assert rows and all(e["charger_id"].startswith("VOLT") for e in rows)


def test_changing_ids_in_urls_or_bodies_cannot_cross_tenants(two_orgs):
    a, b, org_b, ticket_b, ticket_a = two_orgs
    # modify another organisation's charger / ticket / technician
    assert b.post("/api/sim/inject", json={"charger_id": "SIM-HSR-01", "category": "overcurrent"}).status_code == 404
    assert b.post(f"/api/tickets/{ticket_a}/transition", json={"to_status": "resolved", "note": "x"}).status_code == 404
    assert b.post(f"/api/tickets/{ticket_a}/notes", json={"note": "hello"}).status_code == 404
    assert b.put("/api/technicians/TECH-01", json={"availability": "off_duty"}).status_code == 404
    assert a.get(f"/api/chargers/VOLT2-SIM-HSR-01").status_code == 404
    # assign one's own ticket to the other organisation's technician
    r = b.post(f"/api/tickets/{ticket_b}/assign", json={"technician_id": "TECH-01", "confirm_conflicts": True, "reason": "try"})
    assert r.status_code in (404, 409) and "TECH-01" in str(r.json())
    tech = a.get("/api/technicians").json()
    assert all(t["id"].startswith("TECH-") for t in tech) and tech[0]["availability"] == "available"


def test_exports_and_reports_are_scoped_to_the_current_organisation(two_orgs):
    a, b, org_b, ticket_b, ticket_a = two_orgs
    a_csv = a.get("/api/reports/tickets.csv").text
    b_csv = b.get("/api/reports/tickets.csv").text
    assert ticket_a in a_csv and ticket_b not in a_csv
    assert ticket_b in b_csv and ticket_a not in b_csv
    a_fleet = a.get("/api/reports/fleet.csv").text
    assert "VOLT" not in a_fleet and "VOLT" in b.get("/api/reports/fleet.csv").text


def test_subscription_billing_and_integrations_are_tenant_scoped(two_orgs):
    a, b, org_b, ticket_b, ticket_a = two_orgs
    # B's billing overview is B's own
    assert b.get("/api/billing").json()["subscription"]["org_id"] == org_b
    assert a.get("/api/billing").json()["subscription"]["org_id"] == 1
    chk = b.post("/api/billing/checkout", json={"plan_key": "starter", "billing_months": 1}).json()
    assert a.get(f"/api/billing/checkout/{chk['id']}").status_code == 404
    pay = {"method": "upi", "upi_app": "gpay", "upi_id": "success@test"}
    assert a.post(f"/api/billing/checkout/{chk['id']}/payment-method", json=pay).status_code == 404   # v9.2 step is tenant-scoped too
    assert b.post(f"/api/billing/checkout/{chk['id']}/payment-method", json=pay).status_code == 200
    assert a.post(f"/api/billing/checkout/{chk['id']}/complete", json={"acknowledge_simulation": True},
                  headers={"Idempotency-Key": "cross-tenant-1"}).status_code == 404
    r = b.post(f"/api/billing/checkout/{chk['id']}/complete", json={"acknowledge_simulation": True}, headers={"Idempotency-Key": "own-key-001"})
    rec = r.json()["receipt"]["number"]
    assert a.get(f"/api/billing/receipts/{rec}").status_code == 404
    b.post("/api/billing/checkout", json={"plan_key": "growth", "billing_months": 1})        # integrations need Growth (v9.1)
    up = b.get("/api/billing").json()["open_checkout"]
    b.post(f"/api/billing/checkout/{up['id']}/payment-method", json=pay)
    b.post(f"/api/billing/checkout/{up['id']}/complete", json={"acknowledge_simulation": True}, headers={"Idempotency-Key": "own-key-002"})
    cfg = b.post("/api/org/integrations", json={"kind": "operator_api", "name": "B operator"}).json()
    assert a.get(f"/api/org/integrations/{cfg['id']}").status_code == 404
    assert a.put(f"/api/org/integrations/{cfg['id']}", json={"base_url": "https://evil.example"}).status_code == 404
    assert cfg["id"] not in [c["id"] for c in a.get("/api/org/integrations").json()["connections"]]


def test_orm_tenant_filter_hides_rows_even_for_direct_lookups(two_orgs):
    from app import db as D
    from app.models import Charger, Ticket
    from app.tenancy import set_org

    a, b, org_b, ticket_b, ticket_a = two_orgs
    s = D.SessionLocal()
    set_org(s, org_b)
    assert s.get(Charger, "SIM-HSR-01") is None
    assert s.scalar(select(Ticket).where(Ticket.code == ticket_a)) is None
    assert {t.code for t in s.scalars(select(Ticket))} == {ticket_b}
    s.close()


def test_new_organisations_never_receive_the_existing_prototype_data(client):
    b, org_b, _ = make_org(client.app, fleet=False)
    assert b.get("/api/tickets").json() == [] and b.get("/api/chargers").json() == []
    assert b.get("/api/technicians").json() == []
    assert b.get("/api/planning/candidates").json() == [] if b.get("/api/planning/candidates").status_code == 200 else True
    assert b.get("/api/investment/sites").json() == []


# --------------------------------------------------------------------------- roles
def _member(app, org_id, role, email, technician_id=None):
    from app import db as D
    from app.models_saas import Membership

    ensure_user(email, "Member-Pass-2026!x", role.title(), org_id=org_id, role=role)
    if technician_id:
        with D.session_scope() as s:
            m = s.scalar(select(Membership).where(Membership.org_id == org_id, Membership.role == role))
            m.technician_id = technician_id
    c = TestClient(app)
    sign_in(c, email, "Member-Pass-2026!x")
    return c


def test_viewer_is_read_only(client):
    v = _member(client.app, 1, "viewer", "viewer@demo.example")
    assert v.get("/api/tickets").status_code == 200
    assert v.get("/api/analytics/timeseries").status_code == 200
    for method, path, body in (("POST", "/api/sim/step", {"n": 1}), ("POST", "/api/technicians", {"name": "X"}),
                               ("PUT", "/api/priority/config", {"weights": {"severity": 1}}),
                               ("POST", "/api/billing/checkout", {"plan_key": "starter", "billing_months": 1})):
        r = v.request(method, path, json=body)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "forbidden", (path, r.text)
    assert v.get("/api/org/members").status_code == 403
    assert v.get("/api/admin/organisations").status_code == 403


def test_technician_can_only_update_tickets_assigned_to_them(client):
    t1 = client.post("/api/sim/inject", json={"charger_id": "SIM-HBL-02", "category": "payment_reader"}).json()["ticket"]
    t2 = client.post("/api/sim/inject", json={"charger_id": "SIM-HBL-03", "category": "payment_reader"}).json()["ticket"]
    client.post(f"/api/tickets/{t1}/assign", json={"technician_id": "TECH-02"})
    tech = _member(client.app, 1, "technician", "tech2@demo.example", technician_id="TECH-02")
    assert tech.post(f"/api/tickets/{t1}/transition", json={"to_status": "en_route"}).status_code == 200
    r = tech.post(f"/api/tickets/{t2}/transition", json={"to_status": "en_route"})
    assert r.status_code == 403 and "assigned to them" in r.json()["detail"]["message"]
    assert tech.post(f"/api/tickets/{t2}/assign", json={"technician_id": "TECH-02"}).status_code == 403
    assert tech.post(f"/api/tickets/{t1}/transition", json={"to_status": "assigned", "technician_id": "TECH-01"}).status_code == 403
    assert tech.get("/api/analytics/faults").status_code == 403             # least privilege: no analysis pages
    assert tech.get("/api/reports/tickets.csv").status_code == 403


def test_admin_cannot_manage_billing_but_can_manage_users(client):
    adm = _member(client.app, 1, "admin", "admin@demo.example")
    assert adm.get("/api/billing").status_code == 200
    assert adm.post("/api/billing/checkout", json={"plan_key": "starter", "billing_months": 1}).status_code == 403
    assert adm.get("/api/org/members").status_code == 200


def test_platform_admin_endpoints_are_for_platform_admins_only(client):
    b, org_b, _ = make_org(client.app, fleet=False)
    assert b.get("/api/admin/organisations").status_code == 403
    rows = client.get("/api/admin/organisations").json()                 # client = platform administrator
    assert {r["id"] for r in rows} >= {1, org_b}
    assert all("tickets" not in r for r in rows)                          # no tenant operational data in the admin list


def test_removed_members_lose_access_immediately(client):
    v = _member(client.app, 1, "viewer", "leaver@demo.example")
    mid = next(m["id"] for m in client.get("/api/org/members").json()["members"] if m["email"] == "leaver@demo.example")
    assert client.post(f"/api/org/members/{mid}/remove").status_code == 200
    r = v.get("/api/tickets")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "no_organisation"
