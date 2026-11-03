"""Read-only analytics over stored history (snapshots, readings, tickets, events).

All series are computed from persisted rows, never invented. Every response
names its data source and label; when a source cannot support a series (for
example fleet snapshots exist only for the simulator) the response says so.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import mean

from .tickets import ACTIVE_WORK as TICKET_ACTIVE_WORK, DONE as TICKET_DONE
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import iso
from ..core.catalog import CAN_CHARGE, FAULT_TYPES, SOURCE_LABELS
from ..models import Charger, ChargerReading, Event, FleetSnapshot, Site, Technician, Ticket, TicketHistory
from .events import now_for_source
from .settings_store import sla_hours

UPTIME_TARGET_PCT = 97.0     # ASSUMPTION: demo service-level target
MAX_POINTS = 240
AGE_BUCKETS = [(0, 1, "<1 h"), (1, 4, "1-4 h"), (4, 12, "4-12 h"), (12, 24, "12-24 h"), (24, None, ">24 h")]


def _meta(source: str) -> dict:
    return {"source": source, "label": SOURCE_LABELS.get(source, source)}


def _downsample(rows: list, n: int = MAX_POINTS) -> list:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    picked = [rows[int(i * step)] for i in range(n)]
    if picked[-1] is not rows[-1]:
        picked[-1] = rows[-1]
    return picked


# ------------------------------------------------------------------ fleet time series
def timeseries(db: Session, minutes: int = 30, source: str = "simulation", bucket_min: int | None = None) -> dict:
    base = {**_meta(source), "minutes": minutes, "target_uptime_pct": UPTIME_TARGET_PCT,
            "target_label": "ASSUMPTION (demo service target)",
            "uptime_definition": "100 x (available + charging) / all chargers, sampled once per simulated minute."}
    if source != "simulation":
        return {**base, "supported": False, "points": [],
                "reason": "Fleet history snapshots are recorded by the simulator only. Imported/live sources show "
                          "current state; a history needs a continuous feed (operator API polling or OCPP)."}
    # snapshots with no chargers (recorded by an empty simulation before v9.2) carry no information: ignore them
    real = (FleetSnapshot.source == source, FleetSnapshot.total > 0)
    latest = db.scalar(select(func.max(FleetSnapshot.ts)).where(*real))
    if latest is None:
        return {**base, "supported": True, "points": [], "reason": "No history yet. This workspace has no simulated fleet history; open the Presentation Demo for sample history, or start the simulator if this workspace has simulated chargers."}
    start = latest - timedelta(minutes=minutes)
    rows = db.scalars(select(FleetSnapshot).where(*real, FleetSnapshot.ts >= start)
                      .order_by(FleetSnapshot.ts)).all()
    prev = db.scalars(select(FleetSnapshot).where(*real, FleetSnapshot.ts < start)
                      .order_by(FleetSnapshot.ts.desc()).limit(1)).first()
    bucket_min = bucket_min or max(1, minutes // 24)
    buckets = _energy_buckets(rows, prev, start, bucket_min)
    rows = _downsample(rows)
    points = []
    last_e = prev.energy_kwh_total if prev else (rows[0].energy_kwh_total if rows else 0)
    last_s = prev.sessions_total if prev else (rows[0].sessions_total if rows else 0)
    for r in rows:
        points.append({
            "ts": iso(r.ts), "uptime_pct": r.uptime_pct, "available": r.available, "charging": r.charging,
            "faulted": r.faulted, "offline": r.offline, "unavailable": r.unavailable, "power_kw": r.power_kw,
            "energy_kwh": round(max(0.0, r.energy_kwh_total - last_e), 3),
            "sessions": max(0, r.sessions_total - last_s),
            "energy_kwh_total": r.energy_kwh_total, "sessions_total": r.sessions_total,
            "open_tickets": r.open_tickets, "critical_open": r.critical_open,
        })
        last_e, last_s = r.energy_kwh_total, r.sessions_total
    up = [p["uptime_pct"] for p in points]
    summary = {
        "uptime_now": up[-1] if up else None, "uptime_start": up[0] if up else None,
        "uptime_change": round(up[-1] - up[0], 2) if up else None,
        "uptime_mean": round(mean(up), 2) if up else None, "uptime_min": min(up) if up else None,
        "energy_kwh": round(sum(p["energy_kwh"] for p in points), 2),
        "sessions": sum(p["sessions"] for p in points),
        "peak_power_kw": max((p["power_kw"] for p in points), default=None),
        "minutes_below_target": sum(1 for v in up if v < UPTIME_TARGET_PCT),
    }
    return {**base, "supported": True, "as_of": iso(latest), "points": points, "summary": summary,
            "bucket_min": bucket_min, "buckets": buckets,
            "bucket_definition": "Energy (kWh) and sessions started in each interval, from cumulative fleet counters."}


def _energy_buckets(rows, prev, start, bucket_min: int) -> list[dict]:
    if not rows:
        return []
    out: list[dict] = []
    last_e = prev.energy_kwh_total if prev else rows[0].energy_kwh_total
    last_s = prev.sessions_total if prev else rows[0].sessions_total
    width = timedelta(minutes=bucket_min)
    cur = None
    for r in rows:
        k = start + width * int((r.ts - start) / width)
        if cur is None or cur["_k"] != k:
            cur = {"_k": k, "start": iso(k), "end": iso(k + width), "energy_kwh": 0.0, "sessions": 0,
                   "peak_power_kw": 0.0, "mean_uptime_pct": [], }
            out.append(cur)
        cur["energy_kwh"] += max(0.0, r.energy_kwh_total - last_e)
        cur["sessions"] += max(0, r.sessions_total - last_s)
        cur["peak_power_kw"] = max(cur["peak_power_kw"], r.power_kw)
        cur["mean_uptime_pct"].append(r.uptime_pct)
        last_e, last_s = r.energy_kwh_total, r.sessions_total
    for b in out:
        b.pop("_k")
        b["energy_kwh"] = round(b["energy_kwh"], 2)
        b["mean_uptime_pct"] = round(mean(b["mean_uptime_pct"]), 2)
    return out


# ------------------------------------------------------------------ helpers over tickets
def _window(db: Session, source: str, hours: float) -> tuple[datetime, datetime]:
    now = now_for_source(db, source)
    return now - timedelta(hours=hours), now


def _buckets(start: datetime, end: datetime, bucket_min: int) -> list[datetime]:
    out, t = [], start
    while t < end:
        out.append(t)
        t += timedelta(minutes=bucket_min)
    return out


def faults(db: Session, hours: float = 6, bucket_min: int = 30, source: str = "simulation") -> dict:
    start, now = _window(db, source, hours)
    tickets = db.scalars(select(Ticket).where(Ticket.source == source, Ticket.detected_at >= start,
                                              Ticket.detected_at <= now)).all()
    edges = _buckets(start, now, bucket_min)
    cats = list(FAULT_TYPES)
    rows = []
    for b in edges:
        e = b + timedelta(minutes=bucket_min)
        inb = [t for t in tickets if b <= t.detected_at < e]
        rows.append({"start": iso(b), "end": iso(e),
                     "by_category": {c: sum(1 for t in inb if t.fault_category == c) for c in cats},
                     "by_severity": {s: sum(1 for t in inb if t.severity == s) for s in ("critical", "major", "minor")},
                     "total": len(inb)})
    return {**_meta(source), "hours": hours, "bucket_min": bucket_min, "buckets": rows,
            "categories": [{"key": c, "label": FAULT_TYPES[c].label, "severity": FAULT_TYPES[c].severity} for c in cats],
            "totals_by_category": {c: sum(1 for t in tickets if t.fault_category == c) for c in cats},
            "totals_by_severity": {s: sum(1 for t in tickets if t.severity == s) for s in ("critical", "major", "minor")},
            "definition": "Faults counted by ticket detection time (one ticket per charger and fault type; duplicates are merged)."}


def maintenance(db: Session, hours: float = 6, bucket_min: int = 30, source: str = "simulation") -> dict:
    start, now = _window(db, source, hours)
    tickets = db.scalars(select(Ticket).where(Ticket.source == source)).all()
    sla = sla_hours(db)
    trend = []
    for b in _buckets(start, now, bucket_min) + [now]:
        t_end = min(b + timedelta(minutes=bucket_min), now) if b != now else now
        ack = [(t.assigned_at - t.detected_at).total_seconds() / 60 for t in tickets
               if t.assigned_at and t.assigned_at <= t_end]
        res = [(t.resolved_at - t.detected_at).total_seconds() / 60 for t in tickets
               if t.resolved_at and t.resolved_at <= t_end]
        backlog = [t for t in tickets if t.detected_at <= t_end and (t.resolved_at is None or t.resolved_at > t_end)]
        trend.append({"ts": iso(t_end), "mtta_min": round(mean(ack), 1) if ack else None,
                      "mttr_min": round(mean(res), 1) if res else None, "mtta_n": len(ack), "mttr_n": len(res),
                      "backlog": len(backlog),
                      "backlog_critical": sum(1 for t in backlog if t.severity == "critical"),
                      "overdue": sum(1 for t in backlog
                                     if (t_end - t.detected_at).total_seconds() / 3600 > sla.get(t.severity, 24))})
        if b == now:
            break
    # de-duplicate the final point (bucket end may equal now)
    dedup = []
    for p in trend:
        if dedup and dedup[-1]["ts"] == p["ts"]:
            dedup[-1] = p
        else:
            dedup.append(p)
    open_t = [t for t in tickets if t.status not in TICKET_DONE]
    ages = []
    for lo, hi, label in AGE_BUCKETS:
        sel = [t for t in open_t if (now - t.detected_at).total_seconds() / 3600 >= lo
               and (hi is None or (now - t.detected_at).total_seconds() / 3600 < hi)]
        ages.append({"bucket": label, "count": len(sel),
                     "by_severity": {s: sum(1 for t in sel if t.severity == s) for s in ("critical", "major", "minor")},
                     "tickets": [t.code for t in sel]})
    return {**_meta(source), "hours": hours, "bucket_min": bucket_min, "trend": dedup, "age_histogram": ages,
            "open_now": len(open_t), "as_of": iso(now),
            "definitions": {
                "mtta_min": "Cumulative mean of (assigned_at - detected_at) over tickets assigned by each point in time, minutes.",
                "mttr_min": "Cumulative mean of (resolved_at - detected_at) over tickets resolved by each point in time (technician, remote reset or reconnect), minutes.",
                "backlog": "Tickets detected but not yet resolved at each point in time.",
                "overdue": "Backlog tickets older than the SLA for their severity (ASSUMPTION thresholds).",
            }}


def _union_minutes(intervals: list[tuple[datetime, datetime]]) -> float:
    total, cs, ce = 0.0, None, None
    for a, b in sorted(intervals):
        if ce is None or a > ce:
            if ce is not None:
                total += (ce - cs).total_seconds() / 60
            cs, ce = a, b
        else:
            ce = max(ce, b)
    if ce is not None:
        total += (ce - cs).total_seconds() / 60
    return total


def sites(db: Session, hours: float = 6, source: str = "simulation") -> dict:
    start, now = _window(db, source, hours)
    window_min = hours * 60
    out = []
    site_rows = db.scalars(select(Site).where(Site.source == source).order_by(Site.code)).all()
    chargers = db.scalars(select(Charger).where(Charger.source == source)).all()
    tickets = db.scalars(select(Ticket).where(Ticket.source == source)).all()
    for s in site_rows:
        cs = [c for c in chargers if c.site_id == s.id]
        st = [t for t in tickets if t.site_id == s.id]
        down = 0.0
        for c in cs:
            iv = []
            for t in st:
                if t.charger_id != c.id:
                    continue
                a, b = max(t.detected_at, start), min(t.resolved_at or now, now)
                if b > a:
                    iv.append((a, b))
            down += _union_minutes(iv)
        cap = len(cs) * window_min
        out.append({
            "code": s.code, "name": s.name, "lat": s.lat, "lng": s.lng, "chargers": len(cs),
            "window_availability_pct": round(100 * (1 - down / cap), 2) if cap else None,
            "availability_now_pct": round(100 * sum(1 for c in cs if c.status in CAN_CHARGE) / len(cs), 1) if cs else None,
            "status_counts": {k: sum(1 for c in cs if c.status == k)
                              for k in ("available", "charging", "faulted", "offline", "unavailable")},
            "power_kw": round(sum(c.power_now_kw or 0 for c in cs), 1),
            "open_tickets": sum(1 for t in st if t.status not in TICKET_DONE),
            "critical_open": sum(1 for t in st if t.status not in TICKET_DONE and t.severity == "critical"),
            "faults_in_window": sum(1 for t in st if t.detected_at >= start),
            "downtime_h": round(down / 60, 2), "daily_sessions": s.daily_sessions,
        })
    return {**_meta(source), "hours": hours, "target_uptime_pct": UPTIME_TARGET_PCT, "as_of": iso(now), "sites": out,
            "definition": "Window availability = 100 x (1 - charger downtime / (chargers x window)); downtime is the union of each charger's open-ticket intervals inside the window."}


def charger_readings(db: Session, charger_id: str, hours: float = 6) -> dict:
    c = db.get(Charger, charger_id)
    if c is None:
        raise LookupError(charger_id)
    meta = _meta(c.source)
    if c.source != "simulation":
        return {**meta, "charger_id": c.id, "supported": False, "points": [],
                "reason": "Per-charger power/temperature history is recorded by the simulator only."}
    latest = db.scalar(select(func.max(ChargerReading.ts)).where(ChargerReading.charger_id == c.id))
    if latest is None:
        return {**meta, "charger_id": c.id, "supported": True, "points": [], "reason": "No readings yet."}
    rows = db.scalars(select(ChargerReading).where(ChargerReading.charger_id == c.id,
                                                   ChargerReading.ts >= latest - timedelta(hours=hours))
                      .order_by(ChargerReading.ts)).all()
    return {**meta, "charger_id": c.id, "supported": True, "hours": hours, "rated_kw": c.power_kw,
            "temp_label": "SIMULATED connector temperature model (illustrative)",
            "points": [{"ts": iso(r.ts), "power_kw": r.power_kw, "temp_c": r.temp_c, "status": r.status}
                       for r in _downsample(rows)]}


# ------------------------------------------------------------------ unified timeline
TIMELINE_TYPES = ("fault", "offline", "reconnect", "fault_resolved", "reset_attempt", "reset_result", "boot",
                  "session_start", "session_stop", "status_available", "status_change", "heartbeat",
                  "meter_values", "ticket")


def _event_severity(e: Event) -> str:
    if e.type == "fault":
        p = e.payload or {}
        if p.get("severity_override"):
            return p["severity_override"]
        ft = FAULT_TYPES.get(p.get("category", ""))
        return ft.severity if ft else "critical"
    if e.type == "offline":
        return "major"
    if e.type == "reset_result" and not (e.payload or {}).get("success"):
        return "major"
    return "info"


def _event_text(e: Event) -> str:
    p = e.payload or {}
    t = e.type
    if t == "fault":
        ft = FAULT_TYPES.get(p.get("category", ""))
        extra = f", {p['temperature_C']} °C" if p.get("temperature_C") is not None else ""
        code = f" ({p['errorCode']})" if p.get("errorCode") else ""
        return f"{ft.label if ft else p.get('category', 'Fault')}{code}{extra}; charger set to Faulted"
    if t == "offline":
        return "No heartbeat for 3 intervals; marked Offline"
    if t == "reconnect":
        return f"Charger back online ({p.get('reason', 'reconnect')})"
    if t == "fault_resolved":
        return f"Fault cleared ({p.get('resolved_by', 'resolved')}); charger Available"
    if t == "reset_attempt":
        return f"Remote {p.get('type', 'Soft')} Reset sent for {p.get('ticket', '')}"
    if t == "reset_result":
        return f"Remote reset {'succeeded' if p.get('success') else 'failed'} ({p.get('ticket', '')})"
    if t == "boot":
        return f"BootNotification ({p.get('reason', 'boot')})"
    if t == "session_start":
        return f"Charging session started (idTag {p.get('idTag', '-')})"
    if t == "session_stop":
        return f"Session ended: {p.get('reason', '')}, {p.get('meterStop_kWh', 0)} kWh"
    if t == "meter_values":
        return f"{p.get('power_kW', 0)} kW, session {p.get('session_kWh', 0)} kWh"
    if t == "heartbeat":
        return "Heartbeat"
    return t.replace("_", " ").capitalize()


def timeline(db: Session, *, source: str | None = "simulation", site: str | None = None, charger: str | None = None,
             type: str | None = None, severity: str | None = None, q: str | None = None,
             include_chatter: bool = False, operational: bool = False, limit: int = 50, offset: int = 0) -> dict:
    site_ids = None
    if site:
        site_ids = [s.id for s in db.scalars(select(Site).where(Site.code == site.upper()))]
    want_events = type != "ticket"
    want_tickets = type in (None, "ticket") and severity in (None, "critical", "major", "minor")
    items: list[dict] = []
    total = 0
    fetch = offset + limit
    if want_events:
        eq = select(Event)
        if source:
            eq = eq.where(Event.source == source)
        if charger:
            eq = eq.where(Event.charger_id == charger)
        if site_ids is not None:
            eq = eq.where(Event.charger_id.in_(select(Charger.id).where(Charger.site_id.in_(site_ids))))
        if type:
            eq = eq.where(Event.type == type)
        elif operational:
            eq = eq.where(Event.type.not_in(("heartbeat", "meter_values", "session_start", "session_stop", "status_available")))
        elif not include_chatter:
            eq = eq.where(Event.type.not_in(("heartbeat", "meter_values")))
        if severity == "info":
            eq = eq.where(Event.type.not_in(("fault", "offline")))
        elif severity:
            eq = eq.where(Event.type.in_(("fault", "offline", "reset_result")))
        if q:
            eq = eq.where(Event.charger_id.ilike(f"%{q}%") | Event.type.ilike(f"%{q}%"))
        rows = db.scalars(eq.order_by(Event.ts.desc(), Event.id.desc()).limit(fetch if not severity else fetch * 4)).all()
        chargers = {c.id: c for c in db.scalars(select(Charger).where(Charger.id.in_({r.charger_id for r in rows})))}
        for e in rows:
            sev = _event_severity(e)
            if severity and sev != severity:
                continue
            c = chargers.get(e.charger_id)
            items.append({"ts": iso(e.ts), "kind": "event", "type": e.type, "ocpp_action": e.ocpp_action,
                          "charger_id": e.charger_id, "site_code": c.site.code if c and c.site else None,
                          "site_name": c.site.name if c and c.site else None, "severity": sev,
                          "source": e.source, "label": SOURCE_LABELS.get(e.source, e.source),
                          "description": _event_text(e), "ticket": (e.payload or {}).get("ticket"),
                          "status_after": e.status_after, "_k": (e.ts, 0, e.id)})
        total += db.scalar(select(func.count()).select_from(eq.subquery())) or 0
    if want_tickets:
        hq = select(TicketHistory, Ticket).join(Ticket, Ticket.id == TicketHistory.ticket_id)
        if source:
            hq = hq.where(Ticket.source == source)
        if charger:
            hq = hq.where(Ticket.charger_id == charger)
        if site_ids is not None:
            hq = hq.where(Ticket.site_id.in_(site_ids))
        if severity:
            hq = hq.where(Ticket.severity == severity)
        if q:
            hq = hq.where(Ticket.code.ilike(f"%{q}%") | Ticket.charger_id.ilike(f"%{q}%") |
                          TicketHistory.note.ilike(f"%{q}%"))
        techs = {t.id: t.name for t in db.scalars(select(Technician))}
        for h, t in db.execute(hq.order_by(TicketHistory.at.desc(), TicketHistory.id.desc()).limit(fetch)).all():
            kind = h.kind or "status"
            if kind == "status":
                frm = h.from_status or "new"
                text = f"{t.code} {frm} → {h.to_status}"
                tech = (h.data or {}).get("technician_id") or (t.technician_id if h.to_status == "assigned" else None)
                if h.to_status == "assigned" and tech:
                    text += f" ({techs.get(tech, tech)}; advisory, not a physical dispatch)"
            else:
                text = f"{t.code} [{ {'priority': 'prioritised', 'assignment': 'assignment', 'escalation': 'ESCALATION', 'note': 'note', 'inspection': 'inspection'}.get(kind, kind)}]"
            if h.note:
                text += f": {h.note}"
            items.append({"ts": iso(h.at), "kind": "ticket", "type": "ticket", "ocpp_action": None,
                          "charger_id": t.charger_id, "site_code": t.site.code if t.site else None,
                          "site_name": t.site.name if t.site else None, "severity": t.severity,
                          "source": t.source, "label": SOURCE_LABELS.get(t.source, t.source),
                          "description": text, "ticket": t.code, "actor": h.actor, "status_after": h.to_status,
                          "ticket_event": h.kind or "status", "origin": t.origin or None,
                          "_k": (h.at, 1, h.id)})
        total += db.scalar(select(func.count()).select_from(hq.subquery())) or 0
    items.sort(key=lambda d: d["_k"], reverse=True)
    page = items[offset:offset + limit]
    for d in page:
        d.pop("_k", None)
    return {"items": page, "total": total, "limit": limit, "offset": offset,
            "note": "OCPP-style messages are generated by the simulator (SIMULATED); no charger is connected."
            if source == "simulation" else None}
