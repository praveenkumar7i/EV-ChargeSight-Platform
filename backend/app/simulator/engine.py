"""Live fleet simulator (SIMULATED data only).

This is an internal event generator that emits OCPP-1.6-style events
(BootNotification, Heartbeat, StatusNotification, MeterValues,
Start/StopTransaction, Reset). It does NOT open an OCPP WebSocket connection;
the optional real OCPP central system lives in integrations/ocpp_central.py.

Determinism: step k uses `random.Random(f"{seed}:{k}")` and chargers are
processed in id order, so the same seed + parameters + step count (without
manual interventions) always produce the same events.
"""
from __future__ import annotations

import math
import random
import threading
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.catalog import FAULT_TYPES, SAFETY_CATEGORIES
from ..models import Charger, ChargerReading, Event, FleetSnapshot, SimState, Site, Technician, Ticket
from ..services import tickets as ticket_svc
from ..services.events import process_fault, process_offline, record_event
from ..services.seed import DEFAULT_SIM_PARAMS
from ..services.tickets import DONE, is_safety
from ..tenancy import as_org, sim_state

# Telemetry cadence (in simulated time) and retention. Keeps history bounded.
SNAPSHOT_EVERY_S = 60          # fleet snapshot once per simulated minute
READING_EVERY_S = 300          # per-charger power/temperature every 5 simulated minutes
PRUNE_EVERY_S = 3600
SNAPSHOT_KEEP_H = 48
READING_KEEP_H = 12
CHATTER_KEEP_H = 24            # heartbeat / meter-value events older than this are pruned
AMBIENT_C = 30.0               # assumed Bengaluru ambient, simulated connector model

_lock = threading.RLock()


def lock() -> threading.RLock:
    return _lock


def _params(state: SimState) -> dict:
    p = dict(DEFAULT_SIM_PARAMS)
    p.update(state.params or {})
    return p


def step(db: Session, n: int = 1) -> dict:
    """Advance the simulation by n steps. Caller commits."""
    created = {"events": 0, "tickets": 0}
    with _lock, as_org(db):          # one organisation's fleet; a system session steps the legacy workspace
        state = sim_state(db)
        if state is None:
            raise RuntimeError("Simulator state missing; initialise the database first.")
        for _ in range(int(n)):
            created_step = _one_step(db, state)
            created["events"] += created_step["events"]
            created["tickets"] += created_step["tickets"]
        db.flush()
    return created


