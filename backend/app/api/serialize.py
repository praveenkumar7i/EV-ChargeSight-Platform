"""Model -> JSON helpers shared by the routers."""
from __future__ import annotations

from ..clock import iso
from ..core.catalog import FAULT_TYPES, IMPACTS, ISSUE_CATEGORIES, ORIGIN_LABELS, SOURCE_LABELS
from ..core.priority import PriorityResult
from ..models import Charger, Event, Site, Ticket
from ..services.tickets import DONE, STATUS_LABELS, allowed_actions, is_safety


def site(s: Site, stats: dict | None = None) -> dict:
    d = {
        "id": s.id, "code": s.code, "name": s.name, "lat": s.lat, "lng": s.lng,
        "has_position": not (s.lat == 0.0 and s.lng == 0.0),
        "daily_sessions": s.daily_sessions, "daily_sessions_label": "ASSUMPTION" if s.source == "simulation" else "IMPORTED",
        "travel_min": [s.travel_min_low, s.travel_min_high],
        "is_demo_location": s.is_demo_location, "source": s.source, "label": SOURCE_LABELS[s.source],
    }
    if stats is not None:
        d.update(stats)
    return d


def label_for_charger(c: Charger | None, source: str) -> str:
    """OCPP traffic from the local simulator is labelled SIMULATED OCPP, never LIVE."""
    if c is not None and c.source == "ocpp" and getattr(c, "test_traffic", False):
        return "SIMULATED OCPP"
    return SOURCE_LABELS[source]


def charger(c: Charger, open_tickets: list[Ticket] | None = None, top: PriorityResult | None = None) -> dict:
    return {
        "id": c.id, "site_id": c.site_id, "site_code": c.site.code if c.site else None,
        "site_name": c.site.name if c.site else None, "connector_type": c.connector_type,
        "power_kw": c.power_kw, "status": c.status, "last_event_at": iso(c.last_event_at),
        "last_heartbeat_at": iso(c.last_heartbeat_at), "session_kwh": round(c.session_kwh, 3),
        "total_kwh": round(c.total_kwh, 3), "source": c.source, "label": label_for_charger(c, c.source),
        "test_traffic": bool(getattr(c, "test_traffic", False)), "ocpp_vendor": getattr(c, "ocpp_vendor", None),
        "ocpp_model": getattr(c, "ocpp_model", None),
        "external_id": c.external_id,
        "open_tickets": [t.code for t in (open_tickets or [])],
        "top_priority_score": round(top.score, 4) if top else None,
        "top_priority_ticket": top.ticket_ref if top else None,
        "lat": c.site.lat if c.site else None, "lng": c.site.lng if c.site else None,
        "power_now_kw": round(c.power_now_kw or 0.0, 2),
        "temp_c": None if c.temp_c is None else round(c.temp_c, 1),
        "temp_label": "SIMULATED model" if c.source == "simulation" else "reported",
        "session_started_at": iso(c.session_started_at),
        "fault_code": _fault_code(open_tickets),
    }


def _fault_code(open_tickets) -> str | None:
    for t in open_tickets or []:
        ft = FAULT_TYPES.get(t.fault_category)
        if ft is not None:
            return ft.ocpp_error_code or ft.key
        return t.fault_category
    return None


def recommended_action(t: Ticket, overdue: bool = False, now=None) -> str:
    """Plain-language next step for the ticket's current workflow state.
    Advisory only: ChargeSight records the workflow but does not physically dispatch anyone."""
    if t.status == "closed":
        return "None. Ticket verified and closed."
    if t.status == "resolved":
        if is_safety(t):
            return "Record the post-repair safety inspection, then close. The charger stays out of service until then."
        return "Verify the charger reports Available, then close (optional)."
    if t.status == "open":
        if t.reset_due_at is not None and not t.remote_reset_attempted:
            mins = None if now is None else max(0, round((t.reset_due_at - now).total_seconds() / 60))
            when = f"in {mins} min" if mins is not None else "shortly"
            act = f"Wait for the automatic remote reset ({when}); a technician is assigned if it fails."
        elif t.dispatch_needed:
            act = "DISPATCH NEEDED: no eligible technician. Free up or add a qualified technician, or assign manually."
        elif t.remote_reset_attempted:
            act = "Remote reset failed. Assign a technician."
        elif t.severity == "critical":
            act = "Assign a qualified technician now (charger cannot charge)."
        else:
            act = "Assign a technician."
    elif (t.assignment_note or "").startswith("Review: assigned before v8"):
        act = "Reassign to a safety-certified technician (current technician is not certified for this fault)."
    elif t.status == "assigned":
        act = "Technician to accept the job (en route)."
    elif t.status == "en_route":
        act = "Mark on site when the technician arrives."
    elif t.status == "waiting_parts":
        act = f"Waiting for parts ({t.waiting_reason or 'no detail'}); resume work when they arrive."
    elif t.status == "waiting_access":
        act = f"Waiting for access ({t.waiting_reason or 'no detail'}); resume when access is arranged."
    else:
        act = "Complete the repair and record it (resolve) with notes."
    return ("OVERDUE (SLA breached): escalate. " + act) if overdue else act


