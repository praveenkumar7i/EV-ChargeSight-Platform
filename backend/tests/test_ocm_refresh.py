"""Open Charge Map refresh: success, every failure mode, record preservation, concurrency.

All HTTP is mocked with httpx.MockTransport. These tests prove how ChargeSight handles
each kind of response; they do NOT prove that the real Open Charge Map API works.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

KEY = "TEST-OCM-KEY-0000-do-not-log"


def _poi(i, lat=12.95, lng=77.60, **extra):
    p = {"ID": i, "UsageType": {"Title": "Public"}, "StatusType": {"Title": "Operational"},
         "OperatorInfo": {"Title": "Test operator"}, "DataProvider": {"Title": "Open Charge Map Contributors", "License": "CC BY 4.0"},
         "AddressInfo": {"Title": f"Mock station {i}", "Latitude": lat, "Longitude": lng, "Town": "Bengaluru"},
         "Connections": [{"ConnectionType": {"Title": "CCS (Type 2)"}, "PowerKW": 60, "Quantity": 1}]}
    p.update(extra)
    return p


@pytest.fixture
def ocm(client, settings_env, monkeypatch):
    """Configure a fake key and route the OCM client's HTTP through `handler` (set by each test)."""
    from app.integrations import open_charge_map as M

    settings_env(ocm_api_key=KEY)
    state = {"handler": lambda r: httpx.Response(200, json=[]), "calls": []}
    orig = M.OpenChargeMapClient.__init__

    def init(self, settings=None, transport=None):
        def h(req):
            state["calls"].append(req)
            return state["handler"](req)
        orig(self, settings, transport or httpx.MockTransport(h))

    monkeypatch.setattr(M.OpenChargeMapClient, "__init__", init)
    return state


def _post(client, force=True):
    return client.post("/api/public-stations/refresh", json={"providers": ["open_charge_map"], "force": force})


def _stored(db_url):
    from app import db as D
    from app.models import PublicStation

    s = D.SessionLocal()
    try:
        rows = s.scalars(select(PublicStation).where(PublicStation.provider == "open_charge_map")).all()
        return {r.provider_id: (r.origin, r.name, r.lat, r.lng) for r in rows}
    finally:
        s.close()


def _health(client):
    return next(h for h in client.get("/api/integrations/health").json() if h["connector"] == "open_charge_map")


# ------------------------------------------------------------------ success
def test_successful_refresh_counts_and_request_format(client, ocm, db_url):
    before = _stored(db_url)
    assert before, "bundled snapshot should be loaded"
    existing_id = next(iter(before))
    ocm["handler"] = lambda r: httpx.Response(200, json=[_poi(int(existing_id)), _poi(990001), _poi(990002, lat=12.97)])
    r = _post(client)
    assert r.status_code == 200
    res = r.json()["results"]["open_charge_map"]
    assert res["ok"] and res["received"] == 3 and res["inserted"] == 2 and res["updated"] == 1 and res["rejected"] == 0
    assert res["skipped"] == 0 and res["absent_from_refresh"] == len(before) - 1

    req = ocm["calls"][0]
    assert req.url.path == "/v3/poi/" and req.headers["X-API-Key"] == KEY       # documented header auth
    q = req.url.params
    assert q["countrycode"] == "IN" and q["maxresults"] == "1000" and q["boundingbox"].startswith("(")
    assert KEY not in str(req.url)                                                # key never in the URL

    after = _stored(db_url)
    assert set(before) <= set(after) and len(after) == len(before) + 2            # nothing deleted
    assert after[existing_id][0] == "api" and after["990001"][0] == "api"
    assert all(after[k][0] == "snapshot" for k in before if k != existing_id)   # provenance kept distinct
    h = _health(client)
    assert h["state"] == "connected" and h["last_success_at"]


def test_malformed_pois_are_skipped_not_fatal(client, ocm, db_url):
    bad_conn = _poi(990010, Connections="not-a-list-of-objects")
    no_coords = {"ID": 990011, "AddressInfo": {"Title": "x"}}
    ocm["handler"] = lambda r: httpx.Response(200, json=[_poi(990012), bad_conn, no_coords, "junk"])
    res = _post(client).json()["results"]["open_charge_map"]
    assert res["ok"] and res["received"] == 1 and res["skipped"] == 3


def test_throttle_returns_cached_without_a_request(client, ocm):
    ocm["handler"] = lambda r: httpx.Response(200, json=[_poi(990020)])
    assert _post(client).json()["results"]["open_charge_map"]["ok"]
    n = len(ocm["calls"])
    res = _post(client, force=False).json()["results"]["open_charge_map"]
    assert res["cached"] and len(ocm["calls"]) == n                               # no extra request to OCM


