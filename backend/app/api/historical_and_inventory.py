"""APIs for Historical Charging Sessions, Station Inventory and Simulation on Imported Stations."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Integer, case, desc, func, select
from sqlalchemy.orm import Session

from ..clock import iso
from ..db import get_db
from ..models import HistoricalChargingSession, ImportedStation
from ..services.imported_sim import (
    get_imported_sim_status,
    reset_imported_simulation,
    run_simulation_on_imported_stations,
)
from ..tenancy import effective_org

router = APIRouter(prefix="/api")


# ------------------------------------------------------------------ Historical Sessions
def _apply_historical_filters(
    stmt,
    org_id: int,
    charger_id: str | None = None,
    charger_type: str | None = None,
    company: str | None = None,
    location: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
):
    stmt = stmt.where(HistoricalChargingSession.org_id == org_id)
    c_id = charger_id if isinstance(charger_id, str) and charger_id.strip() else None
    c_type = charger_type if isinstance(charger_type, str) and charger_type.strip() else None
    c_comp = company if isinstance(company, str) and company.strip() else None
    c_loc = location if isinstance(location, str) and location.strip() else None
    d_from = date_from if isinstance(date_from, str) and date_from.strip() else None
    d_to = date_to if isinstance(date_to, str) and date_to.strip() else None
    query_str = q if isinstance(q, str) and q.strip() else None

    if c_id:
        stmt = stmt.where(HistoricalChargingSession.charger_id == c_id)
    if c_type:
        stmt = stmt.where(HistoricalChargingSession.charger_type == c_type)
    if c_comp:
        stmt = stmt.where(HistoricalChargingSession.charger_company.ilike(f"%{c_comp}%"))
    if c_loc:
        stmt = stmt.where(HistoricalChargingSession.location.ilike(f"%{c_loc}%"))
    if d_from:
        stmt = stmt.where(HistoricalChargingSession.start_day >= d_from)
    if d_to:
        stmt = stmt.where(HistoricalChargingSession.start_day <= d_to)
    if query_str:
        stmt = stmt.where(
            (HistoricalChargingSession.charger_id.ilike(f"%{query_str}%"))
            | (HistoricalChargingSession.location.ilike(f"%{query_str}%"))
            | (HistoricalChargingSession.user_id.ilike(f"%{query_str}%"))
            | (HistoricalChargingSession.charger_company.ilike(f"%{query_str}%"))
        )
    return stmt


@router.get("/historical/sessions")
def list_historical_sessions(
    charger_id: str | None = Query(None, max_length=64),
    charger_type: str | None = Query(None, max_length=60),
    company: str | None = Query(None, max_length=120),
    location: str | None = Query(None, max_length=200),
    date_from: str | None = Query(None, max_length=30),
    date_to: str | None = Query(None, max_length=30),
    q: str | None = Query(None, max_length=80),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Retrieve imported historical charging session records (IEEE dataset)."""
    org_id = effective_org(db)
    stmt = _apply_historical_filters(
        select(HistoricalChargingSession),
        org_id=org_id,
        charger_id=charger_id,
        charger_type=charger_type,
        company=company,
        location=location,
        date_from=date_from,
        date_to=date_to,
        q=q,
    )

    lim = limit if isinstance(limit, int) else 50
    off = offset if isinstance(offset, int) else 0

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(HistoricalChargingSession.id.desc()).offset(off).limit(lim)).all()

    return {
        "dataset_type": "Historical Charging Sessions (IEEE format)",
        "label": "HISTORICAL RESEARCH DATA — Not Live Telemetry",
        "total": total,
        "offset": off,
        "limit": lim,
        "sessions": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "charger_id": r.charger_id,
                "charger_company": r.charger_company,
                "location": r.location,
                "charger_type": r.charger_type,
                "start_day": r.start_day,
                "start_time": r.start_time,
                "end_day": r.end_day,
                "end_time": r.end_time,
                "start_datetime": iso(r.start_datetime),
                "end_datetime": iso(r.end_datetime),
                "duration": r.duration,
                "duration_raw": r.duration_raw,
                "demand": r.demand,
                "demand_raw": r.demand_raw,
                "demand_unit": r.demand_unit,
                "dataset_label": r.dataset_label,
                "created_at": iso(r.created_at),
            }
            for r in rows
        ],
    }


