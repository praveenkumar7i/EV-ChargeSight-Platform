"""v9 authentication and session security."""
from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.conftest import sign_in

SIGNUP = dict(full_name="Asha Rao", email="Asha@GridVolt.example", password="Gr1d-Volt-Secure!", confirm_password="Gr1d-Volt-Secure!",
              organisation_name="GridVolt Charging", organisation_type="cpo", country="India", accept_terms=True)


def _signup(c, **over):
    r = c.post("/api/auth/signup", json={**SIGNUP, **over})
    if r.status_code == 200:
        c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return r


def test_signup_creates_user_org_owner_and_demo_subscription(anon_client):
    from app import db as D
    from app.models_saas import Membership, Organisation, Subscription, User

    r = _signup(anon_client)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["user"]["email"] == "asha@gridvolt.example"            # normalised
    assert d["role"] == "owner" and d["organisation"]["name"] == "GridVolt Charging"
    assert d["entitlements"]["plan_key"] == "demo" and d["entitlements"]["state"] == "demo"
    assert d["verification"]["email_sent"] is False                    # never claims an e-mail was sent
    assert not d["user"]["email_verified"]                             # labelled unverified
    cookie = r.headers["set-cookie"].lower()
    assert "cs_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    with D.session_scope() as s:
        u = s.scalar(select(User).where(User.email == "asha@gridvolt.example"))
        org = s.scalar(select(Organisation).where(Organisation.name == "GridVolt Charging"))
        assert org.id != 1 and not org.is_system_demo
        assert s.scalar(select(Membership).where(Membership.user_id == u.id)).role == "owner"
        assert s.scalar(select(Subscription).where(Subscription.org_id == org.id)).state == "demo"


def test_password_is_hashed_never_stored_in_plain_text(anon_client):
    from app import db as D
    from app.models_saas import User

    _signup(anon_client)
    with D.session_scope() as s:
        u = s.scalar(select(User).where(User.email == "asha@gridvolt.example"))
        assert u.password_hash.startswith("$argon2id$")
        assert SIGNUP["password"] not in u.password_hash


def test_duplicate_email_is_rejected_case_insensitively(anon_client):
    assert _signup(anon_client).status_code == 200
    anon_client.cookies.clear()
    r = _signup(anon_client, email="ASHA@gridvolt.EXAMPLE", organisation_name="Another")
    assert r.status_code == 422
    assert "already exists" in r.json()["detail"]["fields"]["email"]


@pytest.mark.parametrize("over, field", [
    ({"password": "short", "confirm_password": "short"}, "password"),
    ({"password": "alllowercase1", "confirm_password": "alllowercase1"}, "password"),   # < 16 chars, 2 classes
    ({"confirm_password": "Different-Pass-99"}, "confirm_password"),
    ({"email": "not-an-email"}, "email"),
    ({"accept_terms": False}, "accept_terms"),
    ({"organisation_type": "bank"}, "organisation_type"),
    ({"full_name": ""}, "full_name"),
])
def test_signup_validation_messages(anon_client, over, field):
    r = _signup(anon_client, **over)
    assert r.status_code == 422 and field in r.json()["detail"]["fields"]


