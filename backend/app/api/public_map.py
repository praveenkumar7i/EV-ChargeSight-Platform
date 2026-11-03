"""Public charging-station listings for the Map page (PUBLIC LISTING data, never live status)."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..clock import iso, wall_now
from ..config import get_settings
from ..core.areas import AREA_BY_KEY, AREAS, BENGALURU_BBOX, OTHER, area_name, assign_area, haversine_km
from ..db import begin_immediate, get_db, is_db_busy
from ..integrations import google_places, public_listings as PL
from ..integrations.base import ConnectorError, set_health
from ..integrations.open_charge_map import OpenChargeMapClient
from ..integrations.osm_overpass import OverpassClient

router = APIRouter(prefix="/api/public-stations")

ATTRIBUTIONS = [
    {"provider": "open_charge_map", "text": "Open Charge Map and contributors (CC BY 4.0)", "url": "https://openchargemap.org"},
    {"provider": "osm", "text": "© OpenStreetMap contributors (ODbL)", "url": "https://www.openstreetmap.org/copyright"},
    {"provider": "google_places", "text": "Google Maps", "url": "https://maps.google.com"},
]


REFRESH_MIN_INTERVAL_S = 600     # do not hit a provider again within 10 minutes unless forced


def _health(db: Session | None, name: str) -> dict:
    if db is None:
        return {}
    from ..models import ConnectorHealth

    from ..integrations.base import get_health
    h = get_health(db, name)
    if h is None:
        return {}
    return {"state": h.state, "last_checked_at": iso(h.last_checked_at), "last_success_at": iso(h.last_success_at),
            "last_error": h.last_error or None, "detail": h.detail or {}}


def providers_status(db: Session | None = None) -> list[dict]:
    s = get_settings()
    out = _providers_static(s)
    for p in out:
        h = _health(db, p["provider"])
        p.update({"health": h.get("state"), "last_success_at": h.get("last_success_at"),
                  "last_checked_at": h.get("last_checked_at"), "last_error": h.get("last_error"),
                  "last_detail": h.get("detail")})
        if p["provider"] == "google_places":
            # never claim Places works unless a real request has succeeded
            p["verified"] = bool(p["configured"] and h.get("state") == "connected" and h.get("last_success_at"))
            p["state_label"] = ("not configured" if not p["configured"] else
                                "verified" if p["verified"] else
                                "key present, not verified" if h.get("state") != "error" else "error")
        else:
            p["verified"] = h.get("state") == "connected"
            p["state_label"] = ("not configured" if not p["configured"] else
                                "last refresh OK" if p["verified"] else
                                "last refresh failed" if h.get("state") == "error" else "configured, not yet refreshed")
    return out


def _providers_static(s) -> list[dict]:
    return [
        {"provider": "open_charge_map", "label": "Open Charge Map", "configured": OpenChargeMapClient().is_configured(),
         "stored": True, "setup": None if s.ocm_api_key else "Set OCM_API_KEY in .env (free key from openchargemap.org) for live refresh. A verified Bengaluru snapshot is bundled."},
        {"provider": "osm", "label": "OpenStreetMap (Overpass)", "configured": OverpassClient().is_configured(),
         "stored": True, "setup": None if s.osm_enabled else "OSM refresh is disabled (OSM_ENABLED=false)."},
        {"provider": "google_places", "label": "Google Places (New)", "configured": bool(s.google_places_api_key),
         "stored": False, "setup": None if s.google_places_api_key else "Set GOOGLE_PLACES_API_KEY (server-side key with 'Places API (New)' enabled). Results are shown live and never stored."},
    ]


@router.get("")
def list_stations(area: str | None = Query(None, max_length=40), operator: str | None = Query(None, max_length=160),
                  provider: str | None = Query(None, pattern="^(open_charge_map|osm|csv_import|imported_inventory)$"),
                  q: str | None = Query(None, max_length=80), db: Session = Depends(get_db)):
    if area and area not in AREA_BY_KEY and area != OTHER:
        raise HTTPException(422, f"Unknown area '{area}'.")
    stations = PL.query(db, area=area, operator=operator, provider=provider, q=q)
    all_st = PL.query(db) if (area or operator or provider or q) else stations
    return {
        "label": "PUBLIC LISTING — not live status",
        "status_note": PL.LIVE_STATUS_NOTE,
        "configured": OpenChargeMapClient().is_configured(),
        "attribution": "; ".join(a["text"] for a in ATTRIBUTIONS),
        "attributions": ATTRIBUTIONS,
        "providers": providers_status(db),
        "areas": [{"key": a.key, "name": a.name, "lat": a.lat, "lng": a.lng, "radius_km": a.radius_km} for a in AREAS],
        "operators": sorted({s["operator"] or "Unknown operator" for s in all_st}),
        "bbox": BENGALURU_BBOX,
        "count": len(stations), "total": len(all_st),
        "stations": stations,
    }


@router.get("/localities")
def localities(db: Session = Depends(get_db)):
    """Per-locality counts, operator and connector breakdowns and freshness, from stored records."""
    return PL.localities(db)


@router.get("/inventory")
def inventory(db: Session = Depends(get_db)):
    return {**PL.inventory(db), "providers_status": providers_status(db), "attributions": ATTRIBUTIONS}


class RefreshIn(BaseModel):
    providers: list[str] | None = Field(None, max_length=3)
    force: bool = False


log = logging.getLogger("chargesight.public_refresh")


class RefreshGuard:
    """One in-flight refresh per provider (in this server process).

    A provider is marked busy only while ITS fetch + write runs, so a slow or unreachable
    provider (e.g. OpenStreetMap timing out) never blocks a refresh of another provider,
    while two overlapping refreshes of the same provider are still refused. `begin` never
    blocks; `end` is always called from a `finally`, so an exception cannot leave a provider
    marked busy. Process restart starts empty (the state lives only in memory)."""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._active: dict[str, tuple[float, str]] = {}   # provider -> (monotonic start, ISO start)

    def begin(self, provider: str) -> bool:
        with self._mu:
            if provider in self._active:
                return False
            self._active[provider] = (time.monotonic(), iso(wall_now()))
            return True

    def end(self, provider: str) -> None:
        with self._mu:
            self._active.pop(provider, None)

    def active(self) -> dict[str, dict]:
        with self._mu:
            now = time.monotonic()
            return {p: {"started_at": started_iso, "running_for_s": round(now - t0, 1)}
                    for p, (t0, started_iso) in self._active.items()}


REFRESH_GUARD = RefreshGuard()
PROVIDER_NAME = {"osm": "OpenStreetMap", "open_charge_map": "Open Charge Map"}


def _busy_message(p: str, info: dict | None) -> str:
    since = f" (started {info['running_for_s']:.0f} s ago)" if info else ""
    name = PROVIDER_NAME.get(p, p)
    article = "An" if name[:1].upper() in "AEIOU" else "A"
    return (f"{article} {name} refresh is already running{since}. "
            "Wait for it to finish; stored records are unchanged.")


def _write_refresh(db: Session, p: str, rows: list[dict]) -> dict:
    """Store fetched rows and the connector health in one short write transaction.
    BEGIN IMMEDIATE takes SQLite's write lock up front, so a concurrent writer (simulator,
    OCPP, operator sync) can only make us wait, never fail with a busy snapshot.
    Existing records are only updated or flagged, never deleted."""
    last: Exception | None = None
    for attempt in range(3):
        try:
            begin_immediate(db)
            res = PL.upsert(db, rows, retrieved_at=wall_now())
            absent = PL.mark_absent(db, p, {str(r["provider_id"]) for r in rows}) if rows else 0
            detail = {"received": res["received"], "inserted": res["inserted"], "updated": res["updated"],
                      "rejected": res["rejected"], "duplicates_linked": res["duplicates_linked"],
                      "absent_from_refresh": absent}
            set_health(db, p, "connected", success=True, detail=detail)
            db.commit()
            return {**detail, "errors": res["errors"][:20]}
        except OperationalError as e:
            db.rollback()
            if not is_db_busy(e):
                raise
            last = e
            log.warning("refresh %s: database busy on write attempt %d; retrying", p, attempt + 1)
            time.sleep(0.5 * (attempt + 1))
    raise ConnectorError("The local database stayed busy (another writer held it); stored records are unchanged. Try again in a moment.") from last


def _record_error(db: Session, p: str, message: str) -> None:
    try:
        begin_immediate(db)
        set_health(db, p, "error", error=message)
        db.commit()
    except Exception:  # health is best-effort; never turn a handled error into a 500
        db.rollback()
        log.exception("refresh %s: could not record connector health", p)


@router.post("/refresh")
def refresh(body: RefreshIn | None = None, db: Session = Depends(get_db)):
    """Refresh stored listings from each configured provider. One failing provider never blocks
    the others, and a failed refresh never deletes or replaces stored records."""
    wanted = (body.providers if body and body.providers else ["osm", "open_charge_map"])
    bad = set(wanted) - {"osm", "open_charge_map"}
    if bad:
        raise HTTPException(422, f"Unknown or non-storable provider(s): {', '.join(sorted(bad))}. Google Places results are live-only.")
    clients = {"osm": OverpassClient(), "open_charge_map": OpenChargeMapClient()}
    configured = [p for p in wanted if clients[p].is_configured()]
    if not configured:
        begin_immediate(db)
        for p in wanted:
            set_health(db, p, "not_configured")
        db.commit()
        raise HTTPException(400, "No public-listing provider is configured for refresh. Open Charge Map needs OCM_API_KEY; "
                                 "OpenStreetMap needs OSM_ENABLED=true. The bundled Open Charge Map snapshot is still shown.")
    results = {}
    force = bool(body and body.force)
    busy = []
    for p in wanted:
        c = clients[p]
        if not c.is_configured():
            results[p] = {"ok": False, "state": "not_configured", "message": next(x["setup"] for x in providers_status() if x["provider"] == p)}
            continue
        if not REFRESH_GUARD.begin(p):
            info = REFRESH_GUARD.active().get(p)
            log.info("refresh %s refused: already running %s", p, info)
            results[p] = {"ok": False, "state": "busy", "message": _busy_message(p, info), "running": info}
            busy.append(p)
            continue
        try:
            results[p] = _refresh_one(db, p, c, force)
        finally:
            REFRESH_GUARD.end(p)
    if busy and len(busy) == len(configured):
        # Nothing was refreshed because every requested provider is already being refreshed.
        raise HTTPException(409, " ".join(results[p]["message"] for p in busy))
    return {"retrieved_at": iso(wall_now()), "results": results}


@router.get("/refresh/status")
def refresh_status():
    """Which providers are being refreshed right now in this server process (no DB, no network)."""
    act = REFRESH_GUARD.active()
    return {"running": bool(act), "active": act}


def _refresh_one(db: Session, p: str, c, force: bool) -> dict:
    h = _health(db, p)
    db.rollback()  # end the read transaction before any network I/O
    if not force and h.get("state") == "connected" and h.get("last_success_at"):
        age = (wall_now() - datetime.fromisoformat(h["last_success_at"].rstrip("Z"))).total_seconds()
        if age < REFRESH_MIN_INTERVAL_S:
            return {"ok": True, "cached": True, "state": "cached",
                    "message": f"Refreshed {int(age // 60)} min ago; showing stored records (refresh again after 10 minutes, or force).",
                    "last_success_at": h["last_success_at"], **(h.get("detail") or {})}
    t0 = time.monotonic()
    try:
        rows = c.fetch()
        stored = _write_refresh(db, p, rows)
        stored["skipped"] = getattr(c, "last_skipped", 0)
        log.info("refresh %s ok: received=%s inserted=%s updated=%s rejected=%s in %.1fs", p,
                 stored["received"], stored["inserted"], stored["updated"], stored["rejected"], time.monotonic() - t0)
        return {"ok": True, **stored,
                "message": ("Provider returned zero records for the Bengaluru area; stored records are kept." if not rows else None)}
    except ConnectorError as e:
        db.rollback()
        log.warning("refresh %s failed after %.1fs: %s", p, time.monotonic() - t0, e)
        _record_error(db, p, str(e))
        return {"ok": False, "state": "error", "message": str(e)}
    except Exception as e:  # unexpected: log the traceback (never contains the key), return a safe message
        db.rollback()
        log.exception("refresh %s: unexpected error", p)
        msg = f"Refresh failed unexpectedly ({type(e).__name__}); stored records are unchanged. Details are in the server log."
        _record_error(db, p, msg)
        return {"ok": False, "state": "error", "message": msg}


@router.post("/reload-snapshot")
def reload_snapshot(db: Session = Depends(get_db)):
    res = PL.load_snapshot(db)
    db.commit()
    return res


@router.get("/google")
def google_live(areas: str = Query("", max_length=200), confirm_billing: bool = False, db: Session = Depends(get_db)):
    """Live Google Places lookup for the chosen areas. Not stored; matched against stored listings for display only."""
    client = google_places.GooglePlacesClient()
    if not client.is_configured():
        begin_immediate(db)
        set_health(db, "google_places", "not_configured")
        db.commit()
        raise HTTPException(400, "Google Places is not configured. Set GOOGLE_PLACES_API_KEY in .env (server-side key with 'Places API (New)' enabled) and restart.")
    if not confirm_billing:
        raise HTTPException(400, "Google Places requests are billed by Google. Repeat with confirm_billing=true to run this lookup once.")
    keys = [k for k in areas.split(",") if k] or [a.key for a in AREAS]
    out = google_places.search(keys, client)
    stored = PL.query(db)
    for r in out["results"]:
        m = None
        for s in stored:
            dm = haversine_km(r["lat"], r["lng"], s["lat"], s["lng"]) * 1000
            if dm <= PL.DUP_DISTANCE_M or (dm <= PL.DUP_NAME_DISTANCE_M and PL.names_match(r["name"], s["name"])):
                m = {"id": s["id"], "name": s["name"], "provider_label": s["provider_label"], "distance_m": round(dm)}
                break
        r["matches_stored"] = m
        r["status"] = PL.STATUS_UNKNOWN
        a, _d = assign_area(r["lat"], r["lng"])
        r["area"], r["area_name"] = a, area_name(a)
    db.rollback()  # end the read; record health in a short write transaction
    try:
        begin_immediate(db)
        if out["errors"] and not out["results"]:
            set_health(db, "google_places", "error", error=out["errors"][0]["error"])
        else:
            set_health(db, "google_places", "connected", success=True,
                       detail={"results": len(out["results"]), "areas": keys, "verified_by": "Places API (New) Text Search returned HTTP 200"})
        db.commit()
    except OperationalError:
        db.rollback()
        log.exception("google places: could not record connector health")
    return {"label": "PUBLIC LISTING (Google Places, live lookup — not stored)", "attribution": "Google Maps",
            "status_note": PL.LIVE_STATUS_NOTE, "retrieved_at": iso(wall_now()), **out}
