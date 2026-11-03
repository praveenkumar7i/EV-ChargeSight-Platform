"""The six Bengaluru focus areas used to group PUBLIC LISTING stations.

Centres are approximate public locality centroids (the same points used for the
demonstration fleet). A station belongs to the nearest area whose radius
contains it; anything else is "Other Bengaluru". Radii are a presentation
choice (ASSUMPTION), not an administrative boundary.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Bengaluru metropolitan bounding box used for public-listing searches: (south, west, north, east)
BENGALURU_BBOX = (12.75, 77.45, 13.20, 77.80)


@dataclass(frozen=True)
class Area:
    key: str
    name: str
    lat: float
    lng: float
    radius_km: float
    search_hint: str        # used to build text queries (Google Places)


AREAS: tuple[Area, ...] = (
    Area("electronic_city", "Electronic City", 12.8452, 77.6602, 5.0, "Electronic City, Bengaluru"),
    Area("hebbal", "Hebbal", 13.0358, 77.5970, 3.5, "Hebbal, Bengaluru"),
    Area("hsr_layout", "HSR Layout", 12.9116, 77.6474, 3.5, "HSR Layout, Bengaluru"),
    Area("koramangala", "Koramangala", 12.9352, 77.6245, 3.0, "Koramangala, Bengaluru"),
    Area("whitefield", "Whitefield", 12.9698, 77.7500, 5.0, "Whitefield, Bengaluru"),
    Area("yelahanka", "Yelahanka", 13.1007, 77.5963, 4.0, "Yelahanka, Bengaluru"),
)
AREA_BY_KEY = {a.key: a for a in AREAS}
OTHER = "other"
OTHER_NAME = "Other Bengaluru"


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def assign_area(lat: float, lng: float) -> tuple[str, float]:
    """Return (area key or 'other', distance in km to that area's centre).
    Among areas whose radius contains the point, the nearest centre wins; if none
    contains it, 'other' and the distance to the nearest centre are returned."""
    dists = [(haversine_km(lat, lng, a.lat, a.lng), a) for a in AREAS]
    inside = [(d, a) for d, a in dists if d <= a.radius_km]
    if inside:
        d, a = min(inside, key=lambda x: x[0])
        return a.key, d
    d, _a = min(dists, key=lambda x: x[0])
    return OTHER, d


def area_name(key: str) -> str:
    return AREA_BY_KEY[key].name if key in AREA_BY_KEY else OTHER_NAME


def in_bbox(lat: float, lng: float, bbox=BENGALURU_BBOX) -> bool:
    s, w, n, e = bbox
    return s <= lat <= n and w <= lng <= e
