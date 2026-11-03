"""Connector for an authorised operator REST API.

No real operator API is connected in this repository. The expected contract
(documented in docs/INTEGRATION.md) is:

  GET  {OPERATOR_API_BASE_URL}/health              -> 200 {"status": "ok"}
  GET  {OPERATOR_API_BASE_URL}/events?since=<ISO>  -> 200 {"events": [ {...}, ... ]}

Each event object uses the fields of `ExternalEvent` (see schema.py). Adapting
to a real operator means changing `_map_event` and the paths, not the rest of
the application. The API key is sent as a Bearer token from the server only.
"""
from __future__ import annotations

import time
from datetime import datetime

import httpx
from pydantic import ValidationError

from ..clock import iso
from ..config import Settings, get_settings
from .base import ChargerDataConnector, ConnectorError, redact
from .schema import ExternalEvent


class OperatorApiConnector(ChargerDataConnector):
    name = "operator_api"
    source = "operator_api"

    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep):
        self.s = settings or get_settings()
        self._transport = transport
        self._sleep = sleep

    def is_configured(self) -> bool:
        return bool(self.s.operator_api_base_url and self.s.operator_api_key)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.s.operator_api_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {self.s.operator_api_key}", "Accept": "application/json"},
            timeout=self.s.operator_api_timeout_s,
            transport=self._transport,
        )

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        if not self.is_configured():
            raise ConnectorError("Operator API is not configured (OPERATOR_API_BASE_URL / OPERATOR_API_KEY).")
        attempts = max(1, int(self.s.operator_api_max_retries))
        last_err = ""
        for i in range(attempts):
            try:
                with self._client() as c:
                    r = c.get(path, params=params)
                if r.status_code in (429, 500, 502, 503, 504):
                    last_err = f"HTTP {r.status_code}"
                elif r.status_code in (401, 403):
                    raise ConnectorError(f"Operator API rejected credentials (HTTP {r.status_code}).")
                elif r.status_code >= 400:
                    raise ConnectorError(f"Operator API returned HTTP {r.status_code}.")
                else:
                    return r
            except httpx.TimeoutException:
                last_err = "timeout"
            except httpx.TransportError as e:
                last_err = f"transport error: {type(e).__name__}"
            if i < attempts - 1:
                self._sleep(min(2 ** i, 8))
        raise ConnectorError(redact(f"Operator API unavailable after {attempts} attempt(s): {last_err}"))

    def health_check(self) -> dict:
        r = self._get("/health")
        try:
            body = r.json()
        except ValueError:
            raise ConnectorError("Operator API health response is not valid JSON.")
        if not isinstance(body, dict) or body.get("status") != "ok":
            raise ConnectorError("Operator API health response did not report status 'ok'.")
        return {"ok": True, "checked_path": "/health"}

    @staticmethod
    def _map_event(obj: dict) -> ExternalEvent:
        return ExternalEvent(**obj)

    def fetch_events(self, since: datetime | None = None) -> tuple[list[ExternalEvent], list[dict]]:
        r = self._get("/events", params={"since": iso(since)} if since else None)
        try:
            body = r.json()
        except ValueError:
            raise ConnectorError("Operator API events response is not valid JSON.")
        items = body.get("events") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise ConnectorError("Operator API events response must be an object with an 'events' list.")
        good, errors = [], []
        for i, obj in enumerate(items):
            try:
                if not isinstance(obj, dict):
                    raise ValueError("event is not an object")
                good.append(self._map_event(obj))
            except (ValidationError, ValueError, TypeError) as e:
                errors.append({"index": i, "error": redact(str(e))[:300]})
        return good, errors


    # ------------------------------------------------------------------ diagnostics
    def diagnose(self) -> dict:
        """Find out exactly where the configuration-to-data path stops. Read-only: nothing is stored.

        States: missing_base_url | missing_key | unreachable | auth_failed | incompatible_response |
                zero_records | ok
        """
        base = {"base_url_set": bool(self.s.operator_api_base_url), "key_set": bool(self.s.operator_api_key)}
        if not self.s.operator_api_base_url:
            return {**base, "ok": False, "state": "missing_base_url",
                    "message": "OPERATOR_API_BASE_URL is empty. An authorised operator must provide the API base URL."}
        if not self.s.operator_api_key:
            return {**base, "ok": False, "state": "missing_key",
                    "message": "OPERATOR_API_KEY is empty. An authorised operator must issue a read-only key."}
        try:
            self.health_check()
        except ConnectorError as e:
            msg = str(e)
            state = ("auth_failed" if "rejected credentials" in msg else
                     "incompatible_response" if ("JSON" in msg or "status 'ok'" in msg or "HTTP 404" in msg) else
                     "unreachable")
            return {**base, "ok": False, "state": state, "step": "GET /health", "message": msg}
        try:
            events, errors = self.fetch_events()
        except ConnectorError as e:
            msg = str(e)
            state = "auth_failed" if "rejected credentials" in msg else (
                "incompatible_response" if ("JSON" in msg or "'events'" in msg or "HTTP 404" in msg) else "unreachable")
            return {**base, "ok": False, "state": state, "step": "GET /events", "message": msg}
        if not events and errors:
            return {**base, "ok": False, "state": "incompatible_response", "step": "map events",
                    "message": f"All {len(errors)} events failed validation against the ChargeSight event schema.",
                    "errors": errors[:5]}
        if not events:
            return {**base, "ok": True, "state": "zero_records", "step": "GET /events",
                    "message": "Connected and authenticated, but the provider returned zero events."}
        return {**base, "ok": True, "state": "ok", "events_available": len(events), "invalid_events": len(errors),
                "message": f"Connected. {len(events)} valid events available ({len(errors)} invalid)."}
