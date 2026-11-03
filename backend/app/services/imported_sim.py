"""Simulation on Imported Stations service.

Runs repeatable, deterministic simulation specifically on imported station records.
Generates simulated availability, charging sessions, faults, alerts, and maintenance tickets
while keeping generated simulation data completely separate from original imported records.
Resetting simulation never deletes or alters imported stations or sessions.
"""
from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..clock import wall_now
from ..core.catalog import FAULT_TYPES, SEVERITY_VALUES
from ..models import (
    Charger,
    ChargerReading,
    Event,
    FleetSnapshot,
    ImportedStation,
    Site,
    Technician,
    Ticket,
    TicketHistory,
)
from ..services.events import now_for_source, process_fault, record_event
from ..services.tickets import DONE
from ..tenancy import as_org, effective_org, nid

log = logging.getLogger("chargesight.imported_sim")

SIM_IMP_PREFIX = "SIM-IMP-"


def _extract_kw(rating_str: str | None, default: float = 50.0) -> float:
    if not rating_str:
        return default
    m = re.search(r"(\d+(\.\d+)?)", rating_str)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return default


def get_imported_sim_status(db: Session) -> dict[str, Any]:
    """Get the current simulation state for imported stations."""
    org_id = effective_org(db)
    eligible = db.scalar(
        select(func.count(ImportedStation.id)).where(ImportedStation.org_id == org_id)
    ) or 0

    sim_chargers = db.scalar(
        select(func.count(Charger.id)).where(
            Charger.org_id == org_id,
            Charger.source == "simulation",
            Charger.id.like(f"{SIM_IMP_PREFIX}%"),
        )
    ) or 0

    sim_sites = db.scalar(
        select(func.count(Site.id)).where(
            Site.org_id == org_id,
            Site.source == "simulation",
            Site.code.like(f"{SIM_IMP_PREFIX}%"),
        )
    ) or 0

    sim_tickets = db.scalar(
        select(func.count(Ticket.id)).where(
            Ticket.org_id == org_id,
            Ticket.source == "simulation",
            Ticket.charger_id.like(f"{SIM_IMP_PREFIX}%"),
            Ticket.status.not_in(DONE),
        )
    ) or 0

    return {
        "eligible_imported_stations": eligible,
        "simulated_imported_sites": sim_sites,
        "simulated_imported_chargers": sim_chargers,
        "open_simulated_tickets": sim_tickets,
        "is_active": sim_chargers > 0,
        "label": "SIMULATED IMPORTED STATIONS",
        "description": "Simulation generated over imported station records. Original imported facts are untouched.",
    }


def reset_imported_simulation(db: Session) -> dict[str, Any]:
    """Safely reset and clear all simulation data generated on imported stations.

    NEVER touches ImportedStation or HistoricalChargingSession tables.
    """
    org_id = effective_org(db)

    # 1. Identify simulated chargers created from imported stations
    sim_charger_ids = db.scalars(
        select(Charger.id).where(
            Charger.org_id == org_id,
            Charger.source == "simulation",
            Charger.id.like(f"{SIM_IMP_PREFIX}%"),
        )
    ).all()

    deleted_tickets = 0
    deleted_events = 0

    if sim_charger_ids:
        # Delete associated ticket history and tickets
        ticket_ids = db.scalars(
            select(Ticket.id).where(
                Ticket.org_id == org_id,
                Ticket.charger_id.in_(sim_charger_ids),
            )
        ).all()

        if ticket_ids:
            db.execute(delete(TicketHistory).where(TicketHistory.ticket_id.in_(ticket_ids)))
            del_t = db.execute(delete(Ticket).where(Ticket.id.in_(ticket_ids)))
            deleted_tickets = del_t.rowcount

        # Delete events for these chargers
        del_e = db.execute(
            delete(Event).where(
                Event.org_id == org_id,
                Event.charger_id.in_(sim_charger_ids),
            )
        )
        deleted_events = del_e.rowcount

        # Delete charger readings
        db.execute(
            delete(ChargerReading).where(
                ChargerReading.org_id == org_id,
                ChargerReading.charger_id.in_(sim_charger_ids),
            )
        )

        # Delete chargers
        db.execute(
            delete(Charger).where(
                Charger.org_id == org_id,
                Charger.id.in_(sim_charger_ids),
            )
        )

    # Delete sites created for imported sim
    db.execute(
        delete(Site).where(
            Site.org_id == org_id,
            Site.source == "simulation",
            Site.code.like(f"{SIM_IMP_PREFIX}%"),
        )
    )

    db.flush()

    return {
        "ok": True,
        "message": "Simulation on imported stations was reset successfully. All imported station records remain completely intact.",
        "removed_chargers": len(sim_charger_ids),
        "deleted_simulated_chargers": len(sim_charger_ids),
        "deleted_simulated_events": deleted_events,
        "deleted_simulated_tickets": deleted_tickets,
        "imported_stations_preserved": db.scalar(
            select(func.count(ImportedStation.id)).where(ImportedStation.org_id == org_id)
        ) or 0,
    }


