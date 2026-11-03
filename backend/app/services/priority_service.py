"""Builds priority-engine inputs from live database state. The calculation itself lives in
core/priority.py and is never duplicated in the UI or anywhere else."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.catalog import CAN_CHARGE
from ..core.priority import PriorityInput, PriorityResult, compute, rank
from ..models import Charger, Site, Ticket
from .events import now_for_source
from .settings_store import priority_config

DONE = ("resolved", "closed")


def _inputs(db: Session, tickets: list[Ticket], source: str) -> list[PriorityInput]:
    sites = db.scalars(select(Site).where(Site.source == source)).all()
    max_sessions = max((s.daily_sessions for s in sites), default=0)
    chargers = db.scalars(select(Charger).where(Charger.source == source)).all()
    by_site: dict[int, list[Charger]] = {}
    for c in chargers:
        by_site.setdefault(c.site_id, []).append(c)
    now = now_for_source(db, source)
    out = []
    for t in tickets:
        site = db.get(Site, t.site_id)
        charger = db.get(Charger, t.charger_id)
        kw = charger.power_kw if charger else 0
        other_kw = sum(c.power_kw for c in by_site.get(t.site_id, []) if c.id != t.charger_id and c.status in CAN_CHARGE)
        notes = []
        if t.origin == "user_reported":
            notes.append("Reported by a user; not yet confirmed by charger telemetry.")
        elif t.origin:
            notes.append(f"Raised from {t.origin.replace('_', ' ')} data.")
        if t.severity_basis:
            notes.append(f"Severity: {t.severity_basis}")
        n = max(1, int(t.affected_chargers or 1))
        if n > 1:
            # Reported site-wide issue: N chargers of the same rating lose capacity (ASSUMPTION).
            others_affected = min(n - 1, sum(1 for c in by_site.get(t.site_id, []) if c.id != t.charger_id))
            lost = kw * (1 + others_affected)
            other_kw = max(0.0, other_kw - kw * others_affected)
            notes.append(f"Capacity factor uses {1 + others_affected} affected charger(s) as reported "
                         f"(ASSUMPTION: same {kw:g} kW rating).")
            kw = lost
        out.append(PriorityInput(
            ticket_ref=t.code, fault_category=t.fault_category,
            site_daily_sessions=site.daily_sessions if site else 0, max_site_daily_sessions=max_sessions,
            charger_kw=kw, other_working_kw_at_site=other_kw,
            age_hours=max(0.0, (now - t.detected_at).total_seconds() / 3600), detected_at=t.detected_at,
            site_name=site.name if site else "", severity_level=t.severity, notes=notes))
    return out


def rank_open_tickets(db: Session, source: str = "simulation") -> list[PriorityResult]:
    cfg = priority_config(db)
    tickets = db.scalars(select(Ticket).where(Ticket.status.not_in(DONE), Ticket.source == source)).all()
    if not tickets:
        return []
    return rank([compute(i, cfg) for i in _inputs(db, tickets, source)])


def ranking_map(db: Session, source: str = "simulation") -> dict[str, PriorityResult]:
    return {r.ticket_ref: r for r in rank_open_tickets(db, source)}
