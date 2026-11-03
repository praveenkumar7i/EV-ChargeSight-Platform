"""CSV exports and a printable HTML report.

Every CSV starts with '# key: value' metadata lines (data source, generated
time, time range, definitions, assumptions) followed by a normal CSV table.
Values are computed by the same functions that feed the UI.
"""
from __future__ import annotations

import csv
import html
import io
import json

from .tickets import ACTIVE_WORK as TICKET_ACTIVE_WORK, DONE as TICKET_DONE
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import iso, wall_now
from ..core import finance
from ..core.catalog import ORIGIN_LABELS, FAULT_TYPES, SOURCE_LABELS
from ..core.policy_sim import METRIC_DEFS
from ..integrations.base import list_health
from ..models import (
    Charger, ConnectorHealth, Event, ExperimentRun, IngestionRun, InvestmentSite, Site, Ticket,
)
from . import settings_store
from .events import now_for_source
from .priority_service import ranking_map
from .tickets import is_overdue, ticket_metrics
from .validation import anonymised_interviews, tracks_summary

REPORTS = {
    "fleet": "Fleet availability and charger status",
    "tickets": "Fault and maintenance-ticket history",
    "priority": "Priority-score explanations (open tickets)",
    "events": "Charger event log",
    "experiment": "FIFO vs priority experiment results (latest or selected run)",
    "sensitivity": "Sensitivity-analysis results (latest run)",
    "downtime_cost": "Estimated downtime cost and assumptions (latest experiment)",
    "investment": "Site-investment feasibility (all saved sites)",
    "integrations": "Real-data integration status",
    "validation": "Operator interview and validation findings (anonymised)",
    "public_stations": "Public charging-station listings in Bengaluru (PUBLIC LISTING, not live status)",
}


def _csv(meta: dict, header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    for k, v in meta.items():
        text = v if isinstance(v, str) else json.dumps(v, default=str)
        for line in str(text).splitlines() or [""]:
            buf.write(f"# {k}: {line}\n")
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if x is None else (round(x, 4) if isinstance(x, float) else x) for x in r])
    return buf.getvalue()


def _base_meta(title: str, source: str, extra: dict | None = None) -> dict:
    m = {"report": title, "generated_at_utc": iso(wall_now()), "data_source": source,
         "provenance_labels": "SIMULATED=simulation, LIVE=operator_api/ocpp, IMPORTED=operator_import, ESTIMATED=model output, ASSUMPTION=user/default input"}
    m.update(extra or {})
    return m


def fleet_csv(db: Session, source: str = "simulation") -> str:
    chargers = db.scalars(select(Charger).where(Charger.source == source).order_by(Charger.id)).all()
    ranks = ranking_map(db, source)
    open_by_charger: dict[str, list[Ticket]] = {}
    for t in db.scalars(select(Ticket).where(Ticket.source == source, Ticket.status.not_in(TICKET_DONE))):
        open_by_charger.setdefault(t.charger_id, []).append(t)
    rows = []
    for c in chargers:
        ts = open_by_charger.get(c.id, [])
        top = max((ranks[t.code].score for t in ts if t.code in ranks), default=None)
        rows.append([c.id, c.site.code, c.site.name, c.connector_type, c.power_kw, c.status, SOURCE_LABELS[c.source],
                     iso(c.last_event_at), iso(c.last_heartbeat_at), len(ts), top, round(c.total_kwh, 3)])
    up = sum(1 for c in chargers if c.status in ("available", "charging"))
    meta = _base_meta(REPORTS["fleet"], source, {
        "snapshot_time": iso(now_for_source(db, source)),
        "availability_now": f"{up}/{len(chargers)} chargers able to charge" if chargers else "no chargers",
        "definitions": "able to charge = status available or charging",
    })
    return _csv(meta, ["charger_id", "site_code", "site_name", "connector", "power_kw", "status", "label",
                       "last_event_utc", "last_heartbeat_utc", "open_tickets", "top_priority_score", "total_kwh"], rows)


