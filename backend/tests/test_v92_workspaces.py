"""v9.2: empty-dashboard root cause, My Fleet vs Presentation Demo, idempotent demo data, isolation."""
from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import func, select

from tests.test_v9_tenancy import make_org


def s_open(c, source="simulation"):
    return c.get(f"/api/fleet/summary?source={source}").json()["open_tickets"]


def _counts(c, source="simulation"):
    s = c.get(f"/api/fleet/summary?source={source}").json()
    return s["total_chargers"], len(s["sites"]), len(c.get(f"/api/tickets?source={source}").json())


# ----------------------------------------------------------------------------- Phase 1: root cause
def test_simulator_without_a_fleet_cannot_report_a_running_simulation(client):
    """Root cause seen in the real v9.1 database: an Operator-API workspace with 0 chargers had a running simulator
    that recorded 0-charger snapshots, so the console showed 'Simulating' next to zero KPIs and 0 % uptime."""
    b, org_b, _ = make_org(client.app, email="empty@ops.example", name="Empty Ops", fleet=False)
    st = b.get("/api/sim/state").json()
    assert st["fleet_size"] == 0 and "Presentation Demo" in st["no_fleet_message"]
    for path, body in (("/api/sim/start", None), ("/api/sim/resume", None), ("/api/sim/step", {"n": 4})):
        r = b.post(path, json=body)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "no_fleet", path
    assert b.get("/api/sim/state").json()["running"] is False
    assert _counts(b) == (0, 0, 0)


def test_empty_snapshots_from_an_empty_simulation_are_ignored_by_history(client):
    from app import db as D
    from app.clock import SIM_EPOCH
    from app.models import FleetSnapshot
    from app.tenancy import as_org

    b, org_b, _ = make_org(client.app, email="snap@ops.example", name="Snap Ops", fleet=False)
    with D.session_scope() as s, as_org(s, org_b):
        for i in range(5):        # what the pre-v9.2 loop wrote for an empty workspace
            s.add(FleetSnapshot(ts=SIM_EPOCH + timedelta(minutes=i), source="simulation", total=0, available=0, charging=0,
                                faulted=0, offline=0, unavailable=0, uptime_pct=0.0, power_kw=0.0, energy_kwh_total=0.0,
                                sessions_total=0, open_tickets=0, critical_open=0))
    ts = b.get("/api/analytics/timeseries?minutes=30&source=simulation").json()
    assert ts["points"] == [] and "Presentation Demo" in ts["reason"]


def test_startup_repair_pauses_empty_simulators_after_a_verified_backup(client, tmp_path):
    from app import db as D
    from app.models import SimState
    from app.models_saas import AuditEvent
    from app.services.workspace_repair import repair_running_without_fleet, running_without_fleet

    b, org_b, _ = make_org(client.app, email="repair@ops.example", name="Repair Ops", fleet=False)
    with D.session_scope() as s:
        s.get(SimState, org_b).running = True
        s.get(SimState, 1).running = True                  # the legacy workspace has a fleet: must stay running
    with D.session_scope() as s:
        assert running_without_fleet(s) == [org_b]
    out = repair_running_without_fleet()
    assert out["repaired"] == [org_b] and out["backup"]
    info = D.verify_backup(__import__("pathlib").Path(out["backup"]))
    assert info["ok"] and info["quick_check"] == "ok"
    with D.session_scope() as s:
        assert s.get(SimState, org_b).running is False and s.get(SimState, 1).running is True
        assert s.scalar(select(AuditEvent).where(AuditEvent.org_id == org_b, AuditEvent.action == "simulator.paused_no_fleet"))
    assert repair_running_without_fleet() == {"repaired": [], "backup": None}          # idempotent, no extra backup


