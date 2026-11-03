"""Real-world validation: interviews, historical data checks, retrospective
replay and financial estimate comparison.

Nothing in this module creates evidence on its own. Track statuses can only
advance when the database holds the evidence each status requires, and
datasets flagged as synthetic samples never count as evidence.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from statistics import mean, median

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import wall_now
from ..core import finance
from ..core.catalog import FAULT_TYPES
from ..core.policy_sim import ExperimentConfig, Fault, Trace, metrics, simulate
from ..core.stats import paired_summary
from ..models import ExperimentRun, FinancialReference, Interview, InvestmentSite, ValidationDataset, ValidationTrack

TRACK_STATUSES = ("not_started", "awaiting_access", "data_received", "in_progress", "completed")
TRACK_LABELS = {
    "not_started": "Not started",
    "awaiting_access": "Awaiting operator access",
    "data_received": "Data received",
    "in_progress": "Validation in progress",
    "completed": "Completed",
}
FIN_METRICS = {
    "initial_investment": "Initial investment (INR)",
    "sessions_per_day": "Sessions per day (year 1)",
    "energy_kwh_year1": "Energy delivered, year 1 (kWh)",
    "revenue_year1": "Gross revenue, year 1 (INR)",
    "operating_contribution_year1": "Operating contribution, year 1 (INR)",
}


class EvidenceError(ValueError):
    pass


def _parse(iso_z: str | None) -> datetime | None:
    if not iso_z:
        return None
    return datetime.fromisoformat(iso_z.replace("Z", ""))


# --------------------------------------------------------------------- interviews
INTERVIEW_FIELDS = ("interview_date", "participant_ref", "consent_identifiable", "role", "organisation_type",
                    "current_workflow", "prioritisation_today", "difficulties", "data_shareable", "feedback",
                    "disagreements", "follow_ups", "confidentiality_notes")


def create_interview(db: Session, data: dict) -> Interview:
    d = {k: data.get(k) for k in INTERVIEW_FIELDS if k in data}
    if not d.get("interview_date") or not d.get("role"):
        raise ValueError("interview_date and role are required.")
    try:
        datetime.strptime(d["interview_date"], "%Y-%m-%d")
    except ValueError:
        raise ValueError("interview_date must be YYYY-MM-DD.")
    if not d.get("consent_identifiable"):
        d["participant_ref"] = ""          # no identifying reference without consent
        d["consent_identifiable"] = False
    row = Interview(**{k: (v if v is not None else "") for k, v in d.items()})
    db.add(row)
    db.flush()
    return row


# --------------------------------------------------------------------- datasets
def add_dataset(db: Session, label: str, records: list[dict], quality: dict, *, provided_by: str = "",
                permission_note: str = "", is_synthetic_sample: bool = False) -> ValidationDataset:
    if not label.strip():
        raise ValueError("A dataset label is required.")
    if not is_synthetic_sample and not permission_note.strip():
        raise ValueError("Record how permission to use this data was obtained (permission_note).")
    ds = ValidationDataset(label=label.strip(), provided_by=provided_by, permission_note=permission_note,
                           is_synthetic_sample=is_synthetic_sample, records=records, quality=quality)
    db.add(ds)
    db.flush()
    return ds


def compare_operational(ds: ValidationDataset, cfg: ExperimentConfig | None = None) -> dict:
    """Compare observed timings with the model's assumptions."""
    cfg = cfg or ExperimentConfig()
    recs = ds.records
    rows = []

    def add(metric, category, estimate, observed, n, unit, basis):
        abs_err = (estimate - observed) if (estimate is not None and observed is not None) else None
        pct = (abs_err / observed * 100) if (abs_err is not None and observed not in (0, None)) else None
        rows.append({"metric": metric, "category": category, "model_estimate": estimate, "observed": observed,
                     "n": n, "unit": unit, "abs_error": abs_err, "pct_error": pct, "estimate_basis": basis})

    by_cat: dict[str, list[dict]] = {}
    for r in recs:
        by_cat.setdefault(r["fault_category"], []).append(r)

    for cat, items in sorted(by_cat.items()):
        repairs = []
        for r in items:
            if r.get("repair_minutes") is not None:
                repairs.append(r["repair_minutes"])
        obs = mean(repairs) if repairs else None
        add("Mean repair duration", cat, cfg.repair_mean_min.get(cat), obs, len(repairs), "minutes",
            "ASSUMPTION repair_mean_min")

    acks = [(_parse(r["acknowledged_at"]) - _parse(r["detected_at"])).total_seconds() / 60
            for r in recs if r.get("acknowledged_at")]
    res = [(_parse(r["resolved_at"]) - _parse(r["detected_at"])).total_seconds() / 60
           for r in recs if r.get("resolved_at")]
    add("Time to acknowledge (observed MTTA)", "all", None, mean(acks) if acks else None, len(acks), "minutes",
        "no single model estimate; compare with experiment MTTA under matching load")
    add("Time to resolve (observed MTTR)", "all", None, mean(res) if res else None, len(res), "minutes",
        "no single model estimate; compare with experiment MTTR under matching load")

    total = len(recs) or 1
    mix_total = sum(v for v in cfg.category_mix.values() if v > 0) or 1
    for cat in FAULT_TYPES:
        add("Fault category share", cat, 100 * cfg.category_mix.get(cat, 0) / mix_total,
            100 * len(by_cat.get(cat, [])) / total, len(by_cat.get(cat, [])), "%", "ASSUMPTION category_mix")

    energy = [r["energy_kwh"] for r in recs if r.get("energy_kwh") is not None]
    return {
        "dataset_id": ds.id,
        "dataset_label": ds.label,
        "is_synthetic_sample": ds.is_synthetic_sample,
        "label": "IMPORTED" if not ds.is_synthetic_sample else "SYNTHETIC SAMPLE",
        "rows": rows,
        "observed_summary": {
            "records": len(recs),
            "median_ack_min": median(acks) if acks else None,
            "median_resolve_min": median(res) if res else None,
            "energy_kwh_total": sum(energy) if energy else None,
        },
        "quality": ds.quality,
        "limitations": [
            "Observed values reflect the operator's own logging practices and may include data-entry delays.",
            "Model estimates are assumptions; differences show where assumptions should be revised.",
        ] + (["This dataset is a SYNTHETIC SAMPLE for testing the import path; it is not evidence."]
             if ds.is_synthetic_sample else []),
    }


