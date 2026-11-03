"""Event recording and processing.

`record_event` is the single entry point that changes a charger's status. It
validates the status transition, stores the event with its provenance and
updates the charger. `process_fault` / `process_offline` turn qualifying
events into maintenance tickets (deduplicated per charger + fault category).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.catalog import FAULT_TYPES, VALID_TRANSITIONS
from ..models import Charger, Event, SimState
from ..tenancy import sim_state

OCPP_ACTION = {
    "boot": "BootNotification",
    "heartbeat": "Heartbeat",
    "status_available": "StatusNotification",
    "session_start": "StartTransaction",
    "session_stop": "StopTransaction",
    "meter_values": "MeterValues",
    "fault": "StatusNotification",
    "reset_attempt": "Reset",
    "reconnect": "BootNotification",
    "fault_resolved": "StatusNotification",
    "status_change": "StatusNotification",
}

EVENT_TYPES = (
    "boot", "heartbeat", "status_available", "session_start", "session_stop", "meter_values",
    "fault", "offline", "reconnect", "reset_attempt", "reset_result", "fault_resolved", "status_change",
)


class InvalidTransition(ValueError):
    pass


def _next_seq(db: Session, ts: datetime) -> int:
    """Per-timestamp ordinal. Cached on the session so a simulator step does not
    issue (and autoflush for) one query per event."""
    cache = db.info.setdefault("event_seq", {})
    if ts in cache:
        cache[ts] += 1
        return cache[ts]
    if len(cache) > 64:
        cache.clear()
    with db.no_autoflush:
        pending = max((e.seq for e in db.new if isinstance(e, Event) and e.ts == ts), default=0)
        cur = db.scalar(select(func.max(Event.seq)).where(Event.ts == ts)) or 0
    cache[ts] = max(cur, pending) + 1
    return cache[ts]


def record_event(
    db: Session,
    charger: Charger,
    type: str,
    ts: datetime,
    *,
    status_after: str | None = None,
    payload: dict | None = None,
    source: str | None = None,
    external_event_id: str | None = None,
    seq: int | None = None,
) -> Event:
    if type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type '{type}'.")
    source = source or charger.source
    if status_after is not None and status_after != charger.status:
        allowed = VALID_TRANSITIONS.get(charger.status, set())
        if status_after not in allowed:
            raise InvalidTransition(
                f"{charger.id}: transition {charger.status} -> {status_after} is not allowed."
            )
    ev = Event(
        charger_id=charger.id,
        ts=ts,
        seq=seq if seq is not None else _next_seq(db, ts),
        type=type,
        ocpp_action=OCPP_ACTION.get(type),
        status_after=status_after,
        payload=payload or {},
        source=source,
        external_event_id=external_event_id,
    )
    db.add(ev)
    if status_after is not None:
        charger.status = status_after
    charger.last_event_at = ts
    if type in ("heartbeat", "boot", "reconnect"):
        charger.last_heartbeat_at = ts
    return ev


def process_fault(db: Session, charger: Charger, category: str, ts: datetime, *,
                  payload: dict | None = None, source: str | None = None,
                  external_event_id: str | None = None):
    """Record a fault event, move the charger to Faulted and open a ticket."""
    from .tickets import create_ticket

    ft = FAULT_TYPES.get(category)
    payload = dict(payload or {})
    payload.setdefault("category", category)
    if ft and ft.ocpp_error_code:
        payload.setdefault("errorCode", ft.ocpp_error_code)
    status_after = "faulted" if charger.status != "offline" else None
    ev = record_event(db, charger, "fault", ts, status_after=status_after, payload=payload,
                      source=source, external_event_id=external_event_id)
    ticket = create_ticket(db, charger, category, ts, source=source or charger.source)
    return ev, ticket


def process_offline(db: Session, charger: Charger, ts: datetime, *, source: str | None = None,
                    external_event_id: str | None = None):
    """Mark a silent charger offline and open a communication-loss ticket."""
    from .tickets import create_ticket

    last = charger.last_heartbeat_at
    ev = record_event(db, charger, "offline", ts, status_after="offline",
                      payload={"last_heartbeat_at": last.isoformat() + "Z" if last else None},
                      source=source, external_event_id=external_event_id)
    ticket = create_ticket(db, charger, "comm_loss", ts, source=source or charger.source)
    return ev, ticket


def sim_now(db: Session) -> datetime:
    from ..clock import SIM_EPOCH

    state = sim_state(db)
    return state.sim_time if state else SIM_EPOCH


def now_for_source(db: Session, source: str) -> datetime:
    from ..clock import wall_now

    return sim_now(db) if source == "simulation" else wall_now()