def tickets_csv(db: Session, source: str = "simulation") -> str:
    tickets = db.scalars(select(Ticket).where(Ticket.source == source).order_by(Ticket.id)).all()
    ranks = ranking_map(db, source)
    now = now_for_source(db, source)
    sla = settings_store.sla_hours(db)
    m = ticket_metrics(tickets, now, sla)
    rows = []
    for t in tickets:
        r = ranks.get(t.code)
        rows.append([t.code, t.charger_id, t.site.code, t.fault_category, t.severity, t.status, iso(t.detected_at),
                     iso(t.assigned_at), iso(t.acknowledged_at), iso(t.on_site_at), iso(t.resolved_at),
                     t.technician_id, t.est_travel_min, t.est_repair_min, t.resolved_by, t.resolution_notes,
                     r.score if r else None, r.rank if r else None, is_overdue(t, now, sla), len(t.history),
                     SOURCE_LABELS[t.source], ORIGIN_LABELS.get(t.origin or "", "UNKNOWN"), t.issue_category, t.impact,
                     t.unable_to_charge, t.affected_chargers, t.reported_by, iso(t.sla_due_at), iso(t.escalated_at),
                     t.dispatch_needed, t.auto_assigned, t.assignment_note, iso(t.closed_at), t.inspection_passed, t.reopen_count])
    first = min((t.detected_at for t in tickets), default=None)
    meta = _base_meta(REPORTS["tickets"], source, {
        "time_range_utc": f"{iso(first)} to {iso(now)}" if first else "no tickets",
        "mtta_min": m["mtta_min"], "mttr_min": m["mttr_min"],
        "definitions": json.dumps(m["definitions"]),
        "sla_hours_assumption": json.dumps(sla),
    })
    return _csv(meta, ["code", "charger_id", "site", "fault_category", "severity", "status", "detected_utc",
                       "assigned_utc", "acknowledged_utc", "on_site_utc", "resolved_utc", "technician",
                       "est_travel_min", "est_repair_min", "resolved_by", "resolution_notes", "priority_score",
                       "priority_rank", "overdue", "history_entries", "label", "origin", "issue_category", "impact",
                       "unable_to_charge", "affected_chargers", "reported_by", "sla_due_utc", "escalated_utc", "dispatch_needed",
                       "auto_assigned", "assignment_note", "closed_utc", "inspection_passed", "reopen_count"], rows)


def priority_csv(db: Session, source: str = "simulation") -> str:
    ranks = list(ranking_map(db, source).values())
    ranks.sort(key=lambda r: r.rank or 0)
    cfg = settings_store.priority_config(db)
    rows = []
    for r in ranks:
        f = {x.key: x for x in r.factors}
        rows.append([r.rank, r.ticket_ref, round(r.score, 4), r.escalated,
                     f["severity"].value, f["severity"].contribution, f["utilisation"].value, f["utilisation"].contribution,
                     f["capacity"].value, f["capacity"].contribution, f["age"].value, f["age"].contribution,
                     r.explanation])
    meta = _base_meta(REPORTS["priority"], source, {
        "formula": "P = wS*S + wU*U + wR*R + wA*A; weights normalised to sum to 1",
        "weights_assumption": json.dumps(cfg.weights.normalised().as_dict()),
        "age_cap_hours": cfg.age_cap_hours, "starvation_cap_hours": cfg.starvation_cap_hours,
        "snapshot_time": iso(now_for_source(db, source)),
    })
    return _csv(meta, ["rank", "ticket", "score", "escalated", "S", "S_contribution", "U", "U_contribution",
                       "R", "R_contribution", "A", "A_contribution", "explanation"], rows)


def events_csv(db: Session, source: str = "simulation", limit: int = 20000) -> str:
    evs = db.scalars(select(Event).where(Event.source == source).order_by(Event.id.desc()).limit(limit)).all()
    evs.reverse()
    rows = [[e.id, iso(e.ts), e.charger_id, e.type, e.ocpp_action, e.status_after, json.dumps(e.payload),
             SOURCE_LABELS[e.source], e.external_event_id, iso(e.ingested_at)] for e in evs]
    meta = _base_meta(REPORTS["events"], source, {
        "time_range_utc": f"{iso(evs[0].ts)} to {iso(evs[-1].ts)}" if evs else "no events",
        "rows_limit": limit,
        "note": "ocpp_action names the OCPP 1.6 message type the event corresponds to; for SIMULATED rows no OCPP connection was used.",
    })
    return _csv(meta, ["id", "ts_utc", "charger_id", "type", "ocpp_action", "status_after", "payload", "label",
                       "external_event_id", "ingested_at_utc"], rows)


