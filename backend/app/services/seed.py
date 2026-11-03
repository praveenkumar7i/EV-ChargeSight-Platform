"""Database initialisation and SYNTHETIC demonstration data."""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..clock import SIM_EPOCH
from ..config import get_settings
from ..core.catalog import DEMO_SITES, DEMO_TECH_PROFILE, DEMO_TECHNICIANS, charger_id_for
from ..core.priority import PriorityConfig
from ..models import (
    Charger, ChargerReading, ConnectorHealth, Event, FleetSnapshot, SimState, Site, Technician, Ticket, TicketHistory, ValidationTrack,
)
from . import settings_store
from ..tenancy import as_org, effective_org, nid, sim_state

# Simulator parameters (ASSUMPTIONS). v8 "Normal Operations" profile: a healthy fleet with occasional
# faults that are repaired through the shared ticket / assignment pipeline. Faults arrive at
# 0.15 per charger-day (3x the research baseline of 0.05 so a demo shows a fault every few minutes
# at speed 4) and simulated DEMO technicians accept, travel, repair and (for safety faults) inspect.
DEFAULT_SIM_PARAMS = {
    "fault_rate_per_charger_day": 0.15,
    "category_mix": {"high_temperature": 0.15, "overcurrent": 0.10, "ground_fault": 0.08, "comm_loss": 0.27,
                     "payment_reader": 0.15, "connector_fault": 0.17, "power_delivery": 0.08},
    "session_start_prob_per_step": 0.008,     # scaled by site utilisation
    "mean_session_min": 40,
    "heartbeat_interval_s": 300,              # OCPP HeartbeatInterval
    "offline_after_missed_heartbeats": 3,
    "meter_values_interval_s": 300,
    "reconnect_prob_per_step": 0.002,
    "reset_delay_min": 10,
    "auto_dispatch": True,                    # simulated technicians work assigned SIMULATED tickets
    "auto_dispatch_policy": "priority",       # priority | fifo (queue order when technicians are scarce)
    "accept_delay_min": 3,                    # assigned -> en route
    "parts_wait_prob": 0.10,                  # share of on-site repairs that need parts
    "parts_wait_min": 120,
    "inspection_min": 15,                     # post-repair safety inspection before a safety ticket is closed
    "recurrence_multiplier": 2.0,             # fault rate multiplier for 24 h after a repair (fault recurrence)
}
# The pre-v8 defaults. A database whose simulator still uses exactly these (never changed by the user)
# is moved to the v8 defaults once, because they had no repair path and a 12x baseline fault rate,
# which drove the fleet towards "almost every charger faulted" (see docs/TICKETING.md).
OLD_DEFAULT_SIM_PARAMS_V7 = {
    "fault_rate_per_charger_day": 0.6,
    "category_mix": {"high_temperature": 0.20, "overcurrent": 0.15, "ground_fault": 0.15, "comm_loss": 0.30, "payment_reader": 0.20},
    "session_start_prob_per_step": 0.008, "mean_session_min": 40, "heartbeat_interval_s": 300,
    "offline_after_missed_heartbeats": 3, "meter_values_interval_s": 300, "reconnect_prob_per_step": 0.002,
    "reset_delay_min": 10, "auto_dispatch": False, "auto_dispatch_policy": "priority",
}

CONNECTORS_TRACKED = ("google_maps", "open_charge_map", "osm", "google_places", "operator_api", "csv_import", "ocpp")


def seed_reference_data(db: Session, with_fleet: bool = True) -> bool:
    """Create the demo fleet, technicians and default settings if absent, for the session's organisation
    (a system session works on the legacy demo workspace). Returns True when data was created."""
    from .orgs import ensure_platform

    ensure_platform(db)
    with as_org(db):
        return _seed_reference_data(db, with_fleet)


