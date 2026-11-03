"""Demo dataset, provenance, telemetry history, scenarios, analytics and reset safety."""
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select

SAMPLE = Path(__file__).resolve().parents[2] / "sample_data" / "SYNTHETIC_operator_events_sample.csv"


def _import_sample(c):
    r = c.post("/api/integrations/import/events", files={"file": ("s.csv", SAMPLE.read_bytes(), "text/csv")},
               data={"dataset_label": "SYNTHETIC SAMPLE (test)"})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ seeding
def test_demo_dataset_mixture(demo_client):
    c = demo_client
    chargers = c.get("/api/chargers").json()
    assert len(chargers) == 40
    assert len({ch["site_code"] for ch in chargers}) == 6
    status = {}
    for ch in chargers:
        status[ch["status"]] = status.get(ch["status"], 0) + 1
    assert status.get("offline", 0) >= 1
    assert status.get("faulted", 0) >= 3
    assert status.get("charging", 0) >= 3 and status.get("available", 0) >= 3

    tickets = c.get("/api/tickets").json()
    assert len(tickets) == 8
    states = {t["status"] for t in tickets}
    assert {"open", "assigned", "en_route", "on_site", "resolved", "closed"} <= states
    assert any(t["overdue"] for t in tickets)
    assert any(t["severity"] == "critical" and t["status"] == "open" for t in tickets)
    assert {t["resolved_by"] for t in tickets if t["status"] in ("resolved", "closed")} == {"technician", "remote_reset"}
    # v8: the overcurrent repair (safety) was closed only after a recorded post-repair inspection
    t2 = c.get("/api/tickets/TKT-00002").json()
    assert t2["status"] == "closed" and t2["inspection_passed"] is True
    assert any(h["kind"] == "inspection" for h in t2["history"])
    assert all(t["source"] == "simulation" and t["label"] == "SIMULATED" for t in tickets)
    for t in tickets:
        assert t["recommended_action"]
        assert t["age_min"] is not None and t["age_min"] >= 0

    runs = c.get("/api/experiments").json()
    assert sum(1 for r in runs if r["label"].startswith("[DEMO]")) == 2
    events = c.get("/api/timeline?limit=500").json()
    faults = [e for e in events["items"] if e["type"] in ("fault", "offline", "fault_resolved", "reset_result")]
    assert 10 <= len(faults) <= 30


def test_demo_timestamps_consistent(demo_url):
    from app import db as D
    from app.clock import SIM_EPOCH
    from app.models import Event, SimState, Ticket, TicketHistory
    from app.services.demo_seed import HISTORY_START

    D.init_engine(demo_url)
    with D.session_scope() as s:
        now = s.get(SimState, 1).sim_time
        assert now == SIM_EPOCH
        assert s.scalar(select(func.min(Event.ts))) >= HISTORY_START
        assert s.scalar(select(func.max(Event.ts))) <= now
        for t in s.scalars(select(Ticket)):
            seq = [x for x in (t.detected_at, t.assigned_at, t.acknowledged_at, t.on_site_at, t.resolved_at) if x]
            assert seq == sorted(seq), t.code
            assert all(HISTORY_START <= x <= now for x in seq)
        for h in s.scalars(select(TicketHistory)):
            assert h.at <= now


def test_demo_is_deterministic(demo_db_template, tmp_path, monkeypatch):
    """Building the demo twice from scratch gives the same tickets and fleet state."""
    from app import db as D
    from app.config import get_settings
    from app.models import Charger, Ticket
    from app.services.demo_seed import build_demo
    from app.services.seed import seed_reference_data

    def snapshot(url):
        D.init_engine(url)
        with D.session_scope() as s:
            return ([(t.code, t.charger_id, t.status, t.detected_at) for t in s.scalars(select(Ticket).order_by(Ticket.id))],
                    [(c.id, c.status, round(c.total_kwh, 3)) for c in s.scalars(select(Charger).order_by(Charger.id))])

    first = snapshot(f"sqlite:///{demo_db_template.as_posix()}")
    url = f"sqlite:///{(tmp_path / 'again.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    D.init_engine(url)
    D.create_schema()
    with D.session_scope() as s:
        seed_reference_data(s)
    with D.session_scope() as s:
        build_demo(s)
    assert snapshot(url) == first