def test_restore_never_fills_my_fleet_with_fictional_chargers(client):
    b, org_b, _ = make_org(client.app, email="mine@ops.example", name="Mine Ops", fleet=False)
    r = b.post("/api/demo/restore", json={"confirm": "RESET"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_demo_workspace"
    assert _counts(b) == (0, 0, 0)


# ----------------------------------------------------------------------------- Phase 2: Presentation Demo
def test_presentation_demo_is_a_separate_populated_workspace_and_my_fleet_stays_empty(client):
    b, org_b, _ = make_org(client.app, email="present@ops.example", name="Present Ops", fleet=False)
    t0 = time.time()
    r = b.post("/api/auth/presentation-demo")
    assert r.status_code == 200, r.text
    built_s = time.time() - t0
    me = r.json()
    pres = me["organisation"]
    assert pres["workspace_kind"] == "presentation" and pres["id"] != org_b
    assert {o["workspace_kind"] for o in me["organisations"]} == {"customer", "presentation"}
    # populated immediately: no fault injection or waiting
    total, sites, tickets = _counts(b)
    assert (total, sites, tickets) == (40, 6, 8), built_s
    assert len(b.get("/api/technicians").json()) == 4
    st = b.get("/api/sim/state").json()
    assert st["running"] is False and st["fleet_size"] == 40              # paused ...
    ts = b.get("/api/analytics/timeseries?minutes=360&source=simulation").json()
    assert len(ts["points"]) > 100                                           # ... and history still charts
    states = {t["status"] for t in b.get("/api/tickets?source=simulation").json()}
    assert {"open", "assigned", "en_route", "on_site", "closed"} <= states
    assert b.get("/api/timeline?source=simulation&limit=20&operational=true").json()["items"]
    ranking = b.get("/api/priority/ranking?source=simulation").json()
    assert len(ranking) == s_open(b) and all(x["factors"] and "score" in x for x in ranking)     # explainable ranking
    # back to My Fleet: still empty, nothing was copied into it
    mf = b.post("/api/auth/my-fleet").json()
    assert mf["organisation"]["id"] == org_b and mf["organisation"]["workspace_kind"] == "customer"
    assert _counts(b) == (0, 0, 0)


def test_dashboard_counts_agree_with_the_underlying_records(client):
    b, _, _ = make_org(client.app, email="counts@ops.example", name="Counts Ops", fleet=False)
    b.post("/api/auth/presentation-demo")
    s = b.get("/api/fleet/summary?source=simulation").json()
    chargers = b.get("/api/chargers?source=simulation").json()
    open_t = b.get("/api/tickets?source=simulation&open_only=true").json()
    assert s["total_chargers"] == len(chargers) == sum(s["status_counts"].values())
    assert s["open_tickets"] == len(open_t)
    assert sum(site["chargers"] for site in s["sites"]) == s["total_chargers"]
    assert sum(site["open_tickets"] for site in s["sites"]) == s["open_tickets"]
    by_status = {}
    for c in chargers:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
    assert all(s["status_counts"][k] == v for k, v in by_status.items())
    m = b.get("/api/tickets/metrics?source=simulation").json()
    assert m["open"] == s["open_tickets"]


def test_presentation_demo_is_idempotent_and_restore_keeps_non_simulated_records(client):
    from app import db as D
    from app.models import Charger, Site, Ticket
    from app.tenancy import as_org

    b, _, _ = make_org(client.app, email="idem@ops.example", name="Idem Ops", fleet=False)
    pres_id = b.post("/api/auth/presentation-demo").json()["organisation"]["id"]
    again = b.post("/api/auth/presentation-demo").json()
    assert again["organisation"]["id"] == pres_id and again["presentation"]["built_dataset"] is False
    assert _counts(b) == (40, 6, 8)
    # a user-created imported record in the presentation workspace must survive a restore
    with D.session_scope() as s, as_org(s, pres_id):
        site = Site(code="IMP-1", name="Imported depot", lat=12.9, lng=77.6, source="operator_import")
        s.add(site)
        s.flush()
        s.add(Charger(id="IMP-1-01", site_id=site.id, connector_type="CCS2", power_kw=60, status="available", source="operator_import"))
    for _ in range(2):
        r = b.post("/api/demo/restore", json={"confirm": "RESET"})
        assert r.status_code == 200, r.text
    assert _counts(b) == (40, 6, 8)                                               # no duplicates
    with D.session_scope() as s, as_org(s, pres_id):
        assert s.scalar(select(func.count(Charger.id)).where(Charger.source == "operator_import")) == 1
        assert s.scalar(select(func.count(Ticket.id)).where(Ticket.source == "simulation")) == 8
    assert _counts(b, "operator_import")[0] == 1                                  # IMPORTED stays IMPORTED, not SIMULATED


def test_presentation_workspaces_never_leak_between_users_or_into_customer_workspaces(client):
    b, org_b, _ = make_org(client.app, email="leak-b@ops.example", name="Leak B", fleet=False)
    c, org_c, _ = make_org(client.app, email="leak-c@ops.example", name="Leak C", fleet=False)
    pb = b.post("/api/auth/presentation-demo").json()["organisation"]
    pc = c.post("/api/auth/presentation-demo").json()["organisation"]
    assert pb["id"] != pc["id"]
    tb = {t["code"] for t in b.get("/api/tickets").json()}
    tc = {t["code"] for t in c.get("/api/tickets").json()}
    assert tb and tc and not (tb & tc)
    assert all(b.get(f"/api/tickets/{code}").status_code == 404 for code in list(tc)[:3])
    assert b.post("/api/auth/switch-org", json={"org_id": pc["id"]}).status_code == 404       # not a member
    legacy = {t["code"] for t in client.get("/api/tickets").json()}
    assert not (legacy & tb)
    c.post("/api/auth/my-fleet")
    assert _counts(c) == (0, 0, 0)


def test_misleading_seed_flag_is_repaired_without_touching_other_data(client):
    from app import db as D
    from app.models_saas import Organisation
    from app.services import settings_store
    from app.tenancy import as_org

    b, org_b, _ = make_org(client.app, email="flag@ops.example", name="Flag Ops", fleet=False)
    with D.session_scope() as s:
        s.get(Organisation, org_b).demo_workspace_ready = True
        with as_org(s, org_b):
            settings_store.put(s, "demo_seeded", {"version": 3, "note": "flag without records"})
    r = b.post("/api/org/demo-workspace")                  # owner explicitly chose Demo mode in onboarding
    assert r.status_code == 200 and r.json()["created"] is True
    assert _counts(b) == (40, 6, 8)


def test_workspace_switch_endpoints_need_a_session_and_csrf(client, anon_client):
    b, _, _ = make_org(client.app, email="csrf@ops.example", name="Csrf Ops", fleet=False)
    token = b.headers.pop("X-CSRF-Token")
    for path in ("/api/auth/presentation-demo", "/api/auth/my-fleet"):
        r = b.post(path)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "csrf_failed", path
        assert anon_client.post(path).status_code in (401, 403), path
    b.headers["X-CSRF-Token"] = token
    assert b.post("/api/auth/my-fleet").status_code == 200
