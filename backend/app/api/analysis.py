"""Experiments (policy comparison, sensitivity), investment feasibility and map data."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import iso, wall_now
from ..config import get_settings
from ..core import finance
from ..core.policy_sim import METRIC_DEFS, SENSITIVITY_VARIANTS, ExperimentConfig, run_comparison, run_sensitivity
from ..db import get_db
from ..models import ExperimentRun, InvestmentSite

router = APIRouter(prefix="/api")


# ------------------------------------------------------------------ experiments
class ExperimentIn(BaseModel):
    config: dict = Field(default_factory=dict)
    mode: Literal["quick", "full", "custom"] = "quick"
    label: str = Field("", max_length=160)


def _cfg(body: ExperimentIn) -> ExperimentConfig:
    d = dict(body.config or {})
    if body.mode == "quick":
        d.setdefault("seeds", list(range(1, 6)))
        d.setdefault("days", 14)
    elif body.mode == "full":
        d["seeds"] = list(range(1, 31))
        d["days"] = 30
    try:
        return ExperimentConfig.from_dict(d)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e))


def _run_summary(r: ExperimentRun, full: bool = False) -> dict:
    d = {"id": r.id, "kind": r.kind, "created_at": iso(r.created_at), "label": r.label, "config_hash": r.config_hash,
         "status": r.status, "data_source": r.data_source,
         "seeds": len(r.config.get("seeds", [])) if r.kind != "retrospective" else None,
         "days": r.config.get("days"), "technicians": r.config.get("technicians")}
    if full:
        d["config"] = r.config
        d["results"] = r.results
    return d


@router.get("/experiments/defaults")
def experiment_defaults():
    return {"config": ExperimentConfig().to_dict(), "metric_definitions": {k: {"label": v[0], "unit": v[1], "definition": v[2]} for k, v in METRIC_DEFS.items()},
            "sensitivity_variants": SENSITIVITY_VARIANTS, "label": "ASSUMPTION",
            "note": "Defaults follow the research report's initial design; they are assumptions, not Indian field statistics."}


@router.post("/experiments/run")
def run_experiment(body: ExperimentIn, db: Session = Depends(get_db)):
    cfg = _cfg(body)
    results = run_comparison(cfg)
    run = ExperimentRun(kind="comparison", config=cfg.to_dict(), config_hash=cfg.hash(), results=results,
                        label=body.label or f"{body.mode} run", data_source="simulation", created_at=wall_now())
    db.add(run)
    db.commit()
    return _run_summary(run, full=True)


@router.post("/experiments/sensitivity")
def run_sens(body: ExperimentIn, variants: list[str] | None = None, db: Session = Depends(get_db)):
    cfg = _cfg(body)
    try:
        results = run_sensitivity(cfg, variants)
    except ValueError as e:
        raise HTTPException(422, str(e))
    run = ExperimentRun(kind="sensitivity", config=cfg.to_dict(), config_hash=cfg.hash(), results=results,
                        label=body.label or f"sensitivity ({body.mode})", data_source="simulation", created_at=wall_now())
    db.add(run)
    db.commit()
    return _run_summary(run, full=True)


@router.get("/experiments")
def list_runs(kind: str | None = None, db: Session = Depends(get_db)):
    q = select(ExperimentRun).order_by(ExperimentRun.id.desc()).limit(100)
    if kind:
        q = q.where(ExperimentRun.kind == kind)
    return [_run_summary(r) for r in db.scalars(q)]


@router.get("/experiments/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    r = db.get(ExperimentRun, run_id)
    if r is None:
        raise HTTPException(404, "Experiment run not found.")
    return _run_summary(r, full=True)


@router.delete("/experiments/{run_id}")
def delete_run(run_id: int, db: Session = Depends(get_db)):
    r = db.get(ExperimentRun, run_id)
    if r is None:
        raise HTTPException(404, "Experiment run not found.")
    db.delete(r)
    db.commit()
    return {"deleted": run_id}


# ------------------------------------------------------------------ investment
class InvestmentIn(BaseModel):
    inputs: dict = Field(default_factory=dict)
    include_scenarios: bool = True


class InvestmentSiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    inputs: dict = Field(default_factory=dict)
    notes: str = Field("", max_length=4000)


class CompareIn(BaseModel):
    site_ids: list[int] = Field(min_length=1, max_length=10)


def _inputs(d: dict) -> finance.InvestmentInputs:
    try:
        return finance.InvestmentInputs.from_dict(d)
    except (finance.FinanceInputError, TypeError) as e:
        raise HTTPException(422, str(e))


@router.get("/investment/defaults")
def investment_defaults():
    return {"inputs": finance.InvestmentInputs().to_dict(), "label": "ASSUMPTION",
            "note": finance.InvestmentInputs.ASSUMPTION_NOTE, "scenario_multipliers": finance.SCENARIOS,
            "formulas": finance.__doc__}


@router.post("/investment/evaluate")
def evaluate(body: InvestmentIn):
    inp = _inputs(body.inputs)
    r = finance.evaluate(inp)
    if body.include_scenarios:
        r["scenarios"] = finance.scenarios(inp)
    return r


def _site_dict(s: InvestmentSite) -> dict:
    try:
        r = finance.evaluate(finance.InvestmentInputs.from_dict(s.inputs))
        summary = {"initial_investment": r["initial_investment"]["total"], "year1_revenue": r["year1"]["revenue"],
                   "year1_operating_contribution": r["year1"]["operating_contribution"], "npv": r["npv"],
                   "payback_years": r["payback_years"], "flags": [f["code"] for f in r["flags"]], "valid": True}
    except finance.FinanceInputError as e:
        summary = {"valid": False, "error": str(e)}
    return {"id": s.id, "name": s.name, "lat": s.lat, "lng": s.lng, "inputs": s.inputs, "notes": s.notes,
            "created_at": iso(s.created_at), "updated_at": iso(s.updated_at), "summary": summary,
            "label": "ESTIMATED"}


@router.get("/investment/sites")
def list_inv_sites(db: Session = Depends(get_db)):
    return [_site_dict(s) for s in db.scalars(select(InvestmentSite).order_by(InvestmentSite.id))]


@router.post("/investment/sites")
def create_inv_site(body: InvestmentSiteIn, db: Session = Depends(get_db)):
    d = dict(body.inputs)
    d["site_name"] = body.name
    inp = _inputs(d)
    s = InvestmentSite(name=body.name, lat=inp.lat, lng=inp.lng, inputs=inp.to_dict(), notes=body.notes)
    db.add(s)
    db.commit()
    return _site_dict(s)


@router.put("/investment/sites/{site_id}")
def update_inv_site(site_id: int, body: InvestmentSiteIn, db: Session = Depends(get_db)):
    s = db.get(InvestmentSite, site_id)
    if s is None:
        raise HTTPException(404, "Site not found.")
    d = dict(body.inputs)
    d["site_name"] = body.name
    inp = _inputs(d)
    s.name, s.lat, s.lng, s.inputs, s.notes, s.updated_at = body.name, inp.lat, inp.lng, inp.to_dict(), body.notes, wall_now()
    db.commit()
    return _site_dict(s)


@router.delete("/investment/sites/{site_id}")
def delete_inv_site(site_id: int, db: Session = Depends(get_db)):
    s = db.get(InvestmentSite, site_id)
    if s is None:
        raise HTTPException(404, "Site not found.")
    from ..models import FinancialReference

    for ref in db.scalars(select(FinancialReference).where(FinancialReference.investment_site_id == site_id)):
        db.delete(ref)
    db.delete(s)
    db.commit()
    return {"deleted": site_id}


@router.post("/investment/compare")
def compare_sites(body: CompareIn, db: Session = Depends(get_db)):
    sites = []
    for sid in body.site_ids:
        s = db.get(InvestmentSite, sid)
        if s is None:
            raise HTTPException(404, f"Site {sid} not found.")
        sites.append((str(s.id), _inputs(s.inputs)))
    return {"rows": finance.compare(sites), "label": "ESTIMATED",
            "note": "Demand is user-supplied. Coordinates are used for display only; no demand or profitability is inferred from location."}