def test_autoseed_idempotent_on_restart(db_url, settings_env, monkeypatch):
    from fastapi.testclient import TestClient

    from app import db as D
    from app.main import create_app

    settings_env(DEMO_AUTOSEED="true")
    D.init_engine(db_url)
    from tests.conftest import authed

    with TestClient(create_app(run_simulator_loop=False)) as c:
        authed(c)                                   # v9: the API requires a signed-in member
        first = c.get("/api/tickets").json()
        assert len(first) == 8
        assert c.get("/api/demo/status").json()["seeded"] is True
    D.init_engine(db_url)
    with TestClient(create_app(run_simulator_loop=False)) as c:
        authed(c)
        again = c.get("/api/tickets").json()
        assert [t["code"] for t in again] == [t["code"] for t in first]
        assert len(c.get("/api/experiments").json()) == 2


def test_autoseed_skips_existing_simulation(db, settings_env):
    """An older database that already has simulated tickets is never reseeded."""
    from app.services.demo_seed import should_autoseed
    from app.simulator import engine

    settings_env(DEMO_AUTOSEED="true")
    engine.inject_fault(db, "SIM-KRM-01", "overcurrent")
    db.commit()
    assert should_autoseed(db) is False


# ------------------------------------------------------------------ reset safety
def test_restore_requires_confirmation(demo_client):
    assert demo_client.post("/api/demo/restore", json={"confirm": "yes"}).status_code == 400
    assert demo_client.post("/api/demo/restore", json={}).status_code == 422


def test_restore_keeps_imported_and_user_data(demo_client):
    c = demo_client
    _import_sample(c)
    imported_before = c.get("/api/tickets?source=operator_import").json()
    imported_chargers = c.get("/api/chargers?source=operator_import").json()
    assert len(imported_chargers) == 3
    user_run = c.post("/api/experiments/run", json={"mode": "quick", "label": "my run"}).json()["id"]
    c.post("/api/sim/inject", json={"charger_id": "SIM-KRM-05", "category": "overcurrent"})
    assert len(c.get("/api/tickets").json()) == 9

    r = c.post("/api/demo/restore", json={"confirm": "RESET"})
    assert r.status_code == 200
    assert len(c.get("/api/tickets").json()) == 8
    assert [t["code"] for t in c.get("/api/tickets?source=operator_import").json()] == \
        [t["code"] for t in imported_before]
    assert len(c.get("/api/chargers?source=operator_import").json()) == 3
    labels = {x["id"]: x["label"] for x in c.get("/api/experiments").json()}
    assert labels.get(user_run) == "my run"
    assert sum(1 for v in labels.values() if v.startswith("[DEMO]")) == 2


def test_sim_reset_clears_history_but_not_imports(demo_client):
    c = demo_client
    _import_sample(c)
    r = c.post("/api/sim/reset", json={})
    assert r.status_code == 200 and "Imported/live data untouched" in r.json()["message"]
    assert c.get("/api/tickets").json() == []
    assert c.get("/api/analytics/timeseries").json()["points"] == []
    assert len(c.get("/api/chargers?source=operator_import").json()) == 3


# ------------------------------------------------------------------ telemetry + analytics
def test_timeseries_and_target(demo_client):
    ts = demo_client.get("/api/analytics/timeseries?minutes=30").json()
    assert ts["supported"] and ts["target_uptime_pct"] == 97.0
    assert 25 <= len(ts["points"]) <= 31
    p = ts["points"][-1]
    assert p["available"] + p["charging"] + p["faulted"] + p["offline"] + p["unavailable"] == 40
    assert abs(p["uptime_pct"] - 100 * (p["available"] + p["charging"]) / 40) < 0.01
    assert ts["summary"]["energy_kwh"] > 0
    long = demo_client.get("/api/analytics/timeseries?minutes=360").json()
    assert len(long["points"]) <= 240                      # downsampled
    other = demo_client.get("/api/analytics/timeseries?source=operator_import").json()
    assert other["supported"] is False and other["reason"]


