"""Check ChargeSight's external connections from YOUR computer, using the keys in .env.

Run from the project root with the virtual environment active:
    python scripts\\verify_integrations.py                 # Open Charge Map + operator API (if configured)
    python scripts\\verify_integrations.py --osm           # also query OpenStreetMap Overpass once
    python scripts\\verify_integrations.py --places        # also ONE paid Google Places request (Koramangala)

Keys are never printed. Nothing is written to the database; use the app's
"Refresh listings" button to store results. Exit code is 0 only if every
requested check succeeded.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import get_settings  # noqa: E402
from app.core.areas import AREA_BY_KEY, assign_area  # noqa: E402
from app.integrations.base import ConnectorError  # noqa: E402


def check_ocm(s) -> dict:
    from app.integrations.open_charge_map import OpenChargeMapClient

    c = OpenChargeMapClient(s)
    if not c.is_configured():
        return {"check": "open_charge_map", "ok": False, "state": "not_configured", "message": "OCM_API_KEY is empty in .env"}
    try:
        rows = c.fetch()
    except ConnectorError as e:
        return {"check": "open_charge_map", "ok": False, "state": "error", "message": str(e)}
    areas = {}
    for r in rows:
        a, _ = assign_area(r["lat"], r["lng"])
        areas[a] = areas.get(a, 0) + 1
    return {"check": "open_charge_map", "ok": True, "records": len(rows), "by_area": areas,
            "examples": [f"{r['name']} ({r['lat']:.5f}, {r['lng']:.5f})" for r in rows[:5]],
            "key_length": len(s.ocm_api_key)}


def check_osm(s) -> dict:
    from app.integrations.osm_overpass import OverpassClient

    try:
        rows = OverpassClient(s, max_retries=1).fetch()
    except ConnectorError as e:
        return {"check": "osm_overpass", "ok": False, "state": "error", "message": str(e)}
    return {"check": "osm_overpass", "ok": True, "records": len(rows), "examples": [r["name"] for r in rows[:5]]}


def check_places(s) -> dict:
    from app.integrations.google_places import GooglePlacesClient

    c = GooglePlacesClient(s, max_pages=1)
    if not c.is_configured():
        return {"check": "google_places", "ok": False, "state": "not_configured",
                "message": "GOOGLE_PLACES_API_KEY is empty (the browser key GOOGLE_MAPS_BROWSER_KEY is NOT used for Places)."}
    try:
        rows = c.search_area(AREA_BY_KEY["koramangala"])
    except ConnectorError as e:
        return {"check": "google_places", "ok": False, "state": "error", "message": str(e)}
    return {"check": "google_places", "ok": True, "results": len(rows), "examples": [r["name"] for r in rows[:5]],
            "note": "One billed Text Search request (1 page). Results were not stored."}


def check_operator(s) -> dict:
    from app.integrations.operator_api import OperatorApiConnector

    c = OperatorApiConnector(s)
    d = c.diagnose()
    return {"check": "operator_api", **d}


def check_browser_key(s) -> dict:
    k = s.google_maps_browser_key
    return {"check": "google_maps_browser_key", "ok": bool(k), "present": bool(k), "key_length": len(k),
            "note": "The browser key can only be verified in the browser (open the Map page). Its HTTP-referrer "
                    "restriction must allow http://127.0.0.1:8000/* and http://localhost:8000/*."}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm", action="store_true")
    ap.add_argument("--places", action="store_true", help="make ONE billed Google Places request")
    a = ap.parse_args()
    s = get_settings()
    results = [check_browser_key(s), check_ocm(s), check_operator(s)]
    if a.osm:
        results.append(check_osm(s))
    if a.places:
        results.append(check_places(s))
    text = json.dumps(results, indent=2, ensure_ascii=False)
    for secret in s.secret_values() + ([s.google_maps_browser_key] if s.google_maps_browser_key else []):
        text = text.replace(secret, "***")
    print(text)
    required = [r for r in results if r["check"] in ("open_charge_map", "osm_overpass", "google_places")]
    return 0 if all(r.get("ok") for r in required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
