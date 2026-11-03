"""Apply normalised external events to the database with provenance.

* Duplicate events (same source + external_event_id) are skipped and counted.
* Unknown chargers are registered automatically under the given source. A
  site is created from site_code/site_name/lat/lng if provided; otherwise the
  charger goes into an "Unassigned (<source>)" site with no map position.
* Invalid status transitions are rejected per event, not for the whole batch.
* Fault and offline events open tickets through the same pipeline as the
  simulator, so dedupe and prioritisation behave identically.
"""
from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import wall_now
from ..models import Charger, Event, IngestionRun, Site
from ..services.events import InvalidTransition, process_fault, process_offline, record_event
from ..services.entitlements import LimitReached
from ..tenancy import nid
from .base import redact
from .schema import ExternalEvent

log = logging.getLogger("chargesight.ingest")


def _site_for(db: Session, ev: ExternalEvent, source: str) -> Site:
    code = nid(db, (ev.site_code or f"UNASSIGNED-{source}").upper()[:20])[:32]
    site = db.scalar(select(Site).where(Site.code == code))
    if site is None:
        from ..services.entitlements import check_new_managed_site
        check_new_managed_site(db, code)                   # plan limit on real sites; raises LimitReached
        has_pos = ev.lat is not None and ev.lng is not None
        site = Site(
            code=code,
            name=ev.site_name or (code if ev.site_code else f"Unassigned ({source})"),
            lat=ev.lat if has_pos else 0.0,
            lng=ev.lng if has_pos else 0.0,
            daily_sessions=0,
            is_demo_location=False,
            source=source,
        )
        db.add(site)
        db.flush()
    return site


def _charger_for(db: Session, ev: ExternalEvent, source: str) -> Charger:
    cid = nid(db, f"{source.upper()}-{ev.external_charger_id}")[:64]
    ch = db.get(Charger, cid)
    if ch is None:
        test = source == "ocpp" and (bool((ev.raw or {}).get("test_traffic")) or str(ev.external_charger_id).upper().startswith("SIMOCPP"))
        if not test:
            # plan limit on managed chargers (real imported / operator API / OCPP chargers); raises LimitReached
            from ..services.entitlements import check_new_managed_charger
            check_new_managed_charger(db)
        site = _site_for(db, ev, source)
        ch = Charger(
            id=cid,
            site_id=site.id,
            connector_type=ev.connector_type or "unknown",
            power_kw=ev.power_kw or 0.0,
            status="available",
            source=source,
            external_id=ev.external_charger_id,
            test_traffic=test,
        )
        db.add(ch)
        db.flush()
    return ch


def ingest(db: Session, events: Iterable[ExternalEvent], *, source: str, connector: str,
           dataset_label: str = "", pre_errors: list[dict] | None = None) -> IngestionRun:
    if source not in ("operator_api", "ocpp", "operator_import"):
        raise ValueError("source must be operator_api, ocpp or operator_import")
    run = IngestionRun(connector=connector, source=source, dataset_label=dataset_label,
                       started_at=wall_now(), errors=list(pre_errors or []))
    run.rejected = len(run.errors)
    run.received = len(run.errors)
    db.add(run)
    db.flush()

    ordered = sorted(events, key=lambda e: (e.timestamp, e.external_event_id))
    for ev in ordered:
        run.received += 1
        # external ids are namespaced per organisation (legacy workspace: unchanged), so two operators'
        # identical event ids can never be mistaken for duplicates of each other
        ev = ev.model_copy(update={"external_event_id": nid(db, ev.external_event_id)[:160]})
        exists = db.scalar(select(Event.id).where(Event.source == source,
                                                  Event.external_event_id == ev.external_event_id))
        if exists is not None:
            run.duplicates += 1
            continue
        sp = db.begin_nested()
        try:
            ch = _charger_for(db, ev, source)
            if ev.event_type == "fault":
                if not ev.fault_category:
                    raise ValueError("fault event requires fault_category")
                process_fault(db, ch, ev.fault_category, ev.timestamp, payload={"raw": ev.raw},
                              source=source, external_event_id=ev.external_event_id)
            elif ev.event_type == "offline":
                process_offline(db, ch, ev.timestamp, source=source, external_event_id=ev.external_event_id)
            else:
                status_after = ev.status
                if ev.event_type == "session_start":
                    status_after = status_after or "charging"
                elif ev.event_type in ("session_stop", "status_available", "fault_resolved"):
                    status_after = status_after or "available"
                if ev.meter_kwh is not None:
                    ch.total_kwh = max(ch.total_kwh, ev.meter_kwh)
                record_event(db, ch, ev.event_type, ev.timestamp, status_after=status_after,
                             payload={"raw": ev.raw, "meter_kwh": ev.meter_kwh}, source=source,
                             external_event_id=ev.external_event_id)
            db.flush()
            sp.commit()
            run.accepted += 1
        except LimitReached as e:
            sp.rollback()
            run.rejected += 1
            run.errors.append({"external_event_id": ev.external_event_id, "error": str(e), "code": "plan_limit"})
        except (InvalidTransition, ValueError) as e:
            sp.rollback()
            run.rejected += 1
            run.errors.append({"external_event_id": ev.external_event_id, "error": redact(str(e))})
        except Exception as e:  # pragma: no cover - defensive
            sp.rollback()
            run.rejected += 1
            run.errors.append({"external_event_id": ev.external_event_id, "error": redact(f"unexpected: {e}")})
            log.exception("ingest failure")
    run.finished_at = wall_now()
    run.ok = run.rejected == 0
    run.errors = run.errors[:200]
    db.flush()
    return run
