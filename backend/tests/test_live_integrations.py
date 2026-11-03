"""Live-integration paths: OCM refresh origins/throttling, Google Places gating, operator API
diagnostics/sync/staleness, OCPP simulator over a real local WebSocket, source-mode states,
sample import, and source separation.

External services are NEVER contacted: OCM and the operator API are exercised with mocks or
monkeypatched fetchers. The OCPP test uses a genuine local WebSocket between the central system
and the bundled simulator. None of this proves the real providers work with real credentials.
"""
import asyncio
import importlib.util
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[2]


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _ocm_row(i, name, lat, lng):
    return {"provider": "open_charge_map", "provider_id": str(i), "name": name, "operator_name": "Statiq (IN)",
            "lat": lat, "lng": lng, "address": "Bengaluru", "connections": [], "licence": "CC BY 4.0",
            "source_url": f"https://openchargemap.org/poi/details/{i}", "attribution": "OCM", "origin": "api",
            "retrieval_method": "Open Charge Map API v3"}


# ------------------------------------------------------------------ Open Charge Map refresh
def test_ocm_refresh_replaces_snapshot_and_flags_absent(client, settings_env, monkeypatch):
    from app.integrations import open_charge_map

    settings_env(ocm_api_key="OCM-TEST-KEY-123")
    before = {s["provider_id"]: s for s in client.get("/api/public-stations").json()["stations"]}
    assert before["309187"]["origin"] == "snapshot"
    rows = [_ocm_row(309187, "Elements Mall (API)", 13.04612, 77.6269), _ocm_row(999001, "New API station", 12.93, 77.62)]
    monkeypatch.setattr(open_charge_map.OpenChargeMapClient, "fetch", lambda self, *a, **k: rows)
    r = client.post("/api/public-stations/refresh", json={"providers": ["open_charge_map"]}).json()["results"]["open_charge_map"]
    assert r["ok"] and r["inserted"] == 1 and r["updated"] == 1 and r["absent_from_refresh"] == len(before) - 1
    after = {s["provider_id"]: s for s in client.get("/api/public-stations").json()["stations"]}
    assert after["309187"]["origin"] == "api" and after["309187"]["name"] == "Elements Mall (API)"
    assert after["999001"]["origin"] == "api"
    assert after["308920"]["origin"] == "snapshot" and after["308920"]["absent_from_latest_refresh"] is True
    # reloading the snapshot never overwrites API-fetched records
    client.post("/api/public-stations/reload-snapshot")
    assert {s["provider_id"]: s for s in client.get("/api/public-stations").json()["stations"]}["309187"]["origin"] == "api"
    # second refresh within 10 minutes is served from stored records unless forced
    again = client.post("/api/public-stations/refresh", json={"providers": ["open_charge_map"]}).json()["results"]["open_charge_map"]
    assert again["cached"] is True
    forced = client.post("/api/public-stations/refresh", json={"providers": ["open_charge_map"], "force": True}).json()
    assert forced["results"]["open_charge_map"].get("cached") is None


def test_ocm_rejected_key_keeps_records(client, settings_env):
    from app.integrations import open_charge_map

    settings_env(ocm_api_key="BAD-KEY-0000")
    n = client.get("/api/public-stations").json()["total"]
    orig = open_charge_map.OpenChargeMapClient.__init__

    def mock_init(self, settings=None, transport=None):
        orig(self, settings, transport=httpx.MockTransport(lambda req: httpx.Response(403, text="forbidden")))
    open_charge_map.OpenChargeMapClient.__init__ = mock_init
    try:
        r = client.post("/api/public-stations/refresh", json={"providers": ["open_charge_map"]}).json()
    finally:
        open_charge_map.OpenChargeMapClient.__init__ = orig
    res = r["results"]["open_charge_map"]
    assert res["ok"] is False and "rejected the API key" in res["message"] and "BAD-KEY" not in res["message"]
    assert client.get("/api/public-stations").json()["total"] == n
    p = next(x for x in client.get("/api/public-stations").json()["providers"] if x["provider"] == "open_charge_map")
    assert p["state_label"] == "last refresh failed" and p["last_error"]