def retrospective_replay(ds: ValidationDataset, technicians: int = 2, priority: dict | None = None,
                         default_travel_min: float = 30.0) -> dict:
    """Replay the historical fault trace under FIFO and priority (offline)."""
    recs = [r for r in ds.records if r.get("detected_at")]
    if len(recs) < 2:
        raise ValueError("At least two records with detected_at are needed for a replay.")
    t0 = min(_parse(r["detected_at"]) for r in recs)
    chargers: dict[str, tuple[str, float]] = {}
    sites: dict[str, float] = {}
    for r in recs:
        site = r.get("site_code") or "UNKNOWN"
        chargers.setdefault(r["charger_id"], (site, float(r.get("charger_kw") or 30.0)))
        sites[site] = max(sites.get(site, 0.0), float(r.get("site_daily_sessions") or 0.0))
    charger_ids = sorted(chargers)
    idx = {cid: i for i, cid in enumerate(charger_ids)}
    faults = []
    assumed_repairs = 0
    for r in sorted(recs, key=lambda x: (x["detected_at"], x["record_id"])):
        det = (_parse(r["detected_at"]) - t0).total_seconds() / 60
        if r.get("repair_minutes") is not None:
            rep = float(r["repair_minutes"])
        else:
            rep = FAULT_TYPES[r["fault_category"]].repair_mean_min
            assumed_repairs += 1
        faults.append(Fault(id=len(faults) + 1, charger=idx[r["charger_id"]], site=chargers[r["charger_id"]][0],
                            category=r["fault_category"], detected=det, reset_success=False,
                            travel=default_travel_min, repair=max(rep, 1.0)))
    last = max(f.detected for f in faults)
    horizon = last + 7 * 24 * 60
    site_dicts = [{"code": code, "daily_sessions": sess, "travel_min": [default_travel_min, default_travel_min],
                   "chargers_kw": [chargers[c][1] for c in charger_ids if chargers[c][0] == code]}
                  for code, sess in sites.items()]
    cfg = ExperimentConfig.from_dict({
        "days": horizon / 1440, "seeds": [0], "technicians": technicians,
        "remote_reset_success": {k: 0.0 for k in FAULT_TYPES},
        "priority": priority, "sites": site_dicts,
        "utilisation_fraction": {s: 0.0 for s in sites},
    })
    trace = Trace(seed=0, horizon_min=horizon, chargers=[chargers[c] for c in charger_ids], faults=faults)
    fifo = metrics(trace, simulate(trace, cfg, "fifo"), cfg)
    prio = metrics(trace, simulate(trace, cfg, "priority"), cfg)
    keys = ("availability_pct", "downtime_h", "weighted_downtime_h", "high_sev_wait_mean_min",
            "low_sev_wait_max_min", "mtta_min", "mttr_min")
    return {
        "label": "RETROSPECTIVE OFFLINE SIMULATION",
        "disclaimer": ("Offline replay of a historical fault trace under two repair policies. "
                       "It is not a real-world intervention trial and does not show that either policy "
                       "would have changed actual uptime."),
        "dataset_id": ds.id,
        "dataset_label": ds.label,
        "is_synthetic_sample": ds.is_synthetic_sample,
        "faults": len(faults),
        "chargers": len(charger_ids),
        "technicians": technicians,
        "assumptions": {
            "travel_min": default_travel_min,
            "repairs_using_assumed_duration": assumed_repairs,
            "remote_reset": "not modelled (historical outcome unknown)",
        },
        "fifo": {k: fifo[k] for k in keys},
        "priority": {k: prio[k] for k in keys},
        "difference": {k: (prio[k] - fifo[k]) if (prio[k] is not None and fifo[k] is not None) else None for k in keys},
        "config": cfg.to_dict(),
    }