@router.get("/historical/summary")
def historical_summary(
    charger_id: str | None = Query(None, max_length=64),
    charger_type: str | None = Query(None, max_length=60),
    company: str | None = Query(None, max_length=120),
    location: str | None = Query(None, max_length=200),
    date_from: str | None = Query(None, max_length=30),
    date_to: str | None = Query(None, max_length=30),
    q: str | None = Query(None, max_length=80),
    db: Session = Depends(get_db),
):
    """Aggregate analytics over stored historical charging sessions."""
    org_id = effective_org(db)

    # Check total rows in workspace first
    workspace_total = db.scalar(
        select(func.count(HistoricalChargingSession.id)).where(HistoricalChargingSession.org_id == org_id)
    ) or 0
    if workspace_total == 0:
        return {
            "has_data": False,
            "total_sessions": 0,
            "message": "No historical charging sessions have been imported yet.",
            "next_action": "Data integrations -> Import Historical Charging Sessions (IEEE ChargingRecords.csv)",
        }

    # 1. KPIs
    kpi_stmt = _apply_historical_filters(
        select(
            func.count(HistoricalChargingSession.id),
            func.count(func.distinct(HistoricalChargingSession.charger_id)),
            func.count(func.distinct(HistoricalChargingSession.user_id)),
            func.coalesce(func.sum(HistoricalChargingSession.demand), 0.0),
            func.coalesce(func.avg(HistoricalChargingSession.demand), 0.0),
            func.coalesce(func.avg(HistoricalChargingSession.duration), 0.0),
        ),
        org_id=org_id,
        charger_id=charger_id,
        charger_type=charger_type,
        company=company,
        location=location,
        date_from=date_from,
        date_to=date_to,
        q=q,
    )
    kpi_res = db.execute(kpi_stmt).fetchone() or (0, 0, 0, 0.0, 0.0, 0.0)
    total_sessions = kpi_res[0] or 0
    unique_chargers = kpi_res[1] or 0
    unique_users = kpi_res[2] or 0
    total_demand = kpi_res[3] or 0.0
    avg_demand = kpi_res[4] or 0.0
    avg_duration = kpi_res[5] or 0.0

    # 2. Breakdown by charger type
    type_stmt = _apply_historical_filters(
        select(
            func.coalesce(HistoricalChargingSession.charger_type, "Unspecified"),
            func.count(HistoricalChargingSession.id),
        ),
        org_id=org_id,
        charger_id=charger_id,
        charger_type=charger_type,
        company=company,
        location=location,
        date_from=date_from,
        date_to=date_to,
        q=q,
    ).group_by(HistoricalChargingSession.charger_type).order_by(desc(func.count(HistoricalChargingSession.id)))
    type_counts = dict(db.execute(type_stmt).all())

    # 3. Top locations
    loc_stmt = _apply_historical_filters(
        select(
            func.coalesce(HistoricalChargingSession.location, "Unknown Location"),
            func.count(HistoricalChargingSession.id),
        ),
        org_id=org_id,
        charger_id=charger_id,
        charger_type=charger_type,
        company=company,
        location=location,
        date_from=date_from,
        date_to=date_to,
        q=q,
    ).group_by(HistoricalChargingSession.location).order_by(desc(func.count(HistoricalChargingSession.id))).limit(10)
    location_counts = dict(db.execute(loc_stmt).all())

    # 4. Operator companies
    comp_stmt = _apply_historical_filters(
        select(
            func.coalesce(HistoricalChargingSession.charger_company, "Other/Independent"),
            func.count(HistoricalChargingSession.id),
        ),
        org_id=org_id,
        charger_id=charger_id,
        charger_type=charger_type,
        company=company,
        location=location,
        date_from=date_from,
        date_to=date_to,
        q=q,
    ).group_by(HistoricalChargingSession.charger_company).order_by(desc(func.count(HistoricalChargingSession.id))).limit(10)
    company_counts = dict(db.execute(comp_stmt).all())

    # 5. Hourly distribution (00:00 - 23:00)
    hr_col = func.cast(
        func.coalesce(
            func.strftime("%H", HistoricalChargingSession.start_datetime),
            func.substr(HistoricalChargingSession.start_time, 1, 2),
        ),
        Integer,
    )
    hr_stmt = _apply_historical_filters(
        select(hr_col, func.count(HistoricalChargingSession.id)),
        org_id=org_id,
        charger_id=charger_id,
        charger_type=charger_type,
        company=company,
        location=location,
        date_from=date_from,
        date_to=date_to,
        q=q,
    ).where(
        (HistoricalChargingSession.start_datetime.is_not(None))
        | ((HistoricalChargingSession.start_time.is_not(None)) & (func.length(HistoricalChargingSession.start_time) >= 2))
    ).group_by(hr_col).order_by(hr_col)
    hourly_rows = db.execute(hr_stmt).all()
    hourly_map = {int(r[0]): r[1] for r in hourly_rows if r[0] is not None and 0 <= int(r[0]) < 24}
    hourly = [hourly_map.get(h, 0) for h in range(24)]

    # 6. Session duration distribution
    dur_stmt = _apply_historical_filters(
        select(
            func.sum(case((HistoricalChargingSession.duration < 30, 1), else_=0)),
            func.sum(case(((HistoricalChargingSession.duration >= 30) & (HistoricalChargingSession.duration < 60), 1), else_=0)),
            func.sum(case(((HistoricalChargingSession.duration >= 60) & (HistoricalChargingSession.duration < 120), 1), else_=0)),
            func.sum(case(((HistoricalChargingSession.duration >= 120) & (HistoricalChargingSession.duration < 240), 1), else_=0)),
            func.sum(case(((HistoricalChargingSession.duration >= 240) & (HistoricalChargingSession.duration < 480), 1), else_=0)),
            func.sum(case((HistoricalChargingSession.duration >= 480, 1), else_=0)),
        ),
        org_id=org_id,
        charger_id=charger_id,
        charger_type=charger_type,
        company=company,
        location=location,
        date_from=date_from,
        date_to=date_to,
        q=q,
    ).where(HistoricalChargingSession.duration.is_not(None))
    dur_row = db.execute(dur_stmt).fetchone() or (0, 0, 0, 0, 0, 0)
    duration_distribution = {
        "< 30 min": int(dur_row[0] or 0),
        "30–60 min": int(dur_row[1] or 0),
        "1–2 hours": int(dur_row[2] or 0),
        "2–4 hours": int(dur_row[3] or 0),
        "4–8 hours": int(dur_row[4] or 0),
        "> 8 hours": int(dur_row[5] or 0),
    }

    # 7. Charging demand distribution
    dem_stmt = _apply_historical_filters(
        select(
            func.sum(case((HistoricalChargingSession.demand < 5, 1), else_=0)),
            func.sum(case(((HistoricalChargingSession.demand >= 5) & (HistoricalChargingSession.demand < 15), 1), else_=0)),
            func.sum(case(((HistoricalChargingSession.demand >= 15) & (HistoricalChargingSession.demand < 30), 1), else_=0)),
            func.sum(case(((HistoricalChargingSession.demand >= 30) & (HistoricalChargingSession.demand < 50), 1), else_=0)),
            func.sum(case((HistoricalChargingSession.demand >= 50, 1), else_=0)),
        ),
        org_id=org_id,
        charger_id=charger_id,
        charger_type=charger_type,
        company=company,
        location=location,
        date_from=date_from,
        date_to=date_to,
        q=q,
    ).where(HistoricalChargingSession.demand.is_not(None))
    dem_row = db.execute(dem_stmt).fetchone() or (0, 0, 0, 0, 0)
    demand_distribution = {
        "< 5 kWh": int(dem_row[0] or 0),
        "5–15 kWh": int(dem_row[1] or 0),
        "15–30 kWh": int(dem_row[2] or 0),
        "30–50 kWh": int(dem_row[3] or 0),
        "> 50 kWh": int(dem_row[4] or 0),
    }

    # 8. Available filter options from the workspace
    types_avail = [
        r[0] for r in db.execute(
            select(HistoricalChargingSession.charger_type)
            .where(HistoricalChargingSession.org_id == org_id, HistoricalChargingSession.charger_type.is_not(None))
            .distinct()
            .order_by(HistoricalChargingSession.charger_type)
        ).all()
    ]
    comps_avail = [
        r[0] for r in db.execute(
            select(HistoricalChargingSession.charger_company)
            .where(HistoricalChargingSession.org_id == org_id, HistoricalChargingSession.charger_company.is_not(None))
            .distinct()
            .order_by(HistoricalChargingSession.charger_company)
        ).all()
    ]
    locs_avail = [
        r[0] for r in db.execute(
            select(HistoricalChargingSession.location)
            .where(HistoricalChargingSession.org_id == org_id, HistoricalChargingSession.location.is_not(None))
            .distinct()
            .order_by(HistoricalChargingSession.location)
        ).all()
    ]
    date_min_max = db.execute(
        select(
            func.min(func.coalesce(HistoricalChargingSession.start_day, func.strftime("%Y-%m-%d", HistoricalChargingSession.start_datetime))),
            func.max(func.coalesce(HistoricalChargingSession.start_day, func.strftime("%Y-%m-%d", HistoricalChargingSession.start_datetime))),
        ).where(HistoricalChargingSession.org_id == org_id)
    ).fetchone()

    return {
        "has_data": True,
        "dataset_type": "Historical EV Charging Sessions",
        "provenance": "IEEE Research Dataset — Historical Records",
        "total_sessions": total_sessions,
        "unique_chargers": unique_chargers,
        "unique_users": unique_users,
        "total_demand": round(float(total_demand), 2),
        "avg_demand": round(float(avg_demand), 2),
        "avg_duration": round(float(avg_duration), 2),
        "duration_unit_note": "Original Duration value retained without unit assumption.",
        "demand_unit_note": "Demand stored as original value; IEEE dataset commonly records kWh.",
        "charger_type_breakdown": type_counts,
        "top_locations": location_counts,
        "company_breakdown": company_counts,
        "hourly_distribution": hourly,
        "duration_distribution": duration_distribution,
        "demand_distribution": demand_distribution,
        "filter_options": {
            "charger_types": types_avail,
            "companies": comps_avail,
            "locations": locs_avail,
            "date_range": {
                "min": date_min_max[0] if date_min_max else None,
                "max": date_min_max[1] if date_min_max else None,
            },
        },
    }


