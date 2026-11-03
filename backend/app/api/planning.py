"""Candidate-site planning API (Map → Plan a new charging site → Site Feasibility)."""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import iso, wall_now
from ..core import finance, planning as P
from ..db import get_db
from ..integrations import public_listings as PL
from ..integrations.base import ConnectorError
from ..integrations.osm_context import OverpassContextClient
from ..models import InvestmentSite, PlanningCandidate

router = APIRouter(prefix="/api/planning")
log = logging.getLogger("chargesight.planning")
CONTEXT_MIN_INTERVAL_S = 600          # at most one OSM lookup per candidate per 10 minutes unless forced
_CONTEXT_LOCK = threading.Lock()


class CandidateInputs(BaseModel):
    """Values the USER entered for a candidate. Every field is optional; nothing is defaulted here."""
    n_chargers: int | None = Field(None, ge=1, le=50)
    charger_type: Literal["AC", "DC"] | None = None
    power_kw: float | None = Field(None, gt=0, le=500)
    budget_inr: float | None = Field(None, gt=0, le=1e10)
    sessions_per_day: float | None = Field(None, ge=0, le=2000)
    site_area_m2: float | None = Field(None, gt=0, le=1e6)
    grid_available_kva: float | None = Field(None, ge=0, le=1e5)
    grid_confirmed: bool | None = None              # True only if the user says the utility/landlord confirmed it
    road_access: Literal["arterial", "main", "local", "unknown"] | None = None
    amenities_observed: int | None = Field(None, ge=0, le=10000)
    evidence_note: str | None = Field(None, max_length=2000)


class CandidateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    lat: float
    lng: float
    inputs: CandidateInputs = Field(default_factory=CandidateInputs)
    notes: str = Field("", max_length=4000)


class CandidatePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    lat: float | None = None
    lng: float | None = None
    inputs: CandidateInputs | None = None
    notes: str | None = Field(None, max_length=4000)


class LinkIn(BaseModel):
    site_id: int


def _point(lat, lng) -> tuple[float, float]:
    try:
        return P.validate_point(lat, lng)
    except P.CoordinateError as e:
        raise HTTPException(422, str(e))


def _evidence(db: Session, lat: float, lng: float) -> dict:
    return {**P.locate(lat, lng), "nearby": P.nearby(PL.query(db), lat, lng)}


def _cand(db: Session, c: PlanningCandidate, with_evidence: bool = True) -> dict:
    d = {"id": c.id, "name": c.name, "lat": c.lat, "lng": c.lng, "area": c.area, "area_name": P.locate(c.lat, c.lng)["area_name"],
         "inputs": {k: v for k, v in (c.inputs or {}).items() if v is not None}, "context": c.context or {}, "notes": c.notes,
         "saved_site_id": c.saved_site_id, "created_at": iso(c.created_at), "updated_at": iso(c.updated_at), "label": "CANDIDATE"}
    if with_evidence:
        d["evidence"] = _evidence(db, c.lat, c.lng)
    return d


def _get(db: Session, cid: int) -> PlanningCandidate:
    c = db.get(PlanningCandidate, cid)
    if c is None:
        raise HTTPException(404, "Candidate not found.")
    return c


@router.get("/preview")
def preview(lat: float = Query(...), lng: float = Query(...), db: Session = Depends(get_db)):
    """Validate a point and show nearby stored listings, without saving anything."""
    la, ln = _point(lat, lng)
    return {"lat": la, "lng": ln, **_evidence(db, la, ln)}


@router.get("/candidates")
def list_candidates(db: Session = Depends(get_db)):
    return [_cand(db, c) for c in db.scalars(select(PlanningCandidate).order_by(PlanningCandidate.id))]


@router.post("/candidates")
def create_candidate(body: CandidateIn, db: Session = Depends(get_db)):
    la, ln = _point(body.lat, body.lng)
    c = PlanningCandidate(name=body.name.strip(), lat=la, lng=ln, area=P.locate(la, ln)["area"],
                          inputs=body.inputs.model_dump(exclude_none=True), notes=body.notes)
    db.add(c)
    db.commit()
    return _cand(db, c)


