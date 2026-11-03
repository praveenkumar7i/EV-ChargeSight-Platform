"""OpenStreetMap charging stations via the Overpass API (no API key).

Data: nodes and ways tagged amenity=charging_station inside the Bengaluru box.
Licence: Open Database License (ODbL) 1.0. Attribution "© OpenStreetMap
contributors" is stored with every record and shown in the UI and exports.

Overpass is a shared, volunteer-run service: ChargeSight queries it only when
you press Refresh, sends one request with a descriptive User-Agent, honours
HTTP 429 / 504 with a short back-off, and never polls in the background.

Tags read (when present): name, brand, operator, network, addr:*, socket:*
(+ socket:*:output), capacity, opening_hours, fee, charge, access.
Tags that are absent are stored as unknown, never guessed.
"""
from __future__ import annotations

import re
import time

import httpx

from ..config import Settings, get_settings
from ..core.areas import BENGALURU_BBOX
from .base import ConnectorError, redact

OSM_ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"
SOCKET_LABEL = {
    "type2": "Type 2", "type2_cable": "Type 2 (Tethered Connector)", "type2_combo": "CCS (Type 2)",
    "chademo": "CHAdeMO", "gb_dc": "GB-T DC", "gb_ac": "GB-T AC", "type1": "Type 1", "type1_combo": "CCS (Type 1)",
    "bharat_ac001": "Bharat AC-001", "bharat_dc001": "Bharat DC-001", "schuko": "Schuko",
    "bs1363": "BS1363 3-pin", "cee_blue": "IEC 60309 (blue)", "cee_red_16a": "IEC 60309 (red 16 A)",
    "cee_red_32a": "IEC 60309 (red 32 A)", "nema_5_15": "NEMA 5-15", "tesla_supercharger": "Tesla Supercharger",
}
DC_SOCKETS = {"type2_combo", "chademo", "gb_dc", "type1_combo", "bharat_dc001", "tesla_supercharger"}


def build_query(bbox=BENGALURU_BBOX, timeout_s: int = 60) -> str:
    s, w, n, e = bbox
    return (f"[out:json][timeout:{timeout_s}];"
            f"(node[\"amenity\"=\"charging_station\"]({s},{w},{n},{e});"
            f"way[\"amenity\"=\"charging_station\"]({s},{w},{n},{e}););"
            "out center tags;")


def _kw(v: str | None) -> float | None:
    if not v:
        return None
    m = re.search(r"([\d.]+)\s*(kw|w)?", str(v).lower())
    if not m:
        return None
    x = float(m.group(1))
    if m.group(2) == "w" and x > 1000:      # "22000 W"
        x /= 1000
    return x if 0 < x <= 1000 else None


class OverpassClient:
    name = "osm"

    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None,
                 max_retries: int = 2, backoff_s: float = 2.0):
        self.s = settings or get_settings()
        self._transport = transport
        self.max_retries = max_retries
        self.backoff_s = backoff_s

    def is_configured(self) -> bool:
        return bool(self.s.osm_enabled and self.s.osm_overpass_url)

    def fetch(self, bbox=BENGALURU_BBOX) -> list[dict]:
        if not self.is_configured():
            raise ConnectorError("OpenStreetMap refresh is disabled (OSM_ENABLED=false).")
        q = build_query(bbox)
        last = None
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=httpx.Timeout(75.0, connect=15.0), transport=self._transport,
                                  headers={"User-Agent": "ChargeSight-academic-prototype (BMSCE PME project)"}) as c:
                    r = c.post(self.s.osm_overpass_url, data={"data": q})
            except httpx.HTTPError as ex:
                last = ConnectorError(redact(f"Overpass request failed: {type(ex).__name__}"))
                if isinstance(ex, (httpx.ConnectError, httpx.ConnectTimeout)) and attempt >= 1:
                    # The server could not be reached twice in a row (blocked network, DNS, firewall):
                    # a third try only holds the refresh open for another connect timeout.
                    break
            else:
                if r.status_code in (429, 504) and attempt < self.max_retries:
                    last = ConnectorError(f"Overpass busy (HTTP {r.status_code}).")
                elif r.status_code == 429:
                    raise ConnectorError("Overpass rate limit reached (HTTP 429). Wait a few minutes and refresh again.")
                elif r.status_code >= 400:
                    raise ConnectorError(f"Overpass returned HTTP {r.status_code}.")
                else:
                    try:
                        data = r.json()
                    except ValueError:
                        raise ConnectorError("Overpass response is not valid JSON.")
                    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
                        raise ConnectorError("Overpass response has no 'elements' list.")
                    return [x for x in (self._parse(el) for el in data["elements"]) if x]
            if attempt < self.max_retries and self.backoff_s:
                time.sleep(self.backoff_s * (attempt + 1))
        raise last or ConnectorError("Overpass request failed.")

    @staticmethod
    def _parse(el: dict) -> dict | None:
        if not isinstance(el, dict):
            return None
        tags = el.get("tags") or {}
        if el.get("type") == "node":
            lat, lng = el.get("lat"), el.get("lon")
        else:
            c = el.get("center") or {}
            lat, lng = c.get("lat"), c.get("lon")
        if lat is None or lng is None:
            return None
        conns = []
        for k, v in tags.items():
            m = re.fullmatch(r"socket:([a-z0-9_]+)", k)
            if not m:
                continue
            sock = m.group(1)
            try:
                qty = int(str(v).split(";")[0])
            except ValueError:
                qty = None if str(v).lower() in ("yes", "") else None
            conns.append({"type": SOCKET_LABEL.get(sock, sock.replace("_", " ")),
                          "power_kw": _kw(tags.get(f"socket:{sock}:output")),
                          "quantity": qty, "current": "DC" if sock in DC_SOCKETS else "AC"})
        addr = ", ".join(tags[k] for k in ("addr:housenumber", "addr:street", "addr:suburb", "addr:city", "addr:postcode")
                         if tags.get(k))
        name = tags.get("name") or tags.get("brand") or tags.get("operator")
        osm_type = el.get("type", "node")
        cost = tags.get("charge") or ({"yes": "fee charged (amount not listed)", "no": "free (per OSM tag)"}.get(tags.get("fee", "")) if tags.get("fee") else None)
        return {
            "provider": "osm", "provider_id": f"{osm_type}/{el.get('id')}",
            "name": name or "Unnamed charging station (OSM)",
            "operator_name": tags.get("operator") or tags.get("brand") or tags.get("network") or "",
            "lat": float(lat), "lng": float(lng), "address": addr,
            "connections": conns,
            "usage_type": tags.get("access"), "usage_cost": cost, "opening_hours": tags.get("opening_hours"),
            "listed_status": None,
            "source_url": f"https://www.openstreetmap.org/{osm_type}/{el.get('id')}",
            "data_provider": "OpenStreetMap contributors", "licence": "ODbL 1.0", "attribution": OSM_ATTRIBUTION,
            "retrieval_method": "Overpass API: amenity=charging_station in the Bengaluru bounding box",
            "classification": "PUBLIC LISTING",
            "origin": "api",
        }