def test_invalid_credentials_get_one_generic_message(anon_client):
    _signup(anon_client)
    anon_client.cookies.clear()
    wrong = anon_client.post("/api/auth/signin", json={"email": "asha@gridvolt.example", "password": "Wrong-Password-1"})
    unknown = anon_client.post("/api/auth/signin", json={"email": "nobody@nowhere.example", "password": "Wrong-Password-1"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert "incorrect" in wrong.json()["detail"]["message"]


def test_signin_returns_next_step_and_signout_invalidates_the_session(anon_client):
    _signup(anon_client)
    anon_client.cookies.clear()
    d = sign_in(anon_client, "asha@gridvolt.example", SIGNUP["password"])
    assert d["next"] == "/app#onboarding"                              # onboarding not completed yet
    token = anon_client.cookies.get("cs_session")
    assert anon_client.get("/api/tickets").status_code == 200
    assert anon_client.post("/api/auth/signout").status_code == 200
    anon_client.cookies.set("cs_session", token)                       # replaying the old cookie
    assert anon_client.get("/api/tickets").status_code == 401


def test_expired_and_idle_sessions_are_rejected(anon_client):
    from app import db as D
    from app.clock import wall_now
    from app.models_saas import AuthSession

    _signup(anon_client)
    with D.session_scope() as s:
        sess = s.scalar(select(AuthSession))
        sess.expires_at = wall_now() - timedelta(seconds=1)
    assert anon_client.get("/api/tickets").status_code == 401
    anon_client.cookies.clear()
    sign_in(anon_client, "asha@gridvolt.example", SIGNUP["password"])
    with D.session_scope() as s:
        sess = s.scalars(select(AuthSession).order_by(AuthSession.id.desc())).first()
        sess.last_seen_at = wall_now() - timedelta(hours=5)              # idle longer than the 2 h default
    assert anon_client.get("/api/tickets").status_code == 401


def test_protected_endpoints_require_authentication(anon_client):
    from app.security.guard import RULES

    checked = 0
    for (method, path), rule in RULES.items():
        if rule.perm is None or "{" in path:
            continue
        r = anon_client.request(method, path, json={})
        assert r.status_code == 401, (method, path, r.status_code)
        checked += 1
    assert checked > 60


def test_every_api_route_has_an_explicit_guard_rule(anon_client):
    from app.security.guard import RULES

    paths = anon_client.app.openapi()["paths"]
    missing = [(m.upper(), p) for p, ops in paths.items() for m in ops if p.startswith("/api/") and (m.upper(), p) not in RULES]
    assert missing == []


def test_state_changing_requests_need_the_csrf_token(client):
    token = client.headers.pop("X-CSRF-Token")
    r = client.post("/api/sim/step", json={"n": 1})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "csrf_failed"
    client.headers["X-CSRF-Token"] = "wrong"
    assert client.post("/api/sim/step", json={"n": 1}).status_code == 403
    client.headers["X-CSRF-Token"] = token
    assert client.post("/api/sim/step", json={"n": 1}).status_code == 200


def test_cross_site_posts_are_refused(client):
    r = client.post("/api/sim/step", json={"n": 1}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "bad_origin"


def test_signin_is_rate_limited(anon_client, settings_env):
    settings_env(auth_signin_limit=3)
    _signup(anon_client)
    anon_client.cookies.clear()
    codes = [anon_client.post("/api/auth/signin", json={"email": "asha@gridvolt.example", "password": "nope-nope-1"}).status_code
             for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3] == 429


def test_forgot_password_is_generic_and_reset_flow_uses_the_dev_outbox(anon_client, settings_env, tmp_path, monkeypatch):
    from app.services import accounts

    settings_env(dev_outbox_enabled="true")
    monkeypatch.setattr(accounts, "outbox_dir", lambda: tmp_path / "outbox")
    _signup(anon_client)
    old_cookie = anon_client.cookies.get("cs_session")
    anon_client.cookies.clear()
    a = anon_client.post("/api/auth/forgot-password", json={"email": "asha@gridvolt.example"}).json()
    b = anon_client.post("/api/auth/forgot-password", json={"email": "unknown@nowhere.example"}).json()
    assert a == b and a["email_sent"] is False                          # no account enumeration, no false claim
    files = sorted((tmp_path / "outbox").glob("*.txt"))
    reset_msgs = [f for f in files if "Reset your ChargeSight password" in f.read_text(encoding="utf-8")]
    assert len(reset_msgs) == 1 and "NOT E-MAILED" in reset_msgs[0].read_text(encoding="utf-8")
    token = reset_msgs[0].read_text(encoding="utf-8").split("reset-password?token=")[1].split()[0]
    r = anon_client.post("/api/auth/reset-password", json={"token": token, "password": "Brand-New-Pass-42", "confirm_password": "Brand-New-Pass-42"})
    assert r.status_code == 200
    again = anon_client.post("/api/auth/reset-password", json={"token": token, "password": "Other-New-Pass-43", "confirm_password": "Other-New-Pass-43"})
    assert again.status_code == 400                                      # single use
    anon_client.cookies.set("cs_session", old_cookie)
    assert anon_client.get("/api/tickets").status_code == 401           # sessions revoked after reset
    anon_client.cookies.clear()
    sign_in(anon_client, "asha@gridvolt.example", "Brand-New-Pass-42")


def test_email_verification_link(anon_client, settings_env, tmp_path, monkeypatch):
    from app.services import accounts

    settings_env(dev_outbox_enabled="true")
    monkeypatch.setattr(accounts, "outbox_dir", lambda: tmp_path / "outbox")
    d = _signup(anon_client).json()
    assert d["verification"]["delivery"] == "dev_outbox"
    msg = next((tmp_path / "outbox").glob("*.txt")).read_text(encoding="utf-8")
    token = msg.split("verify-email?token=")[1].split()[0]
    assert anon_client.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    assert anon_client.get("/api/auth/me").json()["user"]["email_verified"] is True


def test_passwords_and_tokens_never_reach_the_logs(anon_client, caplog):
    caplog.set_level(logging.DEBUG)
    r = _signup(anon_client)
    token = anon_client.cookies.get("cs_session")
    anon_client.post("/api/auth/signin", json={"email": "asha@gridvolt.example", "password": "Wrong-Password-1"})
    text = caplog.text + "".join(str(rec.args) for rec in caplog.records)
    assert SIGNUP["password"] not in text and "Wrong-Password-1" not in text
    assert token not in text and r.json()["csrf_token"] not in text


def test_me_requires_a_session_and_reports_role_and_entitlements(anon_client):
    assert anon_client.get("/api/auth/me").status_code == 401
    _signup(anon_client)
    me = anon_client.get("/api/auth/me").json()
    assert me["role"] == "owner" and "billing.manage" in me["permissions"]
    assert me["entitlements"]["limits"]["users"] == 2 and me["entitlements"]["access"] == "full"


def test_change_password_signs_out_other_sessions(anon_client):
    from fastapi.testclient import TestClient

    _signup(anon_client)
    other = TestClient(anon_client.app)
    sign_in(other, "asha@gridvolt.example", SIGNUP["password"])
    r = anon_client.post("/api/auth/change-password", json={"current_password": SIGNUP["password"], "new_password": "Changed-Pass-77",
                                                            "confirm_password": "Changed-Pass-77"})
    assert r.status_code == 200
    assert other.get("/api/tickets").status_code == 401
    assert anon_client.get("/api/tickets").status_code == 200
