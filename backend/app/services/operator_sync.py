"""Operator REST API sync, shared by the manual "Sync events" button and the optional poller.

Deduplication happens in ingest() (source + external_event_id), so overlapping
polls never create duplicate events or tickets. Nothing is ever fabricated:
if the provider returns no events, nothing is written.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import iso, wall_now
from ..config import get_settings
from ..db import begin_immediate
from ..integrations.base import ConnectorError, set_health
from ..integrations.ingest import ingest
from ..integrations.operator_api import OperatorApiConnector
from ..models import Charger, Event, IngestionRun


def sync(db: Session, connector: OperatorApiConnector | None = None, trigger: str = "manual") -> IngestionRun:
    conn = connector or OperatorApiConnector()
    if not conn.is_configured():
        set_health(db, "operator_api", "not_configured")
        raise ConnectorError("Operator API is not configured (OPERATOR_API_BASE_URL / OPERATOR_API_KEY).")
    last = db.scalar(select(IngestionRun).where(IngestionRun.connector == "operator_api", IngestionRun.ok.is_(True))
                     .order_by(IngestionRun.id.desc()).limit(1))
    since = last.started_at if last else None
    db.commit()  # end the read (keeping any caller changes); no transaction stays open across the network call
    try:
        events, errors = conn.fetch_events(since=since)
    except ConnectorError as e:
        begin_immediate(db)
        set_health(db, "operator_api", "error", error=str(e))
        db.commit()
        raise
    begin_immediate(db)  # write lock first: avoids "database is locked" if another writer committed meanwhile
    run = ingest(db, events, source="operator_api", connector="operator_api",
                 dataset_label=f"operator API sync ({trigger})", pre_errors=errors)
    set_health(db, "operator_api", "connected", success=True,
               detail={"last_sync_run": run.id, "received": run.received, "accepted": run.accepted,
                       "duplicates": run.duplicates, "trigger": trigger})
    db.commit()
    return run


def status(db: Session) -> dict:
    """No network calls: configuration, last result and staleness."""
    from ..models import ConnectorHealth

    s = get_settings()
    from ..integrations.base import get_health
    h = get_health(db, "operator_api")
    last_ok = db.scalar(select(IngestionRun).where(IngestionRun.connector == "operator_api", IngestionRun.ok.is_(True))
                        .order_by(IngestionRun.id.desc()).limit(1))
    last_event = db.scalar(select(func.max(Event.ts)).where(Event.source == "operator_api"))
    chargers = db.scalar(select(func.count(Charger.id)).where(Charger.source == "operator_api")) or 0
    stale = None
    if last_ok is not None:
        stale = (wall_now() - last_ok.finished_at).total_seconds() > s.operator_api_stale_after_s if last_ok.finished_at else None
    configured = bool(s.operator_api_base_url and s.operator_api_key)
    if not s.operator_api_base_url:
        state, nxt = "NOT CONFIGURED", "Ask the operator for an API base URL and a read-only key; set OPERATOR_API_BASE_URL and OPERATOR_API_KEY in .env and restart."
    elif not s.operator_api_key:
        state, nxt = "NOT CONFIGURED", "Set OPERATOR_API_KEY in .env (issued by the operator) and restart."
    elif h is not None and h.state == "error":
        state, nxt = "ERROR", "Open Data integrations and press Diagnose to see which step fails."
    elif last_ok is None:
        state, nxt = "NOT CONNECTED", "Press Diagnose, then Sync events."
    elif chargers == 0:
        state, nxt = "NO LIVE DATA", "The operator returned no events yet. Sync again later or check the date range with the operator."
    elif stale:
        state, nxt = "STALE", "No successful sync recently. Press Sync events or enable polling (OPERATOR_API_POLL_SECONDS)."
    else:
        state, nxt = "RECEIVING DATA", ""
    return {"configured": configured, "base_url_set": bool(s.operator_api_base_url), "key_set": bool(s.operator_api_key),
            "state": state, "next_action": nxt, "health": h.state if h else None,
            "last_error": (h.last_error or None) if h else None,
            "last_success_at": iso(last_ok.finished_at) if last_ok else None, "last_event_at": iso(last_event),
            "chargers": chargers, "stale": stale, "stale_after_s": s.operator_api_stale_after_s,
            "poll_seconds": effective_poll_seconds()}


def effective_poll_seconds() -> int:
    v = int(get_settings().operator_api_poll_seconds or 0)
    return 0 if v <= 0 else max(60, v)


def _poll_once() -> None:
    import logging

    from ..db import session_scope

    log = logging.getLogger("chargesight.operator")
    try:
        with session_scope() as db:
            from ..tenancy import LEGACY_ORG_ID, set_org
            set_org(db, LEGACY_ORG_ID)          # the .env connection belongs to the system demo workspace only
            run = sync(db, trigger="poll")
            log.info("Operator API poll: %s received, %s accepted, %s duplicates", run.received, run.accepted, run.duplicates)
    except ConnectorError as e:
        log.warning("Operator API poll failed: %s", e)


async def poll_loop(stop) -> None:
    import asyncio

    from starlette.concurrency import run_in_threadpool

    interval = effective_poll_seconds()
    if not interval:
        return
    while not stop.is_set():
        await run_in_threadpool(_poll_once)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


_ = datetime  # (kept for type hints in callers)
