"""Analytics (chart data), unified timeline, demo dataset and quick scenarios."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..services import analytics, demo_seed, scenarios

router = APIRouter(prefix="/api")
SourceQ = Query("simulation", pattern="^(simulation|operator_api|ocpp|operator_import|historical_sessions|station_inventory|simulated_imported)$")


@router.get("/analytics/timeseries")
def a_timeseries(minutes: int = Query(30, ge=5, le=48 * 60), bucket_min: int | None = Query(None, ge=1, le=360),
                 source: str = SourceQ, db: Session = Depends(get_db)):
    return analytics.timeseries(db, minutes, source, bucket_min)


@router.get("/analytics/faults")
def a_faults(hours: float = Query(6, gt=0, le=48), bucket_min: int = Query(30, ge=5, le=360),
             source: str = SourceQ, db: Session = Depends(get_db)):
    return analytics.faults(db, hours, bucket_min, source)


@router.get("/analytics/maintenance")
def a_maintenance(hours: float = Query(6, gt=0, le=48), bucket_min: int = Query(30, ge=5, le=360),
                  source: str = SourceQ, db: Session = Depends(get_db)):
    return analytics.maintenance(db, hours, bucket_min, source)


@router.get("/analytics/sites")
def a_sites(hours: float = Query(6, gt=0, le=48), source: str = SourceQ, db: Session = Depends(get_db)):
    return analytics.sites(db, hours, source)


@router.get("/chargers/{charger_id}/readings")
def charger_readings(charger_id: str, hours: float = Query(6, gt=0, le=12), db: Session = Depends(get_db)):
    try:
        return analytics.charger_readings(db, charger_id, hours)
    except LookupError:
        raise HTTPException(404, f"Charger '{charger_id}' not found.")


@router.get("/timeline")
def get_timeline(source: str | None = Query("simulation", pattern="^(simulation|operator_api|ocpp|operator_import|historical_sessions|station_inventory|simulated_imported|all)$"),
                 site: str | None = Query(None, max_length=16), charger: str | None = Query(None, max_length=64),
                 type: str | None = None, severity: Literal["critical", "major", "minor", "info"] | None = None,
                 q: str | None = Query(None, max_length=64), include_chatter: bool = False, operational: bool = False,
                 limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0, le=100000),
                 db: Session = Depends(get_db)):
    if type and type not in analytics.TIMELINE_TYPES:
        raise HTTPException(422, f"Unknown type '{type}'.")
    return analytics.timeline(db, source=None if source == "all" else source, site=site, charger=charger, type=type,
                              severity=severity, q=q, include_chatter=include_chatter, operational=operational,
                              limit=limit, offset=offset)


# ------------------------------------------------------------------ demo dataset
class ConfirmIn(BaseModel):
    confirm: str = Field(..., description='Must be the literal string "RESET".')


@router.get("/demo/status")
def demo_status(db: Session = Depends(get_db)):
    return demo_seed.demo_status(db)


@router.post("/demo/restore")
def demo_restore(body: ConfirmIn, request: Request, db: Session = Depends(get_db)):
    if body.confirm != "RESET":
        raise HTTPException(400, 'Confirmation required: send {"confirm": "RESET"}.')
    from ..models_saas import Organisation
    from ..services.presentation import is_demo_workspace

    org = db.get(Organisation, getattr(request.state, "org_id", None) or 1)
    if org is not None and not is_demo_workspace(org):
        # My Fleet: never silently fill an operator's own workspace with fictional chargers
        raise HTTPException(409, {"code": "not_demo_workspace",
                                  "message": "This is your own workspace (My Fleet), so the demo dataset is not restored here. "
                                             "Open the Presentation Demo for the SIMULATED sample fleet."})
    info = demo_seed.restore_demo(db)
    db.commit()
    return {**info, "message": "Demo dataset restored. Only SIMULATED records and [DEMO] samples were replaced; "
                               "imported and live records were not touched."}


@router.get("/demo/scenarios")
def list_scenarios(db: Session = Depends(get_db)):
    return scenarios.list_scenarios(db)


class ScenarioIn(BaseModel):
    site: str | None = Field(None, max_length=8)


@router.post("/demo/scenarios/{key}")
def apply_scenario(key: str, body: ScenarioIn | None = None, db: Session = Depends(get_db)):
    try:
        active = scenarios.apply(db, key, (body.site if body else None))
    except KeyError:
        raise HTTPException(404, f"Unknown scenario '{key}'.")
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.commit()
    return active
