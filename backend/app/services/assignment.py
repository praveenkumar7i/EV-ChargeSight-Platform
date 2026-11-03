"""Technician eligibility and assignment (automatic and manual). One implementation, used by the
API, the simulator and every ticket source.

Eligibility: all must hold, otherwise the technician is listed with the blocking reasons.
  1. active, availability == "available" and, if a shift is set, on shift at the ticket's clock time;
  2. has the skill the fault needs (catalog.REQUIRED_SKILL);
  3. equipped for the charger ("dc" for DC/rapid chargers, "ac" otherwise);
  4. safety-certified when the fault is safety-related;
  5. active workload < capacity (existing rule: capacity 1 = one job at a time);
  6. DEMO technicians only take tickets on SIMULATED chargers (fictional staff never appear on real faults).

Choice among eligible technicians (deterministic):
  can reach the site before the SLA deadline (only when a travel estimate exists)
  -> lower workload share -> lower workload -> shorter distance (only when both locations are known) -> id.
Travel estimates are straight-line distance / travel_speed_kmh (ASSUMPTION), shown only when the
technician base and the site both have coordinates; otherwise no proximity or travel time is claimed.

Queue order when technicians are scarce: the existing priority ranking (escalated first, then score),
or FIFO when the simulator's auto_dispatch_policy is "fifo". Workload never lowers a ticket's priority.

No eligible technician: the ticket stays open and unassigned, the reasons are stored, critical or
escalated tickets get `dispatch_needed`, and assignment is retried whenever availability, skills,
workload or SLA state changes. Emergency override (policy, default off): a qualified technician may be
assigned above capacity for critical/escalated tickets. Skills and safety certification are never bypassed
automatically.

Manual assignment: warnings for skill, equipment, availability, capacity and SLA conflicts must be
confirmed with a reason; safety certification can only be overridden with the administrative token.
Every assignment, reassignment and unassignment is recorded (who, when, why).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.areas import haversine_km
from ..core.catalog import REQUIRED_SKILL
from ..models import Charger, SimState, Technician, Ticket
from . import settings_store
from .tickets import ACTIVE_WORK, DONE, history, is_safety, transition
from ..tenancy import sim_state


class AssignmentConflict(ValueError):
    def __init__(self, message: str, conflicts: list[str], safety: bool = False):
        super().__init__(message)
        self.conflicts = conflicts
        self.safety = safety


def required_skill(t: Ticket) -> str:
    return REQUIRED_SKILL.get(t.fault_category, "other")


def required_equipment(c: Charger | None) -> str:
    if c is None:
        return "ac"
    return "dc" if ("DC" in (c.connector_type or "").upper() or (c.power_kw or 0) > 22) else "ac"


def workload(db: Session, tech_id: str, exclude_ticket: int | None = None) -> int:
    return sum(1 for i in db.scalars(select(Ticket.id).where(Ticket.technician_id == tech_id, Ticket.status.in_(ACTIVE_WORK)))
               if i != exclude_ticket)


def effective_status(db: Session, tech: Technician) -> str:
    if not tech.active:
        return "inactive"
    if (tech.availability or "available") != "available":
        return tech.availability
    wl = workload(db, tech.id)
    if wl >= max(1, int(tech.capacity or 1)):
        return "at_capacity"
    return "assigned" if wl else "available"


def _on_shift(tech: Technician, now: datetime) -> bool | None:
    if not tech.shift_start or not tech.shift_end:
        return None
    hm = now.strftime("%H:%M")
    a, b = tech.shift_start, tech.shift_end
    return (a <= hm < b) if a <= b else (hm >= a or hm < b)


def auto_assign_enabled(db: Session, source: str) -> bool:
    from ..tenancy import effective_org
    from .entitlements import has_feature

    if not has_feature(db, effective_org(db), "auto_assignment"):
        return False               # plan without automatic assignment (or read-only): tickets wait for manual dispatch
    pol = settings_store.assignment_policy(db)
    if not pol.get("auto_assign"):
        return False
    if source == "simulation":
        st = sim_state(db)
        return bool({**_default_params(), **(st.params or {})}.get("auto_dispatch", True)) if st else False
    if source == "operator_import":
        return False                                   # historical records are never dispatched
    return bool(pol.get("auto_assign_live_sources"))


def _default_params() -> dict:
    from .seed import DEFAULT_SIM_PARAMS
    return DEFAULT_SIM_PARAMS


def evaluate(db: Session, t: Ticket, tech: Technician, now: datetime) -> dict:
    pol = settings_store.assignment_policy(db)
    charger = db.get(Charger, t.charger_id)
    site = t.site
    skill, equip, safety = required_skill(t), required_equipment(charger), is_safety(t)
    wl = workload(db, tech.id, exclude_ticket=t.id if t.technician_id == tech.id else None)
    cap = max(1, int(tech.capacity or 1))
    blockers, ok = [], []
    if not tech.active:
        blockers.append("inactive")
    if (tech.availability or "available") != "available":
        blockers.append(f"availability: {(tech.availability or '').replace('_', ' ')}")
    if _on_shift(tech, now) is False:
        blockers.append(f"off shift ({tech.shift_start}–{tech.shift_end})")
    if skill in (tech.skills or []):
        ok.append(f"has skill '{skill.replace('_', ' ')}'")
    else:
        blockers.append(f"missing skill '{skill.replace('_', ' ')}'")
    if equip in (tech.equipment or []):
        ok.append(f"{equip.upper()} equipment")
    else:
        blockers.append(f"not equipped for {equip.upper()} chargers")
    if safety:
        if tech.safety_certified:
            ok.append("safety-certified")
        else:
            blockers.append("not safety-certified (safety-related fault)")
    if wl >= cap:
        blockers.append(f"at capacity ({wl}/{cap} active)")
    else:
        ok.append(f"workload {wl}/{cap}")
    if tech.is_demo and t.source != "simulation":
        blockers.append("DEMO technician: only handles SIMULATED chargers")
    dist = travel = None
    basis = "no reliable location data (no proximity claimed)"
    if tech.base_lat is not None and tech.base_lng is not None and site is not None and not (site.lat == 0 and site.lng == 0):
        dist = haversine_km(tech.base_lat, tech.base_lng, site.lat, site.lng)
        if tech.is_demo or site.is_demo_location:
            # Demo area centroids are approximate: distance only breaks ties; no travel time is claimed.
            basis = f"approx. {dist:.1f} km between demo area centroids (tie-break only; no travel time claimed)"
        else:
            travel = dist / float(pol.get("travel_speed_kmh", 20.0)) * 60
            basis = f"straight-line {dist:.1f} km from {tech.base_label or 'base'} at {float(pol.get('travel_speed_kmh', 20)):g} km/h (ASSUMPTION)"
    can_meet = None
    if travel is not None and t.sla_due_at is not None:
        can_meet = now.timestamp() + travel * 60 <= t.sla_due_at.timestamp()
    capacity_only = blockers == [f"at capacity ({wl}/{cap} active)"]
    return {"technician_id": tech.id, "name": tech.name, "is_demo": tech.is_demo, "eligible": not blockers,
            "blockers": blockers, "qualifies": ok, "workload": wl, "capacity": cap,
            "distance_km": round(dist, 2) if dist is not None else None,
            "travel_min": round(travel) if travel is not None else None, "travel_basis": basis,
            "can_meet_sla": can_meet, "safety_blocker": safety and not tech.safety_certified,
            "capacity_only_blocker": capacity_only,
            "required": {"skill": skill, "equipment": equip, "safety_certified": safety}}


def candidates(db: Session, t: Ticket, now: datetime) -> list[dict]:
    rows = [evaluate(db, t, tech, now) for tech in db.scalars(select(Technician).where(Technician.org_id == t.org_id)   # never across organisations
                                                           .order_by(Technician.id))]
    rows.sort(key=lambda r: (0 if r["eligible"] else 1, 0 if r["can_meet_sla"] is not False else 1,
                             r["workload"] / r["capacity"], r["workload"],
                             r["distance_km"] if r["distance_km"] is not None else 1e9, r["technician_id"]))
    return rows


def _summarise_blockers(rows: list[dict]) -> str:
    if not rows:
        return "No technicians are registered."
    counts: dict[str, int] = {}
    for r in rows:
        for b in r["blockers"]:
            key = b.split(" (")[0] if b.startswith(("at capacity", "off shift")) else b
            counts[key] = counts.get(key, 0) + 1
    parts = [f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    return f"No eligible technician available (0 of {len(rows)}): " + "; ".join(parts) + "."


def _assign(db: Session, t: Ticket, best: dict, now: datetime, *, actor: str, auto: bool, reason: str,
            override: bool = False) -> None:
    prev = t.technician_id
    travel = best.get("travel_min")
    if t.source == "simulation" and t.site is not None:
        travel = (t.site.travel_min_low + t.site.travel_min_high) / 2     # existing simulator travel ASSUMPTION
    if t.status == "open":
        transition(db, t, "assigned", now, actor=actor, technician_id=best["technician_id"], est_travel_min=travel,
                   note=reason, override=False)
    else:
        # Reassignment: the new technician must accept the job, so the ticket returns to "assigned".
        frm = t.status
        t.technician_id = best["technician_id"]
        t.assigned_at = now
        t.acknowledged_at = None
        t.est_travel_min = travel if travel is not None else t.est_travel_min
        if frm != "assigned":
            t.status = "assigned"
            history(db, t, now, kind="status", from_status=frm, to_status="assigned", actor=actor,
                    note=f"Reassigned while {frm.replace('_', ' ')}; the new technician must accept the job.")
    t.auto_assigned = auto
    t.assignment_note = reason
    t.dispatch_needed = False
    t.sim_work_due_at = None
    history(db, t, now, kind="assignment", actor=actor, override=override,
            note=(f"Reassigned from {prev} to {best['technician_id']}. " if prev and prev != best["technician_id"] else
                  f"Assigned to {best['technician_id']}. ") + reason,
            data={"technician_id": best["technician_id"], "previous": prev, "auto": auto, "workload_before": best["workload"],
                  "capacity": best["capacity"], "distance_km": best["distance_km"], "travel_basis": best["travel_basis"]})


def try_auto_assign(db: Session, t: Ticket, now: datetime, *, allow_emergency: bool = True) -> dict | None:
    """Assign the best eligible technician to an open ticket when automatic assignment applies.
    Returns the chosen candidate, or None (reasons recorded on the ticket)."""
    if t.status != "open" or not auto_assign_enabled(db, t.source):
        return None
    pol = settings_store.assignment_policy(db)
    if pol.get("wait_for_remote_reset") and t.reset_due_at is not None and not t.remote_reset_attempted and not is_safety(t):
        _set_note(db, t, now, "Waiting for the scheduled remote reset before dispatching (existing behaviour).", dispatch=False)
        return None
    db.flush()
    rows = candidates(db, t, now)
    best = next((r for r in rows if r["eligible"]), None)
    if best is not None:
        why = (f"Auto-assigned: {', '.join(best['qualifies'])}; " +
               (f"{best['travel_basis']}, est. {best['travel_min']} min." if best["travel_min"] is not None else best["travel_basis"] + "."))
        _assign(db, t, best, now, actor="auto-assign", auto=True, reason=why)
        return best
    urgent = t.severity == "critical" or t.escalated_at is not None
    if allow_emergency and urgent and pol.get("emergency_override"):
        emer = next((r for r in rows if r["capacity_only_blocker"]), None)
        if emer is not None:
            _assign(db, t, emer, now, actor="auto-assign", auto=True, override=True,
                    reason=f"EMERGENCY OVERRIDE (policy): qualified but above capacity ({emer['workload']}/{emer['capacity']}).")
            return emer
    _set_note(db, t, now, _summarise_blockers(rows), dispatch=urgent)
    return None


def _set_note(db: Session, t: Ticket, now: datetime, note: str, dispatch: bool) -> None:
    if t.assignment_note != note:
        t.assignment_note = note
        history(db, t, now, kind="assignment", actor="auto-assign", note=note, data={"assigned": False})
    if dispatch and not t.dispatch_needed:
        t.dispatch_needed = True
        history(db, t, now, kind="escalation", actor="auto-assign",
                note="Dispatch needed: urgent ticket with no eligible technician.", data={"dispatch_needed": True})


def retry_unassigned(db: Session, source: str, now: datetime) -> int:
    """Retry automatic assignment for open tickets, most urgent first (existing priority ranking or FIFO)."""
    if not auto_assign_enabled(db, source):
        return 0
    open_t = db.scalars(select(Ticket).where(Ticket.source == source, Ticket.status == "open")).all()
    if not open_t:
        return 0
    order = "priority"
    if source == "simulation":
        st = sim_state(db)
        order = ({**_default_params(), **((st.params or {}) if st else {})}).get("auto_dispatch_policy", "priority")
    if order == "fifo":
        open_t.sort(key=lambda t: (t.detected_at, t.id))
    else:
        from .priority_service import rank_open_tickets
        ranks = {r.ticket_ref: i for i, r in enumerate(rank_open_tickets(db, source))}
        open_t.sort(key=lambda t: ranks.get(t.code, 10 ** 6))
    n = 0
    for t in open_t:
        if try_auto_assign(db, t, now) is not None:
            n += 1
    return n


def manual_assign(db: Session, t: Ticket, technician_id: str, now: datetime, *, actor: str, reason: str,
                  confirm_conflicts: bool = False, admin: bool = False) -> dict:
    if t.status in DONE:
        raise AssignmentConflict("Ticket is resolved or closed; reopen it first.", [])
    tech = db.get(Technician, technician_id)
    if tech is not None and tech.org_id != t.org_id:
        tech = None                      # another organisation's technician is never visible
    if tech is None:
        raise AssignmentConflict(f"Unknown technician '{technician_id}'.", [])
    if t.technician_id == technician_id:
        raise AssignmentConflict(f"Ticket is already assigned to {technician_id}.", [])
    if t.status == "on_site":
        raise AssignmentConflict("The technician is on site; record the outcome (resolve or waiting for parts) before reassigning.", [])
    ev = evaluate(db, t, tech, now)
    conflicts = list(ev["blockers"])
    if t.sla_due_at is not None and ev["can_meet_sla"] is False:
        conflicts.append("estimated arrival after the SLA deadline")
    if ev["safety_blocker"] and not admin:
        raise AssignmentConflict("This fault is safety-related: only a safety-certified technician can be assigned "
                                 "(administrative override required).", conflicts, safety=True)
    if conflicts and not confirm_conflicts:
        raise AssignmentConflict("Manual assignment conflicts: " + "; ".join(conflicts) + ". Confirm to override.", conflicts)
    if conflicts and not reason.strip():
        raise AssignmentConflict("Give a reason for overriding the conflicts.", conflicts)
    why = f"Manual assignment by {actor or 'user'}" + (f": {reason.strip()}" if reason.strip() else ".")
    if conflicts:
        why += " OVERRIDE of: " + "; ".join(conflicts) + "."
    _assign(db, t, ev, now, actor=(actor or "user")[:40], auto=False, reason=why, override=bool(conflicts))
    return ev


def unassign(db: Session, t: Ticket, now: datetime, *, actor: str, reason: str) -> None:
    if t.status not in ("assigned", "en_route", "waiting_access"):
        raise AssignmentConflict("Only an assigned, en-route or waiting-for-access ticket can be unassigned.", [])
    if not reason.strip():
        raise AssignmentConflict("Give a reason for unassigning.", [])
    prev, frm = t.technician_id, t.status
    t.technician_id = None
    t.assigned_at = t.acknowledged_at = None
    t.status = "open"
    t.auto_assigned = False
    t.sim_work_due_at = None
    history(db, t, now, kind="status", from_status=frm, to_status="open", actor=actor, note=f"Unassigned from {prev}: {reason}")
    history(db, t, now, kind="assignment", actor=actor, note=f"Unassigned from {prev}: {reason}", data={"previous": prev, "assigned": False})
    retry_unassigned(db, t.source, now)
