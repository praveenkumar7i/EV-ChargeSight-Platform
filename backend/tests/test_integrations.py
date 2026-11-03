import asyncio
import json
import logging
import socket
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.integrations.base import ConnectorError, RedactingFilter, redact
from app.integrations.csv_import import CsvFormatError, parse_events_csv, parse_ticket_history_csv
from app.integrations.ingest import ingest
from app.integrations.open_charge_map import OpenChargeMapClient, refresh_public_stations
from app.integrations.operator_api import OperatorApiConnector
from app.integrations.schema import ExternalEvent
from app.models import Charger, ConnectorHealth, Event, PublicStation, Ticket

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"


# ------------------------------------------------------------ Google Maps
def test_missing_maps_key_is_reported_safely(client):
    cfg = client.get("/api/config").json()
    assert cfg["google_maps_browser_key"] is None
    client.post("/api/integrations/maps/status", json={"state": "connected"})
    h = {x["connector"]: x for x in client.get("/api/integrations/health").json()}
    assert h["google_maps"]["state"] == "not_configured"


def test_failed_map_load_is_recorded(client, settings_env):
    settings_env(google_maps_browser_key="browser-key-abc")
    assert client.get("/api/config").json()["google_maps_browser_key"] == "browser-key-abc"
    client.post("/api/integrations/maps/status", json={"state": "error", "error": "RefererNotAllowedMapError"})
    h = {x["connector"]: x for x in client.get("/api/integrations/health").json()}
    assert h["google_maps"]["state"] == "error"
    assert "RefererNotAllowed" in h["google_maps"]["last_error"]


def test_frontend_handles_missing_key_and_failure():
    js = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "views" / "map.js").read_text(encoding="utf-8")
    assert "Google Maps is not configured" in js          # missing key → fallback message
    assert "gm_authFailure" in js and "onerror" in js     # rejected key / blocked script → fallback
    assert "schematicEngine" in js and "leafletEngine" in js  # OSM basemap, then coordinate-plot fallback


# ------------------------------------------------------------ operator API
def _connector(settings_env, handler, retries=3):
    s = settings_env(operator_api_base_url="https://operator.example/api", operator_api_key="TOPSECRET-KEY-123",
                     operator_api_max_retries=retries)
    sleeps = []
    return OperatorApiConnector(s, transport=httpx.MockTransport(handler), sleep=sleeps.append), sleeps


def test_operator_not_configured(client):
    r = client.post("/api/integrations/operator/test")
    assert r.status_code == 400


def test_operator_unavailable_retries_then_fails(settings_env):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(503)

    conn, sleeps = _connector(settings_env, handler, retries=3)
    with pytest.raises(ConnectorError) as e:
        conn.health_check()
    assert len(calls) == 3 and len(sleeps) == 2
    assert "TOPSECRET" not in str(e.value)
    assert calls[0].headers["Authorization"] == "Bearer TOPSECRET-KEY-123"   # sent server-side only


def test_operator_timeout_handled(settings_env):
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    conn, _ = _connector(settings_env, handler, retries=2)
    with pytest.raises(ConnectorError, match="timeout"):
        conn.fetch_events()


def test_operator_invalid_responses(settings_env):
    conn, _ = _connector(settings_env, lambda r: httpx.Response(200, text="not json"))
    with pytest.raises(ConnectorError, match="not valid JSON"):
        conn.fetch_events()
    conn, _ = _connector(settings_env, lambda r: httpx.Response(200, json={"items": []}))
    with pytest.raises(ConnectorError, match="'events' list"):
        conn.fetch_events()
    conn, _ = _connector(settings_env, lambda r: httpx.Response(401))
    with pytest.raises(ConnectorError, match="credentials"):
        conn.health_check()
    conn, _ = _connector(settings_env, lambda r: httpx.Response(200, json={"status": "degraded"}))
    with pytest.raises(ConnectorError):
        conn.health_check()