# ------------------------------------------------------------------ Station Inventory
@router.get("/imported-stations")
def list_imported_stations(
    state: str | None = Query(None, max_length=80),
    district: str | None = Query(None, max_length=100),
    operator: str | None = Query(None, max_length=160),
    karnataka_only: bool = Query(False),
    bengaluru_only: bool = Query(False),
    q: str | None = Query(None, max_length=80),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List imported station inventory records."""
    org_id = effective_org(db)
    stmt = select(ImportedStation).where(ImportedStation.org_id == org_id)

    if state:
        stmt = stmt.where(ImportedStation.state.ilike(f"%{state}%"))
    if district:
        stmt = stmt.where(ImportedStation.district.ilike(f"%{district}%"))
    if operator:
        stmt = stmt.where(ImportedStation.operator.ilike(f"%{operator}%"))
    if karnataka_only:
        stmt = stmt.where(ImportedStation.is_karnataka.is_(True))
    if bengaluru_only:
        stmt = stmt.where(ImportedStation.is_bengaluru.is_(True))
    if q:
        stmt = stmt.where(
            (ImportedStation.station_name.ilike(f"%{q}%"))
            | (ImportedStation.operator.ilike(f"%{q}%"))
            | (ImportedStation.district.ilike(f"%{q}%"))
            | (ImportedStation.city_or_village.ilike(f"%{q}%"))
        )

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(ImportedStation.id.desc()).offset(offset).limit(limit)).all()

    return {
        "dataset_type": "Imported Station Inventory",
        "total": total,
        "offset": offset,
        "limit": limit,
        "stations": [
            {
                "id": r.id,
                "station_id": r.station_id,
                "operator": r.operator,
                "ownership": r.ownership,
                "state": r.state,
                "district": r.district,
                "city_or_village": r.city_or_village,
                "station_name": r.station_name,
                "lat": r.lat,
                "lng": r.lng,
                "charger_type": r.charger_type,
                "charger_rating": r.charger_rating,
                "connector_rating": r.connector_rating,
                "total_connectors": r.total_connectors,
                "is_karnataka": r.is_karnataka,
                "is_bengaluru": r.is_bengaluru,
                "dataset_label": r.dataset_label,
                "created_at": iso(r.created_at),
            }
            for r in rows
        ],
    }


@router.get("/imported-stations/summary")
def imported_stations_summary(db: Session = Depends(get_db)):
    """Aggregate statistics for imported station inventory."""
    org_id = effective_org(db)
    total_stations = db.scalar(select(func.count(ImportedStation.id)).where(ImportedStation.org_id == org_id)) or 0
    if total_stations == 0:
        return {
            "has_data": False,
            "total_stations": 0,
            "total_connectors": 0,
            "message": "No station inventory has been imported yet.",
            "next_action": "Data integrations -> Import Station Inventory CSV",
        }

    total_connectors = db.scalar(select(func.sum(ImportedStation.total_connectors)).where(ImportedStation.org_id == org_id)) or 0
    kar_count = db.scalar(select(func.count(ImportedStation.id)).where(ImportedStation.org_id == org_id, ImportedStation.is_karnataka.is_(True))) or 0
    blr_count = db.scalar(select(func.count(ImportedStation.id)).where(ImportedStation.org_id == org_id, ImportedStation.is_bengaluru.is_(True))) or 0

    operator_counts = dict(
        db.execute(
            select(
                func.coalesce(ImportedStation.operator, "Independent CPO"),
                func.count(ImportedStation.id),
            )
            .where(ImportedStation.org_id == org_id)
            .group_by(ImportedStation.operator)
            .order_by(desc(func.count(ImportedStation.id)))
            .limit(10)
        ).all()
    )

    district_counts = dict(
        db.execute(
            select(
                func.coalesce(ImportedStation.district, "Other District"),
                func.count(ImportedStation.id),
            )
            .where(ImportedStation.org_id == org_id)
            .group_by(ImportedStation.district)
            .order_by(desc(func.count(ImportedStation.id)))
            .limit(10)
        ).all()
    )

    ownership_counts = dict(
        db.execute(
            select(
                func.coalesce(ImportedStation.ownership, "Unspecified"),
                func.count(ImportedStation.id),
            )
            .where(ImportedStation.org_id == org_id)
            .group_by(ImportedStation.ownership)
        ).all()
    )

    return {
        "has_data": True,
        "total_stations": total_stations,
        "total_connectors": int(total_connectors),
        "karnataka_stations": kar_count,
        "bengaluru_stations": blr_count,
        "other_regions": max(0, total_stations - kar_count),
        "operator_breakdown": operator_counts,
        "district_breakdown": district_counts,
        "ownership_breakdown": ownership_counts,
        "connector_count_note": "Connectors accurately counted from inventory; not treated as independent fleet chargers.",
    }


# ------------------------------------------------------------------ Simulation on Imported Stations
class ImportedSimIn(BaseModel):
    seed: int = Field(42, description="Configurable random seed for repeatability")
    stations_limit: int = Field(15, ge=1, le=50, description="Max imported stations to simulate")
    fault_probability: float = Field(0.35, ge=0.0, le=1.0, description="Probability of fault generation")


@router.get("/sim/imported/status")
def sim_imported_status(db: Session = Depends(get_db)):
    """Status of simulation on imported stations."""
    return get_imported_sim_status(db)


@router.post("/sim/imported/start")
def sim_imported_start(body: ImportedSimIn, db: Session = Depends(get_db)):
    """Run a repeatable simulation pass on eligible imported stations."""
    try:
        res = run_simulation_on_imported_stations(
            db,
            seed=body.seed,
            stations_limit=body.stations_limit,
            fault_probability=body.fault_probability,
        )
        db.commit()
        return res
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))


@router.post("/sim/imported/reset")
def sim_imported_reset(db: Session = Depends(get_db)):
    """Reset simulated chargers, events, and tickets on imported stations without altering original imported data."""
    res = reset_imported_simulation(db)
    db.commit()
    return res