# ------------------------------------------------------------------ failures keep stored records
FAILURES = [
    ("401", lambda r: httpx.Response(401, text="Unauthorized"), "rejected the API key (HTTP 401)"),
    ("403", lambda r: httpx.Response(403, text="Forbidden"), "rejected the API key (HTTP 403)"),
    ("429", lambda r: httpx.Response(429, headers={"Retry-After": "120"}), "rate limit reached (HTTP 429). Retry after 120 seconds"),
    ("500", lambda r: httpx.Response(503, text="down"), "server error (HTTP 503) after one retry"),
    ("timeout", lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)), "did not respond in time"),
    ("network", lambda r: (_ for _ in ()).throw(httpx.ConnectError("dns failed", request=r)), "Could not reach Open Charge Map (ConnectError)"),
    ("html", lambda r: httpx.Response(200, text="<html>captive portal</html>"), "not valid JSON"),
    ("object", lambda r: httpx.Response(200, json={"error": "nope"}), "not a list of POIs"),
    ("empty-body", lambda r: httpx.Response(200, content=b""), "empty response body"),
]


@pytest.mark.parametrize("name,handler,expect", FAILURES, ids=[f[0] for f in FAILURES])
def test_failures_are_safe_and_preserve_records(client, ocm, db_url, caplog, name, handler, expect):
    before = _stored(db_url)
    ocm["handler"] = handler
    with caplog.at_level(logging.DEBUG):
        r = _post(client)
    assert r.status_code == 200, r.text                                           # never a 500
    res = r.json()["results"]["open_charge_map"]
    assert res["ok"] is False and res["state"] == "error" and expect in res["message"]
    assert KEY not in r.text and KEY not in caplog.text                          # key never in response or logs
    assert _stored(db_url) == before                                              # records untouched
    h = _health(client)
    assert h["state"] == "error" and expect in h["last_error"]
    if name in ("500", "timeout", "network"):
        assert len(ocm["calls"]) == 2                                             # exactly one retry
    elif name in ("401", "403", "429"):
        assert len(ocm["calls"]) == 1                                             # no retry on auth/rate limit


def test_empty_list_keeps_records_and_says_so(client, ocm, db_url):
    before = _stored(db_url)
    ocm["handler"] = lambda r: httpx.Response(200, json=[])
    res = _post(client).json()["results"]["open_charge_map"]
    assert res["ok"] and res["received"] == 0 and "stored records are kept" in res["message"]
    assert res["absent_from_refresh"] == 0                                        # empty answer flags nothing
    assert _stored(db_url) == before


def test_missing_key_is_reported_not_crashed(client, settings_env):
    settings_env(ocm_api_key="")
    r = client.post("/api/public-stations/refresh", json={"providers": ["open_charge_map"]})
    assert r.status_code == 400 and "OCM_API_KEY" in r.json()["detail"]