def _ev(i, etype="session_start", status="Charging", **kw):
    return {"external_event_id": f"X{i}", "external_charger_id": "CP-1", "timestamp": f"2026-10-01T10:{i:02d}:00+05:30",
            "event_type": etype, "status": status, "site_code": "OP1", "site_name": "Operator site",
            "lat": 12.97, "lng": 77.59, "power_kw": 30, **kw}


def test_operator_sync_ingests_and_dedupes(db, settings_env):
    events = [_ev(1, "boot", "Available"), _ev(2), _ev(3, "session_stop", "Available"),
              _ev(4, "fault", None, fault_category="GroundFailure"), {"bad": "event"}]
    conn, _ = _connector(settings_env, lambda r: httpx.Response(200, json={"events": events}))
    good, errors = conn.fetch_events()
    assert len(good) == 4 and len(errors) == 1
    run = ingest(db, good, source="operator_api", connector="operator_api", pre_errors=errors)
    db.commit()
    assert (run.accepted, run.duplicates, run.rejected) == (4, 0, 1)
    again = ingest(db, good, source="operator_api", connector="operator_api")
    db.commit()
    assert (again.accepted, again.duplicates) == (0, 4)
    ch = db.get(Charger, "OPERATOR_API-CP-1")
    assert ch.source == "operator_api" and ch.status == "faulted"
    t = db.scalar(select(Ticket).where(Ticket.charger_id == ch.id))
    assert t.source == "operator_api" and t.fault_category == "ground_fault"
    assert all(e.source == "operator_api" for e in db.scalars(select(Event).where(Event.charger_id == ch.id)))


def test_invalid_transition_rejected_per_event(db):
    good = [ExternalEvent(**_ev(1, "status_change", "Charging")),
            ExternalEvent(**_ev(2, "offline", None)),
            ExternalEvent(**_ev(3, "session_start", "Charging"))]       # offline -> charging is not allowed
    run = ingest(db, good, source="operator_import", connector="csv_import")
    db.commit()
    assert run.accepted == 2 and run.rejected == 1
    assert "not allowed" in run.errors[0]["error"]


# ------------------------------------------------------------ CSV
def test_invalid_csv_imports():
    with pytest.raises(CsvFormatError, match="Missing required"):
        parse_events_csv("a,b\n1,2\n")
    with pytest.raises(CsvFormatError, match="empty"):
        parse_events_csv("# only a comment\n")
    good, errors = parse_events_csv((SAMPLES / "SYNTHETIC_operator_events_sample.csv").read_text())
    assert len(good) >= 40
    msgs = " ".join(e["error"] for e in errors)
    assert "timezone" in msgs and "unknown status" in msgs


def test_csv_import_endpoint(client):
    data = (SAMPLES / "SYNTHETIC_operator_events_sample.csv").read_bytes()
    r = client.post("/api/integrations/import/events", files={"file": ("s.csv", data, "text/csv")},
                    data={"dataset_label": "SYNTHETIC SAMPLE test"})
    body = r.json()
    assert r.status_code == 200 and body["accepted"] > 0 and body["duplicates"] >= 1 and body["rejected"] >= 2
    r = client.post("/api/integrations/import/events", files={"file": ("x.csv", b"foo,bar\n1,2", "text/csv")},
                    data={"dataset_label": "bad"})
    assert r.status_code == 422
    summary = client.get("/api/fleet/summary?source=operator_import").json()
    assert summary["label"] == "IMPORTED" and summary["total_chargers"] == 3
    sim = client.get("/api/fleet/summary?source=simulation").json()
    assert sim["total_chargers"] == 40                     # simulated and imported never merged


def test_ticket_history_quality_report():
    recs, q = parse_ticket_history_csv((SAMPLES / "SYNTHETIC_ticket_history_sample.csv").read_text())
    assert q["valid_records"] == 60 and q["rejected_rows"] == 2
    assert q["timestamp_order_errors"] == 1
    assert q["field_completeness_pct"]["record_id"] == 100.0