def event(e: Event) -> dict:
    return {
        "id": e.id, "ts": iso(e.ts), "charger_id": e.charger_id, "type": e.type, "ocpp_action": e.ocpp_action,
        "status_after": e.status_after, "payload": e.payload, "source": e.source, "label": SOURCE_LABELS[e.source],
        "external_event_id": e.external_event_id, "ingested_at": iso(e.ingested_at),
    }


def ticket(t: Ticket, pr: PriorityResult | None = None, overdue: bool = False, with_history: bool = False,
           now=None, sla_hours: dict | None = None) -> dict:
    from ..services.tickets import sla_due

    ft = FAULT_TYPES.get(t.fault_category)
    due = sla_due(t, sla_hours or {}) if (t.sla_due_at or sla_hours) else None
    remaining = None
    if due is not None and now is not None and t.status not in DONE:
        remaining = round((due - now).total_seconds() / 60, 1)
    d = {
        "code": t.code, "charger_id": t.charger_id, "site_id": t.site_id,
        "site_code": t.site.code if t.site else None, "site_name": t.site.name if t.site else None,
        "fault_category": t.fault_category, "fault_label": ft.label if ft else t.fault_category,
        "severity": t.severity, "severity_basis": t.severity_basis, "status": t.status, "status_label": STATUS_LABELS[t.status],
        "detected_at": iso(t.detected_at), "assigned_at": iso(t.assigned_at),
        "acknowledged_at": iso(t.acknowledged_at), "on_site_at": iso(t.on_site_at),
        "resolved_at": iso(t.resolved_at), "closed_at": iso(t.closed_at), "closed_by": t.closed_by or None,
        "technician_id": t.technician_id, "technician_name": t.technician.name if t.technician else None,
        "technician_is_demo": bool(t.technician.is_demo) if t.technician else None,
        "est_travel_min": t.est_travel_min, "est_repair_min": t.est_repair_min,
        "est_label": "ASSUMPTION", "resolution_notes": t.resolution_notes, "resolved_by": t.resolved_by,
        "remote_reset_attempted": t.remote_reset_attempted, "reset_due_at": iso(t.reset_due_at),
        "overdue": overdue, "source": t.source, "label": label_for_charger(t.charger, t.source),
        "origin": t.origin or None, "origin_label": ORIGIN_LABELS.get(t.origin or "", "UNKNOWN"),
        "issue_category": t.issue_category, "issue_label": ISSUE_CATEGORIES.get(t.issue_category or "", (None,))[0],
        "description": t.description, "symptoms": t.symptoms, "error_code": t.error_code,
        "impact": t.impact, "impact_label": IMPACTS.get(t.impact or ""), "unable_to_charge": t.unable_to_charge,
        "affected_chargers": t.affected_chargers, "reported_by": t.reported_by or None,
        "safety_related": is_safety(t), "inspection_passed": t.inspection_passed,
        "evidence": ("Reported by a user; not confirmed by charger telemetry" if t.origin == "user_reported"
                     else "Imported historical record" if t.origin == "imported_data"
                     else "Detected from charger events" + (" (SIMULATED)" if t.source == "simulation" else "")),
        "priority": pr.to_dict() if pr else None,
        "priority_snapshot": {"score": t.priority_score, "updated_at": iso(t.priority_updated_at)} if t.priority_score is not None else None,
        "triaged_at": iso(t.triaged_at),
        "sla_due_at": iso(due), "sla_remaining_min": remaining if remaining is not None and remaining >= 0 else None,
        "overdue_min": -remaining if remaining is not None and remaining < 0 else None,
        "time_basis": "simulation clock" if t.source == "simulation" else "wall clock (UTC)",
        "escalated": t.escalated_at is not None, "escalated_at": iso(t.escalated_at), "escalation_reason": t.escalation_reason or None,
        "dispatch_needed": bool(t.dispatch_needed), "assignment_note": t.assignment_note or None, "auto_assigned": bool(t.auto_assigned),
        "waiting_reason": t.waiting_reason or None, "reopen_count": t.reopen_count or 0,
        "age_min": round(((t.resolved_at or now) - t.detected_at).total_seconds() / 60, 1) if (t.resolved_at or now) else None,
        "recommended_action": recommended_action(t, overdue, now),
        "allowed_actions": allowed_actions(t),
        "severity_overridden": FAULT_TYPES.get(t.fault_category) is not None and FAULT_TYPES[t.fault_category].severity != t.severity
        and t.origin != "user_reported" and not (t.severity_basis or "").startswith("safety"),
    }
    if with_history:
        d["history"] = [{"from": h.from_status, "to": h.to_status, "at": iso(h.at), "actor": h.actor, "note": h.note,
                         "override": h.override, "kind": h.kind or "status", "data": h.data or {}} for h in t.history]
    return d
