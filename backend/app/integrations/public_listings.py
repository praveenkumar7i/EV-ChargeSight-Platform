"""Public charging-station listings: normalise, store, deduplicate, summarise.

A public listing says where a charging station is and what the source claims
about it (operator, connectors, price, hours). It is NEVER live operational
status. Every record keeps its provider, provider id, source URL, licence,
attribution and retrieval time, and the API always reports status as
"STATUS UNKNOWN" unless an authorised live feed provides it (none does here).

Normalised record (dict) produced by every connector:
    provider, provider_id, name, operator_name, lat, lng, address,
    connections [{type, power_kw, quantity, current}], usage_type, usage_cost,
    opening_hours, listed_status, source_url, data_provider, licence,
    attribution, retrieval_method, classification
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import iso, wall_now
from ..config import PROJECT_ROOT
from ..core.areas import AREAS, BENGALURU_BBOX, OTHER, area_name, assign_area, haversine_km, in_bbox
from ..models import PublicStation

SNAPSHOT_PATH = PROJECT_ROOT / "sample_data" / "PUBLIC_ocm_bengaluru_snapshot.json"
STATUS_UNKNOWN = "STATUS UNKNOWN"
LIVE_STATUS_NOTE = ("Public listing only. A station appearing here (or on Google Maps) does not mean it is working, "
                    "available or connected to ChargeSight. No authorised live feed is configured.")
DUP_DISTANCE_M = 60          # same place if this close and names overlap ...
DUP_NAME_DISTANCE_M = 200    # ... or this close with near-identical names
PROVIDER_LABEL = {"open_charge_map": "Open Charge Map", "osm": "OpenStreetMap", "google_places": "Google Places",
                  "csv_import": "Imported file", "imported_inventory": "Imported station inventory"}
FIELDS = ("name", "operator_name", "lat", "lng", "address", "connections", "usage_type", "usage_cost", "opening_hours",
          "listed_status", "source_url", "data_provider", "licence", "attribution", "retrieval_method", "classification",
          "origin")
ORIGIN_LABEL = {"snapshot": "Bundled snapshot (fallback)", "api": "Fetched from the provider API"}


class ListingError(ValueError):
    pass


# ------------------------------------------------------------------ validation
def validate(rec: dict) -> dict:
    """Return a cleaned copy or raise ListingError. Coordinates must be real numbers inside Bengaluru."""
    r = dict(rec)
    for k in ("provider", "provider_id", "name"):
        if not str(r.get(k) or "").strip():
            raise ListingError(f"missing {k}")
    try:
        lat, lng = float(r["lat"]), float(r["lng"])
    except (KeyError, TypeError, ValueError):
        raise ListingError("missing or invalid coordinates")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        raise ListingError("coordinates out of range")
    if not in_bbox(lat, lng):
        raise ListingError("outside the Bengaluru search area")
    r["lat"], r["lng"] = lat, lng
    r["name"] = str(r["name"]).strip()[:200]
    r["provider_id"] = str(r["provider_id"])[:80]
    conns = []
    for c in r.get("connections") or []:
        if not isinstance(c, dict):
            continue
        kw = c.get("power_kw")
        try:
            kw = float(kw) if kw not in (None, "") else None
        except (TypeError, ValueError):
            kw = None
        if kw is not None and not (0 < kw <= 1000):
            kw = None
        q = c.get("quantity")
        try:
            q = int(q) if q not in (None, "") else None
        except (TypeError, ValueError):
            q = None
        conns.append({"type": str(c.get("type") or "unknown")[:80], "power_kw": kw, "quantity": q,
                      "current": c.get("current") or None})
    r["connections"] = conns
    r.setdefault("classification", "PUBLIC LISTING")
    for k in FIELDS:
        r.setdefault(k, None)
    return r


# ------------------------------------------------------------------ dedup helpers
_STOP = {"ev", "charging", "station", "charger", "hub", "the", "bengaluru", "bangalore", "pvt", "ltd", "point", "-", "&"}


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t and t not in _STOP}


def names_match(a: str, b: str, strict: bool = False) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    j = len(ta & tb) / len(ta | tb)
    return j >= (0.6 if strict else 0.3)


def find_duplicate(db: Session, rec: dict) -> PublicStation | None:
    """A record from ANOTHER provider describing the same place."""
    d_lat = 0.003   # ~330 m prefilter
    cands = db.scalars(select(PublicStation).where(
        PublicStation.provider != rec["provider"], PublicStation.duplicate_of.is_(None),
        PublicStation.lat.between(rec["lat"] - d_lat, rec["lat"] + d_lat),
        PublicStation.lng.between(rec["lng"] - d_lat, rec["lng"] + d_lat))).all()
    best, best_d = None, None
    for c in cands:
        dm = haversine_km(rec["lat"], rec["lng"], c.lat, c.lng) * 1000
        if (dm <= DUP_DISTANCE_M and names_match(rec["name"], c.name)) or \
                (dm <= DUP_NAME_DISTANCE_M and names_match(rec["name"], c.name, strict=True)):
            if best_d is None or dm < best_d:
                best, best_d = c, dm
    return best


def upsert(db: Session, records: list[dict], retrieved_at: datetime | None = None) -> dict:
    """Insert or update records. Same provider + id -> update in place.
    A different provider describing the same place is stored but linked as a duplicate."""
    now = retrieved_at or wall_now()
    out = {"received": len(records), "inserted": 0, "updated": 0, "duplicates_linked": 0, "rejected": 0, "errors": []}
    seen: set[tuple[str, str]] = set()
    for raw in records:
        try:
            r = validate(raw)
        except ListingError as e:
            out["rejected"] += 1
            out["errors"].append({"provider_id": str(raw.get("provider_id")), "name": raw.get("name"), "error": str(e)})
            continue
        key = (r["provider"], r["provider_id"])
        if key in seen:
            out["rejected"] += 1
            out["errors"].append({"provider_id": r["provider_id"], "name": r["name"], "error": "repeated in the same batch"})
            continue
        seen.add(key)
        row = db.scalar(select(PublicStation).where(PublicStation.provider == r["provider"],
                                                    PublicStation.provider_id == r["provider_id"]))
        if row is None:
            row = PublicStation(provider=r["provider"], provider_id=r["provider_id"])
            db.add(row)
            out["inserted"] += 1
        else:
            out["updated"] += 1
        for k in FIELDS:
            v = r.get(k)
            if k in ("operator_name", "address", "source_url", "data_provider", "licence", "attribution", "retrieval_method", "origin"):
                v = v or ""
            setattr(row, k, v)
        row.retrieved_at = now
        row.absent_from_latest_refresh = False
        db.flush()
        if row.duplicate_of is None:
            dup = find_duplicate(db, r)
            if dup is not None and dup.id != row.id:
                row.duplicate_of = dup.id
                out["duplicates_linked"] += 1
    db.flush()
    return out


# ------------------------------------------------------------------ bundled snapshot
def snapshot_retrieved_at(path: Path = SNAPSHOT_PATH) -> str | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("retrieved_at")
    except (OSError, ValueError):
        return None


def load_snapshot(db: Session, path: Path = SNAPSHOT_PATH) -> dict:
    """Load the bundled Open Charge Map snapshot (idempotent: same ids update in place)."""
    if not path.exists():
        return {"loaded": 0, "error": f"snapshot file not found: {path.name}"}
    doc = json.loads(path.read_text(encoding="utf-8"))
    when = datetime.fromisoformat(doc["retrieved_at"].replace("Z", "+00:00")).replace(tzinfo=None)
    recs = []
    for s in doc["stations"]:
        r = dict(s)
        r["retrieval_method"] = "Bundled snapshot: " + doc["retrieval_method"]
        r["attribution"] = doc["attribution"]
        r["classification"] = "PUBLIC LISTING"
        r["origin"] = "snapshot"
        recs.append(r)
    # Never overwrite a record that an API refresh has already fetched or updated.
    fresh = {p for (p,) in db.execute(select(PublicStation.provider_id).where(
        PublicStation.provider == "open_charge_map",
        (PublicStation.origin == "api") | (PublicStation.retrieved_at > when)))}
    res = upsert(db, [r for r in recs if r["provider_id"] not in fresh], retrieved_at=when)
    res["kept_newer_live_records"] = len(fresh)
    res["retrieved_at"] = doc["retrieved_at"]
    return res


def mark_absent(db: Session, provider: str, fetched_ids: set[str]) -> int:
    """After a successful full refresh, flag stored records of that provider which the API did not return."""
    n = 0
    for r in db.scalars(select(PublicStation).where(PublicStation.provider == provider)):
        absent = r.provider_id not in fetched_ids
        if absent != bool(r.absent_from_latest_refresh):
            r.absent_from_latest_refresh = absent
        n += 1 if absent else 0
    db.flush()
    return n


# ------------------------------------------------------------------ read model
def power_class(c: dict) -> str:
    kw, cur = c.get("power_kw"), (c.get("current") or "").upper()
    if kw is None:
        return "unknown"
    if "DC" in cur or (not cur and kw > 22):
        return "DC > 60 kW" if kw > 60 else ("DC 31-60 kW" if kw > 30 else "DC <= 30 kW")
    return "AC <= 7.4 kW" if kw <= 7.4 else "AC > 7.4 kW"


def serialize(r: PublicStation, others: list[PublicStation] | None = None) -> dict:
    area, dist = assign_area(r.lat, r.lng)
    conns = r.connections or []
    powers = [c.get("power_kw") for c in conns if c.get("power_kw")]
    return {
        "id": r.id, "provider": r.provider, "provider_label": PROVIDER_LABEL.get(r.provider, r.provider),
        "provider_id": r.provider_id, "name": r.name, "operator": r.operator_name or None,
        "address": r.address or None, "lat": r.lat, "lng": r.lng,
        "area": area, "area_name": area_name(area), "distance_to_area_centre_km": round(dist, 2),
        "connections": conns, "connector_types": sorted({c.get("type") for c in conns if c.get("type")}),
        "max_power_kw": max(powers) if powers else None,
        "has_dc": any("DC" in (c.get("current") or "").upper() for c in conns) if conns else None,
        "usage_type": r.usage_type, "usage_cost": r.usage_cost, "opening_hours": r.opening_hours,
        "listed_status": r.listed_status,
        "status": STATUS_UNKNOWN, "status_note": LIVE_STATUS_NOTE,
        "source_url": r.source_url or None, "data_provider": r.data_provider or None, "licence": r.licence or None,
        "attribution": r.attribution or None, "retrieval_method": r.retrieval_method or None,
        "retrieved_at": iso(r.retrieved_at), "classification": r.classification or "PUBLIC LISTING",
        "origin": r.origin or ("snapshot" if (r.retrieval_method or "").startswith("Bundled snapshot") else "api"),
        "origin_label": ORIGIN_LABEL.get(r.origin or "", "Fetched from the provider API"),
        "absent_from_latest_refresh": bool(r.absent_from_latest_refresh),
        "also_listed_by": [{"provider": o.provider, "provider_label": PROVIDER_LABEL.get(o.provider, o.provider),
                            "provider_id": o.provider_id, "name": o.name, "source_url": o.source_url or None}
                           for o in (others or [])],
    }


def query(db: Session, *, area: str | None = None, operator: str | None = None, provider: str | None = None,
          q: str | None = None) -> list[dict]:
    rows = db.scalars(select(PublicStation).order_by(PublicStation.name)).all()
    dups: dict[int, list[PublicStation]] = {}
    for r in rows:
        if r.duplicate_of:
            dups.setdefault(r.duplicate_of, []).append(r)
    out = []
    for r in rows:
        if r.duplicate_of:
            continue
        d = serialize(r, dups.get(r.id))
        if area and d["area"] != area:
            continue
        if operator and (d["operator"] or "Unknown operator") != operator:
            continue
        if provider and r.provider != provider:
            continue
        if q:
            hay = " ".join(str(x or "") for x in (d["name"], d["operator"], d["address"], d["area_name"])).lower()
            if q.lower() not in hay:
                continue
        out.append(d)
    return out


def inventory(db: Session) -> dict:
    st = query(db)
    by_area = Counter(s["area"] for s in st)
    areas = []
    for a in AREAS:
        n = by_area.get(a.key, 0)
        ops = Counter(s["operator"] or "Unknown operator" for s in st if s["area"] == a.key)
        areas.append({"key": a.key, "name": a.name, "stations": n, "radius_km": a.radius_km,
                      "operators": dict(ops.most_common()),
                      "message": None if n else f"No verified public listing has been retrieved within {a.radius_km:g} km of the {a.name} centre yet. Refresh OpenStreetMap / Open Charge Map, or add a Google Places key, from your own computer."})
    areas.append({"key": OTHER, "name": "Other Bengaluru", "stations": by_area.get(OTHER, 0), "radius_km": None,
                  "operators": dict(Counter(s["operator"] or "Unknown operator" for s in st if s["area"] == OTHER).most_common()),
                  "message": None})
    conn_types: Counter = Counter()
    power: Counter = Counter()
    unknown_conn = 0
    for s in st:
        if not s["connections"]:
            unknown_conn += 1
        for c in s["connections"]:
            qn = c.get("quantity") or 1
            conn_types[c.get("type") or "unknown"] += qn
            power[power_class(c)] += qn
    rows = db.scalars(select(PublicStation)).all()
    prov = {}
    for r in rows:
        p = prov.setdefault(r.provider, {"provider": r.provider, "label": PROVIDER_LABEL.get(r.provider, r.provider),
                                         "records": 0, "linked_duplicates": 0, "oldest_retrieved_at": None,
                                         "newest_retrieved_at": None, "licence": r.licence, "attribution": r.attribution})
        p["records"] += 1
        p["linked_duplicates"] += 1 if r.duplicate_of else 0
        t = iso(r.retrieved_at)
        p["oldest_retrieved_at"] = min(filter(None, [p["oldest_retrieved_at"], t]))
        p["newest_retrieved_at"] = max(filter(None, [p["newest_retrieved_at"], t]))
    return {
        "total_stations": len(st), "label": "PUBLIC LISTING", "status_note": LIVE_STATUS_NOTE,
        "by_area": areas,
        "by_operator": dict(Counter(s["operator"] or "Unknown operator" for s in st).most_common()),
        "connector_types": dict(conn_types.most_common()), "stations_without_connector_data": unknown_conn,
        "power_classes": dict(power.most_common()),
        "providers": list(prov.values()),
        "by_origin": dict(Counter(s["origin"] for s in st)),
        "absent_from_latest_refresh": sum(1 for s in st if s["absent_from_latest_refresh"]),
        "definitions": {
            "counting": "Counts are computed from stored records; records another provider already lists at the same place are merged (shown under 'also listed by').",
            "connectors": "Connector and power figures count connectors as declared by each listing (quantity, or 1 when not stated).",
            "areas": "A station belongs to the nearest focus area within that area's radius (ASSUMPTION); otherwise 'Other Bengaluru'.",
        },
    }


# ------------------------------------------------------------------ locality summaries (Map page selectors)
STALE_AFTER_DAYS = 30   # ASSUMPTION: a listing set older than this is shown as "stale"


def _summary(stations: list[dict]) -> dict:
    ops = Counter(s["operator"] or "Unknown operator" for s in stations)
    conn_types: Counter = Counter()
    power: Counter = Counter()
    dc = 0
    for s in stations:
        for c in s["connections"]:
            qn = c.get("quantity") or 1
            conn_types[c.get("type") or "unknown"] += qn
            pc = power_class(c)
            power[pc] += qn
            dc += qn if pc.startswith("DC") else 0
    newest = max((s["retrieved_at"] for s in stations if s["retrieved_at"]), default=None)
    oldest = min((s["retrieved_at"] for s in stations if s["retrieved_at"]), default=None)
    kws = [s["max_power_kw"] for s in stations if s["max_power_kw"]]
    return {
        "stations": len(stations),
        "operators": dict(ops.most_common()),
        "operator_count": len(ops),
        "connectors_declared": sum(conn_types.values()),
        "dc_connectors": dc,
        "connector_types": dict(conn_types.most_common()),
        "power_classes": dict(power.most_common()),
        "stations_without_connector_data": sum(1 for s in stations if not s["connections"]),
        "max_power_kw": max(kws) if kws else None,
        "by_provider": dict(Counter(s["provider_label"] for s in stations)),
        "by_origin": dict(Counter(s["origin"] for s in stations)),
        "newest_retrieved_at": newest, "oldest_retrieved_at": oldest,
    }


def localities(db: Session) -> dict:
    """Counts per focus area computed from stored records (merged duplicates counted once).

    A zero is only called a true zero when at least one provider has completed a successful
    refresh of the whole Bengaluru bounding box; with only the bundled snapshot stored, a zero
    means "unknown" because the snapshot is not a complete inventory."""
    from ..models import ConnectorHealth

    st = query(db)
    refreshed = []
    for prov in ("open_charge_map", "osm"):
        from .base import get_health
        h = get_health(db, prov)
        has_api = db.scalar(select(func.count(PublicStation.id)).where(PublicStation.provider == prov,
                                                                     PublicStation.origin == "api")) or 0
        if h is not None and h.last_success_at is not None and has_api:
            refreshed.append({"provider": prov, "label": PROVIDER_LABEL.get(prov, prov), "last_success_at": iso(h.last_success_at)})
    now = wall_now()

    def entry(key, name, lat, lng, radius, rows):
        d = {"key": key, "name": name, "lat": lat, "lng": lng, "radius_km": radius, **_summary(rows)}
        age = None
        if d["newest_retrieved_at"]:
            age = (now - datetime.fromisoformat(d["newest_retrieved_at"].rstrip("Z"))).total_seconds() / 86400
        d["data_age_days"] = round(age, 1) if age is not None else None
        d["stale"] = bool(age is not None and age > STALE_AFTER_DAYS)
        if d["stations"]:
            d["coverage_status"] = "listings"
            d["coverage_message"] = None
        elif refreshed:
            d["coverage_status"] = "true_zero"
            d["coverage_message"] = ("No listing in the latest successful refresh of " + ", ".join(
                f"{r['label']} ({r['last_success_at'][:10]})" for r in refreshed) +
                ". This is a true zero for these sources, not proof that no charger exists.")
        else:
            d["coverage_status"] = "unknown"
            d["coverage_message"] = ("Only the bundled Open Charge Map snapshot is stored, and it is not a complete inventory: "
                                     "zero here means unknown. Refresh listings to check the live directory.")
        return d

    areas = [entry(a.key, a.name, a.lat, a.lng, a.radius_km, [s for s in st if s["area"] == a.key]) for a in AREAS]
    other = entry(OTHER, "Other Bengaluru", None, None, None, [s for s in st if s["area"] == OTHER])
    lat_c = (BENGALURU_BBOX[0] + BENGALURU_BBOX[2]) / 2
    lng_c = (BENGALURU_BBOX[1] + BENGALURU_BBOX[3]) / 2
    all_ = entry("all", "All Bengaluru", lat_c, lng_c, None, st)
    return {"label": "PUBLIC LISTING", "status_note": LIVE_STATUS_NOTE, "all": all_, "areas": areas, "other": other,
            "refreshed_sources": refreshed, "stale_after_days": STALE_AFTER_DAYS,
            "definitions": {
                "counting": "Counts are computed from stored records; a place listed by several providers is counted once.",
                "area": "A station belongs to the nearest focus area within that area's radius (ASSUMPTION); otherwise 'Other Bengaluru'.",
                "true_zero": "Zero after a successful full-area refresh of a provider (true zero for that source).",
                "unknown": "Zero with only the bundled snapshot stored (snapshot is not a complete inventory).",
            }}
