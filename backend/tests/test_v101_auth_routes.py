"""v10.1: public vs protected routes, server-side console guard, sign-in failures, sign-out, duplicate sign-up."""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_v9_tenancy import PW, make_org


def test_public_pages_are_open_and_console_redirects_without_a_session(anon_client):
    for path in ("/", "/features", "/pricing", "/enterprise", "/signin", "/signup", "/demo", "/forgot-password"):
        r = anon_client.get(path)
        assert r.status_code == 200, path
    r = anon_client.get("/app", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signin?next=/app"
    assert r.headers["cache-control"] == "no-store"
    anon_client.cookies.set("cs_session", "not-a-real-session")
    assert anon_client.get("/app", follow_redirects=False).status_code == 303          # a forged cookie is no session


def test_signed_in_console_is_served_and_revalidated(client):
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"          # revalidated on every load
    assert "ChargeSight" in r.text


def test_invalid_credentials_never_create_a_session(client):
    b, _, _ = make_org(client.app, email="badpw@ops.example", name="Bad Pw Ops", fleet=False)
    c = TestClient(client.app)
    r = c.post("/api/auth/signin", json={"email": "badpw@ops.example", "password": "wrong-password-123"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "invalid_credentials"
    assert not r.cookies and c.get("/api/auth/me").status_code == 401
    assert c.get("/app", follow_redirects=False).status_code == 303
    ok = c.post("/api/auth/signin", json={"email": "badpw@ops.example", "password": PW})
    assert ok.status_code == 200 and c.get("/app", follow_redirects=False).status_code == 200


def test_sign_out_invalidates_the_session_everywhere(client):
    b, _, _ = make_org(client.app, email="out@ops.example", name="Out Ops", fleet=False)
    assert b.get("/app", follow_redirects=False).status_code == 200
    assert b.post("/api/auth/signout").status_code == 200
    assert b.get("/api/auth/me").status_code == 401
    assert b.get("/app", follow_redirects=False).status_code == 303
    assert b.get("/api/tickets").status_code in (401, 403)


def test_duplicate_sign_up_is_refused(client):
    make_org(client.app, email="dup@ops.example", name="Dup Ops", fleet=False)
    c = TestClient(client.app)
    r = c.post("/api/auth/signup", json=dict(full_name="Someone Else", email="DUP@ops.example", password=PW, confirm_password=PW,
                                             organisation_name="Dup Two", organisation_type="cpo", country="India", accept_terms=True))
    assert r.status_code in (400, 409, 422) and c.get("/api/auth/me").status_code == 401


def test_presentation_demo_needs_authentication_and_stays_private(client, anon_client):
    assert anon_client.post("/api/auth/presentation-demo").status_code in (401, 403)
    b, org_b, _ = make_org(client.app, email="demo-a@ops.example", name="Demo A", fleet=False)
    pres = b.post("/api/auth/presentation-demo").json()["organisation"]
    assert pres["workspace_kind"] == "presentation"
    # the demo user cannot reach another organisation's billing or data
    assert b.post("/api/auth/switch-org", json={"org_id": 1}).status_code == 404
    assert b.get("/api/billing").json()["subscription"]["org_id"] == pres["id"]
    assert all(not t["code"].startswith("TKT-0") for t in b.get("/api/tickets").json())
