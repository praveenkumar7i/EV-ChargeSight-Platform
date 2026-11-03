"""Maintenance ticket service: the single place where tickets are created, triaged, moved through the
workflow, escalated and audited, for every source (user reports, simulator, OCPP, operator API, imports).

Lifecycle (existing status names kept; v8 adds waiting and closed):

    open ──assign──> assigned ──accept──> en_route ──arrive──> on_site ──repair──> resolved ──verify──> closed
                                             │                   │  ▲                 │                  │
                                             └─> waiting_access ─┘  └─ waiting_parts ─┘                  │
                                                                     (resume -> on_site)                 │
    resolved / closed ──reopen (reason required)──> open  <──────────────────────────────────────────────┘

  * "NEW" = open and not yet triaged; "TRIAGED" = open with severity, SLA deadline and priority set
    (`triaged_at`). Triage happens immediately when the ticket is created.
  * "ESCALATED" is a flag (`escalated_at`), not a status: set once when the SLA deadline passes.
  * "IN_PROGRESS" = on_site. "en_route" = the technician accepted the job and is travelling.
  * Reassignment and unassignment go through services/assignment.py, never through a status jump.
  * Only listed transitions are allowed; anything else needs an administrative override
    (X-Admin-Token == ADMIN_TOKEN), which is recorded.
  * Documented system exception (existing): an open ticket may be closed directly by the system when a
    remote Reset succeeds or a silent charger reconnects (`resolved_by` = remote_reset | reconnect).

Safety (catalog.SAFETY_CATEGORIES or a ticket reported as a safety hazard):
  always critical; resolving does NOT return the charger to service. The charger is held
  "unavailable" until the ticket is closed with a passed post-repair inspection.

Every change is written to ticket_history with a `kind`:
  status | priority | assignment | escalation | note | inspection

Timestamps: assigned_at (assigned), acknowledged_at (accepted, en_route), on_site_at (first arrival),
resolved_at, closed_at. MTTA = mean(assigned_at - detected_at); MTTR = mean(resolved_at - detected_at).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import mean

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.catalog import FAULT_TYPES, ISSUE_CATEGORIES, ORIGIN_FOR_SOURCE, SAFETY_CATEGORIES, SEVERITY_VALUES, severity_of
from ..models import Charger, SimState, Technician, Ticket, TicketHistory
from ..tenancy import sim_state

WORKFLOW = ("open", "assigned", "en_route", "on_site", "resolved")      # main path (kept for compatibility)
STATUSES = ("open", "assigned", "en_route", "on_site", "waiting_parts", "waiting_access", "resolved", "closed")
DONE = ("resolved", "closed")
ACTIVE_WORK = ("assigned", "en_route", "on_site", "waiting_parts", "waiting_access")
NEXT_STATUS = {"open": "assigned", "assigned": "en_route", "en_route": "on_site", "on_site": "resolved"}
STATUS_LABELS = {"open": "Open", "assigned": "Assigned", "en_route": "En Route", "on_site": "On Site",
                 "waiting_parts": "Waiting for parts", "waiting_access": "Waiting for access",
                 "resolved": "Resolved", "closed": "Closed (verified)"}
ALLOWED = {
    "open": {"assigned"},
    "assigned": {"en_route"},
    "en_route": {"on_site", "waiting_access"},
    "on_site": {"resolved", "waiting_parts", "waiting_access"},
    "waiting_parts": {"on_site"},
    "waiting_access": {"on_site", "en_route"},
    "resolved": {"closed", "open"},
    "closed": {"open"},
}
ACTION_LABELS = {"assigned": "Assign technician", "en_route": "Accept job (en route)", "on_site": "Start work (on site)",
                 "waiting_parts": "Waiting for parts", "waiting_access": "Waiting for access", "resolved": "Record repair (resolve)",
                 "closed": "Verify and close", "open": "Reopen"}
LIFECYCLE_EQUIVALENTS = {"NEW": "open (before triage)", "TRIAGED": "open, triaged_at set", "ASSIGNED": "assigned",
                         "IN_PROGRESS": "en_route / on_site", "WAITING": "waiting_parts / waiting_access",
                         "RESOLVED": "resolved", "CLOSED": "closed", "ESCALATED": "escalated_at flag",
                         "REOPENED": "resolved/closed -> open (reopen_count)"}
RECURRENCE_WINDOW_H = 24          # a fault of the same kind within 24 h of the repair reopens the ticket (ASSUMPTION)
PRIORITY_AUDIT_MIN_CHANGE = 0.005


class WorkflowError(ValueError):
    pass


def is_safety(t: Ticket) -> bool:
    return t.fault_category in SAFETY_CATEGORIES or bool(t.safety_related)


def history(db: Session, t: Ticket, at: datetime, *, kind: str, to_status: str | None = None, from_status: str | None = None,
            actor: str = "system", note: str = "", override: bool = False, data: dict | None = None) -> TicketHistory:
    h = TicketHistory(ticket_id=t.id, from_status=from_status, to_status=to_status or t.status, at=at, actor=(actor or "system")[:40],
                      note=note, override=override, kind=kind, data=data or {})
    db.add(h)
    return h


def _next_code(db: Session) -> str:
    """Next free ticket code. Codes are shared by all sources (simulated, imported, OCPP, operator API),
    so the counter skips any code that already exists (e.g. after a simulation reset)."""
    from ..tenancy import LEGACY_ORG_ID, effective_org, org_code

    prefix = "TKT-" if effective_org(db) == LEGACY_ORG_ID else f"TKT-{org_code(db)}-"   # codes are globally unique
    state = sim_state(db)
    n = state.ticket_counter if state is not None else (db.scalar(select(Ticket.id).order_by(Ticket.id.desc()).limit(1)) or 0)
    with db.no_autoflush:
        while True:
            n += 1
            code = f"{prefix}{n:05d}"
            if db.scalar(select(Ticket.id).where(Ticket.code == code)) is None and \
                    not any(isinstance(o, Ticket) and o.code == code for o in db.new):
                break
    if state is not None:
        state.ticket_counter = n
    return code


def open_ticket_for(db: Session, charger_id: str, category: str) -> Ticket | None:
    return db.scalar(select(Ticket).where(Ticket.charger_id == charger_id, Ticket.fault_category == category,
                                          Ticket.status.not_in(DONE)))


def derive_severity(category: str, report: dict | None = None) -> tuple[str, str]:
    """Severity from the existing definitions (critical = cannot charge, major = degraded, minor = no effect).
    Safety faults are always critical and are never downgraded."""
    level, _v, _w = severity_of(category)
    if category in SAFETY_CATEGORIES or (report and report.get("issue_category") == "safety"):
        return "critical", "safety-related fault (always critical; never downgraded)"
    if not report:
        return level, f"catalogue default for {FAULT_TYPES[category].label if category in FAULT_TYPES else category}"
    if report.get("unable_to_charge") or report.get("impact") == "unavailable":
        return "critical", "reported: charger cannot charge (definition of critical)"
    if report.get("impact") in ("reduced_capacity", "intermittent"):
        return "major", f"reported: {report['impact'].replace('_', ' ')} (definition of major: degraded/unreliable)"
    if report.get("impact") == "informational":
        return "minor", "reported: informational, charging not affected (definition of minor)"
    return level, "catalogue default for the reported category (no impact given)"


def create_ticket(db: Session, charger: Charger, category: str, detected_at: datetime, source: str | None = None, *,
                  origin: str | None = None, report: dict | None = None, actor: str = "system",
                  auto_assign: bool = True) -> Ticket | None:
    """Create (or reopen) a ticket. Returns the new/reopened ticket, or None when an active ticket for the
    same charger and fault already exists (the duplicate report is noted on that ticket)."""
    db.flush()
    source = source or charger.source
    origin = origin or ORIGIN_FOR_SOURCE.get(source, "simulator")
    existing = open_ticket_for(db, charger.id, category)
    if existing is not None:
        if report is not None:
            history(db, existing, max(detected_at, existing.detected_at), kind="note", actor=actor,
                    note=f"Duplicate report ({origin.replace('_', ' ')}): {report.get('description', '')[:300]}",
                    data={"duplicate_report": True, "reported_by": report.get("reported_by", "")})
        elif existing.origin == "user_reported" and not any((h.data or {}).get("telemetry_confirmed") for h in existing.history):
            history(db, existing, max(detected_at, existing.detected_at), kind="note", actor="system",
                    note=f"Charger telemetry ({origin}) reported the same fault; the user report is now confirmed.",
                    data={"telemetry_confirmed": True})
        return None

    if origin != "imported_data":
        recent = db.scalar(select(Ticket).where(Ticket.charger_id == charger.id, Ticket.fault_category == category,
                                                Ticket.status.in_(DONE), Ticket.resolved_at.is_not(None),
                                                Ticket.resolved_at >= detected_at - timedelta(hours=RECURRENCE_WINDOW_H),
                                                Ticket.resolved_at <= detected_at).order_by(Ticket.resolved_at.desc()))
        if recent is not None:
            reopen(db, recent, detected_at, actor=actor,
                   note=f"Fault recurred within {RECURRENCE_WINDOW_H} h of the repair ({origin.replace('_', ' ')}).")
            if auto_assign:
                _auto_assign(db, recent, detected_at)
            return recent

    level, basis = derive_severity(category, report)
    ft = FAULT_TYPES.get(category)
    r = report or {}
    t = Ticket(
        code=_next_code(db), charger_id=charger.id, site_id=charger.site_id, fault_category=category, severity=level,
        detected_at=detected_at, status="open", est_repair_min=ft.repair_mean_min if ft else None, source=source,
        origin=origin, severity_basis=basis, safety_related=category in SAFETY_CATEGORIES or r.get("issue_category") == "safety",
        issue_category=r.get("issue_category"), description=r.get("description", ""), symptoms=r.get("symptoms", ""),
        error_code=r.get("error_code"), impact=r.get("impact"), unable_to_charge=r.get("unable_to_charge"),
        affected_chargers=max(1, int(r.get("affected_chargers") or 1)), reported_by=r.get("reported_by", ""),
        client_request_id=r.get("client_request_id"),
    )
    if ft and ft.remote_reset_success > 0 and source == "simulation" and origin == "simulator":
        state = sim_state(db)
        delay = float((state.params or {}).get("reset_delay_min", 10)) if state else 10.0
        t.reset_due_at = detected_at + timedelta(minutes=delay)
    db.add(t)
    db.flush()
    if origin == "user_reported":
        cat_label = ISSUE_CATEGORIES.get(r.get("issue_category", ""), ("", ""))[0] or category
        note = f"Reported by {r.get('reported_by') or 'a user'} (USER_REPORTED): {cat_label}."
    else:
        note = f"Detected {ft.label if ft else category}"
    history(db, t, detected_at, kind="status", from_status=None, to_status="open", actor=actor, note=note,
            data={"origin": origin})
    triage(db, t, detected_at)
    if auto_assign:
        _auto_assign(db, t, detected_at)
    return t


def triage(db: Session, t: Ticket, at: datetime) -> None:
    """Set the SLA deadline and the priority snapshot (existing SLA hours and Priority Engine)."""
    from .settings_store import sla_hours

    hours = float(sla_hours(db).get(t.severity, 24))
    t.sla_due_at = at + timedelta(hours=hours)
    t.triaged_at = at
    refresh_priority(db, t, at, reason=f"Triage: {t.severity} ({t.severity_basis}); SLA {hours:g} h.", force=True)


def refresh_priority(db: Session, t: Ticket, at: datetime, reason: str, force: bool = False):
    """Recompute this ticket's score with the authoritative engine and audit any change of the stored
    snapshot (previous, new, reason, time). The live ranking still moves with the age factor."""
    from .priority_service import ranking_map

    if t.status in DONE:
        return None
    db.flush()
    r = ranking_map(db, t.source).get(t.code)
    if r is None:
        return None
    new = round(r.score, 4)
    old = t.priority_score
    if force or old is None or abs(new - old) >= PRIORITY_AUDIT_MIN_CHANGE:
        history(db, t, at, kind="priority", actor="priority-engine",
                note=reason + f" Score {'—' if old is None else f'{old:.3f}'} → {new:.3f} (rank {r.rank}).",
                data={"previous": old, "new": new, "rank": r.rank, "severity": t.severity, "reason": reason})
        t.priority_score = new
        t.priority_updated_at = at
    return r


def _auto_assign(db: Session, t: Ticket, at: datetime) -> None:
    from . import assignment

    assignment.try_auto_assign(db, t, at)


def _restore_charger_if_clear(db: Session, ticket: Ticket, at: datetime, how: str) -> None:
    from .events import record_event

    db.flush()
    blocking = [o for o in db.scalars(select(Ticket).where(Ticket.charger_id == ticket.charger_id, Ticket.id != ticket.id))
                if o.status not in DONE or (o.status == "resolved" and is_safety(o) and o.inspection_passed is not True)]
    charger = db.get(Charger, ticket.charger_id)
    charger.comm_lost = False
    if not blocking and charger.status in ("faulted", "offline", "unavailable"):
        record_event(db, charger, "fault_resolved", at, status_after="available",
                     payload={"ticket": ticket.code, "resolved_by": how}, source=ticket.source)
    elif blocking and charger.status == "offline" and ticket.fault_category == "comm_loss":
        record_event(db, charger, "reconnect", at, status_after="faulted",
                     payload={"ticket": ticket.code, "note": "reconnected with other open faults"}, source=ticket.source)


def _hold_for_inspection(db: Session, t: Ticket, at: datetime) -> None:
    from .events import record_event

    charger = db.get(Charger, t.charger_id)
    if charger.status == "faulted":
        record_event(db, charger, "status_change", at, status_after="unavailable",
                     payload={"ticket": t.code, "reason": "repaired; awaiting post-repair safety inspection"}, source=t.source)


def transition(db: Session, ticket: Ticket, to_status: str, at: datetime, *, actor: str = "user", note: str = "",
               technician_id: str | None = None, override: bool = False, est_travel_min: float | None = None,
               inspection_passed: bool | None = None) -> Ticket:
    if to_status not in STATUSES:
        raise WorkflowError(f"Unknown status '{to_status}'.")
    frm = ticket.status
    if frm == to_status:
        raise WorkflowError(f"Ticket is already {STATUS_LABELS[frm]}.")
    if not override and to_status not in ALLOWED.get(frm, set()):
        nxt = " or ".join(STATUS_LABELS[s] for s in sorted(ALLOWED.get(frm, set()), key=STATUSES.index)) or "none"
        raise WorkflowError(f"Cannot move from {STATUS_LABELS[frm]} to {STATUS_LABELS[to_status]}; next step is {nxt}.")
    if at < ticket.detected_at:
        raise WorkflowError("Transition time cannot be earlier than detection time.")
    note = (note or "").strip()

    if to_status == "assigned":
        tech_id = technician_id or ticket.technician_id
        if not tech_id:
            raise WorkflowError("A technician is required to assign a ticket.")
        tech = db.get(Technician, tech_id)
        if tech is None or tech.org_id != ticket.org_id:          # never another organisation's technician
            raise WorkflowError(f"Unknown technician '{tech_id}'.")
        ticket.technician_id = tech.id
        ticket.assigned_at = at
        if est_travel_min is not None:
            ticket.est_travel_min = est_travel_min
        elif ticket.est_travel_min is None and ticket.site is not None:
            ticket.est_travel_min = (ticket.site.travel_min_low + ticket.site.travel_min_high) / 2
    elif to_status == "en_route":
        ticket.acknowledged_at = ticket.acknowledged_at or at
    elif to_status == "on_site":
        ticket.on_site_at = ticket.on_site_at or at
        ticket.waiting_reason = ""
    elif to_status in ("waiting_parts", "waiting_access"):
        if not note and not override:
            raise WorkflowError("Give a reason (what part, or what access is needed).")
        ticket.waiting_reason = note[:200]
    elif to_status == "resolved":
        ticket.resolved_at = at
        ticket.resolved_by = "admin" if override and frm != "on_site" else "technician"
        if note:
            ticket.resolution_notes = note
    elif to_status == "closed":
        if frm != "resolved" and not override:
            raise WorkflowError("Only a resolved ticket can be closed.")
        if is_safety(ticket) and inspection_passed is not True and not override:
            raise WorkflowError("Safety-related ticket: record a passed post-repair inspection before closing.")
        ticket.inspection_passed = True if inspection_passed else ticket.inspection_passed
        ticket.closed_at = at
        ticket.closed_by = actor[:80]
        if ticket.resolved_at is None:
            ticket.resolved_at = at
            ticket.resolved_by = ticket.resolved_by or "admin"
    elif to_status == "open":
        if frm not in DONE and not override:
            raise WorkflowError("Only a resolved or closed ticket can be reopened; use reassignment to change the technician.")
        if not note and not override:
            raise WorkflowError("Give a reason for reopening.")

    if override:   # back-fill skipped timestamps so metrics stay well-defined
        idx = WORKFLOW.index(to_status) if to_status in WORKFLOW else None
        if idx is not None:
            if idx >= 1 and ticket.assigned_at is None:
                ticket.assigned_at = at
            if idx >= 2 and ticket.acknowledged_at is None:
                ticket.acknowledged_at = at
            if idx >= 3 and ticket.on_site_at is None:
                ticket.on_site_at = at

    if to_status == "open":
        _reset_for_reopen(ticket, at)
    ticket.status = to_status
    history(db, ticket, at, kind="status", from_status=frm, to_status=to_status, actor=actor, note=note, override=override,
            data={"inspection_passed": True} if to_status == "closed" and inspection_passed else None)
    if to_status == "closed" and inspection_passed:
        history(db, ticket, at, kind="inspection", actor=actor, note="Post-repair inspection passed." + (f" {note}" if note else ""),
                data={"passed": True})

    if to_status == "resolved":
        if is_safety(ticket):
            _hold_for_inspection(db, ticket, at)
        else:
            _restore_charger_if_clear(db, ticket, at, ticket.resolved_by or "technician")
    elif to_status == "closed" and is_safety(ticket):
        _restore_charger_if_clear(db, ticket, at, "inspection passed")
    elif to_status == "open":
        triage(db, ticket, at)
    if frm in ACTIVE_WORK and to_status not in ACTIVE_WORK:
        _retry_assignments(db, ticket.source, at)
    return ticket


def _reset_for_reopen(t: Ticket, at: datetime) -> None:
    t.reopen_count = (t.reopen_count or 0) + 1
    t.technician_id = None
    t.assigned_at = t.acknowledged_at = t.on_site_at = None
    t.resolved_at = None
    t.resolved_by = None
    t.closed_at = None
    t.closed_by = ""
    t.inspection_passed = None
    t.escalated_at = None
    t.escalation_reason = ""
    t.dispatch_needed = False
    t.auto_assigned = False
    t.sim_work_due_at = None


def reopen(db: Session, t: Ticket, at: datetime, *, actor: str, note: str) -> Ticket:
    return transition(db, t, "open", at, actor=actor, note=note)


def _retry_assignments(db: Session, source: str, at: datetime) -> None:
    from . import assignment

    assignment.retry_unassigned(db, source, at)


def resolve_by_system(db: Session, ticket: Ticket, at: datetime, how: str, note: str) -> Ticket:
    """Documented exception: open -> resolved after remote reset / reconnect."""
    if how not in ("remote_reset", "reconnect"):
        raise WorkflowError("System resolution must be remote_reset or reconnect.")
    if ticket.status != "open":
        raise WorkflowError("Only open (unassigned) tickets can be closed automatically.")
    ticket.status = "resolved"
    ticket.resolved_at = at
    ticket.resolved_by = how
    ticket.resolution_notes = note
    ticket.dispatch_needed = False
    history(db, ticket, at, kind="status", from_status="open", to_status="resolved", actor="system", note=note)
    _restore_charger_if_clear(db, ticket, at, how)
    return ticket


def sla_due(t: Ticket, sla_hours: dict) -> datetime:
    return getattr(t, "sla_due_at", None) or (t.detected_at + timedelta(hours=float(sla_hours.get(t.severity, 24))))


def is_overdue(t: Ticket, now: datetime, sla_hours: dict) -> bool:
    return t.status not in DONE and now > sla_due(t, sla_hours)


def sla_sweep(db: Session, source: str, now: datetime) -> int:
    """Escalate active tickets whose SLA deadline has passed. Idempotent: a ticket is escalated once
    (escalated_at), so polling never creates duplicate escalation events."""
    from ..tenancy import effective_org
    from .entitlements import has_feature
    from .settings_store import sla_hours

    if not has_feature(db, effective_org(db), "sla_escalation"):
        return 0                   # plan without SLA escalation: deadlines are still shown, nothing is escalated
    sla = sla_hours(db)
    n = 0
    due = db.scalars(select(Ticket).where(Ticket.source == source, Ticket.status.not_in(DONE), Ticket.escalated_at.is_(None))).all()
    for t in due:
        deadline = sla_due(t, sla)
        if now <= deadline:
            continue
        hours = float(sla.get(t.severity, 24))
        t.escalated_at = now
        t.escalation_reason = f"SLA breached: {t.severity} tickets must be resolved within {hours:g} h."
        if t.technician_id is None:
            t.dispatch_needed = True
        history(db, t, now, kind="escalation", actor="sla-monitor", note=t.escalation_reason +
                (" No technician assigned: dispatch needed." if t.technician_id is None else ""),
                data={"sla_hours": hours, "deadline": deadline.isoformat() + "Z"})
        n += 1
    if n:
        _retry_assignments(db, source, now)
    return n


def backfill_v8(db: Session) -> int:
    """Give pre-v8 tickets an origin, severity basis and SLA deadline (data defaults; no priority change)."""
    from .settings_store import sla_hours

    sla = sla_hours(db)
    n = 0
    for t in db.scalars(select(Ticket).where((Ticket.origin == "") | (Ticket.origin.is_(None)) | (Ticket.sla_due_at.is_(None)))):
        if not t.origin:
            t.origin = ORIGIN_FOR_SOURCE.get(t.source, "simulator")
        if not t.severity_basis:
            t.severity_basis = "catalogue default" if FAULT_TYPES.get(t.fault_category) and FAULT_TYPES[t.fault_category].severity == t.severity \
                else "severity set when the fault was raised"
        if t.sla_due_at is None:
            t.sla_due_at = t.detected_at + timedelta(hours=float(sla.get(t.severity, 24)))
        if t.safety_related is None or (t.fault_category in SAFETY_CATEGORIES and not t.safety_related):
            t.safety_related = t.fault_category in SAFETY_CATEGORIES
        n += 1
    flag_unqualified_assignments(db)
    return n


UNQUALIFIED_NOTE = "Review: assigned before v8 to a technician who is not safety-certified; reassign to a certified technician."


def flag_unqualified_assignments(db: Session) -> int:
    """Pre-v8 assignments of safety faults to uncertified technicians are flagged (once), never changed silently."""
    n = 0
    for t in db.scalars(select(Ticket).where(Ticket.status.in_(ACTIVE_WORK), Ticket.technician_id.is_not(None))):
        tech = db.get(Technician, t.technician_id)
        if tech is None or not is_safety(t) or tech.safety_certified or t.assignment_note == UNQUALIFIED_NOTE:
            continue
        t.assignment_note = UNQUALIFIED_NOTE
        history(db, t, max(t.detected_at, t.assigned_at or t.detected_at), kind="assignment", actor="safety-check",
                note=UNQUALIFIED_NOTE, data={"technician_id": tech.id, "safety_certified": False})
        n += 1
    return n


def allowed_actions(t: Ticket) -> list[dict]:
    out = []
    for s in sorted(ALLOWED.get(t.status, set()), key=STATUSES.index):
        a = {"to_status": s, "label": ACTION_LABELS[s], "needs_note": s in ("waiting_parts", "waiting_access", "open"),
             "needs_technician": s == "assigned", "needs_inspection": s == "closed" and is_safety(t)}
        out.append(a)
    return out


def ticket_metrics(tickets: list[Ticket], now: datetime, sla_hours: dict) -> dict:
    assigned = [(t.assigned_at - t.detected_at).total_seconds() / 60 for t in tickets if t.assigned_at]
    resolved = [(t.resolved_at - t.detected_at).total_seconds() / 60 for t in tickets if t.resolved_at]
    open_t = [t for t in tickets if t.status not in DONE]
    overdue = [t for t in open_t if now > sla_due(t, sla_hours)]
    return {
        "total": len(tickets),
        "open": len(open_t),
        "resolved": len(resolved),
        "unresolved_high_severity": sum(1 for t in open_t if t.severity == "critical"),
        "overdue": len(overdue),
        "escalated": sum(1 for t in open_t if getattr(t, "escalated_at", None) is not None),
        "dispatch_needed": sum(1 for t in open_t if getattr(t, "dispatch_needed", False)),
        "unassigned": sum(1 for t in open_t if t.status == "open"),
        "in_progress": sum(1 for t in open_t if t.status in ACTIVE_WORK),
        "awaiting_verification": sum(1 for t in tickets if t.status == "resolved" and getattr(t, "fault_category", None) is not None and is_safety(t)),
        "user_reported_open": sum(1 for t in open_t if getattr(t, "origin", "") == "user_reported"),
        "mtta_min": mean(assigned) if assigned else None,
        "mttr_min": mean(resolved) if resolved else None,
        "mtta_n": len(assigned),
        "mttr_n": len(resolved),
        "by_status": {s: sum(1 for t in tickets if t.status == s) for s in STATUSES},
        "by_origin": {o: sum(1 for t in tickets if getattr(t, "origin", "") == o) for o in sorted({getattr(t, "origin", "") for t in tickets} - {"", None})},
        "resolved_by": {k: sum(1 for t in tickets if t.resolved_by == k) for k in ("technician", "remote_reset", "reconnect", "admin")},
        "definitions": {
            "mtta_min": "mean(assigned_at - detected_at) over tickets that have been assigned, minutes",
            "mttr_min": "mean(resolved_at - detected_at) over resolved/closed tickets (all resolution routes), minutes",
            "overdue": "active tickets past their SLA deadline (detected + SLA hours for the severity; ASSUMPTION thresholds)",
            "escalated": "active tickets escalated once when their SLA deadline passed",
        },
    }