def test_ocm_client_sends_key_header_and_handles_429(settings_env):
    from app.integrations.base import ConnectorError
    from app.integrations.open_charge_map import OpenChargeMapClient

    s = settings_env(ocm_api_key="HEADER-KEY-777")
    seen = {}

    def ok(req):
        seen["key"] = req.headers.get("X-API-Key")
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=[])
    assert OpenChargeMapClient(s, transport=httpx.MockTransport(ok)).fetch() == []
    assert seen["key"] == "HEADER-KEY-777" and seen["params"]["countrycode"] == "IN" and "boundingbox" in seen["params"]
    with pytest.raises(ConnectorError, match="rate limit"):
        OpenChargeMapClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(429))).fetch()


def test_ocm_config_reads_env_variable(settings_env):
    s = settings_env(ocm_api_key="FROM-ENV-ABCD")
    from app.integrations.open_charge_map import OpenChargeMapClient
    assert OpenChargeMapClient(s).is_configured() and s.ocm_api_key == "FROM-ENV-ABCD"


# ------------------------------------------------------------------ Google Places gating
def test_places_never_claimed_without_successful_request(client, settings_env):
    p = next(x for x in client.get("/api/public-stations").json()["providers"] if x["provider"] == "google_places")
    assert p["configured"] is False and p["verified"] is False and p["state_label"] == "not configured"
    settings_env(google_places_api_key="PLACES-KEY-1234")
    p = next(x for x in client.get("/api/public-stations").json()["providers"] if x["provider"] == "google_places")
    assert p["configured"] is True and p["verified"] is False and p["state_label"] == "key present, not verified"
    r = client.get("/api/public-stations/google?areas=hebbal")             # no confirm_billing -> refused, no request made
    assert r.status_code == 400 and "billed" in r.json()["detail"]
    cfg = client.get("/api/config").json()
    assert "PLACES-KEY-1234" not in str(cfg) and "google_places_api_key" not in cfg


# ------------------------------------------------------------------ operator REST API
def _op(settings_env, handler, **kw):
    from app.integrations.operator_api import OperatorApiConnector

    s = settings_env(operator_api_base_url=kw.get("url", "https://operator.example.test/api"),
                     operator_api_key=kw.get("key", "OP-KEY-9999"), operator_api_max_retries="1")
    return OperatorApiConnector(s, transport=httpx.MockTransport(handler), sleep=lambda *_: None)


def test_operator_diagnose_states(settings_env):
    from app.integrations.operator_api import OperatorApiConnector

    assert OperatorApiConnector(settings_env(operator_api_base_url="", operator_api_key="")).diagnose()["state"] == "missing_base_url"
    assert OperatorApiConnector(settings_env(operator_api_base_url="https://x.test", operator_api_key="")).diagnose()["state"] == "missing_key"
    assert _op(settings_env, lambda r: httpx.Response(401)).diagnose()["state"] == "auth_failed"
    assert _op(settings_env, lambda r: httpx.Response(404)).diagnose()["state"] == "incompatible_response"
    assert _op(settings_env, lambda r: httpx.Response(200, text="<html>")).diagnose()["state"] == "incompatible_response"
    assert _op(settings_env, lambda r: httpx.Response(503)).diagnose()["state"] == "unreachable"

    def zero(req):
        return httpx.Response(200, json={"status": "ok"} if req.url.path.endswith("/health") else {"events": []})
    assert _op(settings_env, zero).diagnose()["state"] == "zero_records"

    def bad_events(req):
        return httpx.Response(200, json={"status": "ok"} if req.url.path.endswith("/health") else {"events": [{"foo": 1}]})
    assert _op(settings_env, bad_events).diagnose()["state"] == "incompatible_response"

    def good(req):
        if req.url.path.endswith("/health"):
            assert req.headers["Authorization"] == "Bearer OP-KEY-9999"
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"events": [{"external_event_id": "e1", "external_charger_id": "K1",
                                                     "timestamp": "2026-10-24T10:00:00+05:30", "event_type": "boot"}]})
    d = _op(settings_env, good).diagnose()
    assert d["state"] == "ok" and d["events_available"] == 1 and "OP-KEY" not in str(d)


