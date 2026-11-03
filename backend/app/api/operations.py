"""Fleet, simulator, ticket and priority endpoints."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from ..services.tickets import ACTIVE_WORK as TICKET_ACTIVE_WORK, DONE as TICKET_DONE
from sqlalchemy import func, not_, select
from sqlalchemy.orm import Session

from ..clock import iso, wall_now
from ..config import get_settings
from ..core.catalog import CAN_CHARGE, FAULT_TYPES, SEVERITY_MEANING, SEVERITY_VALUES, SOURCE_LABELS
from ..core.priority import FACTOR_LABELS, PriorityConfig
from ..db import get_db
from ..models import Charger, Event, SimState, Site, Technician, Ticket
from ..services import settings_store
from ..services.workspace_repair import fleet_size
from ..services import tickets as ticket_svc
from ..services.events import now_for_source
from ..services.priority_service import rank_open_tickets, ranking_map
from ..services.seed import DEFAULT_SIM_PARAMS, reset_simulation
from ..simulator import engine
from . import serialize as S
from ..tenancy import effective_org, nid
from ..tenancy import sim_state as _sim_state

router = APIRouter(prefix="/api")


def _actor(request: Request | None, fallback: str = "user") -> str:
    """The signed-in user is the actor recorded in audit trails (never a name typed by the client)."""
    name = getattr(getattr(request, "state", None), "user_name", None) if request is not None else None
    return (name or fallback)[:40]


SourceQ = Query("simulation", pattern="^(simulation|operator_api|ocpp|operator_import|simulated_imported|station_inventory|historical_sessions)$")


# ------------------------------------------------------------------ fleet
def _open_by_charger(db: Session, source: str) -> dict[str, list[Ticket]]:
    out: dict[str, list[Ticket]] = {}
    if source == "simulated_imported":
        q = select(Ticket).where(Ticket.source == "simulation", Ticket.charger_id.like("SIM-IMP-%"), Ticket.status.not_in(TICKET_DONE))
    elif source == "simulation":
        q = select(Ticket).where(Ticket.source == "simulation", not_(Ticket.charger_id.like("SIM-IMP-%")), Ticket.status.not_in(TICKET_DONE))
    else:
        q = select(Ticket).where(Ticket.source == source, Ticket.status.not_in(TICKET_DONE))
    for t in db.scalars(q):
        out.setdefault(t.charger_id, []).append(t)
    return out


@router.get("/fleet/summary")
def fleet_summary(source: str = SourceQ, db: Session = Depends(get_db)):
    if source == "station_inventory":
        from ..models import ImportedStation
        org_id = effective_org(db)
        inv_count = db.scalar(select(func.count(ImportedStation.id)).where(ImportedStation.org_id == org_id)) or 0
        conn_count = db.scalar(select(func.sum(ImportedStation.total_connectors)).where(ImportedStation.org_id == org_id)) or 0
        now = wall_now()
        counts = {"available": inv_count, "charging": 0, "faulted": 0, "offline": 0, "unavailable": 0}
        m = {"open": 0, "unresolved_high_severity": 0, "overdue": 0, "mtta_min": None, "mttr_min": None}
        return {
            "source": "station_inventory",
            "label": "IMPORTED INVENTORY",
            "as_of": iso(now),
            "total_chargers": inv_count,
            "total_connectors": int(conn_count),
            "status_counts": counts,
            "able_to_charge": inv_count,
            "availability_pct": None,
            "open_tickets": 0,
            "unresolved_high_severity": 0,
            "overdue_tickets": 0,
            "mtta_min": None,
            "mttr_min": None,
            "ticket_metrics": m,
            "sites": [],
            "power_kw_now": 0.0,
            "test_traffic_chargers": 0,
            "real_chargers": inv_count,
            "energy_kwh_total": 0.0,
            "sessions_total": 0,
            "active_sessions": 0,
            "assigned_or_active_tickets": 0,
            "sim": None,
            "note": "Imported station inventory facts. Live operational status is not claimed without an active telemetry feed.",
        }
    elif source == "historical_sessions":
        from ..models import HistoricalChargingSession
        org_id = effective_org(db)
        sess_count = db.scalar(select(func.count(HistoricalChargingSession.id)).where(HistoricalChargingSession.org_id == org_id)) or 0
        chg_count = db.scalar(select(func.count(func.distinct(HistoricalChargingSession.charger_id))).where(HistoricalChargingSession.org_id == org_id)) or 0
        demand_sum = db.scalar(select(func.sum(HistoricalChargingSession.demand)).where(HistoricalChargingSession.org_id == org_id)) or 0.0
        now = wall_now()
        counts = {"available": chg_count, "charging": 0, "faulted": 0, "offline": 0, "unavailable": 0}
        m = {"open": 0, "unresolved_high_severity": 0, "overdue": 0, "mtta_min": None, "mttr_min": None}
        return {
            "source": "historical_sessions",
            "label": "HISTORICAL RESEARCH",
            "as_of": iso(now),
            "total_chargers": chg_count,
            "total_sessions": sess_count,
            "demand_total_kwh": round(float(demand_sum), 1),
            "status_counts": counts,
            "able_to_charge": chg_count,
            "availability_pct": None,
            "open_tickets": 0,
            "unresolved_high_severity": 0,
            "overdue_tickets": 0,
            "mtta_min": None,
            "mttr_min": None,
            "ticket_metrics": m,
            "sites": [],
            "power_kw_now": 0.0,
            "test_traffic_chargers": 0,
            "real_chargers": chg_count,
            "energy_kwh_total": round(float(demand_sum), 1),
            "sessions_total": sess_count,
            "active_sessions": 0,
            "assigned_or_active_tickets": 0,
            "sim": None,
            "note": "Historical research charging records (IEEE format). Historical data is never represented as live telemetry.",
        }
    elif source == "simulated_imported":
        chargers = db.scalars(select(Charger).where(Charger.source == "simulation", Charger.id.like("SIM-IMP-%"))).all()
        sites = db.scalars(select(Site).where(Site.source == "simulation", Site.code.like("SIM-IMP-%")).order_by(Site.code)).all()
        tickets = db.scalars(select(Ticket).where(Ticket.source == "simulation", Ticket.charger_id.like("SIM-IMP-%"))).all()
        now = now_for_source(db, "simulation")
    elif source == "simulation":
        chargers = db.scalars(select(Charger).where(Charger.source == "simulation", not_(Charger.id.like("SIM-IMP-%")))).all()
        sites = db.scalars(select(Site).where(Site.source == "simulation", not_(Site.code.like("SIM-IMP-%"))).order_by(Site.code)).all()
        tickets = db.scalars(select(Ticket).where(Ticket.source == "simulation", not_(Ticket.charger_id.like("SIM-IMP-%")))).all()
        now = now_for_source(db, source)
    else:
        chargers = db.scalars(select(Charger).where(Charger.source == source)).all()
        sites = db.scalars(select(Site).where(Site.source == source).order_by(Site.code)).all()
        tickets = db.scalars(select(Ticket).where(Ticket.source == source)).all()
        now = now_for_source(db, source)
    sla = settings_store.sla_hours(db)
    m = ticket_svc.ticket_metrics(tickets, now, sla)
    counts = {s: 0 for s in ("available", "charging", "faulted", "offline", "unavailable")}
    for c in chargers:
        counts[c.status] = counts.get(c.status, 0) + 1
    # site-level availability and downtime (downtime = sum of open-fault time per charger, this run)
    site_rows = []
    for s in sites:
        cs = [c for c in chargers if c.site_id == s.id]
        st = [t for t in tickets if t.site_id == s.id]
        down_min = 0.0
        per_charger: dict[str, list[tuple]] = {}
        for t in st:
            end = t.resolved_at or now
            per_charger.setdefault(t.charger_id, []).append((t.detected_at, end))
        for iv in per_charger.values():
            iv.sort()
            cur_s, cur_e = None, None
            for a, b in iv:
                if cur_e is None or a > cur_e:
                    if cur_e is not None:
                        down_min += (cur_e - cur_s).total_seconds() / 60
                    cur_s, cur_e = a, b
                else:
                    cur_e = max(cur_e, b)
            if cur_e is not None:
                down_min += (cur_e - cur_s).total_seconds() / 60
        up = sum(1 for c in cs if c.status in CAN_CHARGE)
        site_rows.append({
            **S.site(s), "chargers": len(cs), "able_to_charge": up,
            "power_kw": round(sum(c.power_now_kw or 0.0 for c in cs), 1),
            "critical_open": sum(1 for t in st if t.status not in TICKET_DONE and t.severity == "critical"),
            "availability_pct": 100 * up / len(cs) if cs else None,
            "status_counts": {k: sum(1 for c in cs if c.status == k) for k in counts},
            "open_tickets": sum(1 for t in st if t.status not in TICKET_DONE),
            "downtime_h": down_min / 60,
        })
    state = _sim_state(db)
    return {
        "source": source,
        "label": ("SIMULATED OCPP" if source == "ocpp" and chargers and all(getattr(c, "test_traffic", False) for c in chargers)
                  else SOURCE_LABELS[source]),
        "as_of": iso(now),
        "total_chargers": len(chargers), "status_counts": counts,
        "able_to_charge": counts["available"] + counts["charging"],
        "availability_pct": 100 * (counts["available"] + counts["charging"]) / len(chargers) if chargers else None,
        "open_tickets": m["open"], "unresolved_high_severity": m["unresolved_high_severity"],
        "overdue_tickets": m["overdue"], "mtta_min": m["mtta_min"], "mttr_min": m["mttr_min"],
        "ticket_metrics": m, "sites": site_rows,
        "power_kw_now": round(sum(c.power_now_kw or 0.0 for c in chargers), 1),
        "test_traffic_chargers": sum(1 for c in chargers if getattr(c, "test_traffic", False)),
        "real_chargers": sum(1 for c in chargers if not getattr(c, "test_traffic", False)) if source != "simulation" else 0,
        "energy_kwh_total": round(sum(c.total_kwh or 0.0 for c in chargers), 1),
        "sessions_total": state.sessions_total if state and source == "simulation" else None,
        "active_sessions": counts["charging"],
        "assigned_or_active_tickets": sum(1 for t in tickets if t.status in TICKET_ACTIVE_WORK),
        "sim": {"running": state.running, "step": state.step, "seed": state.seed, "speed": state.speed} if state and source == "simulation" else None,
        "downtime_definition": "Per site: sum over chargers of the union of [detected, resolved or now) ticket intervals in the current run.",
    }


@router.get("/sources/status")
def sources_status(db: Session = Depends(get_db)):
    from ..services.source_status import all_status

    return all_status(db)


@router.get("/sources")
def sources(db: Session = Depends(get_db)):
    rows = db.execute(select(Charger.source, func.count(Charger.id)).group_by(Charger.source)).all()
    return [{"source": s, "label": SOURCE_LABELS[s], "chargers": n} for s, n in rows]


@router.get("/sites")
def list_sites(source: str | None = None, db: Session = Depends(get_db)):
    q = select(Site).order_by(Site.code)
    if source == "simulated_imported":
        q = q.where(Site.source == "simulation", Site.code.like("SIM-IMP-%"))
    elif source == "simulation":
        q = q.where(Site.source == "simulation", not_(Site.code.like("SIM-IMP-%")))
    elif source:
        q = q.where(Site.source == source)
    return [S.site(s) for s in db.scalars(q)]


@router.get("/chargers")
def list_chargers(source: str = SourceQ, site: str | None = None, status: str | None = None,
                  connector: str | None = None, severity: str | None = None, ticket_state: str | None = None,
                  q: str | None = None, db: Session = Depends(get_db)):
    if source == "station_inventory":
        from ..models import ImportedStation
        org_id = effective_org(db)
        inv_stmt = select(ImportedStation).where(ImportedStation.org_id == org_id)
        if q:
            inv_stmt = inv_stmt.where(ImportedStation.station_name.ilike(f"%{q}%") | ImportedStation.operator.ilike(f"%{q}%"))
        stations = db.scalars(inv_stmt.limit(100)).all()
        return [{
            "id": stn.station_id, "site_id": None, "site_code": stn.station_id,
            "site_name": stn.station_name, "connector_type": stn.charger_type or "CCS2 / Type 2",
            "power_kw": 50.0,
            "status": "available", "last_event_at": iso(stn.created_at),
            "last_heartbeat_at": None, "session_kwh": 0.0, "total_kwh": 0.0,
            "source": "station_inventory", "label": "IMPORTED INVENTORY",
            "test_traffic": False, "external_id": stn.station_id,
            "open_tickets": [], "top_priority_score": None, "top_priority_ticket": None,
            "lat": stn.lat, "lng": stn.lng, "power_now_kw": 0.0, "temp_c": None,
            "temp_label": "not reported", "session_started_at": None, "fault_code": None,
        } for stn in stations]
    elif source == "historical_sessions":
        from ..models import HistoricalChargingSession
        org_id = effective_org(db)
        h_stmt = select(HistoricalChargingSession).where(HistoricalChargingSession.org_id == org_id)
        if q:
            h_stmt = h_stmt.where(HistoricalChargingSession.charger_id.ilike(f"%{q}%") | HistoricalChargingSession.location.ilike(f"%{q}%"))
        h_sessions = db.scalars(h_stmt.limit(100)).all()
        return [{
            "id": hs.charger_id, "site_id": None, "site_code": hs.charger_id,
            "site_name": hs.location or hs.charger_id, "connector_type": hs.charger_type or "Level 2 / Fast",
            "power_kw": 50.0, "status": "available", "last_event_at": iso(hs.end_datetime or hs.start_datetime),
            "last_heartbeat_at": None, "session_kwh": hs.demand or 0.0, "total_kwh": hs.demand or 0.0,
            "source": "historical_sessions", "label": "HISTORICAL RESEARCH",
            "test_traffic": False, "external_id": hs.charger_id,
            "open_tickets": [], "top_priority_score": None, "top_priority_ticket": None,
            "lat": None, "lng": None, "power_now_kw": 0.0, "temp_c": None,
            "temp_label": "historical data", "session_started_at": None, "fault_code": None,
        } for hs in h_sessions]
    elif source == "simulated_imported":
        stmt = select(Charger).where(Charger.source == "simulation", Charger.id.like("SIM-IMP-%")).order_by(Charger.id)
    elif source == "simulation":
        stmt = select(Charger).where(Charger.source == "simulation", not_(Charger.id.like("SIM-IMP-%"))).order_by(Charger.id)
    else:
        stmt = select(Charger).where(Charger.source == source).order_by(Charger.id)

    chargers = db.scalars(stmt).all()
    open_by = _open_by_charger(db, source)
    ranks = ranking_map(db, source if source != "simulated_imported" else "simulation")
    all_by: dict[str, list[Ticket]] = {}
    if ticket_state:
        for t in db.scalars(select(Ticket).where(Ticket.source == "simulation" if source == "simulated_imported" else Ticket.source == source, Ticket.status == ticket_state)):
            all_by.setdefault(t.charger_id, []).append(t)
    out = []
    for c in chargers:
        if site and c.site.code != site:
            continue
        if status and c.status != status:
            continue
        if connector and c.connector_type != connector:
            continue
        ots = open_by.get(c.id, [])
        if severity and not any(t.severity == severity for t in ots):
            continue
        if ticket_state and c.id not in all_by:
            continue
        if q and q.lower() not in c.id.lower():
            continue
        top = max((ranks[t.code] for t in ots if t.code in ranks), key=lambda r: r.score, default=None)
        out.append(S.charger(c, ots, top))
    return out


@router.get("/chargers/{charger_id}")
def charger_detail(charger_id: str, db: Session = Depends(get_db)):
    c = db.get(Charger, charger_id)
    if c is None:
        raise HTTPException(404, f"Charger '{charger_id}' not found.")
    tickets = db.scalars(select(Ticket).where(Ticket.charger_id == c.id).order_by(Ticket.id.desc())).all()
    ranks = ranking_map(db, c.source)
    now = now_for_source(db, c.source)
    sla = settings_store.sla_hours(db)
    events = db.scalars(select(Event).where(Event.charger_id == c.id).order_by(Event.id.desc()).limit(50)).all()
    open_t = [t for t in tickets if t.status not in TICKET_DONE]
    top = max((ranks[t.code] for t in open_t if t.code in ranks), key=lambda r: r.score, default=None)
    return {
        **S.charger(c, open_t, top),
        "site": S.site(c.site),
        "current_ticket": S.ticket(open_t[0], ranks.get(open_t[0].code), ticket_svc.is_overdue(open_t[0], now, sla), now=now)
        if open_t else None,
        "fault_history": [S.ticket(t, ranks.get(t.code), ticket_svc.is_overdue(t, now, sla), now=now) for t in tickets],
        "recent_events": [S.event(e) for e in events],
        "last_event": S.event(events[0]) if events else None,
    }


@router.get("/events")
def list_events(source: str | None = None, charger: str | None = None, type: str | None = None,
                after_id: int | None = None, limit: int = Query(100, ge=1, le=1000),
                include_heartbeats: bool = False, db: Session = Depends(get_db)):
    q = select(Event)
    if source:
        q = q.where(Event.source == source)
    if charger:
        q = q.where(Event.charger_id == charger)
    if type:
        q = q.where(Event.type == type)
    elif not include_heartbeats:
        q = q.where(Event.type.not_in(("heartbeat", "meter_values")))
    if after_id:
        q = q.where(Event.id > after_id)
    rows = db.scalars(q.order_by(Event.id.desc()).limit(limit)).all()
    return [S.event(e) for e in rows]


# ------------------------------------------------------------------ simulator
class SimConfigIn(BaseModel):
    speed: int | None = Field(None, ge=1, le=60)
    params: dict | None = None


class ResetIn(BaseModel):
    seed: int | None = Field(None, ge=0, le=2**31 - 1)
    params: dict | None = None


class StepIn(BaseModel):
    n: int = Field(1, ge=1, le=5000)


class InjectIn(BaseModel):
    charger_id: str = Field(..., min_length=1, max_length=64)
    category: str
    severity: Literal["critical", "major", "minor"] | None = None
    note: str | None = Field(None, max_length=200)


def _validate_params(p: dict) -> dict:
    unknown = set(p) - set(DEFAULT_SIM_PARAMS)
    if unknown:
        raise HTTPException(422, f"Unknown simulator parameter(s): {', '.join(sorted(unknown))}.")
    out = dict(p)
    for k in ("fault_rate_per_charger_day", "session_start_prob_per_step", "reconnect_prob_per_step"):
        if k in out and not (0 <= float(out[k]) <= 50):
            raise HTTPException(422, f"{k} out of range.")
    if "mean_session_min" in out and float(out["mean_session_min"]) <= 0:
        raise HTTPException(422, "mean_session_min must be > 0.")
    if "category_mix" in out:
        mix = out["category_mix"]
        if not isinstance(mix, dict) or any(k not in FAULT_TYPES or float(v) < 0 for k, v in mix.items()) \
                or sum(float(v) for v in mix.values()) <= 0:
            raise HTTPException(422, "category_mix must map known categories to non-negative shares.")
    if "auto_dispatch_policy" in out and out["auto_dispatch_policy"] not in ("priority", "fifo"):
        raise HTTPException(422, "auto_dispatch_policy must be 'priority' or 'fifo'.")
    return out


def _state_dict(state: SimState, db: Session) -> dict:
    return {
        "running": state.running, "seed": state.seed, "step": state.step, "sim_time": iso(state.sim_time),
        "speed": state.speed, "params": {**DEFAULT_SIM_PARAMS, **(state.params or {})},
        "step_seconds": get_settings().sim_step_seconds, "tick_ms": get_settings().sim_tick_ms,
        "events_total": db.scalar(select(func.count(Event.id)).where(Event.source == "simulation")),
        "label": "SIMULATED",
        "transport": "internal event bus (OCPP 1.6-style messages; no OCPP connection)",
        "active_scenario": settings_store.get(db, "active_scenario", None),
        "demo": settings_store.get(db, "demo_seeded", None),
        "fleet_size": fleet_size(db),
        "no_fleet_message": NO_FLEET_MESSAGE,
    }


NO_FLEET_MESSAGE = ("This workspace has no simulated chargers, so there is nothing to simulate. Open the Presentation Demo "
                    "for a ready-made SIMULATED fleet, or add your own data: import an authorised CSV, or configure an "
                    "operator API or OCPP connection.")


def _require_fleet(db: Session) -> None:
    if fleet_size(db) == 0:
        raise HTTPException(409, {"code": "no_fleet", "message": NO_FLEET_MESSAGE})


@router.get("/sim/state")
def sim_state(db: Session = Depends(get_db)):
    return _state_dict(_sim_state(db), db)


@router.post("/sim/start")
def sim_start(db: Session = Depends(get_db)):
    _require_fleet(db)
    with engine.lock():
        st = _sim_state(db)
        st.running = True
        active = settings_store.get(db, "active_scenario", None)
        if active and active.get("paused"):
            settings_store.put(db, "active_scenario", {**active, "paused": False})
        db.commit()
    return _state_dict(st, db)


@router.post("/sim/resume")
def sim_resume(db: Session = Depends(get_db)):
    return sim_start(db)


@router.post("/sim/pause")
def sim_pause(db: Session = Depends(get_db)):
    with engine.lock():
        st = _sim_state(db)
        st.running = False
        active = settings_store.get(db, "active_scenario", None)
        if active:
            settings_store.put(db, "active_scenario", {**active, "paused": True})
        db.commit()
    return _state_dict(st, db)


@router.post("/sim/reset")
def sim_reset(body: ResetIn, db: Session = Depends(get_db)):
    params = _validate_params(body.params) if body.params else None
    from ..services import scenarios

    with engine.lock():
        st = reset_simulation(db, seed=body.seed, params=params)
        for tech in db.scalars(select(Technician)):
            tech.available = True
        scenarios.clear_active(db)
        db.commit()
    return {**_state_dict(st, db),
            "message": "Simulation reset: simulated events, tickets and history were cleared and all chargers set to "
                       "Available at 08:00 IST. Imported/live data untouched. Use 'Restore demo' to rebuild the demo dataset."}


@router.post("/sim/step")
def sim_step(body: StepIn, db: Session = Depends(get_db)):
    _require_fleet(db)
    created = engine.step(db, body.n)
    db.commit()
    return {**_state_dict(_sim_state(db), db), "created": created}


@router.patch("/sim/config")
def sim_config(body: SimConfigIn, db: Session = Depends(get_db)):
    with engine.lock():
        st = _sim_state(db)
        if body.speed is not None:
            st.speed = body.speed
        if body.params:
            merged = {**DEFAULT_SIM_PARAMS, **(st.params or {}), **_validate_params(body.params)}
            st.params = merged
        db.commit()
    return _state_dict(st, db)


@router.post("/sim/inject")
def sim_inject(body: InjectIn, db: Session = Depends(get_db)):
    try:
        out = engine.inject_fault(db, body.charger_id, body.category, body.severity, body.note)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.commit()
    return out


@router.get("/catalog")
def catalog():
    return {
        "fault_types": [{"key": f.key, "label": f.label, "severity": f.severity, "ocpp_error_code": f.ocpp_error_code,
                         "remote_reset_success": f.remote_reset_success, "repair_mean_min": f.repair_mean_min,
                         "default_share": f.default_share} for f in FAULT_TYPES.values()],
        "severity_values": SEVERITY_VALUES, "severity_meaning": SEVERITY_MEANING,
        "workflow": list(ticket_svc.STATUSES), "workflow_labels": ticket_svc.STATUS_LABELS,
        "factor_labels": FACTOR_LABELS, "labels": "Catalogue values are ASSUMPTIONS.",
    }


# ------------------------------------------------------------------ tickets
class TransitionIn(BaseModel):
    to_status: Literal["open", "assigned", "en_route", "on_site", "waiting_parts", "waiting_access", "resolved", "closed"]
    technician_id: str | None = None
    note: str = Field("", max_length=2000)
    est_travel_min: float | None = Field(None, ge=0, le=24 * 60)
    override: bool = False
    inspection_passed: bool | None = None
    actor: str = Field("", max_length=40)
    confirm_conflicts: bool = False


class RaiseTicketIn(BaseModel):
    charger_id: str = Field(..., min_length=1, max_length=64)
    issue_category: str
    description: str = Field(..., max_length=4000)
    symptoms: str = Field("", max_length=2000)
    error_code: str | None = Field(None, max_length=60)
    impact: Literal["unavailable", "reduced_capacity", "intermittent", "informational"] | None = None
    unable_to_charge: bool | None = None
    affected_chargers: int = Field(1, ge=1, le=100)
    reported_by: str = Field("", max_length=80)
    client_request_id: str | None = Field(None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class AssignIn(BaseModel):
    technician_id: str = Field(..., min_length=1, max_length=32)
    actor: str = Field("", max_length=40)
    reason: str = Field("", max_length=500)
    confirm_conflicts: bool = False


class UnassignIn(BaseModel):
    actor: str = Field("", max_length=40)
    reason: str = Field(..., min_length=1, max_length=500)


class NoteIn(BaseModel):
    actor: str = Field("", max_length=40)
    note: str = Field(..., min_length=1, max_length=2000)


class TechnicianIn(BaseModel):
    id: str | None = Field(None, min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=80)
    zone: str = Field("", max_length=40)
    active: bool = True
    availability: Literal["available", "unavailable", "off_duty"] = "available"
    skills: list[str] = Field(default_factory=list, max_length=20)
    equipment: list[Literal["ac", "dc"]] = Field(default_factory=list, max_length=2)
    safety_certified: bool = False
    capacity: int = Field(1, ge=1, le=10)
    base_lat: float | None = Field(None, ge=-90, le=90)
    base_lng: float | None = Field(None, ge=-180, le=180)
    base_label: str = Field("", max_length=80)
    shift_start: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    notes: str = Field("", max_length=1000)


class TechnicianPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    zone: str | None = Field(None, max_length=40)
    active: bool | None = None
    availability: Literal["available", "unavailable", "off_duty"] | None = None
    skills: list[str] | None = Field(None, max_length=20)
    equipment: list[Literal["ac", "dc"]] | None = Field(None, max_length=2)
    safety_certified: bool | None = None
    capacity: int | None = Field(None, ge=1, le=10)
    base_lat: float | None = Field(None, ge=-90, le=90)
    base_lng: float | None = Field(None, ge=-180, le=180)
    base_label: str | None = Field(None, max_length=80)
    shift_start: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    notes: str | None = Field(None, max_length=1000)


def _is_admin(token: str | None) -> bool:
    admin = get_settings().admin_token
    return bool(admin) and token == admin


def _tech_dict(db: Session, t: Technician) -> dict:
    from ..services import assignment as A

    active = db.scalars(select(Ticket).where(Ticket.technician_id == t.id, Ticket.status.in_(TICKET_ACTIVE_WORK)).order_by(Ticket.id)).all()
    return {"id": t.id, "name": t.name, "zone": t.zone, "available": t.available, "active": t.active,
            "availability": t.availability, "status": A.effective_status(db, t), "skills": t.skills or [],
            "equipment": t.equipment or [], "safety_certified": t.safety_certified, "capacity": t.capacity,
            "workload": len(active), "active_tickets": [x.code for x in active],
            "active_ticket": active[0].code if active else None,
            "base_lat": t.base_lat, "base_lng": t.base_lng, "base_label": t.base_label,
            "shift_start": t.shift_start, "shift_end": t.shift_end, "is_demo": t.is_demo, "notes": t.notes,
            "label": "DEMO (fictional)" if t.is_demo else "registered technician"}


def _validate_skills(skills: list[str] | None) -> None:
    from ..core.catalog import SKILLS

    bad = [x for x in (skills or []) if x not in SKILLS]
    if bad:
        raise HTTPException(422, f"Unknown skill(s): {', '.join(bad)}. Valid: {', '.join(SKILLS)}.")


def _after_technician_change(db: Session) -> None:
    from ..services.assignment import retry_unassigned

    for src in ("simulation", "ocpp", "operator_api"):
        retry_unassigned(db, src, now_for_source(db, src))


@router.get("/technicians")
def technicians(db: Session = Depends(get_db)):
    return [_tech_dict(db, t) for t in db.scalars(select(Technician).order_by(Technician.id))]


@router.post("/technicians")
def create_technician(body: TechnicianIn, db: Session = Depends(get_db)):
    _validate_skills(body.skills)
    tid = nid(db, body.id or f"TECH-{(db.scalar(select(func.count(Technician.id))) or 0) + 1:02d}")
    while db.get(Technician, tid) is not None:
        if body.id:
            raise HTTPException(409, f"Technician '{tid}' already exists.")
        tid = f"{tid}-X"
    with engine.lock():
        t = Technician(id=tid, **body.model_dump(exclude={"id"}), is_demo=False)
        t.available = body.active and body.availability == "available"
        db.add(t)
        db.flush()
        _after_technician_change(db)
        db.commit()
    return _tech_dict(db, t)


@router.put("/technicians/{tech_id}")
def update_technician(tech_id: str, body: TechnicianPatch, db: Session = Depends(get_db)):
    t = db.get(Technician, tech_id)
    if t is None:
        raise HTTPException(404, f"Technician '{tech_id}' not found.")
    data = body.model_dump(exclude_unset=True)
    _validate_skills(data.get("skills"))
    with engine.lock():
        for k, v in data.items():
            setattr(t, k, v)
        t.available = bool(t.active) and t.availability == "available"
        db.flush()
        _after_technician_change(db)
        db.commit()
    return _tech_dict(db, t)


@router.get("/assignment/policy")
def get_assignment_policy(db: Session = Depends(get_db)):
    return {**settings_store.assignment_policy(db), "label": "ASSUMPTION",
            "note": "Automatic assignment applies to SIMULATED tickets while the simulator's auto_dispatch is on; "
                    "live sources only when auto_assign_live_sources is enabled; imported records never."}


@router.put("/assignment/policy")
def put_assignment_policy(body: dict, db: Session = Depends(get_db)):
    try:
        settings_store.set_assignment_policy(db, body)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e))
    with engine.lock():
        _after_technician_change(db)
        db.commit()
    return get_assignment_policy(db)


@router.get("/tickets/meta")
def tickets_meta():
    from ..core.catalog import IMPACTS, ISSUE_CATEGORIES, ORIGIN_LABELS, SAFETY_CATEGORIES, SKILLS

    return {"issue_categories": [{"key": k, "label": v[0], "fault_category": v[1], "safety": v[1] in SAFETY_CATEGORIES or k == "safety"}
                                 for k, v in ISSUE_CATEGORIES.items()],
            "impacts": [{"key": k, "label": v} for k, v in IMPACTS.items()],
            "origins": ORIGIN_LABELS, "statuses": list(ticket_svc.STATUSES), "status_labels": ticket_svc.STATUS_LABELS,
            "transitions": {k: sorted(v) for k, v in ticket_svc.ALLOWED.items()},
            "lifecycle_equivalents": ticket_svc.LIFECYCLE_EQUIVALENTS, "skills": list(SKILLS)}


@router.post("/tickets")
def raise_ticket(body: RaiseTicketIn, request: Request, db: Session = Depends(get_db)):
    """User-reported ticket (USER_REPORTED). Created through the same service as every other source."""
    from ..core.catalog import ISSUE_CATEGORIES

    desc = body.description.strip()
    if not desc:
        raise HTTPException(422, "Describe the problem (the description cannot be empty).")
    if body.issue_category not in ISSUE_CATEGORIES:
        raise HTTPException(422, f"Unknown issue category '{body.issue_category}'.")
    with engine.lock():
        if body.client_request_id:
            prev = db.scalar(select(Ticket).where(Ticket.client_request_id == body.client_request_id))
            if prev is not None:      # same submission repeated (double click, retry): return the original
                return {"created": False, "idempotent_replay": True, **_ticket_out(db, prev)}
        c = db.get(Charger, body.charger_id.strip())
        if c is None:
            raise HTTPException(422, f"Unknown charger '{body.charger_id}'. Choose a charger from the list.")
        category = ISSUE_CATEGORIES[body.issue_category][1]
        now = now_for_source(db, c.source)
        report = {**body.model_dump(), "description": desc}
        t = ticket_svc.create_ticket(db, c, category, now, c.source, origin="user_reported", report=report,
                                     actor=_actor(request, body.reported_by or "user"))
        if t is None:
            existing = ticket_svc.open_ticket_for(db, c.id, category)
            db.commit()
            return {"created": False, "deduplicated": True,
                    "message": f"{existing.code} is already open for this charger and issue; your report was added to it.",
                    **_ticket_out(db, existing)}
        db.commit()
    out = _ticket_out(db, t)
    if out["technician_id"]:
        nxt = f"Assigned to {out['technician_name']}. Next: the technician accepts the job."
    elif out["dispatch_needed"]:
        nxt = "No eligible technician available: dispatch needed. See the assignment reasons on the ticket."
    else:
        nxt = out["recommended_action"]
    return {"created": True, "next_action": nxt, **out}


def _ticket_out(db: Session, t: Ticket) -> dict:
    ranks = ranking_map(db, t.source)
    now = now_for_source(db, t.source)
    sla = settings_store.sla_hours(db)
    return S.ticket(t, ranks.get(t.code), ticket_svc.is_overdue(t, now, sla), with_history=True, now=now, sla_hours=sla)


@router.get("/tickets")
def list_tickets(source: str = SourceQ, status: str | None = None, severity: str | None = None,
                 site: str | None = None, charger: str | None = None, category: str | None = None,
                 overdue: bool | None = None, open_only: bool = False, origin: str | None = None,
                 dispatch_needed: bool | None = None,
                 sort: Literal["priority", "severity", "age", "status", "newest"] = "priority",
                 q: str | None = Query(None, max_length=64), technician: str | None = None,
                 limit: int = Query(500, ge=1, le=2000), db: Session = Depends(get_db)):
    if source in ("station_inventory", "historical_sessions"):
        return []
    if source == "simulated_imported":
        stmt = select(Ticket).where(Ticket.source == "simulation", Ticket.charger_id.like("SIM-IMP-%"))
    elif source == "simulation":
        stmt = select(Ticket).where(Ticket.source == "simulation", not_(Ticket.charger_id.like("SIM-IMP-%")))
    else:
        stmt = select(Ticket).where(Ticket.source == source)
    if status:
        stmt = stmt.where(Ticket.status == status)
    if open_only:
        stmt = stmt.where(Ticket.status.not_in(TICKET_DONE))
    if severity:
        stmt = stmt.where(Ticket.severity == severity)
    if charger:
        stmt = stmt.where(Ticket.charger_id == charger)
    if category:
        stmt = stmt.where(Ticket.fault_category == category)
    if technician:
        stmt = stmt.where(Ticket.technician_id == technician)
    if origin:
        stmt = stmt.where(Ticket.origin == origin)
    if dispatch_needed is not None:
        stmt = stmt.where(Ticket.dispatch_needed.is_(dispatch_needed))
    tickets = db.scalars(stmt.order_by(Ticket.id.desc())).all()
    if site:
        tickets = [t for t in tickets if t.site and t.site.code == site]
    ranks = ranking_map(db, source)
    now = now_for_source(db, source)
    sla = settings_store.sla_hours(db)
    if q:
        needle = q.lower()
        tickets = [t for t in tickets if needle in t.code.lower() or needle in t.charger_id.lower()
                   or needle in t.fault_category.lower() or (t.site and needle in t.site.name.lower())
                   or (t.technician_id and needle in t.technician_id.lower()) or needle in (t.description or "").lower()]
    rows = [S.ticket(t, ranks.get(t.code), ticket_svc.is_overdue(t, now, sla), now=now, sla_hours=sla) for t in tickets]
    if overdue is not None:
        rows = [r for r in rows if r["overdue"] == overdue]
    sev_order = {"critical": 0, "major": 1, "minor": 2}
    wf = {s: i for i, s in enumerate(ticket_svc.STATUSES)}
    done = lambda r: r["status"] in TICKET_DONE  # noqa: E731
    if sort == "priority":
        rows.sort(key=lambda r: (done(r), r["priority"]["rank"] if r["priority"] else 10**9, r["detected_at"]))
    elif sort == "severity":
        rows.sort(key=lambda r: (done(r), sev_order.get(r["severity"], 9), r["detected_at"]))
    elif sort == "age":
        rows.sort(key=lambda r: (done(r), -(r["age_min"] or 0)))
    elif sort == "status":
        rows.sort(key=lambda r: (wf.get(r["status"], 9), r["detected_at"]))
    return rows[:limit]


@router.get("/tickets/metrics")
def tickets_metrics(source: str = SourceQ, db: Session = Depends(get_db)):
    tickets = db.scalars(select(Ticket).where(Ticket.source == source)).all()
    return ticket_svc.ticket_metrics(tickets, now_for_source(db, source), settings_store.sla_hours(db))


def _get_ticket(db: Session, code: str) -> Ticket:
    t = db.scalar(select(Ticket).where(Ticket.code == code))
    if t is None:
        raise HTTPException(404, f"Ticket '{code}' not found.")
    return t


@router.get("/tickets/{code}")
def get_ticket(code: str, db: Session = Depends(get_db)):
    return _ticket_out(db, _get_ticket(db, code))


@router.get("/tickets/{code}/eligible-technicians")
def eligible_technicians(code: str, db: Session = Depends(get_db)):
    from ..services import assignment as A

    t = _get_ticket(db, code)
    now = now_for_source(db, t.source)
    return {"ticket": t.code, "required": {"skill": A.required_skill(t), "equipment": A.required_equipment(t.charger),
                                           "safety_certified": ticket_svc.is_safety(t)},
            "candidates": A.candidates(db, t, now), "auto_assign_enabled": A.auto_assign_enabled(db, t.source),
            "ranking_rule": "eligible first -> can meet SLA (when travel is known) -> lower workload share -> lower workload -> shorter distance (when known) -> id"}


@router.post("/tickets/{code}/assign")
def assign_ticket(code: str, body: AssignIn, request: Request, x_admin_token: str | None = Header(None),
                  db: Session = Depends(get_db)):
    from ..services import assignment as A

    t = _get_ticket(db, code)
    with engine.lock():
        try:
            A.manual_assign(db, t, body.technician_id, now_for_source(db, t.source), actor=_actor(request, body.actor or "user"),
                            reason=body.reason, confirm_conflicts=body.confirm_conflicts, admin=_is_admin(x_admin_token))
        except A.AssignmentConflict as e:
            db.rollback()
            raise HTTPException(403 if e.safety else 409, {"message": str(e), "conflicts": e.conflicts, "safety": e.safety})
        except ticket_svc.WorkflowError as e:
            db.rollback()
            raise HTTPException(409, str(e))
        db.commit()
    return _ticket_out(db, t)


@router.post("/tickets/{code}/unassign")
def unassign_ticket(code: str, body: UnassignIn, request: Request, db: Session = Depends(get_db)):
    from ..services import assignment as A

    t = _get_ticket(db, code)
    with engine.lock():
        try:
            A.unassign(db, t, now_for_source(db, t.source), actor=_actor(request, body.actor or "user"), reason=body.reason)
        except A.AssignmentConflict as e:
            db.rollback()
            raise HTTPException(409, str(e))
        db.commit()
    return _ticket_out(db, t)


@router.post("/tickets/{code}/auto-assign")
def auto_assign_ticket(code: str, db: Session = Depends(get_db)):
    from ..services import assignment as A

    t = _get_ticket(db, code)
    if t.status != "open":
        raise HTTPException(409, "Only an open (unassigned) ticket can be auto-assigned.")
    with engine.lock():
        pol = settings_store.assignment_policy(db)
        if not A.auto_assign_enabled(db, t.source):
            raise HTTPException(409, "Automatic assignment is not enabled for this data source.")
        res = A.try_auto_assign(db, t, now_for_source(db, t.source))
        db.commit()
    return {"assigned": res is not None, "policy": pol, **_ticket_out(db, t)}


@router.post("/tickets/{code}/notes")
def add_ticket_note(code: str, body: NoteIn, request: Request, db: Session = Depends(get_db)):
    t = _get_ticket(db, code)
    with engine.lock():
        ticket_svc.history(db, t, now_for_source(db, t.source), kind="note", actor=_actor(request, body.actor or "user"),
                           note=body.note.strip())
        db.commit()
    return _ticket_out(db, t)


@router.post("/tickets/{code}/transition")
def transition_ticket(code: str, body: TransitionIn, request: Request, x_admin_token: str | None = Header(None),
                      db: Session = Depends(get_db)):
    from ..security.guard import require_role_in_endpoint

    t = _get_ticket(db, code)
    if body.to_status == "assigned" or body.override:
        require_role_in_endpoint(request, "dispatch")           # technicians cannot dispatch or override
    if body.override and not _is_admin(x_admin_token):
        admin = get_settings().admin_token
        raise HTTPException(403, "Administrative override is disabled (ADMIN_TOKEN not set)." if not admin else "Invalid admin token.")
    if body.to_status == "assigned" and not body.override:
        if not body.technician_id:
            raise HTTPException(409, "A technician is required to assign a ticket.")
        return assign_ticket(code, AssignIn(technician_id=body.technician_id, actor=body.actor or "user", reason=body.note,
                                            confirm_conflicts=body.confirm_conflicts), request, x_admin_token, db)
    with engine.lock():
        try:
            ticket_svc.transition(db, t, body.to_status, now_for_source(db, t.source), actor=_actor(request, body.actor or "user"),
                                  note=body.note, technician_id=body.technician_id, override=body.override,
                                  est_travel_min=body.est_travel_min, inspection_passed=body.inspection_passed)
        except ticket_svc.WorkflowError as e:
            db.rollback()
            raise HTTPException(409, str(e))
        db.commit()
    return get_ticket(code, db)


# ------------------------------------------------------------------ priority
class PriorityConfigIn(BaseModel):
    weights: dict[str, float]
    age_cap_hours: float = 24.0
    starvation_cap_hours: float = 24.0


@router.get("/priority/config")
def get_priority_config(db: Session = Depends(get_db)):
    cfg = settings_store.priority_config(db)
    return {**cfg.to_dict(), "normalised_weights": cfg.weights.normalised().as_dict(),
            "label": "ASSUMPTION", "defaults": PriorityConfig().to_dict(),
            "note": "Default weights are a design assumption from the research report, not an industry standard."}


@router.put("/priority/config")
def put_priority_config(body: PriorityConfigIn, db: Session = Depends(get_db)):
    try:
        settings_store.set_priority_config(db, body.model_dump())
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e))
    db.commit()
    return get_priority_config(db)


@router.get("/priority/ranking")
def priority_ranking(source: str = SourceQ, db: Session = Depends(get_db)):
    return [r.to_dict() for r in rank_open_tickets(db, source)]


@router.post("/priority/preview")
def priority_preview(body: PriorityConfigIn, source: str = SourceQ, db: Session = Depends(get_db)):
    """Rank current open tickets with alternative weights without saving them."""
    try:
        alt = PriorityConfig.from_dict(body.model_dump())
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e))
    settings_store.put(db, "priority", alt.to_dict())
    db.flush()
    try:
        alt_rank = rank_open_tickets(db, source)
    finally:
        db.rollback()
    base_rank = {r.ticket_ref: r.rank for r in rank_open_tickets(db, source)}
    return [{**r.to_dict(), "current_rank": base_rank.get(r.ticket_ref)} for r in alt_rank]