def test_history_grows_with_steps(demo_client):
    before = demo_client.get("/api/analytics/timeseries?minutes=600").json()["points"][-1]["ts"]
    demo_client.post("/api/sim/step", json={"n": 8})
    after = demo_client.get("/api/analytics/timeseries?minutes=600").json()["points"]
    assert after[-1]["ts"] > before


def test_fault_and_maintenance_analytics(demo_client):
    f = demo_client.get("/api/analytics/faults?hours=6").json()
    assert sum(f["totals_by_category"].values()) == 8
    assert sum(b["total"] for b in f["buckets"]) == 8
    m = demo_client.get("/api/analytics/maintenance?hours=6").json()
    last = m["trend"][-1]
    assert last["backlog"] == 6 and last["overdue"] >= 1
    assert last["mtta_min"] is not None and last["mttr_min"] is not None
    assert sum(b["count"] for b in m["age_histogram"]) == 6
    s = demo_client.get("/api/analytics/sites?hours=6").json()
    assert len(s["sites"]) == 6
    assert all(0 <= x["window_availability_pct"] <= 100 for x in s["sites"])


def test_charger_readings_and_detail(demo_client):
    r = demo_client.get("/api/chargers/SIM-KRM-02/readings").json()
    assert r["supported"] and len(r["points"]) >= 60
    assert all(p["power_kw"] >= 0 for p in r["points"])
    hot = demo_client.get("/api/chargers/SIM-ECY-03").json()
    assert hot["fault_code"] == "HighTemperature" and hot["temp_c"] > 45
    assert demo_client.get("/api/chargers/NOPE/readings").status_code == 404


def test_history_is_pruned(db):
    from app.models import ChargerReading, FleetSnapshot, SimState
    from app.simulator import engine

    st = db.get(SimState, 1)
    old = st.sim_time - timedelta(hours=100)
    db.add(FleetSnapshot(ts=old, source="simulation", total=40))
    db.add(ChargerReading(charger_id="SIM-KRM-01", ts=old, status="available", power_kw=0, source="simulation"))
    db.add(FleetSnapshot(ts=old, source="operator_import", total=1))
    db.flush()
    engine.prune_history(db, st.sim_time)
    db.commit()
    assert db.scalar(select(func.count(FleetSnapshot.id)).where(FleetSnapshot.source == "simulation")) == 0
    assert db.scalar(select(func.count(ChargerReading.id))) == 0
    assert db.scalar(select(func.count(FleetSnapshot.id)).where(FleetSnapshot.source == "operator_import")) == 1


# ------------------------------------------------------------------ timeline
def test_timeline_filters_and_pagination(demo_client):
    c = demo_client
    page1 = c.get("/api/timeline?limit=5").json()
    page2 = c.get("/api/timeline?limit=5&offset=5").json()
    assert len(page1["items"]) == 5 and page1["total"] > 10
    assert page1["items"][-1]["ts"] >= page2["items"][0]["ts"]
    assert "SIMULATED" in page1["note"]
    tk = c.get("/api/timeline?type=ticket&limit=100").json()["items"]
    assert tk and all(i["kind"] == "ticket" for i in tk)
    assert any("advisory, not a physical dispatch" in i["description"] for i in tk)
    yl = c.get("/api/timeline?site=YLK&limit=100").json()["items"]
    assert yl and all(i["site_code"] == "YLK" for i in yl)
    crit = c.get("/api/timeline?severity=critical&limit=100").json()["items"]
    assert crit and all(i["severity"] == "critical" for i in crit)
    assert c.get("/api/timeline?q=TKT-00002&type=ticket").json()["items"]
    assert c.get("/api/timeline?type=bogus").status_code == 422


# ------------------------------------------------------------------ inject + tickets
def test_inject_with_severity_override_and_dedup(demo_client):
    c = demo_client
    r = c.post("/api/sim/inject", json={"charger_id": "SIM-HSR-01", "category": "payment_reader", "severity": "critical"})
    assert r.status_code == 200 and r.json()["severity"] == "critical"
    code = r.json()["ticket"]
    t = c.get(f"/api/tickets/{code}").json()
    assert t["severity"] == "critical" and t["severity_overridden"] is True
    assert t["priority"]["severity_level"] == "critical"
    dup = c.post("/api/sim/inject", json={"charger_id": "SIM-HSR-01", "category": "payment_reader"}).json()
    assert dup["deduplicated"] is True
    assert c.post("/api/sim/inject", json={"charger_id": "SIM-HSR-02", "category": "overcurrent",
                                           "severity": "extreme"}).status_code == 422