def test_operator_sync_dedupes_and_never_fabricates(db, settings_env):
    from app.models import Charger, Event, Ticket
    from app.services import operator_sync

    events = [{"external_event_id": "e1", "external_charger_id": "K1", "timestamp": "2026-10-24T10:00:00+05:30", "event_type": "boot"},
              {"external_event_id": "e2", "external_charger_id": "K1", "timestamp": "2026-10-24T10:05:00+05:30",
               "event_type": "fault", "fault_category": "GroundFailure"}]
    conn = _op(settings_env, lambda r: httpx.Response(200, json={"events": events}))
    r1 = operator_sync.sync(db, conn)
    r2 = operator_sync.sync(db, conn)
    db.commit()
    assert (r1.accepted, r2.accepted, r2.duplicates) == (2, 0, 2)
    assert db.scalar(select(func.count(Ticket.id)).where(Ticket.source == "operator_api")) == 1
    st = operator_sync.status(db)
    assert st["state"] == "RECEIVING DATA" and st["chargers"] == 1 and st["last_success_at"]
    empty = _op(settings_env, lambda r: httpx.Response(200, json={"events": []}))
    operator_sync.sync(db, empty)
    assert db.scalar(select(func.count(Charger.id)).where(Charger.source == "operator_api")) == 1
    assert db.scalar(select(func.count(Event.id)).where(Event.source == "operator_api")) == 2


def test_operator_status_and_poll_interval(client, settings_env):
    from app.services.operator_sync import effective_poll_seconds

    st = client.get("/api/integrations/operator/status").json()
    assert st["state"] == "NOT CONFIGURED" and st["poll_seconds"] == 0
    assert client.post("/api/integrations/operator/diagnose").json()["state"] == "missing_base_url"
    assert client.post("/api/integrations/operator/sync").status_code == 400
    settings_env(operator_api_poll_seconds="5")
    assert effective_poll_seconds() == 60            # clamped to a polite minimum
    settings_env(operator_api_poll_seconds="0")
    assert effective_poll_seconds() == 0


# ------------------------------------------------------------------ OCPP: central system + bundled simulator
def _load_simulator():
    spec = importlib.util.spec_from_file_location("ocpp_simulator", ROOT / "scripts" / "ocpp_simulator.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ocpp_simulator_end_to_end_over_websocket(client, db_url):
    from app import db as D
    from app.integrations.ocpp_central import serve
    from app.models import Charger, Event, Ticket

    sim = _load_simulator()
    port = _free_port()
    args = sim.parse(["--url", f"ws://127.0.0.1:{port}", "--id", "SIMOCPP-T1", "--interval", "0", "--meter-count", "3",
                      "--fault", "GroundFailure"])

    async def scenario():
        stop = asyncio.Event()
        server = asyncio.create_task(serve("127.0.0.1", port, stop))
        await asyncio.sleep(0.3)
        code = await sim.main_async(args)
        stop.set()
        await server
        return code

    assert asyncio.run(scenario()) == 0
    with D.session_scope() as s:
        ch = s.get(Charger, "OCPP-SIMOCPP-T1")
        assert ch.source == "ocpp" and ch.test_traffic is True and ch.ocpp_vendor == "ChargeSight-Sim"
        assert ch.status == "faulted" and ch.total_kwh > 100
        types = dict(s.execute(select(Event.type, func.count(Event.id)).where(Event.charger_id == ch.id).group_by(Event.type)).all())
        assert types["meter_values"] == 3 and types["session_start"] == 1 and types["session_stop"] == 1 and types["fault"] == 1
        assert s.scalar(select(func.count(Ticket.id)).where(Ticket.charger_id == ch.id)) == 1
    lst = client.get("/api/chargers?source=ocpp").json()
    assert [c["label"] for c in lst] == ["SIMULATED OCPP"]
    assert client.get("/api/tickets?source=ocpp").json()[0]["label"] == "SIMULATED OCPP"
    o = client.get("/api/sources/status").json()["sources"]["ocpp"]
    assert o["state"] == "RECEIVING TEST DATA" and o["simulated_ocpp_chargers"] == 1 and o["real_ocpp_chargers"] == 0
    assert all(c["source"] == "simulation" for c in client.get("/api/chargers?source=simulation").json())


def test_ocpp_retransmission_is_stored_once(db):
    from app.integrations.ocpp_central import _emit
    from app.models import Event

    from datetime import datetime, timezone
    ts = datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc)
    for _ in range(2):        # same OCPP unique id twice = a retransmitted CALL
        _emit("CP-RT", "status_available", ts, dedupe_key="uid-123", status="Available")
    _emit("CP-RT", "status_available", ts, dedupe_key="uid-124", status="Available")
    db.expire_all()
    assert db.scalar(select(func.count(Event.id)).where(Event.charger_id == "OCPP-CP-RT")) == 2


