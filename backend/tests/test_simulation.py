from collections import defaultdict

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.catalog import VALID_TRANSITIONS
from app.models import Charger, Event, SimState, Ticket
from app.services.seed import reset_simulation
from app.simulator import engine


def _events(db):
    return db.execute(select(Event.charger_id, Event.ts, Event.type, Event.status_after, Event.payload)
                      .order_by(Event.id)).all()


def _run(db, steps, params=None):
    if params:
        st = db.get(SimState, 1)
        st.params = {**st.params, **params}
    for _ in range(steps // 200):
        engine.step(db, 200)
        db.commit()


def test_fixed_seed_is_reproducible(tmp_path):
    from app import db as D
    from app.services.seed import seed_reference_data

    sigs = []
    for name in ("a", "b"):
        D.init_engine(f"sqlite:///{(tmp_path / f'{name}.db').as_posix()}")
        D.create_schema()
        s = D.SessionLocal()
        seed_reference_data(s)
        s.commit()
        _run(s, 1200, {"fault_rate_per_charger_day": 3.0})
        sigs.append([tuple((r[0], r[1], r[2], r[3], str(r[4]))) for r in _events(s)])
        s.close()
    assert sigs[0] == sigs[1]
    assert any(e[2] == "fault" for e in sigs[0])


def test_different_seed_gives_different_run(db):
    _run(db, 400)
    a = _events(db)
    reset_simulation(db, seed=999)
    db.commit()
    _run(db, 400)
    b = _events(db)
    assert a != b


def test_status_transitions_are_valid(db):
    _run(db, 2000, {"fault_rate_per_charger_day": 4.0})
    state = defaultdict(lambda: "available")
    for cid, _ts, _type, status_after, _p in _events(db):
        if status_after is None or status_after == state[cid]:
            continue
        assert status_after in VALID_TRANSITIONS[state[cid]], f"{cid}: {state[cid]} -> {status_after}"
        state[cid] = status_after
    for c in db.scalars(select(Charger).where(Charger.source == "simulation")):
        assert c.status == state[c.id]


def test_events_are_persisted_with_source_and_ordered(db):
    _run(db, 600)
    rows = db.scalars(select(Event).order_by(Event.id)).all()
    assert rows and all(r.source == "simulation" for r in rows)
    ts = [r.ts for r in rows]
    assert ts == sorted(ts)
    st = db.get(SimState, 1)
    assert max(ts) <= st.sim_time


def test_faults_generate_tickets_and_recover(db):
    _run(db, 4000, {"fault_rate_per_charger_day": 6.0})
    tickets = db.scalars(select(Ticket)).all()
    assert tickets
    assert any(t.resolved_by == "remote_reset" for t in tickets), "remote reset should clear some reader faults"
    for t in tickets:
        if t.resolved_by in ("remote_reset", "reconnect"):
            assert t.resolved_at is not None and t.resolved_at >= t.detected_at


def test_comm_loss_goes_offline_then_ticket(db):
    engine.inject_fault(db, "SIM-HBL-01", "comm_loss")
    db.commit()
    c = db.get(Charger, "SIM-HBL-01")
    assert c.status == "offline"
    t = db.scalar(select(Ticket).where(Ticket.charger_id == "SIM-HBL-01"))
    assert t.fault_category == "comm_loss"


def test_no_duplicate_ticket_for_same_unresolved_fault(db):
    first = engine.inject_fault(db, "SIM-KRM-01", "ground_fault")
    second = engine.inject_fault(db, "SIM-KRM-01", "ground_fault")
    db.commit()
    assert first["ticket"] and not first["deduplicated"]
    assert second["deduplicated"] and second["ticket"] == first["ticket"]
    assert len(db.scalars(select(Ticket).where(Ticket.charger_id == "SIM-KRM-01")).all()) == 1


def test_database_constraint_blocks_duplicate_open_ticket(db):
    engine.inject_fault(db, "SIM-KRM-02", "overcurrent")
    db.commit()
    t = db.scalar(select(Ticket).where(Ticket.charger_id == "SIM-KRM-02"))
    db.add(Ticket(code="TKT-DUP", charger_id=t.charger_id, site_id=t.site_id, fault_category="overcurrent",
                  severity="critical", detected_at=t.detected_at, status="open", source="simulation"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_inject_rejects_unknown_inputs(db):
    with pytest.raises(LookupError):
        engine.inject_fault(db, "NOPE", "ground_fault")
    with pytest.raises(ValueError):
        engine.inject_fault(db, "SIM-KRM-01", "lightning")


def test_auto_dispatch_resolves_tickets(db):
    st = db.get(SimState, 1)
    st.params = {**st.params, "auto_dispatch": True, "fault_rate_per_charger_day": 0.0}
    engine.inject_fault(db, "SIM-WFD-01", "ground_fault")
    db.commit()
    _run(db, 2000)
    t = db.scalar(select(Ticket).where(Ticket.charger_id == "SIM-WFD-01"))
    # v8: a ground fault is safety-related, so the simulated technician inspects before closing
    assert t.status == "closed" and t.resolved_by == "technician" and t.inspection_passed is True
    assert t.assigned_at <= t.acknowledged_at <= t.on_site_at <= t.resolved_at <= t.closed_at
    assert t.technician_id in ("TECH-01", "TECH-03")          # safety-certified DEMO technicians only
    assert db.get(Charger, "SIM-WFD-01").status in ("available", "charging")