@router.get("/candidates/{cid}")
def get_candidate(cid: int, db: Session = Depends(get_db)):
    return _cand(db, _get(db, cid))


@router.put("/candidates/{cid}")
def update_candidate(cid: int, body: CandidatePatch, db: Session = Depends(get_db)):
    c = _get(db, cid)
    if body.lat is not None or body.lng is not None:
        la, ln = _point(body.lat if body.lat is not None else c.lat, body.lng if body.lng is not None else c.lng)
        if (la, ln) != (c.lat, c.lng):
            c.context = {}                       # context belonged to the old point
        c.lat, c.lng, c.area = la, ln, P.locate(la, ln)["area"]
    if body.name is not None:
        c.name = body.name.strip()
    if body.inputs is not None:
        c.inputs = body.inputs.model_dump(exclude_none=True)
    if body.notes is not None:
        c.notes = body.notes
    c.updated_at = wall_now()
    db.commit()
    return _cand(db, c)


@router.delete("/candidates/{cid}")
def delete_candidate(cid: int, db: Session = Depends(get_db)):
    c = _get(db, cid)
    db.delete(c)                                  # a saved proposed site created from it is NOT deleted
    db.commit()
    return {"deleted": cid}


@router.post("/candidates/{cid}/context")
def fetch_context(cid: int, force: bool = False, db: Session = Depends(get_db)):
    """Explicit, bounded OpenStreetMap lookup (amenities within 500 m, roads within 150 m)."""
    c = _get(db, cid)
    prev = (c.context or {}).get("osm")
    if prev and prev.get("retrieved_at") and not force:
        age = (wall_now() - datetime.fromisoformat(prev["retrieved_at"].rstrip("Z"))).total_seconds()
        if age < CONTEXT_MIN_INTERVAL_S:
            return {"ok": True, "cached": True, "message": f"Looked up {int(age // 60)} min ago; showing the stored result.", "osm": prev}
    if not _CONTEXT_LOCK.acquire(blocking=False):
        raise HTTPException(409, "Another OpenStreetMap lookup is running. Wait for it to finish.")
    try:
        db.rollback()                             # no transaction open during the network call
        try:
            osm = OverpassContextClient().fetch(c.lat, c.lng)
        except ConnectorError as e:
            log.warning("OSM context for candidate %s failed: %s", cid, e)
            return {"ok": False, "message": str(e), "osm": prev}
        c = _get(db, cid)
        c.context = {**(c.context or {}), "osm": osm}
        c.updated_at = wall_now()
        db.commit()
        return {"ok": True, "osm": osm}
    finally:
        _CONTEXT_LOCK.release()


@router.post("/candidates/{cid}/link")
def link_saved_site(cid: int, body: LinkIn, db: Session = Depends(get_db)):
    """Record that the user saved this candidate as a proposed site (after confirming in Site Feasibility)."""
    c = _get(db, cid)
    if db.get(InvestmentSite, body.site_id) is None:
        raise HTTPException(404, "Proposed site not found.")
    c.saved_site_id = body.site_id
    c.updated_at = wall_now()
    db.commit()
    return _cand(db, c, with_evidence=False)


FINANCE_FIELDS = ("n_chargers", "charger_type", "power_kw", "sessions_per_day")


@router.get("/candidates/{cid}/prefill")
def prefill(cid: int, db: Session = Depends(get_db)):
    """Values for Site Feasibility: only the location and what the user entered, each with its source."""
    c = _get(db, cid)
    loc = P.locate(c.lat, c.lng)
    values = {"site_name": c.name, "lat": c.lat, "lng": c.lng}
    sources = {
        "site_name": {"label": "USER-ENTERED", "text": f"Candidate name given on the Map (candidate #{c.id})."},
        "lat": {"label": "USER-ENTERED", "text": "Point selected on the Map."},
        "lng": {"label": "USER-ENTERED", "text": "Point selected on the Map."},
    }
    for k in FINANCE_FIELDS:
        v = (c.inputs or {}).get(k)
        if v is not None:
            values[k] = v
            sources[k] = {"label": "USER-ENTERED" if k != "sessions_per_day" else "ASSUMPTION",
                          "text": "Entered by you for this candidate on the Map." +
                                  (" Expected demand is your estimate, not a measurement." if k == "sessions_per_day" else "")}
    return {"candidate": _cand(db, c, with_evidence=False), "locality": loc["area_name"], "area": loc["area"],
            "values": values, "sources": sources,
            "not_prefilled_note": "All other inputs keep the example values (ASSUMPTION). Nothing about demand, cost or tariffs is inferred from the location.",
            "defaults": finance.InvestmentInputs().to_dict()}


