"""OpenStreetMap context around a planning candidate (amenities and nearest road class).

One bounded Overpass request per explicit user action (500 m for amenities, 150 m for roads),
never on map movement. Respects OSM_ENABLED and the Overpass usage policy (identifying
User-Agent, small bounded query, no automatic retries). Results carry ODbL attribution and the
retrieval time. OSM coverage varies: a missing amenity in OSM does not prove it does not exist.
"""
from __future__ import annotations

import logging
import time
from collections import Counter

import httpx

from ..clock import iso, wall_now
from ..config import Settings, get_settings
from ..core.areas import haversine_km
from .base import ConnectorError, redact

log = logging.getLogger("chargesight.osm_context")
AMENITY_RADIUS_M = 500
ROAD_RADIUS_M = 150
ATTRIBUTION = "© OpenStreetMap contributors (ODbL 1.0), via the Overpass API"
ROAD_RANK = {"motorway": 5, "trunk": 5, "primary": 4, "secondary": 3, "tertiary": 3, "unclassified": 2,
             "residential": 1, "service": 1, "living_street": 1}
ROAD_CLASS = {5: "arterial", 4: "arterial", 3: "main", 2: "local", 1: "local"}

CATEGORY = {
    "food": {"restaurant", "cafe", "fast_food", "food_court", "bar", "pub", "ice_cream"},
    "parking": {"parking", "parking_space", "parking_entrance"},
    "fuel_or_charging": {"fuel", "charging_station"},
    "healthcare": {"hospital", "clinic", "doctors", "pharmacy", "dentist"},
    "education": {"school", "college", "university", "kindergarten"},
    "finance": {"bank", "atm"},
    "worship": {"place_of_worship"},
}


def build_query(lat: float, lng: float) -> str:
    a, r = AMENITY_RADIUS_M, ROAD_RADIUS_M
    return (f"[out:json][timeout:25];("
            f"node(around:{a},{lat},{lng})[amenity];way(around:{a},{lat},{lng})[amenity];"
            f"node(around:{a},{lat},{lng})[shop];way(around:{a},{lat},{lng})[shop];"
            f"node(around:{a},{lat},{lng})[office];way(around:{a},{lat},{lng})[office];"
            f'way(around:{r},{lat},{lng})[highway~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|service|living_street)$"];'
            f");out center tags 600;")


def categorise(el: dict) -> str | None:
    t = el.get("tags") or {}
    if "highway" in t and not ({"amenity", "shop", "office"} & t.keys()):
        return None
    if t.get("shop"):
        return "retail"
    if t.get("office"):
        return "office"
    am = t.get("amenity")
    if not am:
        return None
    for cat, vals in CATEGORY.items():
        if am in vals:
            return cat
    return "other_amenity"


def summarise(data: dict, lat: float, lng: float) -> dict:
    els = data.get("elements") if isinstance(data, dict) else None
    if not isinstance(els, list):
        raise ConnectorError("Overpass response has no 'elements' list.")
    cats: Counter = Counter()
    examples: dict[str, list[str]] = {}
    roads = []
    seen = set()
    for el in els:
        if not isinstance(el, dict):
            continue
        k = (el.get("type"), el.get("id"))
        if k in seen:
            continue
        seen.add(k)
        t = el.get("tags") or {}
        c = categorise(el)
        if c:
            cats[c] += 1
            if t.get("name") and len(examples.setdefault(c, [])) < 4:
                examples[c].append(t["name"])
        hw = t.get("highway")
        if hw in ROAD_RANK:
            ctr = el.get("center") or {}
            d = haversine_km(lat, lng, ctr["lat"], ctr["lon"]) * 1000 if "lat" in ctr and "lon" in ctr else None
            roads.append({"highway": hw, "name": t.get("name"), "rank": ROAD_RANK[hw], "distance_m": round(d) if d is not None else None})
    roads.sort(key=lambda r: (-r["rank"], r["distance_m"] if r["distance_m"] is not None else 1e9))
    best = roads[0] if roads else None
    return {
        "amenities_within_m": AMENITY_RADIUS_M,
        "amenity_total": sum(cats.values()),
        "amenity_categories": dict(cats.most_common()),
        "examples": examples,
        "roads_within_m": ROAD_RADIUS_M,
        "best_road": ({**best, "class": ROAD_CLASS[best["rank"]]} if best else None),
        "road_count": len(roads),
    }


class OverpassContextClient:
    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None):
        self.s = settings or get_settings()
        self._transport = transport

    def is_configured(self) -> bool:
        return bool(self.s.osm_enabled and self.s.osm_overpass_url)

    def fetch(self, lat: float, lng: float) -> dict:
        if not self.is_configured():
            raise ConnectorError("OpenStreetMap lookups are disabled (OSM_ENABLED=false). Enter the amenity count and road type you observed instead.")
        t0 = time.monotonic()
        try:
            with httpx.Client(timeout=httpx.Timeout(35.0, connect=10.0), transport=self._transport,
                              headers={"User-Agent": "ChargeSight-academic-prototype (BMSCE PME project)"}) as c:
                r = c.post(self.s.osm_overpass_url, data={"data": build_query(lat, lng)})
        except httpx.TimeoutException as ex:
            raise ConnectorError(f"Overpass did not respond in time ({type(ex).__name__}). No context was stored; try later or enter what you observed.")
        except httpx.HTTPError as ex:
            raise ConnectorError(redact(f"Could not reach Overpass ({type(ex).__name__}). No context was stored; enter what you observed instead."))
        log.info("OSM context: HTTP %d, %d bytes, %.1fs", r.status_code, len(r.content), time.monotonic() - t0)
        if r.status_code == 429:
            raise ConnectorError("Overpass rate limit reached (HTTP 429). Wait a few minutes before another lookup.")
        if r.status_code == 504:
            raise ConnectorError("Overpass is busy (HTTP 504). Try again later.")
        if r.status_code >= 400:
            raise ConnectorError(f"Overpass returned HTTP {r.status_code}.")
        try:
            data = r.json()
        except ValueError:
            raise ConnectorError("Overpass response is not valid JSON.")
        out = summarise(data, lat, lng)
        out.update({"source": "OpenStreetMap (Overpass API)", "attribution": ATTRIBUTION, "retrieved_at": iso(wall_now()),
                    "label": "SOURCE DATA", "note": "Counts of mapped OSM features; OSM coverage varies, so absence in OSM does not prove absence on the ground."})
        return out
