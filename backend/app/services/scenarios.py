"""Quick demo scenarios (SIMULATED).

Each scenario changes real simulator state: parameters, technician
availability and/or injected faults. Nothing is faked in the UI. The active
scenario is stored in the `active_scenario` setting so the dashboard can show
its name, source, start time and state. Scenarios never touch imported or live
records.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import wall_now
from ..core.catalog import CAN_CHARGE
from ..core.policy_sim import ExperimentConfig, run_comparison
from ..models import Charger, ExperimentRun, SimState, Site, Technician
from . import settings_store
from .seed import DEFAULT_SIM_PARAMS
from ..tenancy import as_org, nid, sim_state

SCENARIOS = {
    "normal": {
        "title": "Normal operations",
        "summary": "Healthy fleet: occasional faults (0.15 / charger-day), all DEMO technicians on shift, automatic assignment by priority and simulated repairs.",
        "changes": ["fault rate 0.15 / charger-day", "auto-assignment on (priority)", "all technicians available", "simulator running at speed 4"],
    },
    "degraded_network": {
        "title": "Degraded network",
        "summary": "Unreliable mobile network: communication losses and heartbeat timeouts dominate, chargers reconnect slowly.",
        "changes": ["fault rate 0.5 / charger-day, 70% communication loss", "heartbeat timeout after 2 missed heartbeats",
                    "spontaneous reconnects 5x slower", "simulator running"],
    },
    "major_incident": {
        "title": "Major incident",
        "summary": "A power-delivery failure takes out every DC charger at Koramangala, a ground fault hits Whitefield, faults spike and one technician is off duty.",
        "changes": ["power_delivery on all KRM DC chargers + ground fault at WFD", "fault rate 0.8 / charger-day",
                    "TECH-04 off duty", "auto-assignment on (priority)", "simulator running"],
    },
    "fault_burst": {
        "title": "Critical fault burst",
        "summary": "Five critical faults hit the busiest sites at once and the fault rate rises to 2.0 / charger-day.",
        "changes": ["5 critical faults injected", "fault rate 2.0 / charger-day", "simulator running"],
    },
    "site_outage": {
        "title": "Site connectivity outage",
        "summary": "Every charger at one site loses its network link and goes offline (communication-loss tickets).",
        "changes": ["comm_loss on all chargers at the chosen site (default Whitefield)", "simulator running"],
    },
    "high_demand": {
        "title": "High demand",
        "summary": "Session arrivals triple and sessions last longer, so most chargers are busy and power rises.",
        "changes": ["session start probability x3", "mean session 60 min", "simulator running"],
    },
    "backlog": {
        "title": "Maintenance backlog",
        "summary": "Six faults arrive while only one technician is on shift, so tickets queue and age.",
        "changes": ["6 faults injected", "only TECH-01 available", "auto-dispatch on (priority)", "simulator running"],
    },
    "recovery": {
        "title": "Recovery demonstration",
        "summary": "New faults stop; all technicians work the queue automatically at high speed until chargers recover.",
        "changes": ["fault rate 0", "auto-dispatch on", "all technicians available", "speed 20 steps/tick", "simulator running"],
    },
    "policy_comparison": {
        "title": "Repair policy comparison",
        "summary": "Runs FIFO vs priority on identical fault traces for a congested network and stores the result in the Policy lab.",
        "changes": ["new experiment run: 1 technician, 0.3 faults/charger-day, 10 seeds x 30 days"],
    },
}

BURST = [("SIM-KRM-02", "high_temperature"), ("SIM-WFD-05", "ground_fault"), ("SIM-HSR-05", "overcurrent"),
         ("SIM-KRM-04", "overcurrent"), ("SIM-ECY-05", "high_temperature")]
BACKLOG = [("SIM-KRM-03", "overcurrent"), ("SIM-WFD-06", "payment_reader"), ("SIM-HSR-06", "ground_fault"),
           ("SIM-HBL-05", "high_temperature"), ("SIM-YLK-05", "payment_reader"), ("SIM-ECY-06", "overcurrent")]


def list_scenarios(db: Session) -> dict:
    return {"scenarios": [{"key": k, **v} for k, v in SCENARIOS.items()],
            "active": settings_store.get(db, "active_scenario", None), "label": "SIMULATED"}


def _set_params(state: SimState, **over) -> None:
    p = dict(DEFAULT_SIM_PARAMS)
    p.update(over)
    state.params = p


def _techs(db: Session, available_ids: set[str] | None) -> None:
    if available_ids is not None:
        available_ids = {nid(db, i) for i in available_ids}
    for t in db.scalars(select(Technician)):
        on = True if available_ids is None else t.id in available_ids
        t.availability = "available" if on else "off_duty"
        t.available = on and bool(t.active)


def _inject_many(db: Session, items) -> list[dict]:
    from ..simulator.engine import inject_fault

    out = []
    for cid, cat in items:
        cid = nid(db, cid)
        c = db.get(Charger, cid)
        if c is None:
            continue
        if c.status not in CAN_CHARGE and cat != "comm_loss":
            out.append({"charger": cid, "skipped": f"charger is {c.status}"})
            continue
        r = inject_fault(db, cid, cat, note="scenario")
        out.append({"charger": cid, "category": cat, "ticket": r["ticket"], "deduplicated": r["deduplicated"]})
    return out


def apply(db: Session, key: str, site_code: str | None = None) -> dict:
    """Apply a scenario. Caller commits."""
    from ..simulator.engine import lock

    if key not in SCENARIOS:
        raise KeyError(key)
    with as_org(db):
        return _apply(db, key, site_code)


def _apply(db: Session, key: str, site_code: str | None) -> dict:
    from ..simulator.engine import lock

    state = sim_state(db)
    detail: dict = {}
    with lock():
        if key == "normal":
            _set_params(state, auto_dispatch=True, auto_dispatch_policy="priority")
            _techs(db, None)
            state.speed, state.running = 4, True
        elif key == "degraded_network":
            _set_params(state, fault_rate_per_charger_day=0.5, offline_after_missed_heartbeats=2,
                        reconnect_prob_per_step=DEFAULT_SIM_PARAMS["reconnect_prob_per_step"] / 5,
                        category_mix={"comm_loss": 0.70, "connector_fault": 0.15, "payment_reader": 0.15})
            _techs(db, None)
            state.running = True
        elif key == "major_incident":
            _set_params(state, fault_rate_per_charger_day=0.8, auto_dispatch=True, auto_dispatch_policy="priority")
            _techs(db, {"TECH-01", "TECH-02", "TECH-03"})
            krm = db.scalar(select(Site).where(Site.code == nid(db, "KRM"), Site.source == "simulation"))
            items = []
            if krm is not None:
                items = [(c.id, "power_delivery") for c in db.scalars(select(Charger).where(Charger.site_id == krm.id).order_by(Charger.id))
                         if "DC" in (c.connector_type or "").upper()]
            items.append(("SIM-WFD-02", "ground_fault"))
            detail["injected"] = _inject_many(db, items)
            state.running = True
        elif key == "fault_burst":
            _set_params(state, fault_rate_per_charger_day=2.0)
            detail["injected"] = _inject_many(db, BURST)
            state.running = True
        elif key == "site_outage":
            code = (site_code or "WFD").upper()
            site = db.scalar(select(Site).where(Site.code == nid(db, code), Site.source == "simulation"))
            if site is None:
                raise ValueError(f"Unknown simulated site '{code}'.")
            ids = [c.id for c in db.scalars(select(Charger).where(Charger.site_id == site.id).order_by(Charger.id))]
            detail["site"] = code
            detail["injected"] = _inject_many(db, [(cid, "comm_loss") for cid in ids])
            state.running = True
        elif key == "high_demand":
            _set_params(state, session_start_prob_per_step=DEFAULT_SIM_PARAMS["session_start_prob_per_step"] * 3,
                        mean_session_min=60)
            state.running = True
        elif key == "backlog":
            _set_params(state, auto_dispatch=True, auto_dispatch_policy="priority")
            _techs(db, {"TECH-01"})
            detail["injected"] = _inject_many(db, BACKLOG)
            state.running = True
        elif key == "recovery":
            _set_params(state, fault_rate_per_charger_day=0.0, auto_dispatch=True, auto_dispatch_policy="priority",
                        reconnect_prob_per_step=0.02)
            _techs(db, None)
            state.speed, state.running = 20, True
        elif key == "policy_comparison":
            cfg = ExperimentConfig.from_dict({"seeds": list(range(1, 11)), "days": 30, "technicians": 1,
                                              "fault_rate_per_charger_day": 0.3})
            run = ExperimentRun(kind="comparison", config=cfg.to_dict(), config_hash=cfg.hash(),
                                results=run_comparison(cfg), label="Scenario: congested network (1 technician)",
                                data_source="simulation", created_at=wall_now())
            db.add(run)
            db.flush()
            detail["experiment_run_id"] = run.id
        db.flush()
        from .assignment import retry_unassigned
        retry_unassigned(db, "simulation", state.sim_time)
        db.flush()
    active = {"key": key, "title": SCENARIOS[key]["title"], "source": "simulation", "label": "SIMULATED",
              "started_sim_time": state.sim_time.isoformat() + "Z", "started_at": wall_now().isoformat() + "Z",
              "detail": detail}
    settings_store.put(db, "active_scenario", active)
    return active


def clear_active(db: Session) -> None:
    settings_store.put(db, "active_scenario", None)