def run_simulation_on_imported_stations(
    db: Session,
    *,
    seed: int = 42,
    stations_limit: int = 15,
    fault_probability: float = 0.35,
) -> dict[str, Any]:
    """Run a deterministic, repeatable simulation pass over eligible imported stations.

    1. Selects up to `stations_limit` imported stations for the current tenant.
    2. Provisions simulated Site & Charger objects if not already present.
    3. Simulates charging sessions, availability transitions, and faults.
    4. Connects faults to the standard maintenance ticket workflow.
    """
    org_id = effective_org(db)
    imported = db.scalars(
        select(ImportedStation)
        .where(ImportedStation.org_id == org_id)
        .order_by(ImportedStation.id)
        .limit(stations_limit)
    ).all()

    if not imported:
        raise ValueError(
            "No imported stations found for your organisation. "
            "Please import a Station Inventory CSV first under Data Integrations."
        )

    rng = random.Random(seed)
    now = now_for_source(db, "simulation")

    created_sites = 0
    created_chargers = 0
    generated_events = 0
    generated_faults = 0
    generated_tickets: list[dict[str, Any]] = []

    sim_chargers: list[Charger] = []

    for stn in imported:
        site_code = f"{SIM_IMP_PREFIX}{stn.id}"
        site = db.scalar(select(Site).where(Site.org_id == org_id, Site.code == site_code))
        if site is None:
            site = Site(
                org_id=org_id,
                code=site_code,
                name=f"{stn.station_name} (Simulated)",
                lat=stn.lat,
                lng=stn.lng,
                daily_sessions=max(4, (stn.total_connectors or 1) * 3),
                is_demo_location=False,
                source="simulation",
            )
            db.add(site)
            db.flush()
            created_sites += 1

        # Cap simulated connectors per station to 2 for balanced simulation
        num_connectors = min(2, max(1, stn.total_connectors or 1))
        kw = _extract_kw(stn.charger_rating, 50.0)

        for c_idx in range(num_connectors):
            cid = f"{site_code}-C{c_idx + 1}"
            ch = db.get(Charger, cid)
            if ch is None:
                ch = Charger(
                    id=cid,
                    org_id=org_id,
                    site_id=site.id,
                    connector_type=stn.charger_type or "CCS2 / Type 2",
                    power_kw=kw,
                    status="available",
                    source="simulation",
                    external_id=f"{stn.station_id}-C{c_idx + 1}",
                    last_event_at=now,
                    last_heartbeat_at=now,
                )
                db.add(ch)
                db.flush()
                created_chargers += 1
            sim_chargers.append(ch)

    # Deterministic simulation pass per charger
    fault_categories = list(FAULT_TYPES.keys())

    for idx, ch in enumerate(sim_chargers):
        r_val = rng.random()

        # Emit standard heartbeat
        record_event(
            db,
            ch,
            "heartbeat",
            now - timedelta(minutes=rng.randint(1, 10)),
            source="simulation",
        )
        generated_events += 1

        # Check for simulated charging session or fault
        if r_val < fault_probability:
            # Generate simulated fault
            cat = fault_categories[idx % len(fault_categories)]
            ft = FAULT_TYPES[cat]
            fault_ts = now - timedelta(minutes=rng.randint(5, 45))

            ev, ticket = process_fault(
                db,
                ch,
                cat,
                fault_ts,
                payload={
                    "simulated_on_imported_station": True,
                    "station_id": ch.site.code,
                    "seed": seed,
                },
                source="simulation",
            )
            generated_events += 1
            generated_faults += 1

            if ticket:
                generated_tickets.append({
                    "ticket_code": ticket.code,
                    "charger_id": ch.id,
                    "station_name": ch.site.name,
                    "fault_category": cat,
                    "severity": ticket.severity,
                    "priority_score": ticket.priority_score,
                    "detected_at": ticket.detected_at.isoformat(),
                })
        else:
            # Simulate active charging or available
            if rng.random() < 0.5:
                # Active charging session
                ch.status = "charging"
                ch.power_now_kw = round(ch.power_kw * rng.uniform(0.6, 0.95), 1)
                ch.session_kwh = round(rng.uniform(8.0, 32.0), 2)
                ch.total_kwh = round((ch.total_kwh or 0.0) + ch.session_kwh, 2)
                record_event(
                    db,
                    ch,
                    "session_start",
                    now - timedelta(minutes=rng.randint(10, 40)),
                    status_after="charging",
                    payload={"meter_kwh": ch.session_kwh},
                    source="simulation",
                )
                generated_events += 1
            else:
                ch.status = "available"
                ch.power_now_kw = 0.0

    db.flush()

    return {
        "ok": True,
        "seed": seed,
        "imported_stations_selected": len(imported),
        "simulated_sites_active": len(imported),
        "simulated_chargers_active": len(sim_chargers),
        "created_new_chargers": created_chargers,
        "generated_events": generated_events,
        "generated_faults": generated_faults,
        "generated_tickets": generated_tickets,
        "message": (
            f"Simulation executed with seed {seed}: {len(sim_chargers)} chargers active across "
            f"{len(imported)} imported stations, {generated_faults} faults generated and open in maintenance tickets."
        ),
    }