def test_unexpected_error_returns_safe_message(client, ocm, db_url, monkeypatch):
    from app.integrations import public_listings as PL

    before = _stored(db_url)
    ocm["handler"] = lambda r: httpx.Response(200, json=[_poi(990030)])
    monkeypatch.setattr(PL, "upsert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = _post(client)
    assert r.status_code == 200
    res = r.json()["results"]["open_charge_map"]
    assert not res["ok"] and "RuntimeError" in res["message"] and "unchanged" in res["message"]
    assert _stored(db_url) == before


# ------------------------------------------------------------------ root cause regression
def test_concurrent_writer_during_fetch_does_not_cause_500(client, ocm, db_url):
    """Root cause of the reported 'Internal server error': the refresh held a read transaction
    open during the HTTP call; when the simulator/OCPP committed meanwhile, the later INSERT
    failed with 'database is locked' (SQLite busy snapshot)."""
    from app import db as D
    from app.models import SimState

    def handler(req):
        s = D.SessionLocal()                                                      # another writer commits mid-fetch
        try:
            st = s.get(SimState, 1)
            if st is None:
                s.add(SimState(id=1))
            else:
                st.speed = (st.speed or 1) + 1
            s.commit()
        finally:
            s.close()
        return httpx.Response(200, json=[_poi(990040), _poi(990041)])

    ocm["handler"] = handler
    r = _post(client)
    assert r.status_code == 200, r.text
    res = r.json()["results"]["open_charge_map"]
    assert res["ok"] and res["inserted"] == 2


# ------------------------------------------------------------------ concurrency (409) regressions
def _in_thread(fn):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", fn()), daemon=True)
    t.start()
    return t, out


def test_slow_osm_refresh_does_not_block_ocm_refresh(client, ocm, settings_env, monkeypatch):
    """The reported 409: a request without a body (what the old cached v3 Integrations button
    sends) refreshes OSM + OCM. OSM timed out on the user's network for ~51 s while holding one
    global lock, so every Open Charge Map refresh in that window got HTTP 409."""
    from app.integrations import osm_overpass

    settings_env(osm_enabled="true")
    entered, gate = threading.Event(), threading.Event()

    def slow_osm(self, bbox=None):
        entered.set()
        assert gate.wait(10), "test gate never opened"
        raise osm_overpass.ConnectorError("Overpass request failed: ConnectTimeout")

    monkeypatch.setattr(osm_overpass.OverpassClient, "fetch", slow_osm)
    ocm["handler"] = lambda r: httpx.Response(200, json=[_poi(990050)])
    legacy, legacy_out = _in_thread(lambda: client.post("/api/public-stations/refresh"))   # no body, like v3
    try:
        assert entered.wait(5)
        r = _post(client)                                                         # the OCM button meanwhile
        assert r.status_code == 200, r.text                                       # was 409 before the fix
        assert r.json()["results"]["open_charge_map"]["ok"]
        st = client.get("/api/public-stations/refresh/status").json()
        assert st["running"] and set(st["active"]) == {"osm"}
        r = client.post("/api/public-stations/refresh", json={"providers": ["osm"], "force": True})
        assert r.status_code == 409 and "OpenStreetMap refresh is already running" in r.json()["detail"]
    finally:
        gate.set()
        legacy.join(10)
    lr = legacy_out["r"]
    assert lr.status_code == 200
    assert lr.json()["results"]["osm"]["ok"] is False and "ConnectTimeout" in lr.json()["results"]["osm"]["message"]
    assert client.get("/api/public-stations/refresh/status").json() == {"running": False, "active": {}}


def test_concurrent_ocm_refreshes_one_runs_one_is_refused(client, ocm):
    entered, gate = threading.Event(), threading.Event()

    def slow(req):
        entered.set()
        assert gate.wait(10)
        return httpx.Response(200, json=[_poi(990060)])

    ocm["handler"] = slow
    first, out = _in_thread(lambda: _post(client))
    try:
        assert entered.wait(5)
        r = _post(client)
        assert r.status_code == 409
        assert "Open Charge Map refresh is already running (started" in r.json()["detail"]
        assert len(ocm["calls"]) == 1                                             # the refused one sent nothing to OCM
    finally:
        gate.set()
        first.join(10)
    assert out["r"].status_code == 200 and out["r"].json()["results"]["open_charge_map"]["ok"]
    ocm["handler"] = lambda r: httpx.Response(200, json=[_poi(990061)])
    assert _post(client).status_code == 200                                       # provider released afterwards


@pytest.mark.parametrize("failure", ["connector_error", "unexpected_exception", "network"])
def test_provider_is_released_after_every_failure(client, ocm, monkeypatch, failure):
    from app.integrations import public_listings as PL

    if failure == "connector_error":
        ocm["handler"] = lambda r: httpx.Response(401)
    elif failure == "network":
        ocm["handler"] = lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("x", request=r))
    else:
        ocm["handler"] = lambda r: httpx.Response(200, json=[_poi(990070)])
        monkeypatch.setattr(PL, "upsert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = _post(client)
    assert r.status_code == 200 and r.json()["results"]["open_charge_map"]["ok"] is False
    assert client.get("/api/public-stations/refresh/status").json()["running"] is False


def test_refresh_guard_unit():
    from app.api.public_map import RefreshGuard

    g = RefreshGuard()
    assert g.begin("open_charge_map") and not g.begin("open_charge_map") and g.begin("osm")
    assert set(g.active()) == {"open_charge_map", "osm"}
    g.end("open_charge_map")
    assert g.begin("open_charge_map")
    g.end("open_charge_map"); g.end("osm"); g.end("osm")                         # double end is harmless
    assert g.active() == {}


def test_osm_connect_failure_is_retried_once_only(settings_env):
    from app.integrations.base import ConnectorError
    from app.integrations.osm_overpass import OverpassClient

    s = settings_env(osm_enabled="true")
    calls = []

    def h(req):
        calls.append(req)
        raise httpx.ConnectTimeout("blocked", request=req)

    with pytest.raises(ConnectorError, match="ConnectTimeout"):
        OverpassClient(s, transport=httpx.MockTransport(h), backoff_s=0).fetch()
    assert len(calls) == 2


def test_map_button_does_not_send_click_event_as_force(client):
    js = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "views" / "map.js").read_text(encoding="utf-8")
    assert 'onclick = refreshListings;' not in js and "refreshListings(false)" in js
    r = client.post("/api/public-stations/refresh", json={"providers": ["open_charge_map"], "force": {"isTrusted": True}})
    assert r.status_code == 422                                                   # what the old button triggered


# ------------------------------------------------------------------ stale frontend
def test_frontend_assets_are_versioned_and_revalidate(client):
    from app.main import frontend_build_id

    build = frontend_build_id()
    page = client.get("/app")                      # v9: the operations console moved to /app ("/" is the public site)
    assert page.headers.get("cache-control") == "no-cache"
    assert f'src="/static/{build}/js/app.js"' in page.text and f'content="{build}"' in page.text
    js = client.get(f"/static/{build}/js/views/integrations.js")
    assert js.status_code == 200 and js.headers.get("cache-control") == "no-cache"
    assert client.get("/static/js/app.js").headers.get("cache-control") == "no-cache"   # old URL still served
    assert client.get("/api/public-stations").headers.get("x-chargesight-build") == build
