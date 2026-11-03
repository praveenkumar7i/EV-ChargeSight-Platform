"""Integration health, operator API sync and authorised CSV import."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import iso
from ..config import get_settings
from ..db import get_db
from ..integrations.base import ConnectorError, list_health, redact, set_health
from ..integrations.csv_import import CsvFormatError, parse_events_csv
from ..integrations.ingest import ingest
from ..integrations.open_charge_map import OpenChargeMapClient
from ..integrations.operator_api import OperatorApiConnector
from ..models import ConnectorHealth, IngestionRun

router = APIRouter(prefix="/api/integrations")

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_HISTORICAL_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB limit for historical research charging datasets (IEEE)

CONNECTOR_INFO = {
    "google_maps": "Google Maps JavaScript API (browser). State is reported by the browser after the map loads or fails.",
    "open_charge_map": "Open Charge Map public listings (locations only, no live status). A Bengaluru snapshot is bundled; live refresh needs OCM_API_KEY.",
    "osm": "OpenStreetMap via Overpass (no key, ODbL). Public listings only; refreshed on demand from the Map page.",
    "google_places": "Google Places API (New), server-side key. Live lookup for the map; results are never stored (Google policy).",
    "operator_api": "Authorised operator REST API (server-side key).",
    "csv_import": "Authorised CSV import of operator event exports.",
    "ocpp": "Optional OCPP 1.6J central system (separate process). Connected only after an accepted BootNotification.",
}


def _legacy_env_only(db: Session) -> None:
    """The .env operator API credentials belong to the platform's system demo workspace only. Other
    organisations configure their own connections (Data integrations -> Connections)."""
    from ..tenancy import LEGACY_ORG_ID, effective_org

    if effective_org(db) != LEGACY_ORG_ID:
        raise HTTPException(409, {"code": "use_connections", "message": (
            "This organisation's operator API is configured under Data integrations -> Connections "
            "(the server's .env credentials are not shared with organisations).")})


def _configured(name: str, db: Session | None = None) -> bool:
    from ..tenancy import LEGACY_ORG_ID, effective_org

    s = get_settings()
    if name == "operator_api" and db is not None and effective_org(db) != LEGACY_ORG_ID:
        from ..services.integration_configs import org_operator_config
        cfg = org_operator_config(db, effective_org(db))
        return bool(cfg and cfg.base_url and cfg.secret_ciphertext)
    return {
        "google_maps": bool(s.google_maps_browser_key),
        "open_charge_map": OpenChargeMapClient().is_configured(),
        "osm": bool(s.osm_enabled and s.osm_overpass_url),
        "google_places": bool(s.google_places_api_key),
        "operator_api": OperatorApiConnector().is_configured(),
        "csv_import": True,
        "ocpp": True,
    }.get(name, False)


def _run_dict(r: IngestionRun) -> dict:
    return {"id": r.id, "connector": r.connector, "source": r.source, "dataset_label": r.dataset_label,
            "started_at": iso(r.started_at), "finished_at": iso(r.finished_at), "received": r.received,
            "accepted": r.accepted, "duplicates": r.duplicates, "rejected": r.rejected, "errors": r.errors,
            "ok": r.ok}


@router.get("/health")
def health(db: Session = Depends(get_db)):
    out = []
    for h in list_health(db):
        configured = _configured(h.connector, db)
        state = h.state
        if not configured and state not in ("error",):
            state = "not_configured"
        elif configured and state == "not_configured":
            state = "configured_unverified"
        out.append({"connector": h.connector, "description": CONNECTOR_INFO.get(h.connector, ""),
                    "configured": configured, "state": state, "last_checked_at": iso(h.last_checked_at),
                    "last_success_at": iso(h.last_success_at), "last_error": redact(h.last_error or ""),
                    "detail": h.detail})
    return out


@router.get("/runs")
def runs(db: Session = Depends(get_db)):
    return [_run_dict(r) for r in db.scalars(select(IngestionRun).order_by(IngestionRun.id.desc()).limit(50))]


@router.post("/operator/test")
def operator_test(db: Session = Depends(get_db)):
    _legacy_env_only(db)
    conn = OperatorApiConnector()
    if not conn.is_configured():
        set_health(db, "operator_api", "not_configured")
        db.commit()
        raise HTTPException(400, "Operator API is not configured. Set OPERATOR_API_BASE_URL and OPERATOR_API_KEY in .env.")
    try:
        info = conn.health_check()
    except ConnectorError as e:
        set_health(db, "operator_api", "error", error=str(e))
        db.commit()
        raise HTTPException(502, redact(str(e)))
    set_health(db, "operator_api", "connected", success=True, detail=info)
    db.commit()
    return {"state": "connected", **info}


@router.post("/operator/sync")
def operator_sync(db: Session = Depends(get_db)):
    _legacy_env_only(db)
    from ..services import operator_sync as svc

    conn = OperatorApiConnector()
    if not conn.is_configured():
        set_health(db, "operator_api", "not_configured")
        db.commit()
        raise HTTPException(400, "Operator API is not configured. Set OPERATOR_API_BASE_URL and OPERATOR_API_KEY in .env.")
    try:
        run = svc.sync(db, conn, trigger="manual")
    except ConnectorError as e:
        db.commit()
        raise HTTPException(502, redact(str(e)))
    db.commit()
    return _run_dict(run)


@router.post("/operator/diagnose")
def operator_diagnose(db: Session = Depends(get_db)):
    """Step-by-step check of the operator API path. Read-only (nothing stored)."""
    _legacy_env_only(db)
    d = OperatorApiConnector().diagnose()
    if d["state"] in ("missing_base_url", "missing_key"):
        set_health(db, "operator_api", "not_configured")
    elif d["ok"]:
        set_health(db, "operator_api", "connected", success=True, detail={"diagnose": d["state"]})
    else:
        set_health(db, "operator_api", "error", error=d["message"])
    db.commit()
    return {k: (redact(v) if isinstance(v, str) else v) for k, v in d.items()}


@router.get("/operator/status")
def operator_status(db: Session = Depends(get_db)):
    from ..services import operator_sync as svc
    from ..services.source_status import _org_operator_api
    from ..tenancy import LEGACY_ORG_ID, effective_org

    if effective_org(db) != LEGACY_ORG_ID:          # never reveal the server's .env configuration to an organisation
        st = _org_operator_api(db, effective_org(db))
        return {"configured": st["state"] != "NOT CONFIGURED", "base_url_set": None, "key_set": None, "state": st["state"],
                "next_action": st["next_action"], "health": None, "last_error": None, "last_success_at": st["last_update"],
                "last_event_at": st["last_event"], "chargers": st["chargers"], "stale": st["state"] == "STALE",
                "stale_after_s": None, "poll_seconds": 0, "uses_connections": True}
    return svc.status(db)


@router.post("/import/events")
async def import_events(file: UploadFile = File(...), dataset_label: str = Form(..., min_length=1, max_length=200),
                        db: Session = Depends(get_db)):
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large (limit 5 MB).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "File must be UTF-8 encoded CSV.")
    try:
        events, errors = parse_events_csv(text)
    except CsvFormatError as e:
        set_health(db, "csv_import", "error", error=str(e))
        db.commit()
        raise HTTPException(422, str(e))
    run = ingest(db, events, source="operator_import", connector="csv_import", dataset_label=dataset_label,
                 pre_errors=errors)
    set_health(db, "csv_import", "connected" if run.accepted else "error", success=bool(run.accepted),
               error="" if run.accepted else "No rows accepted.", detail={"last_run": run.id})
    db.commit()
    return _run_dict(run)


SAMPLES = {
    "operator-events.csv": ("SYNTHETIC_operator_events_sample.csv", "text/csv"),
    "ticket-history.csv": ("SYNTHETIC_ticket_history_sample.csv", "text/csv"),
    "charging-records.csv": ("IEEE_ChargingRecords_sample.csv", "text/csv"),
    "station-inventory.csv": ("Karnataka_Station_Inventory_sample.csv", "text/csv"),
}


# ------------------------------------------------------------------ Historical charging records (IEEE)
@router.post("/historical/preview")
async def historical_preview(file: UploadFile = File(...)):
    raw = await file.read(MAX_HISTORICAL_UPLOAD_BYTES + 1)
    if len(raw) > MAX_HISTORICAL_UPLOAD_BYTES:
        raise HTTPException(413, "File too large (limit 15 MB).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "File must be UTF-8 encoded CSV.")
    from ..integrations.historical_import import HistoricalCsvError, preview_historical_csv

    try:
        return preview_historical_csv(text)
    except HistoricalCsvError as e:
        raise HTTPException(422, str(e))


@router.post("/historical/import")
async def historical_import(
    file: UploadFile = File(...),
    dataset_label: str = Form("IEEE ChargingRecords Research Dataset", max_length=200),
    db: Session = Depends(get_db),
):
    raw = await file.read(MAX_HISTORICAL_UPLOAD_BYTES + 1)
    if len(raw) > MAX_HISTORICAL_UPLOAD_BYTES:
        raise HTTPException(413, "File too large (limit 15 MB).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "File must be UTF-8 encoded CSV.")
    from ..integrations.historical_import import HistoricalCsvError, import_historical_csv

    try:
        res = import_historical_csv(db, text, dataset_label=dataset_label)
        db.commit()
        return res
    except HistoricalCsvError as e:
        raise HTTPException(422, str(e))


# ------------------------------------------------------------------ Station Inventory CSV
@router.post("/stations/preview")
async def stations_preview(
    file: UploadFile = File(...),
    filter_karnataka: bool = Form(False),
    filter_bengaluru: bool = Form(False),
):
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large (limit 5 MB).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "File must be UTF-8 encoded CSV.")
    from ..integrations.station_inventory import StationInventoryCsvError, preview_station_inventory_csv

    try:
        return preview_station_inventory_csv(
            text, filter_karnataka=filter_karnataka, filter_bengaluru=filter_bengaluru
        )
    except StationInventoryCsvError as e:
        raise HTTPException(422, str(e))


@router.post("/stations/import")
async def stations_import(
    file: UploadFile = File(...),
    dataset_label: str = Form("Imported Station Inventory", max_length=200),
    filter_karnataka: bool = Form(False),
    filter_bengaluru: bool = Form(False),
    db: Session = Depends(get_db),
):
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large (limit 5 MB).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "File must be UTF-8 encoded CSV.")
    from ..integrations.station_inventory import StationInventoryCsvError, import_station_inventory_csv

    try:
        res = import_station_inventory_csv(
            db, text, dataset_label=dataset_label, filter_karnataka=filter_karnataka, filter_bengaluru=filter_bengaluru
        )
        db.commit()
        return res
    except StationInventoryCsvError as e:
        raise HTTPException(422, str(e))


@router.get("/samples/{name}")
def download_sample(name: str):
    """Labelled SYNTHETIC sample files for testing the import paths (not real operator data)."""
    from fastapi.responses import FileResponse

    from ..config import PROJECT_ROOT

    if name not in SAMPLES:
        raise HTTPException(404, "Unknown sample file.")
    fname, media = SAMPLES[name]
    path = PROJECT_ROOT / "sample_data" / fname
    if not path.exists():
        raise HTTPException(404, f"{fname} is missing from sample_data/.")
    return FileResponse(path, media_type=media, filename=fname)


@router.post("/import/sample")
def import_sample(db: Session = Depends(get_db)):
    """Import the bundled SYNTHETIC operator-events sample through the normal CSV import path."""
    from ..config import PROJECT_ROOT

    path = PROJECT_ROOT / "sample_data" / SAMPLES["operator-events.csv"][0]
    try:
        events, errors = parse_events_csv(path.read_text(encoding="utf-8-sig"))
    except (OSError, CsvFormatError) as e:
        raise HTTPException(422, str(e))
    run = ingest(db, events, source="operator_import", connector="csv_import",
                 dataset_label="SYNTHETIC SAMPLE (bundled test file, not real operator data)", pre_errors=errors)
    set_health(db, "csv_import", "connected" if run.accepted else "error", success=bool(run.accepted),
               error="" if run.accepted else "No rows accepted.", detail={"last_run": run.id, "synthetic_sample": True})
    db.commit()
    return _run_dict(run)


class MapStatusIn(BaseModel):
    state: Literal["connected", "error"]
    error: str = Field("", max_length=300)


@router.post("/maps/status")
def maps_status(body: MapStatusIn, db: Session = Depends(get_db)):
    """The browser reports whether the Google Maps script loaded (no key is sent)."""
    if not get_settings().google_maps_browser_key:
        set_health(db, "google_maps", "not_configured")
    elif body.state == "connected":
        set_health(db, "google_maps", "connected", success=True, detail={"verified_by": "browser map load"})
    else:
        set_health(db, "google_maps", "error", error=body.error or "Map failed to load")
    db.commit()
    return {"ok": True}
