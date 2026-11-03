"""Server-side onboarding wizard state and the truthful setup checklist."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import iso, wall_now
from ..models import Charger, IngestionRun, Site, Technician
from ..models_saas import IntegrationConfig, Organisation
from . import settings_store

USE_CASES = {"fleet_monitoring": "Fleet monitoring", "fault_management": "Fault management",
             "technician_dispatch": "Technician dispatch", "multi_site_analytics": "Multi-site analytics",
             "site_planning": "Site planning", "integration": "API / OCPP integration"}
DATA_MODES = {"demo": "Demo mode (synthetic data, labelled SIMULATED)", "csv": "CSV import of an operator export",
              "operator_api": "Operator REST API (authorised credentials)", "ocpp": "OCPP 1.6J charge points"}
STEPS = ["Organisation profile", "Use case", "Data mode", "Configure workspace", "Verify setup", "Enter dashboard"]


def state(org: Organisation) -> dict:
    ob = dict(org.onboarding or {})
    ob.setdefault("step", 1)
    ob.setdefault("completed", False)
    ob.setdefault("use_cases", [])
    ob.setdefault("data_mode", None)
    return ob


def save(db: Session, org: Organisation, data: dict) -> dict:
    ob = state(org)
    if "step" in data and data["step"] is not None:
        ob["step"] = max(1, min(6, int(data["step"])))
    if data.get("use_cases") is not None:
        bad = [u for u in data["use_cases"] if u not in USE_CASES]
        if bad:
            raise ValueError(f"Unknown use case(s): {', '.join(bad)}")
        ob["use_cases"] = list(dict.fromkeys(data["use_cases"]))
    if data.get("data_mode") is not None:
        if data["data_mode"] not in DATA_MODES:
            raise ValueError("Unknown data mode.")
        ob["data_mode"] = data["data_mode"]
    if data.get("complete"):
        ob["completed"] = True
        ob["completed_at"] = iso(wall_now())
        ob["step"] = 6
    if data.get("reopen"):
        ob["completed"] = False
        ob["step"] = 1
    org.onboarding = ob
    return ob


def checklist(db: Session, org: Organisation) -> list[dict]:
    """Every item reflects stored facts; 'Connected' only after a real successful verification."""
    from .integration_configs import STATE_LABELS, effective_state
    from .entitlements import resolve

    ob = state(org)
    items = []
    profile_ok = bool(org.name and org.org_type and org.country and org.primary_contact_name and
                      org.sites_estimate is not None and org.chargers_estimate is not None)
    items.append({"key": "profile", "label": "Organisation profile", "done": profile_ok,
                  "status": "Complete" if profile_ok else "Add sites, estimated charger count and primary contact"})
    items.append({"key": "use_case", "label": "Use case", "done": bool(ob["use_cases"]),
                  "status": ", ".join(USE_CASES[u] for u in ob["use_cases"]) or "Not chosen"})
    mode = ob.get("data_mode")
    mode_status, mode_done = "Not chosen", False
    if mode == "demo":
        mode_done = bool(org.demo_workspace_ready)
        mode_status = "Demo mode: simulated fleet ready (SIMULATED)" if mode_done else "Demo mode: create the simulated workspace"
    elif mode == "csv":
        ok = db.scalar(select(func.count(IngestionRun.id)).where(IngestionRun.org_id == org.id, IngestionRun.connector == "csv_import",
                                                                IngestionRun.accepted > 0)) or 0
        mode_done = ok > 0
        mode_status = f"CSV import: {ok} successful import(s)" if ok else "CSV import: no successful import yet"
    elif mode in ("operator_api", "ocpp"):
        cfgs = list(db.scalars(select(IntegrationConfig).where(IntegrationConfig.org_id == org.id, IntegrationConfig.kind == mode)))
        if not cfgs:
            mode_status = "Not configured"
        else:
            st = effective_state(cfgs[0])
            mode_done = st == "connected"
            mode_status = STATE_LABELS[st] + (" (needs the operator's authorisation and credentials)" if st in
                                              ("not_configured", "draft", "awaiting_credentials") else "")
    items.append({"key": "data_mode", "label": "Data mode", "done": mode_done, "status": mode_status, "mode": mode})
    sites = db.scalar(select(func.count(Site.id)).where(Site.org_id == org.id)) or 0
    techs = db.scalar(select(func.count(Technician.id)).where(Technician.org_id == org.id)) or 0
    chargers = db.scalar(select(func.count(Charger.id)).where(Charger.org_id == org.id)) or 0
    items.append({"key": "sites", "label": "Sites", "done": sites > 0, "status": f"{sites} site(s), {chargers} charger(s)"})
    items.append({"key": "technicians", "label": "Technician records", "done": techs > 0, "status": f"{techs} technician record(s)"})
    sla_saved = bool(settings_store.get(db, "sla_policy_confirmed", None))
    items.append({"key": "sla", "label": "SLA policy", "done": sla_saved,
                  "status": "Confirmed" if sla_saved else "Using defaults (critical 4 h, major 12 h, minor 48 h) - review and confirm"})
    prefs = org.preferences or {}
    items.append({"key": "alerts", "label": "Alert preferences", "done": "alerts" in prefs,
                  "status": "In-app alerts only (no e-mail or SMS is configured)" if "alerts" in prefs else "Not reviewed"})
    ent = resolve(db, org.id)
    items.append({"key": "subscription", "label": "Subscription", "done": True,
                  "status": f"{ent['plan_name']} ({ent['state']})"})
    return items
