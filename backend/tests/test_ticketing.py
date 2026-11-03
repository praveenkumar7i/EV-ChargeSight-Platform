"""v8 ticketing: user reports, priority, technician assignment, SLA, lifecycle, simulator, consistency."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import Charger, SimState, Technician, Ticket, TicketHistory
from app.services import assignment as A
from app.services import tickets as T


def _report(client, charger="SIM-KRM-05", cat="payment", **kw):
    body = {"charger_id": charger, "issue_category": cat, "description": "Card reader does not respond", **kw}
    return client.post("/api/tickets", json=body)


def _techs(client, **availability):
    for tid, av in availability.items():
        assert client.put(f"/api/technicians/{tid}", json={"availability": av}).status_code == 200


def _kinds(t):
    return [h["kind"] for h in t["history"]]


# ------------------------------------------------------------------ user submission and validation
def test_user_ticket_created_prioritised_and_assigned(client):
    before = client.get("/api/fleet/summary").json()["open_tickets"]
    r = _report(client, impact="unavailable", reported_by="Site attendant", symptoms="screen frozen", error_code="E42")
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["created"] is True and t["code"].startswith("TKT-")
    assert t["origin"] == "user_reported" and t["origin_label"] == "USER_REPORTED"
    assert t["source"] == "simulation" and t["label"] == "SIMULATED"            # data domain kept separate from origin
    assert t["severity"] == "critical" and "cannot charge" in t["severity_basis"]
    assert t["reported_by"] == "Site attendant" and t["error_code"] == "E42" and t["impact"] == "unavailable"
    assert t["sla_due_at"] and t["triaged_at"] and t["priority"]["score"] > 0
    assert t["priority_snapshot"]["score"] == pytest.approx(t["priority"]["score"], abs=1e-3)
    assert t["technician_id"] in ("TECH-01", "TECH-02", "TECH-04")             # payment skill + DC equipment
    assert t["auto_assigned"] and "Auto-assigned" in t["assignment_note"]
    assert {"status", "priority", "assignment"} <= set(_kinds(t))
    assert "Reported by a user" in " ".join(t["priority"]["notes"])
    assert t["next_action"]
    assert client.get("/api/fleet/summary").json()["open_tickets"] == before + 1
    assert client.get("/api/chargers/SIM-KRM-05").json()["status"] == "available"   # a report is not telemetry
    tl = client.get(f"/api/timeline?q={t['code']}&type=ticket").json()["items"]
    assert {i["ticket_event"] for i in tl} >= {"status", "priority", "assignment"}


@pytest.mark.parametrize("body,status", [
    ({"description": "   "}, 422),
    ({"description": ""}, 422),
    ({"charger_id": "SIM-NOPE-99"}, 422),
    ({"issue_category": "teleportation"}, 422),
    ({"impact": "catastrophic"}, 422),
    ({"affected_chargers": 0}, 422),
])
def test_user_ticket_validation(client, body, status):
    r = _report(client, **body)
    assert r.status_code == status
    assert client.get("/api/tickets?origin=user_reported").json() == []


def test_duplicate_submission_and_duplicate_fault(client):
    rid = "req-0001-abcdef"
    a = _report(client, client_request_id=rid).json()
    b = _report(client, client_request_id=rid).json()
    assert a["code"] == b["code"] and b["idempotent_replay"] is True
    c = _report(client, description="Still broken, second person").json()      # same charger + issue, no request id
    assert c["created"] is False and c["deduplicated"] and c["code"] == a["code"]
    assert any((h["data"] or {}).get("duplicate_report") for h in c["history"])
    assert len(client.get("/api/tickets?origin=user_reported").json()) == 1


# ------------------------------------------------------------------ priority
@pytest.mark.parametrize("cat,impact,expected", [
    ("display", "informational", "minor"),
    ("connector", "reduced_capacity", "major"),
    ("connector", "intermittent", "major"),
    ("payment", "unavailable", "critical"),
    ("safety", "informational", "critical"),          # safety is never downgraded
    ("overheating", "informational", "critical"),
])
def test_severity_follows_existing_definitions(client, cat, impact, expected):
    t = _report(client, cat=cat, impact=impact, description="x").json()
    assert t["severity"] == expected
    sev = next(f for f in t["priority"]["factors"] if f["key"] == "severity")
    assert sev["value"] == {"critical": 1.0, "major": 0.6, "minor": 0.2}[expected]


def test_priority_uses_the_existing_engine_and_audits_changes(client, db_url):
    from app import db as D
    from app.core.priority import PriorityConfig

    t = _report(client, impact="unavailable").json()
    cfg = client.get("/api/priority/config").json()
    assert cfg["normalised_weights"] == pytest.approx(PriorityConfig().weights.normalised().as_dict())
    total = sum(f["contribution"] for f in t["priority"]["factors"])
    assert total == pytest.approx(t["priority"]["score"], abs=1e-3)            # P = Σ wᵢ·xᵢ, unchanged formula
    # a site-wide report raises the capacity factor R (documented ASSUMPTION), nothing else
    s1 = _report(client, charger="SIM-HSR-04", cat="power_delivery", impact="unavailable", description="one").json()
    s2 = _report(client, charger="SIM-HSR-05", cat="power_delivery", impact="unavailable", description="site", affected_chargers=4).json()
    r1 = next(f for f in s1["priority"]["factors"] if f["key"] == "capacity")["value"]
    r2 = next(f for f in s2["priority"]["factors"] if f["key"] == "capacity")["value"]
    assert r2 > r1 and any("affected charger" in n for n in s2["priority"]["notes"])
    # snapshot changes are audited with previous and new values
    s = D.SessionLocal()
    tk = s.scalar(select(Ticket).where(Ticket.code == t["code"]))
    st = s.get(SimState, 1)
    st.sim_time = st.sim_time + timedelta(hours=10)                          # the age factor A grows with the simulation clock
    T.refresh_priority(s, tk, st.sim_time, reason="test: aged")
    s.commit()
    rows = s.scalars(select(TicketHistory).where(TicketHistory.ticket_id == tk.id, TicketHistory.kind == "priority")).all()
    assert len(rows) >= 2 and rows[-1].data["previous"] is not None and rows[-1].data["reason"] == "test: aged"
    s.close()


# ------------------------------------------------------------------ assignment
def test_automatic_selection_is_deterministic_and_explained(client):
    t = _report(client, charger="SIM-YLK-04", cat="payment").json()       # AC charger, payment skill
    el = client.get(f"/api/tickets/{t['code']}/eligible-technicians").json()
    assert el["required"] == {"skill": "payment", "equipment": "ac", "safety_certified": False}
    assert t["technician_id"] == next(c["technician_id"] for c in el["candidates"] if c["technician_id"] == t["technician_id"])
    tech = next(c for c in el["candidates"] if c["technician_id"] == t["technician_id"])
    assert "has skill 'payment'" in " ".join(tech["qualifies"])
    assert "TECH-03" not in {c["technician_id"] for c in el["candidates"] if c["eligible"]}   # no payment skill, DC only


def test_safety_fault_never_goes_to_uncertified_technician(client):
    _techs(client, **{"TECH-01": "off_duty", "TECH-03": "off_duty"})       # the two certified technicians
    t = _report(client, charger="SIM-KRM-05", cat="safety", description="Exposed wire at the connector").json()
    assert t["status"] == "open" and t["technician_id"] is None
    assert t["dispatch_needed"] is True and "No eligible technician available" in t["assignment_note"]
    assert "not safety-certified" in t["assignment_note"]
    el = client.get(f"/api/tickets/{t['code']}/eligible-technicians").json()
    assert not any(c["eligible"] for c in el["candidates"])
    r = client.post(f"/api/tickets/{t['code']}/assign", json={"technician_id": "TECH-02", "confirm_conflicts": True, "reason": "try"})
    assert r.status_code == 403 and r.json()["detail"]["safety"] is True    # ordinary users cannot bypass safety
    # availability change triggers a retry: a certified technician comes back and is assigned
    _techs(client, **{"TECH-03": "available"})
    t2 = client.get(f"/api/tickets/{t['code']}").json()
    assert t2["technician_id"] == "TECH-03" and t2["dispatch_needed"] is False


def test_skill_availability_and_capacity_constraints(client):
    _techs(client, **{"TECH-01": "off_duty", "TECH-02": "unavailable"})
    a = _report(client, charger="SIM-HBL-04", cat="payment", description="a").json()   # AC: TECH-04 (only one left with payment)
    assert a["technician_id"] == "TECH-04"
    b = _report(client, charger="SIM-HBL-05", cat="payment", description="b").json()   # TECH-04 at capacity 1
    assert b["technician_id"] is None and "at capacity" in b["assignment_note"] and "availability: off duty" in b["assignment_note"]
    assert b["dispatch_needed"] is False                                    # major, not urgent
    client.put("/api/technicians/TECH-04", json={"capacity": 2})
    b2 = client.get(f"/api/tickets/{b['code']}").json()
    assert b2["technician_id"] == "TECH-04"
    techs = {x["id"]: x for x in client.get("/api/technicians").json()}
    assert techs["TECH-04"]["workload"] == 2 and techs["TECH-04"]["status"] == "at_capacity"


def test_emergency_override_policy(client):
    _techs(client, **{"TECH-03": "off_duty"})
    first = _report(client, charger="SIM-ECY-02", cat="overheating", description="hot").json()
    assert first["technician_id"] == "TECH-01"
    second = _report(client, charger="SIM-ECY-04", cat="overheating", description="also hot").json()
    assert second["technician_id"] is None and second["dispatch_needed"]
    client.put("/api/assignment/policy", json={"emergency_override": True})
    s2 = client.get(f"/api/tickets/{second['code']}").json()
    assert s2["technician_id"] == "TECH-01" and "EMERGENCY OVERRIDE" in s2["assignment_note"]


def test_manual_assignment_conflicts_override_and_reassignment(client):
    client.patch("/api/sim/config", json={"params": {"auto_dispatch": False}})
    t = _report(client, charger="SIM-KRM-07", cat="display", impact="informational", description="flicker").json()
    assert t["technician_id"] is None
    r = client.post(f"/api/tickets/{t['code']}/assign", json={"technician_id": "TECH-03", "actor": "ops-lead"})
    assert r.status_code == 409 and any("missing skill" in c for c in r.json()["detail"]["conflicts"])
    r = client.post(f"/api/tickets/{t['code']}/assign", json={"technician_id": "TECH-03", "actor": "ops-lead", "confirm_conflicts": True})
    assert r.status_code == 409 and "reason" in r.json()["detail"]["message"]
    r = client.post(f"/api/tickets/{t['code']}/assign", json={"technician_id": "TECH-03", "actor": "ops-lead", "confirm_conflicts": True,
                                                            "reason": "only one in the area"})
    assert r.status_code == 200 and r.json()["technician_id"] == "TECH-03"
    h = [x for x in r.json()["history"] if x["kind"] == "assignment"][-1]
    # v9: the audit trail records the signed-in user, not a name typed by the client
    assert h["override"] and h["actor"] == "Test Owner" and "OVERRIDE" in h["note"]
    r = client.post(f"/api/tickets/{t['code']}/assign", json={"technician_id": "TECH-02", "actor": "ops-lead", "reason": "skill fit"})
    assert r.status_code == 200 and r.json()["technician_id"] == "TECH-02" and r.json()["status"] == "assigned"
    assert "Reassigned from TECH-03 to TECH-02" in [x for x in r.json()["history"] if x["kind"] == "assignment"][-1]["note"]
    r = client.post(f"/api/tickets/{t['code']}/unassign", json={"actor": "ops-lead", "reason": "wrong part"})
    assert r.status_code == 200 and r.json()["status"] == "open" and r.json()["technician_id"] is None


def test_demo_technicians_only_take_simulated_tickets(client, db_url):
    from app import db as D
    from app.models import Site

    s = D.SessionLocal()
    site = Site(code="OCPPX", name="OCPP test site", lat=12.97, lng=77.6, daily_sessions=10, travel_min_low=10,
                travel_min_high=20, is_demo_location=False, source="ocpp")
    s.add(site)
    s.flush()
    s.add(Charger(id="OCPP-REAL-1", site_id=site.id, connector_type="CCS2 DC", power_kw=60, status="available", source="ocpp"))
    s.commit()
    s.close()
    client.put("/api/assignment/policy", json={"auto_assign_live_sources": True})
    t = _report(client, charger="OCPP-REAL-1", cat="payment", description="real charger report").json()
    assert t["source"] == "ocpp" and t["origin"] == "user_reported" and t["technician_id"] is None
    assert "DEMO technician" in t["assignment_note"]
    assert t["code"] not in {x["code"] for x in client.get("/api/tickets?source=simulation").json()}
    assert t["code"] in {x["code"] for x in client.get("/api/tickets?source=ocpp").json()}


# ------------------------------------------------------------------ lifecycle, SLA, safety, recurrence
def test_transitions_waiting_resolve_close_reopen(client):
    client.patch("/api/sim/config", json={"params": {"auto_dispatch": False}})
    code = _report(client, charger="SIM-WFD-07", cat="connector", impact="reduced_capacity", description="loose").json()["code"]
    tr = lambda to, **kw: client.post(f"/api/tickets/{code}/transition", json={"to_status": to, **kw})  # noqa: E731
    assert tr("on_site").status_code == 409                                   # cannot skip
    assert tr("assigned", technician_id="TECH-02", note="manual").status_code == 200
    assert tr("en_route").status_code == 200 and tr("on_site").status_code == 200
    r = tr("waiting_parts")
    assert r.status_code == 409 and "reason" in r.json()["detail"]
    assert tr("waiting_parts", note="Type 2 socket on order").json()["waiting_reason"] == "Type 2 socket on order"
    assert tr("resolved").status_code == 409                                  # must resume first
    assert tr("on_site").status_code == 200
    assert tr("resolved", note="Socket replaced").json()["status"] == "resolved"
    assert client.get("/api/chargers/SIM-WFD-07").json()["status"] in ("available", "charging")
    assert tr("closed", note="verified").json()["status"] == "closed"
    assert tr("open").status_code == 409                                      # reopen needs a reason
    r = tr("open", note="customer reports it again")
    assert r.json()["status"] == "open" and r.json()["reopen_count"] == 1 and r.json()["technician_id"] is None
    hist = [h for h in r.json()["history"] if h["kind"] == "status"]
    assert [h["to"] for h in hist][-3:] == ["resolved", "closed", "open"]
    meta = client.get("/api/tickets/meta").json()
    assert meta["transitions"]["on_site"] == ["resolved", "waiting_access", "waiting_parts"]


def test_safety_repair_needs_inspection_before_return_to_service(client):
    client.patch("/api/sim/config", json={"params": {"auto_dispatch": False}})
    code = client.post("/api/sim/inject", json={"charger_id": "SIM-HSR-02", "category": "ground_fault"}).json()["ticket"]
    tr = lambda to, **kw: client.post(f"/api/tickets/{code}/transition", json={"to_status": to, **kw})  # noqa: E731
    tr("assigned", technician_id="TECH-01")
    tr("en_route")
    tr("on_site")
    assert tr("resolved", note="RCD replaced").status_code == 200
    assert client.get("/api/chargers/SIM-HSR-02").json()["status"] == "unavailable"
    t = client.get(f"/api/tickets/{code}").json()
    assert "inspection" in t["recommended_action"] and any(a["needs_inspection"] for a in t["allowed_actions"])
    r = tr("closed")
    assert r.status_code == 409 and "inspection" in r.json()["detail"]
    assert tr("closed", inspection_passed=True, note="Insulation 500 MΩ").json()["status"] == "closed"
    assert client.get("/api/chargers/SIM-HSR-02").json()["status"] == "available"


def test_sla_deadline_and_escalation_is_recorded_once(db):
    st = db.get(SimState, 1)
    st.params = {**st.params, "auto_dispatch": False}
    from app.simulator import engine

    out = engine.inject_fault(db, "SIM-KRM-02", "overcurrent")
    db.commit()
    t = db.scalar(select(Ticket).where(Ticket.code == out["ticket"]))
    assert t.sla_due_at == t.detected_at + timedelta(hours=4)                 # existing SLA: critical 4 h
    assert T.sla_sweep(db, "simulation", t.detected_at + timedelta(hours=3)) == 0
    assert T.sla_sweep(db, "simulation", t.detected_at + timedelta(hours=5)) == 1
    assert T.sla_sweep(db, "simulation", t.detected_at + timedelta(hours=6)) == 0     # no duplicates on later polls
    db.commit()
    rows = db.scalars(select(TicketHistory).where(TicketHistory.ticket_id == t.id, TicketHistory.kind == "escalation")).all()
    assert len([r for r in rows if "SLA breached" in r.note]) == 1
    assert t.escalated_at is not None and t.dispatch_needed is True


def test_duplicate_fault_event_and_recurrence_reopens(db):
    from app.services.events import process_fault

    st = db.get(SimState, 1)
    st.params = {**st.params, "auto_dispatch": False}
    c = db.get(Charger, "SIM-WFD-06")
    now = st.sim_time
    _, t1 = process_fault(db, c, "payment_reader", now)
    _, t2 = process_fault(db, c, "payment_reader", now + timedelta(minutes=1))
    assert t1 is not None and t2 is None                                       # same active fault: one ticket
    T.transition(db, t1, "assigned", now + timedelta(minutes=5), technician_id="TECH-02")
    T.transition(db, t1, "en_route", now + timedelta(minutes=6))
    T.transition(db, t1, "on_site", now + timedelta(minutes=30))
    T.transition(db, t1, "resolved", now + timedelta(minutes=60), note="reader swapped")
    db.commit()
    assert db.get(Charger, "SIM-WFD-06").status == "available"
    _, t3 = process_fault(db, c, "payment_reader", now + timedelta(hours=5))
    assert t3 is not None and t3.id == t1.id and t3.status == "open" and t3.reopen_count == 1
    assert "recurred" in [h for h in t3.history if h.kind == "status"][-1].note


# ------------------------------------------------------------------ simulator and consistency
def test_simulator_controls_and_deterministic_events(client):
    assert client.post("/api/sim/start").json()["running"] is True
    assert client.post("/api/sim/pause").json()["running"] is False
    st = client.patch("/api/sim/config", json={"speed": 12}).json()
    assert st["speed"] == 12
    s1 = client.post("/api/sim/step", json={"n": 40}).json()
    assert s1["step"] == 40 and s1["created"]["events"] > 0
    assert client.post("/api/sim/resume").json()["running"] is True


def test_normal_operations_stays_healthy_and_repairs_happen(db):
    """The reported bug: the fleet drifted to ~7.5 % uptime because nothing repaired critical faults."""
    from app.models import FleetSnapshot
    from app.simulator import engine

    st = db.get(SimState, 1)
    assert st.params["auto_dispatch"] is True and st.params["fault_rate_per_charger_day"] == 0.15
    st.params = {**st.params, "fault_rate_per_charger_day": 0.6}             # stress: the old fault rate
    db.commit()
    engine.step(db, 240 * 48)                                                  # 48 simulated hours
    db.commit()
    snaps = db.scalars(select(FleetSnapshot).where(FleetSnapshot.source == "simulation").order_by(FleetSnapshot.ts)).all()
    last_day = [s.uptime_pct for s in snaps[-1440:]]
    assert min(last_day) >= 60 and sum(last_day) / len(last_day) >= 80
    tickets = db.scalars(select(Ticket).where(Ticket.source == "simulation")).all()
    assert sum(1 for t in tickets if t.resolved_by == "technician") >= 5
    assert any(t.status == "closed" and t.inspection_passed for t in tickets)
    assert all(t.technician_id in ("TECH-01", "TECH-03") for t in tickets
               if t.fault_category in ("ground_fault", "overcurrent", "high_temperature") and t.technician_id)


def test_scenarios_change_real_rates(client):
    r = client.post("/api/demo/scenarios/degraded_network").json()
    st = client.get("/api/sim/state").json()
    assert r["key"] == "degraded_network" and st["params"]["category_mix"]["comm_loss"] == 0.7
    assert st["params"]["offline_after_missed_heartbeats"] == 2
    client.post("/api/demo/scenarios/major_incident")
    st = client.get("/api/sim/state").json()
    assert st["params"]["fault_rate_per_charger_day"] == 0.8
    techs = {t["id"]: t for t in client.get("/api/technicians").json()}
    assert techs["TECH-04"]["availability"] == "off_duty"
    krm = client.get("/api/chargers?site=KRM").json()
    assert sum(1 for c in krm if c["status"] == "faulted") >= 4


def test_counts_are_consistent_across_endpoints(client):
    _report(client, impact="unavailable")
    _report(client, charger="SIM-HSR-06", cat="display", impact="informational", description="dim")
    client.post("/api/sim/step", json={"n": 8})
    fleet = client.get("/api/fleet/summary").json()
    metrics = client.get("/api/tickets/metrics").json()
    listed = client.get("/api/tickets?open_only=true").json()
    assert fleet["open_tickets"] == metrics["open"] == len(listed)
    snap = client.get("/api/analytics/timeseries?hours=1").json()
    last = (snap.get("points") or snap.get("series") or [None])[-1] if isinstance(snap, dict) else None
    if isinstance(last, dict) and "open_tickets" in last:
        assert last["open_tickets"] == metrics["open"]


def test_imported_tickets_are_never_auto_assigned(db):
    from app.models import Site

    site = Site(code="IMP1", name="Imported site", lat=12.9, lng=77.6, daily_sessions=5, travel_min_low=10, travel_min_high=20,
                is_demo_location=False, source="operator_import")
    db.add(site)
    db.flush()
    c = Charger(id="IMP-001", site_id=site.id, connector_type="Type 2 AC", power_kw=7.4, status="available", source="operator_import")
    db.add(c)
    db.flush()
    t = T.create_ticket(db, c, "payment_reader", db.get(SimState, 1).sim_time)
    db.commit()
    assert t.origin == "imported_data" and t.technician_id is None and not A.auto_assign_enabled(db, "operator_import")


# ------------------------------------------------------------------ technicians API
def test_technician_registry_crud(client):
    assert client.post("/api/technicians", json={"name": "Bad", "skills": ["levitation"]}).status_code == 422
    r = client.post("/api/technicians", json={"id": "TECH-R1", "name": "Ravi (registered)", "skills": ["payment", "display"],
                                              "equipment": ["ac"], "capacity": 2, "shift_start": "08:00", "shift_end": "17:00"})
    assert r.status_code == 200 and r.json()["is_demo"] is False and r.json()["status"] == "available"
    assert client.post("/api/technicians", json={"id": "TECH-R1", "name": "dup"}).status_code == 409
    r = client.put("/api/technicians/TECH-R1", json={"active": False})
    assert r.json()["status"] == "inactive" and r.json()["available"] is False
    assert client.put("/api/technicians/NOPE", json={"active": False}).status_code == 404
    lst = {t["id"]: t for t in client.get("/api/technicians").json()}
    assert lst["TECH-01"]["is_demo"] is True and "DEMO" in lst["TECH-01"]["label"]


# ------------------------------------------------------------------ migration
OLD_TICKETS_DDL = """CREATE TABLE tickets (id INTEGER NOT NULL PRIMARY KEY, code VARCHAR(20) NOT NULL, charger_id VARCHAR(64) NOT NULL,
 site_id INTEGER NOT NULL, fault_category VARCHAR(40) NOT NULL, severity VARCHAR(20) NOT NULL, detected_at DATETIME NOT NULL,
 status VARCHAR(20) NOT NULL, technician_id VARCHAR(32), est_travel_min FLOAT, est_repair_min FLOAT, assigned_at DATETIME,
 acknowledged_at DATETIME, on_site_at DATETIME, resolved_at DATETIME, resolution_notes TEXT NOT NULL, resolved_by VARCHAR(40),
 remote_reset_attempted BOOLEAN NOT NULL, reset_due_at DATETIME, source VARCHAR(20) NOT NULL,
 CONSTRAINT ck_ticket_status CHECK (status IN ('open','assigned','en_route','on_site','resolved')),
 FOREIGN KEY(charger_id) REFERENCES chargers (id), FOREIGN KEY(site_id) REFERENCES sites (id), FOREIGN KEY(technician_id) REFERENCES technicians (id))"""


def test_migration_keeps_every_ticket_and_allows_new_statuses(tmp_path, monkeypatch):
    import sqlite3

    from app import db as D
    from app.config import get_settings
    from app.services.seed import seed_reference_data

    path = tmp_path / "old.db"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    D.init_engine(url)
    D.create_schema()
    s = D.SessionLocal()
    seed_reference_data(s)
    s.commit()
    s.close()
    D.get_engine().dispose()
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("DROP TABLE tickets")
    con.execute(OLD_TICKETS_DDL)
    for i, cat in enumerate(("payment_reader", "comm_loss", "payment_reader"), start=1):
        con.execute("INSERT INTO tickets (id, code, charger_id, site_id, fault_category, severity, detected_at, status, resolution_notes, "
                    "remote_reset_attempted, source) VALUES (?, ?, 'SIM-KRM-01', 1, ?, 'major', '2026-10-23 01:00:00', ?, '', 0, 'simulation')",
                    (i, f"TKT-9000{i}", cat, "open" if i < 3 else "resolved"))
    con.commit()
    con.close()
    D.init_engine(url)
    D.create_schema()                                                         # runs the v8 migration
    con = sqlite3.connect(path)
    assert con.execute("SELECT code, status FROM tickets ORDER BY id").fetchall() == [("TKT-90001", "open"), ("TKT-90002", "open"), ("TKT-90003", "resolved")]
    assert "'closed'" in con.execute("SELECT sql FROM sqlite_master WHERE name='tickets'").fetchone()[0]
    con.execute("UPDATE tickets SET status='closed' WHERE id=3")              # new status accepted by the CHECK constraint
    con.commit()
    con.close()
    assert list((tmp_path / "backups").glob("old-before-v8-ticket-migration-*.db"))
    get_settings.cache_clear()
