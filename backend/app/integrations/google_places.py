"""Google Places API (New) Text Search for EV charging stations: LIVE LOOKUP ONLY.

Google's Places policies say Places content must not be pre-fetched, cached or
stored (place IDs are the exception). So ChargeSight never writes these results
to the database: each request goes to Google, the results are returned to the
browser for display on the Google map with "Google Maps" attribution, and are
discarded. Nothing here is live charger status; Google's evChargeOptions only
describes connectors.

Requires GOOGLE_PLACES_API_KEY (server-side key with "Places API (New)"
enabled). Requests are billed by Google: the evChargeOptions field triggers the
Text Search Enterprise + Atmosphere SKU. Up to 3 pages x 20 results per area.
"""
from __future__ import annotations

import httpx

from ..config import Settings, get_settings
from ..core.areas import AREA_BY_KEY, BENGALURU_BBOX, Area
from .base import ConnectorError, redact

ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.location", "places.googleMapsUri",
    "places.evChargeOptions", "places.regularOpeningHours.weekdayDescriptions", "nextPageToken",
])
GOOGLE_ATTRIBUTION = "Google Maps"
CONNECTOR_LABEL = {
    "EV_CONNECTOR_TYPE_CCS_COMBO_2": "CCS (Type 2)", "EV_CONNECTOR_TYPE_CCS_COMBO_1": "CCS (Type 1)",
    "EV_CONNECTOR_TYPE_TYPE_2": "Type 2", "EV_CONNECTOR_TYPE_CHADEMO": "CHAdeMO", "EV_CONNECTOR_TYPE_J1772": "Type 1 (J1772)",
    "EV_CONNECTOR_TYPE_GB_T": "GB-T", "EV_CONNECTOR_TYPE_TESLA": "Tesla", "EV_CONNECTOR_TYPE_OTHER": "Other",
    "EV_CONNECTOR_TYPE_UNSPECIFIED_WALL_OUTLET": "Wall outlet", "EV_CONNECTOR_TYPE_NACS": "NACS",
}
DC_TYPES = {"EV_CONNECTOR_TYPE_CCS_COMBO_2", "EV_CONNECTOR_TYPE_CCS_COMBO_1", "EV_CONNECTOR_TYPE_CHADEMO",
            "EV_CONNECTOR_TYPE_TESLA", "EV_CONNECTOR_TYPE_NACS"}


class GooglePlacesClient:
    name = "google_places"

    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None,
                 max_pages: int = 3):
        self.s = settings or get_settings()
        self._transport = transport
        self.max_pages = max_pages

    def is_configured(self) -> bool:
        return bool(self.s.google_places_api_key)

    def search_area(self, area: Area) -> list[dict]:
        if not self.is_configured():
            raise ConnectorError("Google Places is not configured (GOOGLE_PLACES_API_KEY).")
        s, w, n, e = BENGALURU_BBOX
        body = {"textQuery": f"EV charging station in {area.search_hint}",
                "includedType": "electric_vehicle_charging_station", "strictTypeFiltering": True, "pageSize": 20,
                "locationBias": {"circle": {"center": {"latitude": area.lat, "longitude": area.lng},
                                            "radius": area.radius_km * 1000}}}
        out, token = [], None
        headers = {"X-Goog-Api-Key": self.s.google_places_api_key, "X-Goog-FieldMask": FIELD_MASK,
                   "Content-Type": "application/json"}
        with httpx.Client(timeout=20.0, transport=self._transport, headers=headers) as c:
            for _ in range(self.max_pages):
                req = dict(body, **({"pageToken": token} if token else {}))
                try:
                    r = c.post(ENDPOINT, json=req)
                except httpx.HTTPError as ex:
                    raise ConnectorError(redact(f"Google Places request failed: {type(ex).__name__}"))
                if r.status_code == 429:
                    raise ConnectorError("Google Places quota or rate limit reached (HTTP 429).")
                if r.status_code in (400, 401, 403):
                    msg = ""
                    try:
                        msg = (r.json().get("error") or {}).get("message", "")
                    except ValueError:
                        pass
                    raise ConnectorError(redact(f"Google Places rejected the request (HTTP {r.status_code}). {msg} "
                                                "Check that 'Places API (New)' is enabled and the key allows it."))
                if r.status_code >= 400:
                    raise ConnectorError(f"Google Places returned HTTP {r.status_code}.")
                try:
                    data = r.json()
                except ValueError:
                    raise ConnectorError("Google Places response is not valid JSON.")
                for p in data.get("places") or []:
                    x = self._parse(p)
                    if x and s <= x["lat"] <= n and w <= x["lng"] <= e:
                        out.append(x)
                token = data.get("nextPageToken")
                if not token:
                    break
        return out

    @staticmethod
    def _parse(p: dict) -> dict | None:
        loc = p.get("location") or {}
        if loc.get("latitude") is None or loc.get("longitude") is None or not p.get("id"):
            return None
        ev = p.get("evChargeOptions") or {}
        conns = [{"type": CONNECTOR_LABEL.get(a.get("type"), a.get("type") or "unknown"),
                  "power_kw": a.get("maxChargeRateKw"), "quantity": a.get("count"),
                  "current": "DC" if a.get("type") in DC_TYPES else ("AC" if a.get("type") else None)}
                 for a in ev.get("connectorAggregation") or []]
        hours = (p.get("regularOpeningHours") or {}).get("weekdayDescriptions")
        return {
            "provider": "google_places", "provider_id": p["id"],
            "name": (p.get("displayName") or {}).get("text") or "Unnamed place",
            "operator_name": "",             # Places does not return an operator field
            "lat": float(loc["latitude"]), "lng": float(loc["longitude"]),
            "address": p.get("formattedAddress") or "",
            "connections": conns, "usage_type": None, "usage_cost": None,
            "opening_hours": "; ".join(hours) if hours else None, "listed_status": None,
            "source_url": p.get("googleMapsUri") or f"https://www.google.com/maps/place/?q=place_id:{p['id']}",
            "data_provider": "Google Places API", "licence": "Google Maps Platform Terms of Service",
            "attribution": GOOGLE_ATTRIBUTION,
            "retrieval_method": "Google Places API (New) Text Search, live lookup (not stored)",
            "classification": "PUBLIC LISTING",
        }


def search(area_keys: list[str], client: GooglePlacesClient | None = None) -> dict:
    client = client or GooglePlacesClient()
    results, errors = [], []
    for k in area_keys:
        a = AREA_BY_KEY.get(k)
        if a is None:
            errors.append({"area": k, "error": "unknown area"})
            continue
        try:
            for r in client.search_area(a):
                r["queried_area"] = k
                results.append(r)
        except ConnectorError as e:
            errors.append({"area": k, "error": str(e)})
            if "not configured" in str(e) or "rejected" in str(e):
                break
    seen, uniq = set(), []
    for r in results:
        if r["provider_id"] in seen:
            continue
        seen.add(r["provider_id"])
        uniq.append(r)
    return {"results": uniq, "errors": errors}
