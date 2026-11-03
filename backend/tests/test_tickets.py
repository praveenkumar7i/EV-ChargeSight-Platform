from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Charger, SimState, Ticket
from app.services import tickets as T
from app.simulator import engine


def _manual(db):
    """These tests exercise the manual workflow, so automatic assignment is switched off."""
    st = db.get(SimState, 1)
    st.params = {**st.params, "auto_dispatch": False}


def _ticket(db, cid="SIM-KRM-01", cat="ground_fault"):
    _manual(db)
    engine.inject_fault(db, cid, cat)
    db.commit()
    return db.scalar(select(Ticket).where(Ticket.charger_id == cid, Ticket.fault_category == cat))


def test_valid_workflow_and_timestamps(db):
    t = _ticket(db)
    t0 = t.detected_at
    T.transition(db, t, "assigned", t0 + timedelta(minutes=10), technician_id="TECH-01")
    T.transition(db, t, "en_route", t0 + timedelta(minutes=12))
    T.transition(db, t, "on_site", t0 + timedelta(minutes=50))
    T.transition(db, t, "resolved", t0 + timedelta(minutes=140), note="Replaced RCD")
    db.commit()
    assert (t.assigned_at - t0) == timedelta(minutes=10)
    assert t.acknowledged_at == t0 + timedelta(minutes=12)
    assert t.on_site_at == t0 + timedelta(minutes=50)
    assert t.resolved_at == t0 + timedelta(minutes=140)
    assert t.resolved_by == "technician" and t.resolution_notes == "Replaced RCD"
    assert [h.to_status for h in t.history if h.kind == "status"] == ["open", "assigned", "en_route", "on_site", "resolved"]
    # v8: a ground fault is safety-related: resolving does not return the charger to service
    assert db.get(Charger, "SIM-KRM-01").status == "unavailable"
    with pytest.raises(T.WorkflowError, match="inspection"):
        T.transition(db, t, "closed", t0 + timedelta(minutes=150))
    T.transition(db, t, "closed", t0 + timedelta(minutes=150), inspection_passed=True, note="RCD trip test OK")
    db.commit()
    assert t.status == "closed" and t.inspection_passed is True and t.closed_at == t0 + timedelta(minutes=150)
    assert db.get(Charger, "SIM-KRM-01").status == "available"


@pytest.mark.parametrize("target", ["en_route", "on_site", "resolved"])
def test_cannot_skip_steps(db, target):
    t = _ticket(db)
    with pytest.raises(T.WorkflowError):
        T.transition(db, t, target, t.detected_at + timedelta(minutes=1))


def test_assign_requires_known_technician(db):
    t = _ticket(db)
    with pytest.raises(T.WorkflowError):
        T.transition(db, t, "assigned", t.detected_at)
    with pytest.raises(T.WorkflowError):
        T.transition(db, t, "assigned", t.detected_at, technician_id="GHOST")


def test_cannot_go_back_or_reopen_without_override(db):
    t = _ticket(db)
    T.transition(db, t, "assigned", t.detected_at, technician_id="TECH-01")
    with pytest.raises(T.WorkflowError):
        T.transition(db, t, "open", t.detected_at)


def test_override_skips_and_backfills(db):
    t = _ticket(db)
    at = t.detected_at + timedelta(minutes=30)
    T.transition(db, t, "resolved", at, technician_id="TECH-01", override=True, note="admin close")
    assert t.status == "resolved" and t.resolved_by == "admin"
    assert t.assigned_at == at and t.acknowledged_at == at and t.on_site_at == at
    assert t.history[-1].override is True


def test_mtta_mttr_definitions():
    base = datetime(2026, 10, 1)

    class Fake:
        def __init__(self, a, r, status="resolved", severity="critical"):
            self.detected_at = base
            self.assigned_at = base + timedelta(minutes=a) if a is not None else None
            self.resolved_at = base + timedelta(minutes=r) if r is not None else None
            self.status = status
            self.severity = severity
            self.resolved_by = "technician" if r else None

    tickets = [Fake(10, 100), Fake(30, 200), Fake(None, None, status="open"), Fake(20, None, status="assigned")]
    m = T.ticket_metrics(tickets, base + timedelta(hours=10), {"critical": 4})
    assert m["mtta_min"] == pytest.approx((10 + 30 + 20) / 3)
    assert m["mttr_min"] == pytest.approx(150)
    assert m["open"] == 2 and m["resolved"] == 2
    assert m["overdue"] == 2      # both open tickets are older than 4 h
    assert m["unresolved_high_severity"] == 2


def test_unresolved_ticket_excluded_from_mttr(db):
    t = _ticket(db)
    m = T.ticket_metrics([t], t.detected_at + timedelta(hours=1), {"critical": 4})
    assert m["mttr_min"] is None and m["open"] == 1


def test_remote_reset_closes_only_open_tickets(db):
    t = _ticket(db, "SIM-YLK-01", "payment_reader")
    T.transition(db, t, "assigned", t.detected_at, technician_id="TECH-02")
    with pytest.raises(T.WorkflowError):
        T.resolve_by_system(db, t, t.detected_at, "remote_reset", "x")


def test_api_transition_errors_and_admin_override(client, settings_env):
    client.patch("/api/sim/config", json={"params": {"auto_dispatch": False}})
    r = client.post("/api/sim/inject", json={"charger_id": "SIM-HSR-01", "category": "overcurrent"})
    code = r.json()["ticket"]
    r = client.post(f"/api/tickets/{code}/transition", json={"to_status": "resolved"})
    assert r.status_code == 409 and "next step is Assigned" in r.json()["detail"]
    r = client.post(f"/api/tickets/{code}/transition", json={"to_status": "resolved", "override": True})
    assert r.status_code == 403          # override disabled without ADMIN_TOKEN
    settings_env(admin_token="s3cret-token")
    r = client.post(f"/api/tickets/{code}/transition", json={"to_status": "resolved", "override": True},
                    headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 403
    r = client.post(f"/api/tickets/{code}/transition", json={"to_status": "resolved", "override": True, "note": "test"},
                    headers={"X-Admin-Token": "s3cret-token"})
    assert r.status_code == 200 and r.json()["status"] == "resolved"


def test_api_busy_technician_conflict(client):
    client.patch("/api/sim/config", json={"params": {"auto_dispatch": False}})
    a = client.post("/api/sim/inject", json={"charger_id": "SIM-KRM-03", "category": "ground_fault"}).json()["ticket"]
    b = client.post("/api/sim/inject", json={"charger_id": "SIM-KRM-04", "category": "ground_fault"}).json()["ticket"]
    assert client.post(f"/api/tickets/{a}/transition", json={"to_status": "assigned", "technician_id": "TECH-01"}).status_code == 200
    r = client.post(f"/api/tickets/{b}/transition", json={"to_status": "assigned", "technician_id": "TECH-01"})
    assert r.status_code == 409