# ------------------------------------------------------------ secrets
def test_secrets_never_exposed(client, settings_env, caplog):
    settings_env(operator_api_key="TOPSECRET-KEY-123", operator_api_base_url="http://127.0.0.1:1",
                 operator_api_max_retries=1, admin_token="ADMIN-TOKEN-XYZ", ocm_api_key="OCMKEY-999")
    r = client.post("/api/integrations/operator/test")
    assert r.status_code == 502
    for path in ("/api/config", "/api/integrations/health", "/api/reports/integrations.csv", "/api/reports/full.html"):
        text = client.get(path).text
        for s in ("TOPSECRET-KEY-123", "ADMIN-TOKEN-XYZ", "OCMKEY-999"):
            assert s not in text, f"{s} leaked in {path}"
    assert redact("key=TOPSECRET-KEY-123") == "key=***REDACTED***"
    rec = logging.LogRecord("x", logging.ERROR, "", 0, "failed with %s", ("TOPSECRET-KEY-123",), None)
    RedactingFilter().filter(rec)
    assert "TOPSECRET" not in rec.getMessage()


# ------------------------------------------------------------ Open Charge Map
def test_open_charge_map_parsing_and_attribution(db, settings_env):
    s = settings_env(ocm_api_key="OCMKEY-999")
    payload = [{"ID": 1, "AddressInfo": {"Title": "Mall charger", "Latitude": 12.95, "Longitude": 77.6, "Town": "Bengaluru"},
                "DataProvider": {"Title": "Open Charge Map Contributors", "License": "CC BY 4.0"},
                "Connections": [{"ConnectionType": {"Title": "CCS (Type 2)"}, "PowerKW": 60, "Quantity": 2}]},
               {"ID": 2, "AddressInfo": {"Title": "No coords"}}]
    client = OpenChargeMapClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    n = refresh_public_stations(db, client)
    db.commit()
    assert n == 1
    st = db.scalar(select(PublicStation))
    assert st.licence == "CC BY 4.0" and st.data_provider.startswith("Open Charge Map")
    bad = OpenChargeMapClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"x": 1})))
    with pytest.raises(ConnectorError):
        bad.fetch()


def test_public_refresh_without_key(client):
    assert client.post("/api/public-stations/refresh").status_code == 400
    assert "not live status" in client.get("/api/public-stations").json()["label"]


# ------------------------------------------------------------ OCPP 1.6J (real WebSocket handshake)
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_ocpp_boot_and_fault_over_real_websocket(db):
    import websockets
    from ocpp.v16 import ChargePoint as CP
    from ocpp.v16 import call
    from ocpp.v16.enums import ChargePointErrorCode, ChargePointStatus, RegistrationStatus

    from app.integrations.ocpp_central import serve

    port = _free_port()

    async def scenario():
        stop = asyncio.Event()
        server = asyncio.create_task(serve("127.0.0.1", port, stop))
        await asyncio.sleep(0.3)
        async with websockets.connect(f"ws://127.0.0.1:{port}/CP-TEST-1", subprotocols=["ocpp1.6"]) as ws:
            cp = CP("CP-TEST-1", ws)
            listener = asyncio.create_task(cp.start())
            boot = await cp.call(call.BootNotification(charge_point_model="TestModel", charge_point_vendor="TestVendor"))
            await cp.call(call.StatusNotification(connector_id=1, error_code=ChargePointErrorCode.ground_failure,
                                                  status=ChargePointStatus.faulted,
                                                  timestamp=datetime.now(timezone.utc).isoformat()))
            listener.cancel()
        stop.set()
        await server
        return boot

    boot = asyncio.run(scenario())
    assert boot.status == RegistrationStatus.accepted
    db.expire_all()
    ch = db.get(Charger, "OCPP-CP-TEST-1")
    assert ch is not None and ch.source == "ocpp" and ch.status == "faulted"
    t = db.scalar(select(Ticket).where(Ticket.charger_id == ch.id))
    assert t.fault_category == "ground_fault" and t.source == "ocpp"
    from app.integrations.base import get_health

    h = get_health(db, "ocpp")                     # v9: health rows are keyed by (organisation, connector)
    assert h.state == "connected" and h.detail["verified_by"] == "BootNotification accepted"