def _one_step(db: Session, state: SimState) -> dict:
    settings = get_settings()
    p = _params(state)
    state.step += 1
    state.sim_time = state.sim_time + timedelta(seconds=settings.sim_step_seconds)
    now = state.sim_time
    dt_s = settings.sim_step_seconds
    rng = random.Random(f"{state.seed}:{state.step}")
    events = 0
    new_tickets = 0

    sites = {s.id: s for s in db.scalars(select(Site).where(Site.source == "simulation"))}
    max_sessions = max((s.daily_sessions for s in sites.values()), default=1) or 1
    fault_p = p["fault_rate_per_charger_day"] * dt_s / 86400.0
    mix = {k: v for k, v in p["category_mix"].items() if v > 0 and k in FAULT_TYPES}
    cats, weights = list(mix), list(mix.values())
    stop_p = dt_s / (60.0 * p["mean_session_min"])
    hb = timedelta(seconds=p["heartbeat_interval_s"])
    offline_after = hb * p["offline_after_missed_heartbeats"]
    mv_every = max(1, int(p["meter_values_interval_s"] // dt_s))

    chargers = db.scalars(select(Charger).where(Charger.source == "simulation").order_by(Charger.id)).all()
    # fault recurrence: chargers repaired in the last 24 simulated hours fault more often, mostly with the same fault
    recent = {}
    for t in db.scalars(select(Ticket).where(Ticket.source == "simulation", Ticket.status.in_(DONE),
                                             Ticket.resolved_by == "technician",
                                             Ticket.resolved_at >= now - timedelta(hours=24)).order_by(Ticket.resolved_at)):
        recent[t.charger_id] = t.fault_category
    rec_mult = float(p.get("recurrence_multiplier", 1.0))
    for c in chargers:
        r_fault, r_cat, r_sess, r_recon = rng.random(), rng.random(), rng.random(), rng.random()
        c_fault_p = fault_p * (rec_mult if c.id in recent else 1.0)

        # --- silent charger: no heartbeats; offline detection; reconnects ------
        if c.comm_lost:
            if c.status != "offline" and c.last_heartbeat_at and now - c.last_heartbeat_at >= offline_after:
                _, t = process_offline(db, c, now)
                events += 1
                new_tickets += 1 if t else 0
            elif c.status == "offline" and r_recon < p["reconnect_prob_per_step"]:
                _reconnect(db, c, now, "spontaneous reconnect")
                events += 3
            continue

        # --- heartbeat ------------------------------------------------------
        if c.last_heartbeat_at is None or now - c.last_heartbeat_at >= hb:
            record_event(db, c, "heartbeat", now)
            events += 1

        # --- new fault --------------------------------------------------------
        if c.status in ("available", "charging") and r_fault < c_fault_p and cats:
            if c.id in recent and r_fault < fault_p * (rec_mult - 1.0) and recent[c.id] in FAULT_TYPES:
                cat = recent[c.id]                     # the repaired fault came back
            else:
                cat = _weighted(cats, weights, r_cat)
            if c.status == "charging":
                record_event(db, c, "session_stop", now, status_after="available",
                             payload={"reason": "Fault", "meterStop_kWh": round(c.session_kwh, 3)})
                events += 1
            if cat == "comm_loss":
                c.comm_lost = True      # goes silent; detected later by missing heartbeats
            else:
                payload = None
                if cat == "high_temperature":
                    c.temp_c = round(56.0 + 6.0 * rng.random(), 1)
                    payload = {"temperature_C": c.temp_c}
                c.power_now_kw = 0.0
                _, t = process_fault(db, c, cat, now, payload=payload)
                events += 1
                new_tickets += 1 if t else 0
            continue

        # --- charging sessions ---------------------------------------------
        if c.status == "available":
            site = sites.get(c.site_id)
            util = (site.daily_sessions / max_sessions) if site else 0.5
            if r_sess < p["session_start_prob_per_step"] * util:
                c.session_kwh = 0.0
                c.session_started_at = now
                state.sessions_total = (state.sessions_total or 0) + 1
                record_event(db, c, "session_start", now, status_after="charging",
                             payload={"connectorId": 1, "idTag": f"SIM{rng.randint(1000, 9999)}"})
                events += 1
        elif c.status == "charging":
            kw = c.power_kw * (0.6 + 0.3 * rng.random())
            c.power_now_kw = round(kw, 2)
            kwh = kw * dt_s / 3600.0
            c.session_kwh += kwh
            c.total_kwh += kwh
            if r_sess < stop_p:
                record_event(db, c, "session_stop", now, status_after="available",
                             payload={"reason": "EVDisconnected", "meterStop_kWh": round(c.session_kwh, 3)})
                events += 1
            elif state.step % mv_every == 0:
                record_event(db, c, "meter_values", now,
                             payload={"power_kW": round(kw, 2), "session_kWh": round(c.session_kwh, 3)})
                events += 1

    events += _process_remote_resets(db, state, now, rng)
    if p.get("auto_dispatch"):
        events += _simulate_technicians(db, state, now, rng, p)
    if state.step % 4 == 0:                       # once per simulated minute
        ticket_svc.sla_sweep(db, "simulation", now)
    if state.step % 20 == 0 and p.get("auto_dispatch"):   # safety net: retry unassigned every 5 simulated minutes
        from ..services.assignment import retry_unassigned
        retry_unassigned(db, "simulation", now)
    _telemetry(db, state, chargers, now, dt_s)
    return {"events": events, "tickets": new_tickets}


def _thermal(c: Charger, dt_s: float, noise: float, overheated: bool = False) -> None:
    """First-order connector temperature model (SIMULATED, illustrative only).
    Charging heats towards ambient + up to ~16 C at full rated power; idle /
    faulted chargers cool towards ambient. Offline chargers report nothing,
    so their last known value is kept."""
    if c.status != "charging":
        c.power_now_kw = 0.0
        c.session_started_at = None
    if c.status == "offline" or c.comm_lost:
        return
    load = (c.power_now_kw / c.power_kw) if c.power_kw else 0.0
    target = AMBIENT_C + 16.0 * load * min(1.0, c.power_kw / 60.0) + (4.0 if c.power_kw >= 60 and load else 0.0)
    tau = 600.0
    if overheated:
        # an open high-temperature fault models a failed cooling path: the
        # connector stays hot until a technician repairs it
        target, tau = 55.0, 1800.0
    cur = c.temp_c if c.temp_c is not None else AMBIENT_C
    alpha = 1.0 - math.exp(-dt_s / tau)
    c.temp_c = round(cur + (target - cur) * alpha + noise, 2)


def _telemetry(db: Session, state: SimState, chargers, now, dt_s: int) -> None:
    trng = random.Random(f"{state.seed}:{state.step}:thermal")
    hot = set(db.scalars(select(Ticket.charger_id).where(Ticket.source == "simulation", Ticket.status.not_in(DONE),
                                                          Ticket.fault_category == "high_temperature")))
    for c in chargers:
        _thermal(c, dt_s, (trng.random() - 0.5) * 0.2, c.id in hot)
    elapsed = state.step * dt_s
    if elapsed % READING_EVERY_S < dt_s:
        for c in chargers:
            db.add(ChargerReading(charger_id=c.id, ts=now, status=c.status, power_kw=c.power_now_kw or 0.0,
                                  temp_c=None if c.status == "offline" else c.temp_c, source="simulation"))
    if elapsed % SNAPSHOT_EVERY_S < dt_s and chargers:      # no fleet -> no (misleading 0 %) snapshot
        take_snapshot(db, now, chargers, state)
    if elapsed % PRUNE_EVERY_S < dt_s:
        prune_history(db, now)


def take_snapshot(db: Session, now, chargers, state: SimState | None = None) -> FleetSnapshot:
    counts = {k: 0 for k in ("available", "charging", "faulted", "offline", "unavailable")}
    for c in chargers:
        counts[c.status] = counts.get(c.status, 0) + 1
    total = len(chargers)
    db.flush()
    open_q = select(func.count(Ticket.id)).where(Ticket.source == "simulation", Ticket.status.not_in(DONE))
    snap = FleetSnapshot(
        ts=now, source="simulation", total=total, **counts,
        uptime_pct=round(100.0 * (counts["available"] + counts["charging"]) / total, 2) if total else 0.0,
        power_kw=round(sum(c.power_now_kw or 0.0 for c in chargers), 2),
        energy_kwh_total=round(sum(c.total_kwh or 0.0 for c in chargers), 3),
        sessions_total=(state.sessions_total or 0) if state else 0,
        open_tickets=db.scalar(open_q) or 0,
        critical_open=db.scalar(open_q.where(Ticket.severity == "critical")) or 0,
    )
    db.add(snap)
    return snap


def prune_history(db: Session, now) -> None:
    db.execute(delete(FleetSnapshot).where(FleetSnapshot.source == "simulation",
                                           FleetSnapshot.ts < now - timedelta(hours=SNAPSHOT_KEEP_H)))
    db.execute(delete(ChargerReading).where(ChargerReading.source == "simulation",
                                            ChargerReading.ts < now - timedelta(hours=READING_KEEP_H)))
    db.execute(delete(Event).where(Event.source == "simulation", Event.type.in_(("heartbeat", "meter_values")),
                                   Event.ts < now - timedelta(hours=CHATTER_KEEP_H)))


def _weighted(cats: list[str], weights: list[float], u: float) -> str:
    total = sum(weights)
    acc = 0.0
    for c, w in zip(cats, weights):
        acc += w / total
        if u < acc:
            return c
    return cats[-1]


def _reconnect(db: Session, c: Charger, now, reason: str) -> None:
    c.comm_lost = False
    record_event(db, c, "reconnect", now, payload={"reason": reason})
    open_t = ticket_svc.open_ticket_for(db, c.id, "comm_loss")
    if open_t is not None and open_t.status == "open":
        ticket_svc.resolve_by_system(db, open_t, now, "reconnect", f"Charger reconnected ({reason}).")
    elif c.status == "offline":
        other_open = db.scalars(select(Ticket).where(Ticket.charger_id == c.id, Ticket.status.not_in(DONE),
                                                     Ticket.fault_category != "comm_loss")).first()
        record_event(db, c, "status_available" if other_open is None else "fault", now,
                     status_after="available" if other_open is None else "faulted",
                     payload={"after": "reconnect"})


def _process_remote_resets(db: Session, state: SimState, now, rng: random.Random) -> int:
    events = 0
    due = db.scalars(
        select(Ticket).where(Ticket.source == "simulation", Ticket.status == "open",
                             Ticket.remote_reset_attempted.is_(False), Ticket.reset_due_at.is_not(None),
                             Ticket.reset_due_at <= now).order_by(Ticket.id)
    ).all()
    for t in due:
        t.remote_reset_attempted = True
        c = db.get(Charger, t.charger_id)
        ft = FAULT_TYPES[t.fault_category]
        record_event(db, c, "reset_attempt", now, payload={"type": "Soft", "ticket": t.code})
        ok = rng.random() < ft.remote_reset_success
        record_event(db, c, "reset_result", now, payload={"ticket": t.code, "success": ok})
        events += 2
        if ok:
            if t.fault_category == "comm_loss":
                _reconnect(db, c, now, "remote reset")
                events += 1
            else:
                record_event(db, c, "boot", now, payload={"reason": "RemoteReset"})
                ticket_svc.resolve_by_system(db, t, now, "remote_reset", "Remote reset cleared the fault.")
                events += 2
        else:
            from ..services.assignment import try_auto_assign
            try_auto_assign(db, t, now)            # reset failed: dispatch a technician
    return events


def _simulate_technicians(db: Session, state: SimState, now, rng: random.Random, p: dict) -> int:
    """Simulated DEMO technicians work their assigned SIMULATED tickets through the same workflow a
    person would use: accept -> travel (site travel ASSUMPTION) -> repair (catalogue repair time ASSUMPTION)
    -> sometimes wait for parts -> resolve -> for safety faults, inspect and close. Durations are
    simulated time; every step is a normal, audited ticket transition."""
    events = 0
    awaiting_inspection = (Ticket.status == "resolved") & Ticket.closed_at.is_(None) & \
        (Ticket.fault_category.in_(SAFETY_CATEGORIES) | Ticket.safety_related.is_(True))
    active = db.scalars(select(Ticket).where(Ticket.source == "simulation", Ticket.technician_id.is_not(None),
                                             Ticket.status.in_(("assigned", "en_route", "on_site", "waiting_parts")) | awaiting_inspection)
                        .order_by(Ticket.id)).all()
    for t in active:
        tech = db.get(Technician, t.technician_id)
        if tech is None or not tech.is_demo:
            continue                                   # only fictional DEMO staff are simulated
        if is_safety(t) and not tech.safety_certified:
            continue                                   # an uncertified technician does not perform safety work (flagged for review)
        actor = f"{tech.id} (simulated)"
        r = rng.random()
        if t.status == "resolved":
            if not is_safety(t) or t.closed_at is not None:
                continue
            if t.sim_work_due_at is None:
                t.sim_work_due_at = now + timedelta(minutes=float(p.get("inspection_min", 15)))
            elif now >= t.sim_work_due_at:
                ticket_svc.transition(db, t, "closed", now, actor=actor, inspection_passed=True,
                                      note="Simulated post-repair safety inspection passed; charger returned to service.")
                t.sim_work_due_at = None
                events += 1
            continue
        if t.sim_work_due_at is None:
            if t.status == "assigned":
                t.sim_work_due_at = (t.assigned_at or now) + timedelta(minutes=float(p.get("accept_delay_min", 3)))
            else:
                t.sim_work_due_at = now
            continue
        if now < t.sim_work_due_at:
            continue
        repair = float(t.est_repair_min or 60) * (0.7 + 0.6 * r)
        if t.status == "assigned":
            ticket_svc.transition(db, t, "en_route", now, actor=actor, note="Accepted the job (simulated).")
            t.sim_work_due_at = now + timedelta(minutes=float(t.est_travel_min or 30))
        elif t.status == "en_route":
            ticket_svc.transition(db, t, "on_site", now, actor=actor, note="Arrived on site (simulated).")
            t.sim_work_due_at = now + timedelta(minutes=repair)
        elif t.status == "on_site":
            waited = any(h.to_status == "waiting_parts" for h in t.history)
            if not waited and r < float(p.get("parts_wait_prob", 0.0)):
                ticket_svc.transition(db, t, "waiting_parts", now, actor=actor,
                                      note="Replacement part ordered (simulated).")
                t.sim_work_due_at = now + timedelta(minutes=float(p.get("parts_wait_min", 120)))
            else:
                ticket_svc.transition(db, t, "resolved", now, actor=actor, note="Repaired on site (simulated technician).")
                t.sim_work_due_at = None
                events += 1
        elif t.status == "waiting_parts":
            ticket_svc.transition(db, t, "on_site", now, actor=actor, note="Part arrived; resuming repair (simulated).")
            t.sim_work_due_at = now + timedelta(minutes=repair * 0.5)
    return events


def inject_fault(db: Session, charger_id: str, category: str, severity: str | None = None,
                 note: str | None = None) -> dict:
    """Manually inject a fault at the current simulation time (demo control).
    `severity` optionally overrides the catalogue severity of the fault type;
    the override is stored on the ticket and used by the priority engine."""
    from ..core.catalog import SEVERITY_VALUES

    if severity is not None and severity not in SEVERITY_VALUES:
        raise ValueError(f"Unknown severity '{severity}'.")
    with _lock:
        c = db.get(Charger, charger_id)
        if c is None or c.source != "simulation":
            raise LookupError(f"Simulated charger '{charger_id}' not found.")
        if category not in FAULT_TYPES:
            raise ValueError(f"Unknown fault category '{category}'.")
        with as_org(db, c.org_id):
            return _inject(db, c, category, severity, note)


def _inject(db: Session, c: Charger, category: str, severity: str | None, note: str | None) -> dict:
    state = sim_state(db)
    now = state.sim_time
    if category == "comm_loss":
        if c.status == "offline" or c.comm_lost:
            return {"ticket": None, "deduplicated": True, "message": "Charger is already silent/offline."}
        if c.status == "charging":
            record_event(db, c, "session_stop", now, status_after="available",
                         payload={"reason": "PowerLoss", "meterStop_kWh": round(c.session_kwh, 3)})
        c.comm_lost = True
        # immediate offline detection for a manual demo injection
        c.last_heartbeat_at = now - timedelta(seconds=10**6)
        _, t = process_offline(db, c, now)
    else:
        existing = ticket_svc.open_ticket_for(db, c.id, category)
        if existing is not None:
            return {"ticket": existing.code, "deduplicated": True,
                    "message": f"An unresolved {category} ticket already exists for this charger."}
        if c.status == "charging":
            record_event(db, c, "session_stop", now, status_after="available",
                         payload={"reason": "Fault", "meterStop_kWh": round(c.session_kwh, 3)})
        if category == "high_temperature":
            c.temp_c = round(57.0 + (sum(map(ord, c.id)) % 50) / 10.0, 1)
        payload = {"manual": True}
        if c.temp_c is not None and category == "high_temperature":
            payload["temperature_C"] = c.temp_c
        if severity:
            payload["severity_override"] = severity
        if note:
            payload["note"] = note[:200]
        _, t = process_fault(db, c, category, now, payload=payload)
    if t is not None and severity:
        t.severity = severity
    c.power_now_kw = 0.0
    db.flush()
    return {"ticket": t.code if t else None, "deduplicated": t is None, "message": "Fault injected.",
            "severity": t.severity if t else None}