# --------------------------------------------------------------------- financial
def financial_comparison(db: Session, site: InvestmentSite) -> dict:
    inp = finance.InvestmentInputs.from_dict(site.inputs)
    r = finance.evaluate(inp)
    estimates = {
        "initial_investment": r["initial_investment"]["total"],
        "sessions_per_day": r["year1"]["sessions_per_day"],
        "energy_kwh_year1": r["year1"]["energy_kwh"],
        "revenue_year1": r["year1"]["revenue"],
        "operating_contribution_year1": r["year1"]["operating_contribution"],
    }
    refs = db.scalars(select(FinancialReference).where(FinancialReference.investment_site_id == site.id)).all()
    rows = []
    for ref in refs:
        est = estimates.get(ref.metric)
        abs_err = est - ref.reference_value if est is not None else None
        pct = (abs_err / ref.reference_value * 100) if (abs_err is not None and ref.reference_value != 0) else None
        rows.append({"metric": ref.metric, "label": FIN_METRICS.get(ref.metric, ref.metric), "estimate": est,
                     "reference": ref.reference_value, "abs_error": abs_err, "pct_error": pct,
                     "data_source": ref.data_source, "period": ref.period, "limitations": ref.limitations})
    return {
        "site_id": site.id,
        "site_name": site.name,
        "status": "COMPARED WITH REFERENCE DATA" if rows else "NOT YET VALIDATED",
        "estimates": estimates,
        "comparisons": rows,
        "note": ("Percentage error is omitted where the reference value is zero. "
                 "A comparison is evidence about these inputs and this period only."),
    }


# --------------------------------------------------------------------- tracks
def evidence(db: Session) -> dict:
    real_ds = db.scalars(select(ValidationDataset).where(ValidationDataset.is_synthetic_sample.is_(False))).all()
    real_ids = {d.id for d in real_ds}
    replays = [r for r in db.scalars(select(ExperimentRun).where(ExperimentRun.kind == "retrospective"))
               if (r.config or {}).get("dataset_id") in real_ids]
    return {
        "operational": {"datasets": len(real_ds), "retrospective_replays": len(replays)},
        "financial": {"references": len(db.scalars(select(FinancialReference)).all())},
        "interviews": {"interviews": len(db.scalars(select(Interview)).all())},
    }


def _required(track: str, status: str, ev: dict) -> str | None:
    """Return an error message if the evidence does not support the status."""
    e = ev[track]
    if status in ("not_started", "awaiting_access"):
        return None
    if track == "operational":
        if e["datasets"] < 1:
            return "Requires at least one imported, non-synthetic operator dataset."
        if status == "completed" and e["retrospective_replays"] < 1:
            return "Completed requires a retrospective replay on a non-synthetic dataset."
    if track == "financial" and e["references"] < 1:
        return "Requires at least one financial reference value supplied by an operator."
    if track == "interviews" and e["interviews"] < 1:
        return "Requires at least one recorded interview."
    return None


def set_track(db: Session, track: str, status: str, note: str = "") -> ValidationTrack:
    if track not in ("operational", "financial", "interviews"):
        raise ValueError("Unknown track.")
    if status not in TRACK_STATUSES:
        raise ValueError("Unknown status.")
    err = _required(track, status, evidence(db))
    if err:
        raise EvidenceError(err)
    if status == "completed" and not note.strip():
        raise EvidenceError("Describe the evidence and conclusion in the note before marking completed.")
    row = db.scalar(select(ValidationTrack).where(ValidationTrack.track == track)) or ValidationTrack(track=track)
    row.status = status
    row.note = note
    row.updated_at = wall_now()
    db.add(row)
    db.flush()
    return row


def tracks_summary(db: Session) -> dict:
    ev = evidence(db)
    out = {}
    for t in ("operational", "financial", "interviews"):
        row = db.scalar(select(ValidationTrack).where(ValidationTrack.track == t))
        status = row.status if row else "not_started"
        # If evidence was removed, never keep showing an unsupported status.
        if _required(t, status, ev):
            status = "not_started" if status not in ("awaiting_access",) else status
        out[t] = {"status": status, "label": TRACK_LABELS[status], "note": row.note if row else "",
                  "updated_at": row.updated_at.isoformat() + "Z" if row and row.updated_at else None,
                  "evidence": ev[t]}
    return out


def anonymised_interviews(db: Session) -> list[dict]:
    rows = db.scalars(select(Interview).order_by(Interview.id)).all()
    out = []
    for i, r in enumerate(rows, start=1):
        out.append({
            "participant": f"P{i}",
            "participant_hash": hashlib.sha256(f"{r.id}:{r.participant_ref}".encode()).hexdigest()[:10],
            "interview_date": r.interview_date,
            "role": r.role,
            "organisation_type": r.organisation_type,
            "current_workflow": r.current_workflow,
            "prioritisation_today": r.prioritisation_today,
            "difficulties": r.difficulties,
            "data_shareable": r.data_shareable,
            "feedback": r.feedback,
            "disagreements": r.disagreements,
            "follow_ups": r.follow_ups,
        })
    return out