def test_ticket_search_and_sort(demo_client):
    c = demo_client
    by_age = c.get("/api/tickets?sort=age").json()
    open_ages = [t["age_min"] for t in by_age if t["status"] not in ("resolved", "closed")]
    assert open_ages == sorted(open_ages, reverse=True)
    by_sev = [t["severity"] for t in c.get("/api/tickets?sort=severity&open_only=true").json()]
    assert by_sev == sorted(by_sev, key=["critical", "major", "minor"].index)
    assert [t["code"] for t in c.get("/api/tickets?q=YLK-04").json()] == ["TKT-00007"]
    assert c.get("/api/tickets?sort=bogus").status_code == 422


def test_ticket_workflow_persists(demo_client):
    c = demo_client
    # TKT-00008 is a ground fault (safety): a technician without safety certification is refused outright
    r = c.post("/api/tickets/TKT-00008/transition", json={"to_status": "assigned", "technician_id": "TECH-02"})
    assert r.status_code == 403 and r.json()["detail"]["safety"] is True
    # TECH-03 is certified but busy on TKT-00005: a conflict that needs explicit confirmation and a reason
    r = c.post("/api/tickets/TKT-00008/transition", json={"to_status": "assigned", "technician_id": "TECH-03"})
    assert r.status_code == 409 and any("at capacity" in x for x in r.json()["detail"]["conflicts"])
    r = c.post("/api/tickets/TKT-00008/transition", json={"to_status": "assigned", "technician_id": "TECH-03",
                                                          "confirm_conflicts": True, "note": "critical, nearest certified"})
    assert r.status_code == 200 and r.json()["status"] == "assigned"
    hist = c.get("/api/tickets/TKT-00008").json()["history"]
    assert [h for h in hist if h["kind"] == "status"][-1]["to"] == "assigned"
    assert any(h["kind"] == "assignment" and h["override"] and "OVERRIDE" in h["note"] for h in hist)


# ------------------------------------------------------------------ scenarios
def test_scenarios_change_data(demo_client):
    c = demo_client
    lst = c.get("/api/demo/scenarios").json()
    assert {s["key"] for s in lst["scenarios"]} == {"normal", "fault_burst", "site_outage", "high_demand", "backlog",
                                                    "recovery", "policy_comparison", "degraded_network", "major_incident"}
    before = len(c.get("/api/tickets").json())
    r = c.post("/api/demo/scenarios/fault_burst").json()
    assert r["title"] == "Critical fault burst" and r["label"] == "SIMULATED"
    assert len(c.get("/api/tickets").json()) == before + 5
    st = c.get("/api/sim/state").json()
    assert st["running"] is True and st["params"]["fault_rate_per_charger_day"] == 2.0
    assert st["active_scenario"]["key"] == "fault_burst"
    c.post("/api/sim/pause")
    assert c.get("/api/sim/state").json()["active_scenario"]["paused"] is True

    c.post("/api/demo/scenarios/site_outage", json={"site": "WFD"})
    wfd = c.get("/api/chargers?site=WFD").json()
    assert all(ch["status"] == "offline" for ch in wfd)

    c.post("/api/demo/scenarios/backlog")
    techs = c.get("/api/technicians").json()
    assert [t["id"] for t in techs if t["available"]] == ["TECH-01"]

    runs_before = len(c.get("/api/experiments").json())
    rid = c.post("/api/demo/scenarios/policy_comparison").json()["detail"]["experiment_run_id"]
    assert len(c.get("/api/experiments").json()) == runs_before + 1
    q = c.get(f"/api/experiments/{rid}").json()["results"]["queueing"]
    assert q["contention_pct"] > 10

    assert c.post("/api/demo/scenarios/nope").status_code == 404
    assert c.post("/api/demo/scenarios/site_outage", json={"site": "ZZZ"}).status_code == 422
    c.post("/api/sim/reset", json={})
    assert c.get("/api/sim/state").json()["active_scenario"] is None
    assert all(t["available"] for t in c.get("/api/technicians").json())