def _run(db: Session, kind: str, run_id: int | None) -> ExperimentRun | None:
    q = select(ExperimentRun).where(ExperimentRun.kind == kind)
    if run_id:
        q = q.where(ExperimentRun.id == run_id)
    return db.scalar(q.order_by(ExperimentRun.id.desc()).limit(1))


def experiment_csv(db: Session, run_id: int | None = None) -> str:
    run = _run(db, "comparison", run_id)
    if run is None:
        return _csv(_base_meta(REPORTS["experiment"], "simulation", {"note": "No experiment has been run yet."}), ["metric"], [])
    res = run.results
    rows = []
    for key, s in res["summary"].items():
        rows.append(["SUMMARY", key, s["label"], s["unit"], s["mean_a"], s["mean_b"], s["mean_diff"],
                     s.get("ci95_low"), s.get("ci95_high"), s.get("relative_diff"), s.get("significant"), s["n"]])
    for r in res["per_seed"]:
        for key in METRIC_DEFS:
            rows.append([f"seed {r['seed']}", key, METRIC_DEFS[key][0], METRIC_DEFS[key][1], r["fifo"][key],
                         r["priority"][key], (r["priority"][key] - r["fifo"][key])
                         if r["priority"][key] is not None and r["fifo"][key] is not None else None,
                         None, None, None, None, r["trace_signature"]])
    meta = _base_meta(REPORTS["experiment"], "simulation", {
        "run_id": run.id, "label": run.label, "config_hash": run.config_hash,
        "config": json.dumps(run.config), "difference": "priority minus FIFO; 95% CI is a paired t-interval over seeds",
        "definitions": json.dumps({k: v[2] for k, v in METRIC_DEFS.items()}),
    })
    return _csv(meta, ["row", "metric", "label", "unit", "fifo", "priority", "diff", "ci95_low", "ci95_high",
                       "relative_diff", "significant", "n_or_trace"], rows)


def sensitivity_csv(db: Session, run_id: int | None = None) -> str:
    run = _run(db, "sensitivity", run_id)
    if run is None:
        return _csv(_base_meta(REPORTS["sensitivity"], "simulation", {"note": "No sensitivity analysis has been run yet."}), ["variant"], [])
    rows = []
    for v in run.results["variants"]:
        for key, s in v["summary"].items():
            rows.append([v["variant"], json.dumps(v["changes"]), key, s["mean_a"], s["mean_b"], s["mean_diff"],
                         s.get("ci95_low"), s.get("ci95_high"), s.get("significant"), v["config_hash"]])
    meta = _base_meta(REPORTS["sensitivity"], "simulation", {"run_id": run.id, "base_config": json.dumps(run.config)})
    return _csv(meta, ["variant", "changes", "metric", "fifo", "priority", "diff", "ci95_low", "ci95_high",
                       "significant", "config_hash"], rows)


def downtime_cost_csv(db: Session, run_id: int | None = None) -> str:
    run = _run(db, "comparison", run_id)
    if run is None:
        return _csv(_base_meta(REPORTS["downtime_cost"], "simulation", {"note": "No experiment has been run yet."}), ["item"], [])
    s = run.results["summary"]
    cfg = run.config
    rows = [
        ["energy_not_delivered_kwh", s["energy_not_delivered_kwh"]["mean_a"], s["energy_not_delivered_kwh"]["mean_b"]],
        ["downtime_cost_inr", s["downtime_cost_inr"]["mean_a"], s["downtime_cost_inr"]["mean_b"]],
    ]
    meta = _base_meta(REPORTS["downtime_cost"], "simulation", {
        "label": "ESTIMATED from SIMULATED downtime",
        "margin_per_kwh_inr_assumption": cfg.get("margin_per_kwh_inr"),
        "utilisation_fraction_assumption": json.dumps(cfg.get("utilisation_fraction")),
        "formula": METRIC_DEFS["downtime_cost_inr"][2],
        "period_days": cfg.get("days"),
    })
    return _csv(meta, ["item", "fifo_mean_per_run", "priority_mean_per_run"], rows)


