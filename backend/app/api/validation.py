"""Validation workflow endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import iso, wall_now
from ..db import get_db
from ..integrations.csv_import import CsvFormatError, parse_ticket_history_csv
from ..models import ExperimentRun, FinancialReference, Interview, InvestmentSite, ValidationDataset
from ..services import validation as V

router = APIRouter(prefix="/api/validation")
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


class InterviewIn(BaseModel):
    interview_date: str
    role: str = Field(min_length=1, max_length=80)
    consent_identifiable: bool = False
    participant_ref: str = Field("", max_length=80)
    organisation_type: str = Field("", max_length=80)
    current_workflow: str = ""
    prioritisation_today: str = ""
    difficulties: str = ""
    data_shareable: str = ""
    feedback: str = ""
    disagreements: str = ""
    follow_ups: str = ""
    confidentiality_notes: str = ""


def _interview(r: Interview) -> dict:
    return {c: getattr(r, c) for c in V.INTERVIEW_FIELDS} | {"id": r.id, "created_at": iso(r.created_at)}


@router.get("/interviews")
def list_interviews(db: Session = Depends(get_db)):
    return [_interview(r) for r in db.scalars(select(Interview).order_by(Interview.id.desc()))]


@router.post("/interviews")
def create_interview(body: InterviewIn, db: Session = Depends(get_db)):
    try:
        r = V.create_interview(db, body.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.commit()
    return _interview(r)


@router.delete("/interviews/{iid}")
def delete_interview(iid: int, db: Session = Depends(get_db)):
    r = db.get(Interview, iid)
    if r is None:
        raise HTTPException(404, "Interview not found.")
    db.delete(r)
    db.commit()
    return {"deleted": iid}


def _ds(d: ValidationDataset, full: bool = False) -> dict:
    out = {"id": d.id, "label": d.label, "provided_by": d.provided_by, "permission_note": d.permission_note,
           "is_synthetic_sample": d.is_synthetic_sample, "imported_at": iso(d.imported_at),
           "records": len(d.records), "quality": d.quality,
           "tag": "SYNTHETIC SAMPLE (not evidence)" if d.is_synthetic_sample else "IMPORTED"}
    if full:
        out["records_preview"] = d.records[:50]
    return out


@router.get("/datasets")
def list_datasets(db: Session = Depends(get_db)):
    return [_ds(d) for d in db.scalars(select(ValidationDataset).order_by(ValidationDataset.id.desc()))]


@router.post("/datasets")
async def import_dataset(file: UploadFile = File(...), label: str = Form(...), provided_by: str = Form(""),
                         permission_note: str = Form(""), is_synthetic_sample: bool = Form(False),
                         db: Session = Depends(get_db)):
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large (limit 5 MB).")
    try:
        records, quality = parse_ticket_history_csv(raw.decode("utf-8-sig"))
    except (CsvFormatError, UnicodeDecodeError) as e:
        raise HTTPException(422, str(e))
    try:
        ds = V.add_dataset(db, label, records, quality, provided_by=provided_by, permission_note=permission_note,
                           is_synthetic_sample=is_synthetic_sample)
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.commit()
    return _ds(ds, full=True)


@router.get("/datasets/{ds_id}")
def get_dataset(ds_id: int, db: Session = Depends(get_db)):
    d = db.get(ValidationDataset, ds_id)
    if d is None:
        raise HTTPException(404, "Dataset not found.")
    return _ds(d, full=True)


@router.delete("/datasets/{ds_id}")
def delete_dataset(ds_id: int, db: Session = Depends(get_db)):
    d = db.get(ValidationDataset, ds_id)
    if d is None:
        raise HTTPException(404, "Dataset not found.")
    db.delete(d)
    db.commit()
    return {"deleted": ds_id}


@router.post("/datasets/{ds_id}/compare")
def compare(ds_id: int, db: Session = Depends(get_db)):
    d = db.get(ValidationDataset, ds_id)
    if d is None:
        raise HTTPException(404, "Dataset not found.")
    return V.compare_operational(d)


class ReplayIn(BaseModel):
    technicians: int = Field(2, ge=1, le=20)
    priority: dict | None = None
    default_travel_min: float = Field(30.0, ge=0, le=600)


@router.post("/datasets/{ds_id}/replay")
def replay(ds_id: int, body: ReplayIn, db: Session = Depends(get_db)):
    d = db.get(ValidationDataset, ds_id)
    if d is None:
        raise HTTPException(404, "Dataset not found.")
    try:
        res = V.retrospective_replay(d, body.technicians, body.priority, body.default_travel_min)
    except ValueError as e:
        raise HTTPException(422, str(e))
    run = ExperimentRun(kind="retrospective", config={"dataset_id": d.id, **body.model_dump()},
                        config_hash=f"retro-{d.id}", results=res, label=f"Retrospective: {d.label}",
                        data_source="operator_import" if not d.is_synthetic_sample else "synthetic_sample",
                        created_at=wall_now())
    db.add(run)
    db.commit()
    return {"run_id": run.id, **res}


class FinRefIn(BaseModel):
    investment_site_id: int
    metric: str
    reference_value: float
    period: str = Field(min_length=1, max_length=60)
    data_source: str = Field(min_length=1, max_length=160)
    limitations: str = ""


@router.get("/financial-references")
def list_refs(site_id: int | None = None, db: Session = Depends(get_db)):
    q = select(FinancialReference).order_by(FinancialReference.id.desc())
    if site_id:
        q = q.where(FinancialReference.investment_site_id == site_id)
    return [{"id": r.id, "investment_site_id": r.investment_site_id, "metric": r.metric,
             "reference_value": r.reference_value, "period": r.period, "data_source": r.data_source,
             "limitations": r.limitations, "created_at": iso(r.created_at)} for r in db.scalars(q)]


@router.post("/financial-references")
def add_ref(body: FinRefIn, db: Session = Depends(get_db)):
    if db.get(InvestmentSite, body.investment_site_id) is None:
        raise HTTPException(404, "Investment site not found.")
    if body.metric not in V.FIN_METRICS:
        raise HTTPException(422, f"metric must be one of: {', '.join(V.FIN_METRICS)}.")
    r = FinancialReference(**body.model_dump())
    db.add(r)
    db.commit()
    return {"id": r.id}


@router.delete("/financial-references/{ref_id}")
def delete_ref(ref_id: int, db: Session = Depends(get_db)):
    r = db.get(FinancialReference, ref_id)
    if r is None:
        raise HTTPException(404, "Reference not found.")
    db.delete(r)
    db.commit()
    return {"deleted": ref_id}


@router.get("/financial/{site_id}")
def financial(site_id: int, db: Session = Depends(get_db)):
    s = db.get(InvestmentSite, site_id)
    if s is None:
        raise HTTPException(404, "Investment site not found.")
    return V.financial_comparison(db, s)


@router.get("/financial-metrics")
def financial_metrics():
    return V.FIN_METRICS


@router.get("/tracks")
def tracks(db: Session = Depends(get_db)):
    return {"tracks": V.tracks_summary(db), "statuses": V.TRACK_LABELS}


class TrackIn(BaseModel):
    status: str
    note: str = Field("", max_length=4000)


@router.put("/tracks/{track}")
def set_track(track: str, body: TrackIn, db: Session = Depends(get_db)):
    try:
        V.set_track(db, track, body.status, body.note)
    except V.EvidenceError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.commit()
    return V.tracks_summary(db)