def test_recovery_scenario_resolves_tickets(demo_client):
    c = demo_client
    c.post("/api/demo/scenarios/recovery")
    c.post("/api/sim/pause")
    open_before = c.get("/api/fleet/summary").json()["open_tickets"]
    for _ in range(6):
        c.post("/api/sim/step", json={"n": 240})
    s = c.get("/api/fleet/summary").json()
    assert s["open_tickets"] < open_before
    assert s["availability_pct"] > 85.0


# ------------------------------------------------------------------ experiments + finance
def test_experiment_explains_similar_results(client):
    r = client.post("/api/experiments/run", json={"mode": "quick"}).json()
    q = r["results"]["queueing"]
    assert q["dispatch_decisions"] > 0
    assert "explanation" in q and q["contention_pct"] < 10
    big = client.post("/api/experiments/run", json={"mode": "custom", "config": {
        "seeds": [1, 2, 3], "days": 7, "fleet_multiplier": 3, "technicians": 1}}).json()
    assert big["config"]["fleet_multiplier"] == 3
    assert big["results"]["per_seed"][0]["fifo"]["faults"] > r["results"]["per_seed"][0]["fifo"]["faults"]
    bad = client.post("/api/experiments/run", json={"mode": "custom", "config": {"fleet_multiplier": 9}})
    assert bad.status_code == 422


def test_finance_scenarios_have_cash_flow_series(client):
    r = client.post("/api/investment/evaluate", json={"inputs": {}}).json()
    for name in ("conservative", "base", "optimistic"):
        sc = r["scenarios"][name]
        assert len(sc["monthly_cumulative"]) == len(r["monthly"])
        assert sc["cumulative_by_year"]
    assert r["scenarios"]["optimistic"]["npv"] > r["scenarios"]["conservative"]["npv"]


# ------------------------------------------------------------------ migration
def test_migration_adds_missing_columns(tmp_path, monkeypatch):
    import sqlite3

    from app import db as D
    from app.config import get_settings

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE sim_state (id INTEGER PRIMARY KEY, seed INTEGER, step INTEGER, sim_time DATETIME, "
                "running BOOLEAN, speed INTEGER, params JSON, ticket_counter INTEGER)")
    con.execute("INSERT INTO sim_state VALUES (1, 7, 99, '2026-10-23 02:30:00', 0, 4, '{}', 5)")
    con.commit()
    con.close()
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    D.init_engine(url)
    D.create_schema()
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(sim_state)")}
    row = con.execute("SELECT step, ticket_counter, sessions_total FROM sim_state").fetchone()
    con.close()
    assert "sessions_total" in cols
    assert row == (99, 5, 0)                      # existing data kept, default applied
    assert D.migrate_add_missing_columns() == []  # idempotent


def test_energy_buckets_match_summary(demo_client):
    ts = demo_client.get("/api/analytics/timeseries?minutes=360&bucket_min=30").json()
    assert ts["bucket_min"] == 30 and len(ts["buckets"]) >= 12
    assert abs(sum(b["energy_kwh"] for b in ts["buckets"]) - ts["summary"]["energy_kwh"]) < 0.5
    assert sum(b["sessions"] for b in ts["buckets"]) == ts["summary"]["sessions"]


def test_operational_timeline_hides_sessions(demo_client):
    items = demo_client.get("/api/timeline?operational=true&limit=200").json()["items"]
    assert items and not any(i["type"] in ("session_start", "session_stop") for i in items)


def test_exports_include_demo_provenance(demo_client):
    r = demo_client.get("/api/reports/tickets.csv?source=simulation")
    assert r.status_code == 200
    body = r.text
    assert body.startswith("#") and "SIMULATED" in body.upper()
    assert body.count("TKT-") >= 8
    ev = demo_client.get("/api/reports/events.csv?source=simulation")
    assert ev.status_code == 200 and "fault" in ev.text
