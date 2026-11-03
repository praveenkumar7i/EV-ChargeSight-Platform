"""Connection / data state for each data-source mode of the dashboard selector.

A LIVE label never proves that live data exists: each mode reports one of
  SIMULATED | RECEIVING DATA | STALE | WAITING FOR DATA | NO LIVE DATA |
  NOT CONNECTED | NOT CONFIGURED | ERROR | IMPORTED | NO IMPORTED DATA
computed only from records actually stored and connector health.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import iso, wall_now
from ..config import get_settings
from ..models import Charger, ConnectorHealth, Event, IngestionRun, SimState
from ..tenancy import sim_state

OCPP_STALE_AFTER = timedelta(minutes=12)       # > 2 missed 300 s heartbeats


def _counts(db: Session, source: str) -> tuple[int, int, object]:
    chargers = db.scalar(select(func.count(Charger.id)).where(Charger.source == source)) or 0
    test = db.scalar(select(func.count(Charger.id)).where(Charger.source == source, Charger.test_traffic.is_(True))) or 0
    last = db.scalar(select(func.max(Event.ts)).where(Event.source == source))
    return chargers, test, last


def simulation(db: Session) -> dict:
    st = sim_state(db)
    n, _t, _l = _counts(db, "simulation")
    return {"source": "simulation", "mode": "Simulated fleet", "type": "SIMULATED", "state": "SIMULATED",
            "chargers": n, "last_update": iso(st.sim_time) if st else None, "clock": "simulation time",
            "detail": f"{n} synthetic chargers; {'running' if st and st.running else 'paused'}.",
            "next_action": "" if st and st.running else "Press Start or Resume on the Operations console."}


def imported(db: Session) -> dict:
    n, _t, last = _counts(db, "operator_import")
    run = db.scalar(select(IngestionRun).where(IngestionRun.source == "operator_import")
                    .order_by(IngestionRun.id.desc()).limit(1))
    if n == 0:
        return {"source": "operator_import", "mode": "Imported data", "type": "IMPORTED", "state": "NO IMPORTED DATA",
                "chargers": 0, "last_update": None,
                "detail": "No file has been imported yet.",
                "next_action": "Data integrations -> Import authorised operator events (a labelled synthetic sample CSV can be downloaded there)."}
    return {"source": "operator_import", "mode": "Imported data", "type": "IMPORTED", "state": "IMPORTED",
            "chargers": n, "last_update": iso(run.finished_at) if run else None, "last_event": iso(last),
            "detail": f"{n} imported chargers; last import '{run.dataset_label}'." if run else f"{n} imported chargers.",
            "next_action": ""}


def operator_api(db: Session) -> dict:
    from ..tenancy import LEGACY_ORG_ID, effective_org
    from .operator_sync import status

    if effective_org(db) != LEGACY_ORG_ID:
        return _org_operator_api(db, effective_org(db))
    s = status(db)
    return {"source": "operator_api", "mode": "Operator API (live)", "type": "LIVE", "state": s["state"],
            "chargers": s["chargers"], "last_update": s["last_success_at"], "last_event": s["last_event_at"],
            "detail": s["last_error"] or ("Base URL and key are set." if s["configured"] else "No operator credentials configured."),
            "next_action": s["next_action"], "stale": s["stale"], "poll_seconds": s["poll_seconds"]}


def ocpp(db: Session) -> dict:
    s = get_settings()
    from ..integrations.base import get_health
    h = get_health(db, "ocpp")
    n, test, last = _counts(db, "ocpp")
    url = f"ws://{s.ocpp_host}:{s.ocpp_port}/<charge_point_id>"
    from ..tenancy import LEGACY_ORG_ID, effective_org

    if effective_org(db) != LEGACY_ORG_ID:
        from sqlalchemy import select as _select

        from ..models_saas import IntegrationConfig
        cfg = db.scalar(_select(IntegrationConfig).where(IntegrationConfig.org_id == effective_org(db), IntegrationConfig.kind == "ocpp",
                                                          IntegrationConfig.state != "disabled").limit(1))
        url = (f"ws://{s.ocpp_host}:{s.ocpp_port}/{cfg.ocpp_endpoint_key}/<charge_point_id>" if cfg
               else "Add an OCPP connection under Data integrations -> Connections to get your organisation's endpoint.")
    connected = (h.detail or {}).get("connected_charge_points", []) if h else []
    if n == 0:
        state = "WAITING FOR DATA" if h and h.state in ("configured_unverified", "connected") else "NOT CONNECTED"
        nxt = (f"Start the OCPP central system (python -m app.integrations.ocpp_central in backend/) and connect a charge point "
               f"or the test simulator (python scripts/ocpp_simulator.py) to {url}.")
    else:
        fresh = last is not None and wall_now() - last <= OCPP_STALE_AFTER
        if fresh:
            state, nxt = ("RECEIVING DATA", "") if n - test > 0 else (
                "RECEIVING TEST DATA", "Only the local OCPP simulator is sending data (SIMULATED OCPP). Connect an authorised charger for real telemetry.")
        else:
            state, nxt = "STALE", "No OCPP message for over 12 minutes; check the central system and charge points."
    real = n - test
    parts = []
    if test:
        parts.append(f"{test} SIMULATED OCPP test charger(s)")
    if real:
        parts.append(f"{real} real OCPP charger(s)")
    return {"source": "ocpp", "mode": "OCPP (live)", "type": "LIVE", "state": state, "chargers": n,
            "simulated_ocpp_chargers": test, "real_ocpp_chargers": real, "last_update": iso(last), "last_event": iso(last),
            "endpoint": url, "connected_now": connected,
            "detail": ", ".join(parts) if parts else "No charge point has completed a BootNotification yet.",
            "next_action": nxt}


def all_status(db: Session) -> dict:
    return {
        "as_of": iso(wall_now()),
        "sources": {
            "simulation": simulation(db),
            "simulated_imported": simulated_imported_status(db),
            "station_inventory": station_inventory_status(db),
            "historical_sessions": historical_sessions_status(db),
            "operator_import": imported(db),
            "operator_api": operator_api(db),
            "ocpp": ocpp(db),
        },
    }


def station_inventory_status(db: Session) -> dict:
    from ..models import ImportedStation
    from ..tenancy import effective_org

    org_id = effective_org(db)
    n = db.scalar(select(func.count(ImportedStation.id)).where(ImportedStation.org_id == org_id)) or 0
    connectors = db.scalar(select(func.sum(ImportedStation.total_connectors)).where(ImportedStation.org_id == org_id)) or 0
    last = db.scalar(select(func.max(ImportedStation.created_at)).where(ImportedStation.org_id == org_id))
    if n == 0:
        return {
            "source": "station_inventory",
            "mode": "Imported Station Inventory",
            "type": "INVENTORY",
            "state": "NO IMPORTED DATA",
            "chargers": 0,
            "stations": 0,
            "connectors": 0,
            "last_update": None,
            "detail": "No station inventory has been imported yet.",
            "next_action": "Data integrations -> Import Station Inventory CSV.",
        }
    return {
        "source": "station_inventory",
        "mode": "Imported Station Inventory",
        "type": "INVENTORY",
        "state": "IMPORTED INVENTORY",
        "chargers": n,
        "stations": n,
        "connectors": int(connectors),
        "last_update": iso(last),
        "detail": f"{n} stations ({int(connectors)} connectors) across Karnataka/India. Real station inventory facts; not live status.",
        "next_action": "View stations on the Map page or run simulation under Simulator.",
    }


def historical_sessions_status(db: Session) -> dict:
    from ..models import HistoricalChargingSession
    from ..tenancy import effective_org

    org_id = effective_org(db)
    n = db.scalar(select(func.count(HistoricalChargingSession.id)).where(HistoricalChargingSession.org_id == org_id)) or 0
    chargers = db.scalar(
        select(func.count(func.distinct(HistoricalChargingSession.charger_id))).where(
            HistoricalChargingSession.org_id == org_id
        )
    ) or 0
    demand = db.scalar(
        select(func.sum(HistoricalChargingSession.demand)).where(HistoricalChargingSession.org_id == org_id)
    ) or 0.0
    last = db.scalar(select(func.max(HistoricalChargingSession.created_at)).where(HistoricalChargingSession.org_id == org_id))
    if n == 0:
        return {
            "source": "historical_sessions",
            "mode": "Historical Research Data",
            "type": "HISTORICAL",
            "state": "NO HISTORICAL DATA",
            "chargers": 0,
            "sessions": 0,
            "last_update": None,
            "detail": "No historical charging sessions imported yet.",
            "next_action": "Data integrations -> Import Historical Charging Sessions (IEEE ChargingRecords.csv).",
        }
    return {
        "source": "historical_sessions",
        "mode": "Historical Research Data",
        "type": "HISTORICAL",
        "state": "HISTORICAL RESEARCH",
        "chargers": chargers,
        "sessions": n,
        "total_demand": round(float(demand), 1),
        "last_update": iso(last),
        "detail": f"{n} historical charging sessions across {chargers} chargers (IEEE dataset). Historical facts, not live telemetry.",
        "next_action": "Explore analytics on Historical Charging Sessions page.",
    }


def simulated_imported_status(db: Session) -> dict:
    from ..services.imported_sim import get_imported_sim_status

    st = get_imported_sim_status(db)
    n = st["simulated_imported_chargers"]
    if n == 0:
        return {
            "source": "simulated_imported",
            "mode": "Simulated Imported Stations",
            "type": "SIMULATED",
            "state": "NOT SIMULATING",
            "chargers": 0,
            "last_update": None,
            "detail": f"{st['eligible_imported_stations']} eligible imported stations ready for simulation.",
            "next_action": "Run 'Simulation on Imported Stations' in the Simulator view."
            if st["eligible_imported_stations"]
            else "Import station inventory first.",
        }
    return {
        "source": "simulated_imported",
        "mode": "Simulated Imported Stations",
        "type": "SIMULATED",
        "state": "SIMULATING IMPORTED",
        "chargers": n,
        "sites": st["simulated_imported_sites"],
        "open_tickets": st["open_simulated_tickets"],
        "last_update": iso(wall_now()),
        "detail": f"Simulation active on {st['simulated_imported_sites']} imported stations ({n} chargers, {st['open_simulated_tickets']} open tickets).",
        "next_action": "",
    }


def _org_operator_api(db: Session, org_id: int) -> dict:
    """Operator API state of an organisation's own connection (never the server's .env credentials)."""
    from .integration_configs import STATE_LABELS, effective_state, org_operator_config

    cfg = org_operator_config(db, org_id)
    n, _t, last = _counts(db, "operator_api")
    if cfg is None:
        return {"source": "operator_api", "mode": "Operator API (live)", "type": "LIVE", "state": "NOT CONFIGURED", "chargers": n,
                "last_update": None, "last_event": iso(last), "detail": "No operator API connection has been added.",
                "next_action": "Data integrations -> Connections: add an operator API connection (requires the operator's "
                               "authorisation, base URL and API credential)."}
    st = effective_state(cfg)
    state = {"connected": "RECEIVING DATA" if n else "NO LIVE DATA", "stale": "STALE", "auth_failed": "ERROR",
             "connection_failed": "ERROR", "permission_insufficient": "ERROR", "disabled": "NOT CONNECTED"}.get(st, "NOT CONFIGURED"
             if st in ("not_configured", "draft", "awaiting_credentials") else "NOT CONNECTED")
    return {"source": "operator_api", "mode": "Operator API (live)", "type": "LIVE", "state": state, "chargers": n,
            "last_update": iso(cfg.last_success_at), "last_event": iso(last), "detail": f"{cfg.name}: {STATE_LABELS[st]}",
            "next_action": "" if state == "RECEIVING DATA" else "Open Data integrations -> Connections."}