def test_ticket_codes_do_not_collide_across_sources(db):
    from app.integrations.ingest import ingest
    from app.integrations.schema import ExternalEvent
    from app.models import Ticket
    from app.services.seed import reset_simulation
    from app.simulator import engine

    from datetime import datetime, timezone
    ingest(db, [ExternalEvent(external_event_id="x1", external_charger_id="CPX", timestamp=datetime.now(timezone.utc),
                              event_type="fault", fault_category="GroundFailure")], source="ocpp", connector="ocpp")
    db.commit()
    reset_simulation(db)          # resets the simulator's counter
    out = engine.inject_fault(db, "SIM-KRM-01", "overcurrent")
    db.commit()
    codes = [c for (c,) in db.execute(select(Ticket.code))]
    assert len(codes) == len(set(codes)) == 2 and out["ticket"] != "TKT-00001"


# ------------------------------------------------------------------ source modes, sample import
def test_source_modes_report_real_state(client):
    st = client.get("/api/sources/status").json()["sources"]
    assert st["simulation"]["state"] == "SIMULATED"
    assert st["operator_import"]["state"] == "NO IMPORTED DATA"
    assert st["operator_api"]["state"] == "NOT CONFIGURED"
    assert st["ocpp"]["state"] in ("NOT CONNECTED", "WAITING FOR DATA") and st["ocpp"]["chargers"] == 0
    # empty live modes never borrow simulated metrics
    s = client.get("/api/fleet/summary?source=operator_api").json()
    assert s["total_chargers"] == 0 and s["open_tickets"] == 0 and s["sessions_total"] is None


def test_sample_download_and_import(client):
    r = client.get("/api/integrations/samples/operator-events.csv")
    assert r.status_code == 200 and r.text.startswith("# SYNTHETIC SAMPLE")
    assert client.get("/api/integrations/samples/../../.env").status_code == 404
    run = client.post("/api/integrations/import/sample").json()
    assert run["accepted"] > 0 and run["rejected"] >= 2 and "SYNTHETIC" in run["dataset_label"]
    again = client.post("/api/integrations/import/sample").json()
    assert again["accepted"] == 0 and again["duplicates"] >= run["accepted"]        # file also contains a deliberate duplicate id
    st = client.get("/api/sources/status").json()["sources"]["operator_import"]
    assert st["state"] == "IMPORTED" and st["chargers"] == 3
    imp = client.get("/api/chargers?source=operator_import").json()
    assert all(c["label"] == "IMPORTED" for c in imp)
    assert all(c["source"] == "simulation" for c in client.get("/api/chargers").json())


def test_frontend_crosshair_contract():
    js = (ROOT / "frontend" / "js" / "charts.js").read_text(encoding="utf-8")
    assert "chart-ts" in js and "data-ts" in js and "nearestIndex" in js
    assert "ArrowLeft" in js and "pointerdown" in js and "MutationObserver" in js