def investment_csv(db: Session) -> str:
    rows = []
    for s in db.scalars(select(InvestmentSite).order_by(InvestmentSite.id)):
        try:
            r = finance.evaluate(finance.InvestmentInputs.from_dict(s.inputs))
        except finance.FinanceInputError as e:
            rows.append([s.id, s.name, "INVALID INPUTS", str(e)] + [None] * 9)
            continue
        for y in r["years"]:
            rows.append([s.id, s.name, y["year"], r["initial_investment"]["total"], y["sessions"], y["energy_kwh"],
                         y["revenue"], y["electricity_cost"], y["variable_cost"], y["fixed_opex"],
                         y["operating_contribution"], y["cumulative_cash_flow"], r["payback_years"]])
    meta = _base_meta(REPORTS["investment"], "user inputs", {
        "label": "ESTIMATED from ASSUMPTION inputs; NOT YET VALIDATED unless a validation report says otherwise",
        "note": "Revenue is gross revenue; operating contribution = revenue - electricity - variable costs - fixed opex. Pre-tax, no financing.",
    })
    return _csv(meta, ["site_id", "site_name", "year", "initial_investment", "sessions", "energy_kwh", "revenue",
                       "electricity_cost", "variable_cost", "fixed_opex", "operating_contribution",
                       "cumulative_cash_flow", "payback_years"], rows)


def integrations_csv(db: Session) -> str:
    rows = []
    for h in list_health(db):
        rows.append(["health", h.connector, h.state, iso(h.last_checked_at), iso(h.last_success_at), h.last_error,
                     None, None, None, None])
    for r in db.scalars(select(IngestionRun).order_by(IngestionRun.id.desc()).limit(200)):
        rows.append(["ingestion", r.connector, "ok" if r.ok else "with_errors", iso(r.started_at), iso(r.finished_at),
                     r.dataset_label, r.received, r.accepted, r.duplicates, r.rejected])
    meta = _base_meta(REPORTS["integrations"], "connector registry", {
        "note": "'connected' is recorded only after a verified successful response or handshake."})
    return _csv(meta, ["kind", "connector", "state", "checked_or_started_utc", "success_or_finished_utc",
                       "error_or_dataset", "received", "accepted", "duplicates", "rejected"], rows)


def validation_csv(db: Session) -> str:
    tracks = tracks_summary(db)
    rows = [["track", t, v["label"], v["note"], json.dumps(v["evidence"])] + [""] * 6 for t, v in tracks.items()]
    for i in anonymised_interviews(db):
        rows.append(["interview", i["participant"], i["interview_date"], i["role"], i["organisation_type"],
                     i["prioritisation_today"], i["difficulties"], i["feedback"], i["disagreements"],
                     i["data_shareable"], i["follow_ups"]])
    meta = _base_meta(REPORTS["validation"], "recorded validation evidence", {
        "anonymisation": "Participant references removed; replaced with P1, P2, ...; confidentiality notes excluded.",
        "note": "Statuses reflect recorded evidence only; synthetic sample datasets never count as evidence."})
    return _csv(meta, ["kind", "id", "c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8", "c9"], rows)