def _seed_reference_data(db: Session, with_fleet: bool) -> bool:
    created = False
    if with_fleet and db.scalar(select(Site.id).where(Site.source == "simulation").limit(1)) is None:
        for sdef in DEMO_SITES:
            site = Site(code=nid(db, sdef.code), name=sdef.name, lat=sdef.lat, lng=sdef.lng,
                        daily_sessions=sdef.daily_sessions, travel_min_low=sdef.travel_min[0],
                        travel_min_high=sdef.travel_min[1], is_demo_location=True, source="simulation")
            db.add(site)
            db.flush()
            for i, (ctype, kw) in enumerate(sdef.chargers, start=1):
                db.add(Charger(id=nid(db, charger_id_for(sdef.code, i)), site_id=site.id, connector_type=ctype,
                               power_kw=kw, status="available", source="simulation",
                               last_heartbeat_at=SIM_EPOCH, last_event_at=SIM_EPOCH))
        created = True
    for tid, name, zone in (DEMO_TECHNICIANS if with_fleet else ()):   # add any missing demo technician
        if db.get(Technician, nid(db, tid)) is None:
            db.add(Technician(id=nid(db, tid), name=name, zone=zone, available=True))
            created = True
    db.flush()
    apply_demo_technician_profiles(db)
    if sim_state(db) is None:
        db.add(SimState(id=effective_org(db), seed=get_settings().sim_seed, step=0, sim_time=SIM_EPOCH, running=False,
                        speed=4, params=dict(DEFAULT_SIM_PARAMS), ticket_counter=0))
        created = True
    else:
        upgrade_untouched_sim_params(db)
    if settings_store.get(db, "priority", None) is None:
        settings_store.put(db, "priority", PriorityConfig().to_dict())
    if settings_store.get(db, "sla_hours", None) is None:
        settings_store.put(db, "sla_hours", dict(settings_store.DEFAULT_SLA_HOURS))
    for track in ("operational", "financial", "interviews"):
        if db.scalar(select(ValidationTrack).where(ValidationTrack.track == track)) is None:
            db.add(ValidationTrack(track=track, status="not_started"))
    from ..integrations.base import ensure_health
    for c in CONNECTORS_TRACKED:
        ensure_health(db, c)
        db.flush()
    db.flush()
    from .tickets import backfill_v8
    backfill_v8(db)
    db.flush()
    return created


def apply_demo_technician_profiles(db: Session) -> int:
    """Give the four DEMO technicians their v8 profile once (skills empty = never configured).
    They are fictional demonstration staff, flagged is_demo, and only take SIMULATED tickets."""
    sites = {sd.code: sd for sd in DEMO_SITES}
    n = 0
    for tid, prof in DEMO_TECH_PROFILE.items():
        t = db.get(Technician, nid(db, tid))
        if t is None or (t.skills or []):
            continue
        base = sites.get(prof["base"])
        t.is_demo = True
        t.active = True if t.active is None else t.active
        t.availability = "available" if t.available else "unavailable"
        t.skills = list(prof["skills"])
        t.equipment = list(prof["equipment"])
        t.safety_certified = bool(prof["safety_certified"])
        t.capacity = t.capacity or 1
        if base is not None:
            t.base_lat, t.base_lng, t.base_label = base.lat, base.lng, f"{base.name} centroid"
        t.notes = "Fictional DEMO technician for the simulation; not a real employee."
        n += 1
    return n


def upgrade_untouched_sim_params(db: Session) -> bool:
    state = sim_state(db)
    if state is None or settings_store.get(db, "sim_params_v8", None):
        return False
    changed = (state.params or {}) == OLD_DEFAULT_SIM_PARAMS_V7
    if changed:
        state.params = dict(DEFAULT_SIM_PARAMS)
    settings_store.put(db, "sim_params_v8", {"upgraded": changed,
                                            "note": "Pre-v8 default simulator parameters replaced by the v8 Normal Operations profile."
                                            if changed else "Simulator parameters had been customised; left unchanged."})
    return changed


def reset_simulation(db: Session, seed: int | None = None, params: dict | None = None) -> SimState:
    """Delete the organisation's simulated events and tickets and return its fleet to the initial state.
    Imported / live data and other organisations are untouched."""
    with as_org(db):
        return _reset_simulation(db, seed, params)


def _reset_simulation(db: Session, seed: int | None = None, params: dict | None = None) -> SimState:
    sim_ticket_ids = select(Ticket.id).where(Ticket.source == "simulation")
    db.execute(delete(TicketHistory).where(TicketHistory.ticket_id.in_(sim_ticket_ids)))
    db.execute(delete(Ticket).where(Ticket.source == "simulation"))
    db.execute(delete(Event).where(Event.source == "simulation"))
    db.execute(delete(FleetSnapshot).where(FleetSnapshot.source == "simulation"))
    db.execute(delete(ChargerReading).where(ChargerReading.source == "simulation"))
    for c in db.scalars(select(Charger).where(Charger.source == "simulation")):
        c.power_now_kw = 0.0
        c.temp_c = 30.0
        c.session_started_at = None
        c.status = "available"
        c.session_kwh = 0.0
        c.total_kwh = 0.0
        c.comm_lost = False
        c.last_heartbeat_at = SIM_EPOCH
        c.last_event_at = SIM_EPOCH
    state = sim_state(db)
    if state is None:
        seed_reference_data(db)
        state = sim_state(db)
    state.step = 0
    state.sim_time = SIM_EPOCH
    state.running = False
    state.ticket_counter = 0
    state.sessions_total = 0
    if seed is not None:
        state.seed = int(seed)
    merged = dict(DEFAULT_SIM_PARAMS)
    merged.update(params if params is not None else (state.params or {}))
    state.params = merged
    db.flush()
    return state
