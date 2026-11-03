"""v9 onboarding, operator API connections (mocked HTTP only) and OCPP endpoint routing.

No real operator API or charge point is contacted. Operator API responses come from httpx.MockTransport
and DNS answers from a fake resolver; OCPP uses a local WebSocket server on 127.0.0.1.
"""
from __future__ import annotations

import logging

import httpx
import pytest
from sqlalchemy import select

from tests.test_v9_billing import _buy
from tests.test_v9_tenancy import make_org

SECRET = "op-live-secret-token-XYZ123"


def _public_dns(host, port, **_):
    return [(None, None, None, None, ("93.184.215.14", port))]      # a public address; never contacted (MockTransport)


@pytest.fixture
def mock_operator(monkeypatch):
    from app.services import integration_configs as ic

    state = {"status": 200, "events_status": 200, "events": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        if request.url.path.endswith("/health"):
            return httpx.Response(state["status"], json={"status": "ok"})
        if request.url.path.endswith("/events"):
            return httpx.Response(state["events_status"], json={"events": state["events"]})
        return httpx.Response(404)

    monkeypatch.setattr(ic, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(ic, "RESOLVER", _public_dns)
    return state


def _starter_org(app):
    """An organisation on Growth: the smallest plan with configured integrations (v9.1 catalogue)."""
    b, org_b, _ = make_org(app, fleet=False)
    _buy(b, "growth", 1)
    return b, org_b


def test_demo_onboarding_completes_without_any_credentials(client):
    b, org_b, _ = make_org(client.app, fleet=False)
    ob = b.get("/api/org/onboarding").json()
    assert ob["state"]["completed"] is False and len(ob["steps"]) == 6
    assert b.put("/api/org", json={"sites_estimate": 3, "chargers_estimate": 20, "primary_contact_name": "Bina Shah"}).status_code == 200
    assert b.put("/api/org/onboarding", json={"step": 3, "use_cases": ["fleet_monitoring", "fault_management"], "data_mode": "demo"}).status_code == 200
    r = b.post("/api/org/demo-workspace")
    assert r.status_code == 200 and r.json()["label"] == "SIMULATED"
    items = {i["key"]: i for i in b.get("/api/org/onboarding").json()["checklist"]}
    assert items["data_mode"]["done"] and "SIMULATED" in items["data_mode"]["status"]
    assert items["profile"]["done"] and items["use_case"]["done"]
    done = b.put("/api/org/onboarding", json={"complete": True}).json()
    assert done["state"]["completed"] and done["state"]["step"] == 6
    assert len(b.get("/api/tickets").json()) == 8 and len(b.get("/api/chargers").json()) == 40
    me = b.get("/api/auth/me").json()
    assert me["onboarding"]["completed"]                               # persisted on the server
    assert b.get("/api/sources/status").json()["sources"]["operator_api"]["state"] == "NOT CONFIGURED"


def test_onboarding_progress_survives_sign_out(client):
    from tests.conftest import sign_in
    from tests.test_v9_tenancy import PW

    b, org_b, _ = make_org(client.app, fleet=False)
    b.put("/api/org/onboarding", json={"step": 4, "use_cases": ["site_planning"], "data_mode": "csv"})
    b.post("/api/auth/signout")
    b.cookies.clear()
    d = sign_in(b, "ops@voltway.example", PW)
    assert d["next"] == "/app#onboarding" and d["onboarding"]["step"] == 4 and d["onboarding"]["data_mode"] == "csv"
    item = {i["key"]: i for i in b.get("/api/org/onboarding").json()["checklist"]}["data_mode"]
    assert not item["done"] and "no successful import" in item["status"]          # never marked imported early


def test_missing_credentials_show_not_configured(client):
    b, _ = _starter_org(client.app)
    cfg = b.post("/api/org/integrations", json={"kind": "operator_api", "name": "Operator X"}).json()
    assert cfg["state"] == "not_configured" and cfg["credential"]["set"] is False
    cfg = b.put(f"/api/org/integrations/{cfg['id']}", json={"base_url": "https://api.operator.example"}).json()
    assert cfg["state"] == "awaiting_credentials"
    t = b.post(f"/api/org/integrations/{cfg['id']}/test").json()
    assert t["ok"] is False and t["state"] == "awaiting_credentials" and t["state"] != "connected"


def _configured(b):
    cfg = b.post("/api/org/integrations", json={"kind": "operator_api", "name": "Operator X"}).json()
    return b.put(f"/api/org/integrations/{cfg['id']}", json={"base_url": "https://api.operator.example", "credential": SECRET,
                                                            "operator_authorised": True, "authorisation_note": "Letter 2026-10"}).json()


@pytest.mark.parametrize("status, expected", [(401, "auth_failed"), (403, "permission_insufficient"), (500, "connection_failed"),
                                              (404, "connection_failed")])
def test_failed_api_tests_never_show_connected(client, mock_operator, status, expected):
    b, _ = _starter_org(client.app)
    cfg = _configured(b)
    assert cfg["state"] == "configured"
    mock_operator["status"] = status
    t = b.post(f"/api/org/integrations/{cfg['id']}/test").json()
    assert t["ok"] is False and t["state"] == expected and t["connection"]["state"] != "connected"
    assert SECRET not in str(t)


def test_events_permission_failure_after_healthy_health_check(client, mock_operator):
    b, _ = _starter_org(client.app)
    cfg = _configured(b)
    mock_operator["events_status"] = 403
    t = b.post(f"/api/org/integrations/{cfg['id']}/test").json()
    assert t["state"] == "permission_insufficient" and not t["ok"]


def test_private_or_local_destinations_are_blocked(client, mock_operator, monkeypatch):
    from app.services import integration_configs as ic

    b, _ = _starter_org(client.app)
    cfg = b.post("/api/org/integrations", json={"kind": "operator_api", "name": "Internal"}).json()
    for url in ("http://api.operator.example", "https://127.0.0.1", "https://10.0.0.5", "https://user:pw@api.example.com",
                "ftp://api.example.com", "https://localhost"):
        r = b.put(f"/api/org/integrations/{cfg['id']}", json={"base_url": url})
        assert r.status_code == 422, url
    b.put(f"/api/org/integrations/{cfg['id']}", json={"base_url": "https://api.operator.example", "credential": SECRET,
                                                     "operator_authorised": True})
    monkeypatch.setattr(ic, "RESOLVER", lambda host, port, **_: [(None, None, None, None, ("169.254.169.254", port))])
    t = b.post(f"/api/org/integrations/{cfg['id']}/test").json()
    assert t["state"] == "connection_failed" and "non-public" in t["message"]


def test_successful_mocked_api_is_verified_and_sync_imports_into_this_org_only(client, mock_operator, caplog):
    from app import db as D
    from app.models_saas import IntegrationConfig

    caplog.set_level(logging.DEBUG)
    b, org_b = _starter_org(client.app)
    cfg = _configured(b)
    mock_operator["events"] = [{"external_event_id": "op-1", "external_charger_id": "ST-9", "timestamp": "2026-10-01T10:00:00Z",
                                "event_type": "fault", "fault_category": "ground_fault", "site_code": "MG", "site_name": "MG Road"}]
    t = b.post(f"/api/org/integrations/{cfg['id']}/test").json()
    assert t["ok"] and t["state"] == "connected" and t["events_available"] == 1
    assert "Nothing was imported" in t["message"] and b.get("/api/tickets").json() == []
    s = b.post(f"/api/org/integrations/{cfg['id']}/sync").json()
    assert s["accepted"] == 1
    tickets = b.get("/api/tickets?source=operator_api").json()
    assert len(tickets) == 1 and tickets[0]["source"] == "operator_api"
    assert not [t for t in client.get("/api/tickets?source=operator_api").json()]      # never in another organisation
    st = b.get("/api/sources/status").json()["sources"]["operator_api"]
    assert st["state"] == "RECEIVING DATA"
    # secrets: encrypted at rest, masked in responses, absent from logs
    listing = b.get("/api/org/integrations").json()
    assert SECRET not in str(listing) and listing["connections"][0]["credential"] == {"set": True, "set_at": listing["connections"][0]["credential"]["set_at"], "masked": "••••••••"}
    with D.session_scope() as sess:
        row = sess.scalar(select(IntegrationConfig).where(IntegrationConfig.org_id == org_b))
        assert row.secret_ciphertext and SECRET not in row.secret_ciphertext
    assert SECRET not in caplog.text


def test_paid_plan_alone_never_connects_an_integration(client, mock_operator):
    b, _ = _starter_org(client.app)
    cfg = b.post("/api/org/integrations", json={"kind": "operator_api", "name": "Operator X"}).json()
    cfg = b.put(f"/api/org/integrations/{cfg['id']}", json={"base_url": "https://api.operator.example", "credential": SECRET}).json()
    assert cfg["state"] == "draft"                                      # authorisation not confirmed
    t = b.post(f"/api/org/integrations/{cfg['id']}/test").json()
    assert not t["ok"] and "authorised" in t["message"] and t["state"] == "draft"
    assert b.post(f"/api/org/integrations/{cfg['id']}/sync").status_code == 409


def test_changing_the_configuration_requires_a_new_verification(client, mock_operator):
    b, _ = _starter_org(client.app)
    cfg = _configured(b)
    assert b.post(f"/api/org/integrations/{cfg['id']}/test").json()["state"] == "connected"
    cfg = b.put(f"/api/org/integrations/{cfg['id']}", json={"base_url": "https://api2.operator.example"}).json()
    assert cfg["state"] == "configured"
    cfg = b.post(f"/api/org/integrations/{cfg['id']}/disable").json()
    assert cfg["state"] == "disabled"


def test_env_operator_credentials_are_not_shared_with_organisations(client, settings_env):
    settings_env(operator_api_base_url="https://env-operator.example", operator_api_key="env-secret-key-123")
    b, _ = _starter_org(client.app)
    assert b.post("/api/integrations/operator/test").status_code == 409
    assert b.get("/api/sources/status").json()["sources"]["operator_api"]["state"] == "NOT CONFIGURED"


def test_ssrf_check_unit():
    from app.security.ssrf import UnsafeUrl, check_url

    assert check_url("https://api.example.com/v1/", resolver=_public_dns) == "https://api.example.com/v1"
    for bad in ("https://192.168.1.10", "https://[::1]", "https://0.0.0.0", "https://100.64.1.1", "javascript:alert(1)"):
        with pytest.raises(UnsafeUrl):
            check_url(bad, resolver=lambda h, p, **_: [(None, None, None, None, (h.strip("[]"), p))])


def test_ocpp_test_point_and_real_charge_point_stay_distinct(client):
    """Local WebSocket: the SIMOCPP test point is labelled test traffic and never verifies the connection or
    counts as a managed charger; a real charge point on the organisation's endpoint verifies it."""
    import asyncio
    import socket

    import websockets
    from ocpp.v16 import ChargePoint as CP
    from ocpp.v16 import call

    from app.integrations.ocpp_central import serve

    b, org_b = _starter_org(client.app)
    cfg = b.post("/api/org/integrations", json={"kind": "ocpp", "name": "Depot chargers"}).json()
    assert cfg["state"] == "draft" and cfg["ocpp"]["endpoint"].endswith("/<charge_point_id>")
    key = cfg["ocpp"]["endpoint"].split("/")[-2]
    cfg = b.put(f"/api/org/integrations/{cfg['id']}", json={"operator_authorised": True}).json()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    async def boot(path, vendor):
        async with websockets.connect(f"ws://127.0.0.1:{port}/{path}", subprotocols=["ocpp1.6"]) as ws:
            cp = CP(path.split("/")[-1], ws)
            task = asyncio.create_task(cp.start())
            res = await cp.call(call.BootNotification(charge_point_model="M1", charge_point_vendor=vendor))
            task.cancel()
            return res

    async def scenario():
        stop = asyncio.Event()
        server = asyncio.create_task(serve("127.0.0.1", port, stop))
        await asyncio.sleep(0.3)
        await boot(f"{key}/SIMOCPP-1", "ChargeSight-Sim")
        test_only = b.post(f"/api/org/integrations/{cfg['id']}/test").json()
        refused = None
        try:
            await boot("not-a-key/CP-X", "Vendor")
        except Exception as e:                                       # unknown endpoint key: connection refused
            refused = type(e).__name__
        await boot(f"{key}/CP-REAL-1", "RealVendor")
        stop.set()
        await server
        return test_only, refused

    test_only, refused = asyncio.run(scenario())
    assert test_only["ok"] is False and "test simulator" in test_only["message"]
    assert refused is not None
    got = b.get(f"/api/org/integrations/{cfg['id']}").json()
    assert got["state"] == "connected" and got["ocpp"]["real_charge_points"] == 1 and got["ocpp"]["test_charge_points"] == 1
    usage = b.get("/api/entitlements").json()["usage"]
    assert usage["chargers"] == 1                                    # the test point is not a managed charger
    ids = {c["id"] for c in b.get("/api/chargers?source=ocpp").json()}
    assert any(i.endswith("CP-REAL-1") for i in ids) and not [c for c in client.get("/api/chargers?source=ocpp").json()]