def public_stations_csv(db: Session) -> str:
    from ..integrations.public_listings import LIVE_STATUS_NOTE, query

    rows = []
    for st in query(db):
        conns = "; ".join(f"{c.get('type')} {c.get('power_kw') or '?'} kW x{c.get('quantity') or '?'} {c.get('current') or ''}".strip()
                          for c in st["connections"]) or "unknown"
        rows.append([st["classification"], st["provider_label"], st["provider_id"], st["name"], st["operator"] or "unknown",
                     st["address"] or "unknown", st["lat"], st["lng"], st["area_name"], conns,
                     st["max_power_kw"], st["usage_type"] or "unknown", st["usage_cost"] or "unknown",
                     st["opening_hours"] or "unknown", st["listed_status"] or "", st["status"],
                     st["source_url"] or "", st["licence"] or "", st["retrieved_at"],
                     "; ".join(f"{o['provider_label']} {o['provider_id']}" for o in st["also_listed_by"])])
    meta = _base_meta(REPORTS["public_stations"], "public listings (Open Charge Map snapshot / OpenStreetMap / imported)", {
        "status": LIVE_STATUS_NOTE,
        "attribution": "Open Charge Map and contributors (CC BY 4.0); OpenStreetMap contributors (ODbL 1.0). Google Places results are never exported or stored.",
        "unknown": "'unknown' means the source does not provide the field."})
    return _csv(meta, ["classification", "provider", "provider_id", "name", "operator", "address", "lat", "lng", "area",
                       "connectors", "max_power_kw", "usage_type", "usage_cost", "opening_hours",
                       "listed_status_as_declared", "live_status", "source_url", "licence", "retrieved_at_utc",
                       "also_listed_by"], rows)


BUILDERS = {
    "fleet": fleet_csv, "tickets": tickets_csv, "priority": priority_csv, "events": events_csv,
    "experiment": experiment_csv, "sensitivity": sensitivity_csv, "downtime_cost": downtime_cost_csv,
    "investment": investment_csv, "integrations": integrations_csv, "validation": validation_csv,
    "public_stations": public_stations_csv,
}


