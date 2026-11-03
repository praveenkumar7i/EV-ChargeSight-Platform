"""Public charging-location listings from Open Charge Map (OCM).

What this provides: publicly listed station LOCATIONS and declared connectors,
with per-record data-provider and licence attribution.
What it does NOT provide: live operational status. Listings can be stale or
wrong; the UI labels them "PUBLIC LISTING - not live status".

OCM requires a free API key (OCM_API_KEY). Licensing is per record; the
licence/attribution returned with each record is stored and displayed.
"""
from __future__ import annotations

import logging
import time

import httpx
from sqlalchemy.orm import Session

from ..clock import wall_now
from ..config import Settings, get_settings
from ..core.areas import BENGALURU_BBOX as _BB
from .base import ConnectorError, redact

# Bengaluru bounding box: south-west and north-east corners.
BENGALURU_BBOX = ((_BB[0], _BB[1]), (_BB[2], _BB[3]))
OCM_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
log = logging.getLogger("chargesight.ocm")
OCM_ATTRIBUTION = "Open Charge Map (https://openchargemap.org) and its contributors; licence per record (usually CC BY 4.0)."


class OpenChargeMapClient:
    name = "open_charge_map"

    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None):
        self.s = settings or get_settings()
        self._transport = transport

    def is_configured(self) -> bool:
        return bool(self.s.ocm_api_key)

    def fetch(self, bbox=BENGALURU_BBOX, max_results: int = 1000) -> list[dict]:
        """One GET /v3/poi/ for the bounding box (plus at most one retry on 5xx/transport errors).
        Raises ConnectorError with a user-safe message; never includes the key.
        After a successful call, self.last_skipped is the number of POIs that could not be parsed."""
        self.last_skipped = 0
        if not self.is_configured():
            raise ConnectorError("Open Charge Map is not configured: set OCM_API_KEY in the project's .env file and restart the server.")
        (s, w), (n, e) = bbox
        params = {
            "output": "json",
            "boundingbox": f"({s},{w}),({n},{e})",
            "maxresults": max_results,
            "countrycode": "IN",
            "compact": "false",
            "verbose": "false",
        }
        r = None
        t0 = time.monotonic()
        for attempt in range(2):                         # one retry on 5xx / transport errors
            try:
                with httpx.Client(base_url=self.s.ocm_base_url, timeout=OCM_TIMEOUT, transport=self._transport,
                                  headers={"X-API-Key": self.s.ocm_api_key,
                                           "User-Agent": "ChargeSight-academic-prototype (BMSCE PME project)"}) as c:
                    r = c.get("/poi/", params=params)
            except httpx.TimeoutException as ex:
                log.warning("OCM attempt %d timed out (%s)", attempt + 1, type(ex).__name__)
                if attempt == 0:
                    continue
                raise ConnectorError(f"Open Charge Map did not respond in time ({type(ex).__name__}, twice). "
                                     "Check your internet connection and try again later; stored records are unchanged.")
            except httpx.HTTPError as ex:
                log.warning("OCM attempt %d failed: %s", attempt + 1, type(ex).__name__)
                if attempt == 0:
                    continue
                raise ConnectorError(redact(f"Could not reach Open Charge Map ({type(ex).__name__}). Check your internet "
                                            "connection, firewall or proxy; stored records are unchanged."))
            log.info("OCM attempt %d: HTTP %d, %d bytes, %.1fs", attempt + 1, r.status_code, len(r.content), time.monotonic() - t0)
            if r.status_code >= 500 and attempt == 0:
                continue
            break
        if r.status_code == 429:
            ra = r.headers.get("Retry-After", "").strip()
            wait = f" Retry after {ra} seconds." if ra.isdigit() else " Wait a few minutes before trying again."
            raise ConnectorError(f"Open Charge Map rate limit reached (HTTP 429).{wait} Stored records are unchanged.")
        if r.status_code in (401, 403):
            raise ConnectorError(f"Open Charge Map rejected the API key (HTTP {r.status_code}). Check OCM_API_KEY in .env "
                                 "(copy it again from your openchargemap.org profile, no quotes or spaces) and restart the server.")
        if r.status_code >= 500:
            raise ConnectorError(f"Open Charge Map server error (HTTP {r.status_code}) after one retry. Try again later; stored records are unchanged.")
        if r.status_code >= 400:
            raise ConnectorError(f"Open Charge Map refused the request (HTTP {r.status_code}).")
        if not r.content.strip():
            raise ConnectorError("Open Charge Map returned an empty response body; stored records are unchanged.")
        try:
            data = r.json()
        except ValueError:
            raise ConnectorError("Open Charge Map response is not valid JSON (possibly a proxy or captive-portal page); stored records are unchanged.")
        if not isinstance(data, list):
            raise ConnectorError("Open Charge Map response is not a list of POIs; stored records are unchanged.")
        out = []
        for p in data:
            if not self._valid(p):
                self.last_skipped += 1
                continue
            try:
                out.append(self._parse(p))
            except (TypeError, ValueError, KeyError, AttributeError):
                self.last_skipped += 1
        if self.last_skipped:
            log.info("OCM: skipped %d malformed POI(s) of %d", self.last_skipped, len(data))
        return out

    @staticmethod
    def _valid(p) -> bool:
        try:
            a = p["AddressInfo"]
            return isinstance(a.get("Latitude"), (int, float)) and isinstance(a.get("Longitude"), (int, float))
        except (KeyError, TypeError, AttributeError):
            return False

    @staticmethod
    def _parse(p: dict) -> dict:
        a = p.get("AddressInfo") or {}
        dp = p.get("DataProvider") or {}
        licence = dp.get("License") or ((dp.get("DataProviderStatusType") or {}).get("Title")) or ""
        conns = []
        for c in p.get("Connections") or []:
            conns.append({
                "type": ((c.get("ConnectionType") or {}).get("Title")) or "unknown",
                "power_kw": c.get("PowerKW"),
                "quantity": c.get("Quantity"),
                "current": ((c.get("CurrentType") or {}).get("Title")) or None,
            })
        addr = ", ".join(str(x) for x in (a.get("AddressLine1"), a.get("AddressLine2"), a.get("Town"),
                                          a.get("StateOrProvince"), a.get("Postcode")) if x)
        status = (p.get("StatusType") or {}).get("Title")
        return {
            "provider": "open_charge_map",
            "provider_id": str(p.get("ID")),
            "name": a.get("Title") or "Unnamed location",
            "operator_name": ((p.get("OperatorInfo") or {}).get("Title")) or "",
            "lat": float(a["Latitude"]),
            "lng": float(a["Longitude"]),
            "address": addr,
            "connections": conns,
            "usage_type": (p.get("UsageType") or {}).get("Title"),
            "usage_cost": p.get("UsageCost"),
            "opening_hours": None,           # OCM has no structured opening hours
            "listed_status": f"{status} (as declared by the listing, not live)" if status else None,
            "source_url": f"https://openchargemap.org/poi/details/{p.get('ID')}",
            "data_provider": dp.get("Title") or "Open Charge Map contributors",
            "licence": licence,
            "attribution": OCM_ATTRIBUTION,
            "retrieval_method": "Open Charge Map API v3 /poi (bounding box: Bengaluru)",
            "classification": "PUBLIC LISTING",
            "origin": "api",
        }


def refresh_public_stations(db: Session, client: OpenChargeMapClient | None = None) -> int:
    """Fetch OCM listings for Bengaluru and upsert them. Returns the number of valid records received."""
    from .public_listings import upsert

    client = client or OpenChargeMapClient()
    rows = client.fetch()
    res = upsert(db, rows, retrieved_at=wall_now())
    return res["inserted"] + res["updated"]