# ------------------------------------------------------------------ phase 3: explainable ranking + financial comparison
class EvaluateIn(BaseModel):
    candidate_ids: list[int] | None = Field(None, max_length=20)
    weights: dict[str, float] | None = None
    base_inputs: dict | None = None            # shared (identical) financial assumptions; default = example ASSUMPTIONS


def _base(d: dict | None) -> dict:
    base = finance.InvestmentInputs().to_dict()
    if d:
        base.update({k: v for k, v in d.items() if k in base and v is not None and k not in ("site_name", "lat", "lng")})
    try:
        finance.InvestmentInputs.from_dict(base).validate()
    except (finance.FinanceInputError, TypeError) as e:
        raise HTTPException(422, f"Shared assumptions are invalid: {e}")
    return base


def _selected(db: Session, ids: list[int] | None) -> list[PlanningCandidate]:
    if ids:
        out = []
        for i in ids:
            out.append(_get(db, i))
        return out
    return list(db.scalars(select(PlanningCandidate).order_by(PlanningCandidate.id)))


@router.get("/method")
def scoring_method():
    return P.method()


@router.post("/evaluate")
def evaluate(body: EvaluateIn, db: Session = Depends(get_db)):
    cands = _selected(db, body.candidate_ids)
    if not cands:
        raise HTTPException(400, "No planning candidates yet. Use “Plan a new charging site” on the Map first.")
    base = _base(body.base_inputs)
    stations = PL.query(db)
    dicts = [_cand(db, c, with_evidence=False) for c in cands]
    ev = {c["id"]: {**P.locate(c["lat"], c["lng"]), "nearby": P.nearby(stations, c["lat"], c["lng"])} for c in dicts}
    try:
        out = P.evaluate_candidates(dicts, ev, body.weights, base)
    except ValueError as e:
        raise HTTPException(422, str(e))
    out["base_inputs"] = base
    out["base_inputs_note"] = "Every candidate uses these identical assumptions; only its location and the values you entered for it differ."
    return out


class CompareFinanceIn(BaseModel):
    candidate_ids: list[int] = Field(min_length=1, max_length=10)
    base_inputs: dict | None = None
    use_candidate_values: bool = True          # apply each candidate's own charger plan and demand estimate


@router.post("/compare-finance")
def compare_finance(body: CompareFinanceIn, db: Session = Depends(get_db)):
    """Existing Site Feasibility model, identical assumptions for every candidate."""
    base = _base(body.base_inputs)
    sites, used = [], {}
    for cid in body.candidate_ids:
        c = _cand(db, _get(db, cid), with_evidence=False)
        cc = c if body.use_candidate_values else {**c, "inputs": {}}
        try:
            sites.append((str(cid), P.finance_inputs(cc, base)))
        except (finance.FinanceInputError, TypeError) as e:
            raise HTTPException(422, f"Candidate #{cid}: {e}")
        used[str(cid)] = {k: v for k, v in (cc.get("inputs") or {}).items() if k in FINANCE_FIELDS}
    rows = finance.compare(sites)
    for r in rows:
        r["candidate_values_used"] = used[r["ref"]]
    return {"rows": rows, "base_inputs": base, "label": "ESTIMATED",
            "note": "Identical assumptions for all candidates" + (" except each candidate's own charger plan and demand estimate (shown per row)." if body.use_candidate_values else ".")
                    + " Demand is your estimate, not a measurement; nothing is inferred from the location."}
