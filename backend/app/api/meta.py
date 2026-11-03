"""Configuration for the browser, health check and report downloads."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import Charger
from ..services import reports

router = APIRouter(prefix="/api")
VERSION = "2.0.0"


@router.get("/config")
def browser_config():
    """Only values safe for the browser. The Google Maps browser key is public by
    design and must be restricted by HTTP referrer in Google Cloud."""
    s = get_settings()
    return {
        "version": VERSION,
        "google_maps_browser_key": s.google_maps_browser_key or None,
        "google_maps_map_id": s.google_maps_map_id or None,
        "admin_override_enabled": bool(s.admin_token),
        "step_seconds": s.sim_step_seconds,
        "tick_ms": s.sim_tick_ms,
    }


@router.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "version": VERSION, "chargers": db.scalar(select(func.count(Charger.id)))}


@router.get("/reports")
def list_reports():
    return [{"name": k, "title": v, "csv": f"/api/reports/{k}.csv"} for k, v in reports.REPORTS.items()] + \
        [{"name": "full", "title": "Printable summary report (HTML; print to PDF from the browser)", "html": "/api/reports/full.html"}]


@router.get("/reports/full.html", response_class=HTMLResponse)
def full_report(db: Session = Depends(get_db)):
    return HTMLResponse(reports.html_report(db))


@router.get("/reports/{name}.csv")
def report_csv(name: str, source: str = Query("simulation"), run_id: int | None = None,
               db: Session = Depends(get_db)):
    builder = reports.BUILDERS.get(name)
    if builder is None:
        raise HTTPException(404, f"Unknown report '{name}'.")
    if name in ("fleet", "tickets", "priority", "events"):
        body = builder(db, source)
    elif name in ("experiment", "sensitivity", "downtime_cost"):
        body = builder(db, run_id)
    else:
        body = builder(db)
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="chargesight_{name}.csv"'})