def html_report(db: Session) -> str:
    """Printable summary combining all sections (open in a browser, print to PDF)."""
    now = now_for_source(db, "simulation")
    chargers = db.scalars(select(Charger).where(Charger.source == "simulation")).all()
    tickets = db.scalars(select(Ticket).where(Ticket.source == "simulation")).all()
    m = ticket_metrics(tickets, now, settings_store.sla_hours(db))
    status_counts = {s: sum(1 for c in chargers if c.status == s) for s in ("available", "charging", "faulted", "offline", "unavailable")}
    ranks = sorted(ranking_map(db).values(), key=lambda r: r.rank or 0)[:10]
    exp = _run(db, "comparison", None)
    tracks = tracks_summary(db)
    esc = html.escape

    def fmt(v, d=2):
        return "—" if v is None else (f"{v:,.{d}f}" if isinstance(v, float) else esc(str(v)))

    parts = [f"""<!doctype html><html><head><meta charset="utf-8"><title>ChargeSight report</title>
<style>body{{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#1b2230}}
h1{{font-size:22px}}h2{{font-size:17px;margin-top:28px;border-bottom:1px solid #ccd3df;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%;font-size:12.5px;margin:8px 0}}th,td{{border:1px solid #d8dee8;padding:4px 6px;text-align:left;vertical-align:top}}
th{{background:#f1f4f9}}.tag{{display:inline-block;font-size:10.5px;font-weight:600;padding:1px 6px;border-radius:3px;background:#e7ecf7;margin-right:4px}}
.muted{{color:#5b6477;font-size:12px}}</style></head><body>
<h1>ChargeSight report</h1><p class="muted">Generated {esc(iso(wall_now()))} (UTC). Simulation clock: {esc(iso(now))}.
Labels: <span class="tag">SIMULATED</span> fleet and experiments, <span class="tag">ESTIMATED</span> model outputs,
<span class="tag">ASSUMPTION</span> inputs. No section below is real operator data unless marked IMPORTED or LIVE.</p>
<h2>1. Fleet availability <span class="tag">SIMULATED</span></h2>
<table><tr>{''.join(f'<th>{esc(k)}</th>' for k in status_counts)}<th>total</th></tr>
<tr>{''.join(f'<td>{v}</td>' for v in status_counts.values())}<td>{len(chargers)}</td></tr></table>
<h2>2. Maintenance tickets <span class="tag">SIMULATED</span></h2>
<p>Total {m['total']}, open {m['open']}, unresolved critical {m['unresolved_high_severity']}, overdue {m['overdue']}.
MTTA {fmt(m['mtta_min'],1)} min (n={m['mtta_n']}), MTTR {fmt(m['mttr_min'],1)} min (n={m['mttr_n']}).</p>
<p class="muted">MTTA = mean(assigned − detected); MTTR = mean(resolved − detected), simulation clock.</p>
<h2>3. Priority explanations (top 10 open) <span class="tag">SIMULATED</span></h2><table><tr><th>Rank</th><th>Ticket</th><th>Score</th><th>Explanation</th></tr>"""]
    for r in ranks:
        parts.append(f"<tr><td>{r.rank}</td><td>{esc(r.ticket_ref)}</td><td>{r.score:.3f}</td><td>{esc(r.explanation)}</td></tr>")
    if not ranks:
        parts.append('<tr><td colspan="4">No open tickets.</td></tr>')
    parts.append("</table>")
    parts.append('<h2>4. FIFO vs priority experiment <span class="tag">SIMULATED</span></h2>')
    if exp:
        parts.append(f'<p class="muted">Run #{exp.id} · {esc(exp.label)} · {len(exp.config.get("seeds", []))} seeds × {exp.config.get("days")} days · {exp.config.get("technicians")} technicians · config {exp.config_hash[:12]}</p>')
        parts.append("<table><tr><th>Metric</th><th>FIFO</th><th>Priority</th><th>Diff (P−F)</th><th>95% CI</th></tr>")
        for k, s in exp.results["summary"].items():
            ci = f"{fmt(s.get('ci95_low'),3)} to {fmt(s.get('ci95_high'),3)}" if s.get("ci95_low") is not None else "—"
            parts.append(f"<tr><td>{esc(s['label'])} ({esc(s['unit'])})</td><td>{fmt(s['mean_a'])}</td><td>{fmt(s['mean_b'])}</td><td>{fmt(s['mean_diff'],3)}</td><td>{ci}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p>No experiment has been run yet.</p>")
    parts.append('<h2>5. Site investment estimates <span class="tag">ESTIMATED</span> <span class="tag">NOT YET VALIDATED</span></h2><table><tr><th>Site</th><th>Investment (₹)</th><th>Yr-1 revenue (₹)</th><th>Yr-1 operating contribution (₹)</th><th>Payback</th></tr>')
    sites = db.scalars(select(InvestmentSite)).all()
    for s in sites:
        try:
            r = finance.evaluate(finance.InvestmentInputs.from_dict(s.inputs))
            pb = f"{r['payback_years']:.1f} y" if r["payback_years"] else "not reached"
            parts.append(f"<tr><td>{esc(s.name)}</td><td>{r['initial_investment']['total']:,.0f}</td><td>{r['year1']['revenue']:,.0f}</td><td>{r['year1']['operating_contribution']:,.0f}</td><td>{pb}</td></tr>")
        except finance.FinanceInputError as e:
            parts.append(f"<tr><td>{esc(s.name)}</td><td colspan='4'>Invalid inputs: {esc(str(e))}</td></tr>")
    if not sites:
        parts.append('<tr><td colspan="5">No saved sites.</td></tr>')
    parts.append("</table>")
    parts.append("<h2>6. Integrations</h2><table><tr><th>Connector</th><th>State</th><th>Last success</th><th>Last error</th></tr>")
    for h in list_health(db):
        parts.append(f"<tr><td>{esc(h.connector)}</td><td>{esc(h.state)}</td><td>{esc(iso(h.last_success_at) or '—')}</td><td>{esc(h.last_error or '—')}</td></tr>")
    parts.append("</table><h2>7. Validation status</h2><table><tr><th>Track</th><th>Status</th><th>Evidence</th><th>Note</th></tr>")
    for t, v in tracks.items():
        parts.append(f"<tr><td>{esc(t)}</td><td>{esc(v['label'])}</td><td>{esc(json.dumps(v['evidence']))}</td><td>{esc(v['note'] or '—')}</td></tr>")
    parts.append("</table><p class='muted'>Real-world validation is reported only from recorded evidence. Synthetic sample datasets are excluded.</p></body></html>")
    return "".join(parts)
