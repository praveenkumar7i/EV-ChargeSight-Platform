"""v9.2 checkout: TEST payment step, no bypass, no sensitive data, honest results, unchanged prices."""
from __future__ import annotations

import json
import logging

import pytest
from sqlalchemy import func, select

from tests.test_v9_billing import TEST_UPI, _pay
from tests.test_v9_tenancy import make_org

CARD = {"method": "credit_card", "card_name": "Test Holder", "card_number": "4111 1111 1111 1111", "card_expiry": "12/30"}


def _start(c, plan="starter", months=1, **extra):
    r = c.post("/api/billing/checkout", json={"plan_key": plan, "billing_months": months, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _complete(c, cid, key):
    return c.post(f"/api/billing/checkout/{cid}/complete", json={"acknowledge_simulation": True}, headers={"Idempotency-Key": key})


def test_paid_plan_cannot_be_claimed_without_the_payment_step(client):
    from app import db as D
    from app.models_saas import DemoReceipt

    b, org_b, _ = make_org(client.app, email="claim@ops.example", name="Claim Ops", fleet=False)
    chk = _start(b, "professional", 12)
    r = _complete(b, chk["id"], "claim-key-0001")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "payment_method_required"
    ent = b.get("/api/entitlements").json()
    assert ent["plan_key"] == "demo" and "reports_expanded" not in ent["features"]          # nothing granted
    with D.session_scope() as s:
        assert s.scalar(select(func.count(DemoReceipt.id)).where(DemoReceipt.org_id == org_b)) == 0
    assert b.get("/api/billing").json()["open_checkout"]["payment_ready"] is False


@pytest.mark.parametrize("months,total", [(1, "₹499.00"), (6, "₹2,844.30"), (12, "₹5,389.20")])
def test_server_prices_are_unchanged_and_client_prices_are_ignored(client, months, total):
    b, _, _ = make_org(client.app, email=f"price{months}@ops.example", name=f"Price {months}", fleet=False)
    chk = _start(b, "starter", months, total_paise=1, monthly_price_paise=1, discount_paise=999999)
    assert chk["display"]["total"] == total
    paid = b.post(f"/api/billing/checkout/{chk['id']}/payment-method", json={**TEST_UPI, "amount": 1, "total_paise": 1}).json()
    assert paid["display"]["total"] == total
    r = _complete(b, chk["id"], f"price-key-{months:04d}")
    assert r.status_code == 200 and r.json()["receipt"]["total"] == total


def test_each_test_payment_method_is_validated_and_masked(client):
    b, _, _ = make_org(client.app, email="methods@ops.example", name="Methods Ops", fleet=False)
    cid = _start(b)["id"]
    url = f"/api/billing/checkout/{cid}/payment-method"
    cases = [
        ({"method": "bitcoin"}, "method"),
        ({"method": "upi", "upi_app": "gpay", "upi_id": "meera@okaxis"}, "upi_id"),            # real-looking VPA refused
        ({"method": "upi", "upi_app": "unknown", "upi_id": "success@test"}, "upi_app"),
        ({**CARD, "card_number": "4242 4242 4242 4242"}, "card_number"),                       # valid Luhn, not a listed test card
        ({**CARD, "card_number": "4111 1111 1111 1112"}, "card_number"),                       # fails Luhn
        ({**CARD, "card_expiry": "01/20"}, "card_expiry"),
        ({**CARD, "card_expiry": "13/30"}, "card_expiry"),
        ({**CARD, "card_name": "1"}, "card_name"),
        ({"method": "netbanking", "bank": "hdfc"}, "bank"),
    ]
    for body, field in cases:
        r = b.post(url, json=body)
        assert r.status_code == 422, body
        d = r.json()["detail"]
        assert d["code"] == "payment_details" and field in d["fields"], (body, d)
        assert "4242" not in d["message"] and "okaxis" not in d["message"]                    # never echoes input
    ok = {
        "upi": ({**TEST_UPI, "upi_app": "phonepe"}, "UPI · PhonePe · success@test (test UPI ID)"),
        "credit_card": (CARD, "Test credit card · Visa ending 1111"),
        "debit_card": ({**CARD, "method": "debit_card", "card_number": "5555555555554444"}, "Test debit card · Mastercard ending 4444"),
        "netbanking": ({"method": "netbanking", "bank": "test_bank_approve"}, "Net banking · ChargeSight Test Bank (test)"),
    }
    for method, (body, label) in ok.items():
        r = b.post(url, json=body)
        assert r.status_code == 200, r.text
        assert r.json()["payment_method"] == method and r.json()["payment_display"] == label


def test_no_sensitive_payment_data_is_stored_logged_or_returned(client, caplog):
    from app import db as D
    from app.models_saas import AuditEvent, DemoCheckout, DemoReceipt, SubscriptionEvent

    caplog.set_level(logging.DEBUG)
    b, org_b, _ = make_org(client.app, email="secret@ops.example", name="Secret Ops", fleet=False)
    cid = _start(b, "growth", 1)["id"]
    r = b.post(f"/api/billing/checkout/{cid}/payment-method", json={**CARD, "cvv": "123", "otp": "445566", "pin": "9999"})
    assert r.status_code == 200
    res = _complete(b, cid, "secret-key-0001").json()
    overview = b.get("/api/billing").json()
    assert overview.pop("test_payment")["test_cards"]          # the published TEST hints are listed by design ...
    blobs = [json.dumps(r.json()), json.dumps(res), json.dumps(overview), caplog.text]   # ... what was entered is not
    with D.session_scope() as s:
        chk = s.get(DemoCheckout, cid)
        blobs.append(json.dumps({c.name: str(getattr(chk, c.name)) for c in DemoCheckout.__table__.columns}))
        for model in (AuditEvent, SubscriptionEvent, DemoReceipt):
            for row in s.scalars(select(model).where(model.org_id == org_b)):
                blobs.append(json.dumps({c.name: str(getattr(row, c.name)) for c in model.__table__.columns}))
        assert not any(c.name in ("cvv", "otp", "pin", "card_number") for c in DemoCheckout.__table__.columns)
    text = "\n".join(blobs)
    for secret in ("4111111111111111", "4111 1111 1111 1111", "12/30", "445566", "9999", "\"123\""):
        assert secret not in text, secret
    assert "ending 1111" in text                                                    # only the masked label


def test_declined_test_payment_changes_nothing_and_a_new_method_can_be_used(client):
    from app import db as D
    from app.models_saas import DemoReceipt, SubscriptionEvent

    b, org_b, _ = make_org(client.app, email="decline@ops.example", name="Decline Ops", fleet=False)
    cid = _start(b, "growth", 6)["id"]
    _pay(b, cid, {"method": "netbanking", "bank": "test_bank_decline"})
    r = _complete(b, cid, "decline-key-0001")
    assert r.status_code == 402 and r.json()["detail"]["code"] == "test_payment_declined"
    assert "No money moved" in r.json()["detail"]["message"]
    assert b.get("/api/entitlements").json()["plan_key"] == "demo"
    assert b.get(f"/api/billing/checkout/{cid}").json()["checkout"]["payment_ready"] is False      # must choose again
    assert _complete(b, cid, "decline-key-0002").json()["detail"]["code"] == "payment_method_required"
    for declined in ({"method": "upi", "upi_app": "gpay", "upi_id": "failure@test"},
                     {**CARD, "card_number": "4000 0000 0000 0002"}):
        _pay(b, cid, declined)
        assert _complete(b, cid, "decline-key-0003").status_code == 402
    _pay(b, cid, TEST_UPI)
    ok = _complete(b, cid, "decline-key-0003")
    assert ok.status_code == 200 and ok.json()["subscription"]["plan_key"] == "growth"
    with D.session_scope() as s:
        assert s.scalar(select(func.count(DemoReceipt.id)).where(DemoReceipt.org_id == org_b)) == 1
        assert s.scalar(select(func.count(SubscriptionEvent.id)).where(SubscriptionEvent.org_id == org_b,
                                                                       SubscriptionEvent.kind == "test_payment_declined")) == 3


def test_test_checkout_cannot_be_mistaken_for_a_real_payment(client, monkeypatch):
    import socket

    orig_connect = socket.socket.connect

    def no_network(self, address, *a, **k):
        # Allow local loopback (Windows asyncio ProactorEventLoop self-pipe uses socketpair on 127.0.0.1)
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "localhost", "::1"):
            return orig_connect(self, address, *a, **k)
        raise AssertionError("checkout must not open a network connection")
    monkeypatch.setattr(socket.socket, "connect", no_network)          # no gateway, bank or UPI call is possible
    b, _, _ = make_org(client.app, email="honest@ops.example", name="Honest Ops", fleet=False)
    cid = _start(b, "starter", 12)["id"]
    _pay(b, cid, CARD)
    res = _complete(b, cid, "honest-key-0001").json()
    rec = res["receipt"]
    assert rec["record_type"] == "TEST" and rec["payment_taken"] is False and rec["tax_invoice"] is False
    assert rec["payment_label"] == "Test credit card · Visa ending 1111"
    assert "No payment was taken" in rec["label"] and "Not a tax invoice" in rec["label"]
    chk = res["checkout"]
    assert chk["provider"] == "demo" and chk["record_type"] == "TEST"
    assert "not a bank, card or UPI transaction ID" in chk["provider_reference_label"]
    view = b.get(f"/api/billing/receipts/{rec['number']}").json()
    assert view["payment_taken"] is False and view["record_type"] == "TEST"


def test_payment_step_rejects_completed_expired_and_foreign_checkouts(client):
    from datetime import timedelta

    from app import db as D
    from app.models_saas import DemoCheckout

    b, _, _ = make_org(client.app, email="states@ops.example", name="States Ops", fleet=False)
    c, _, _ = make_org(client.app, email="other@ops.example", name="Other Ops", fleet=False)
    cid = _start(b)["id"]
    assert c.post(f"/api/billing/checkout/{cid}/payment-method", json=TEST_UPI).status_code == 404
    _pay(b, cid)
    assert _complete(b, cid, "states-key-0001").status_code == 200
    assert b.post(f"/api/billing/checkout/{cid}/payment-method", json=TEST_UPI).json()["detail"]["code"] == "not_pending"
    cid2 = _start(b, "growth", 1)["id"]
    with D.session_scope() as s:
        row = s.get(DemoCheckout, cid2)
        row.expires_at = row.created_at - timedelta(minutes=1)
    assert b.post(f"/api/billing/checkout/{cid2}/payment-method", json=TEST_UPI).json()["detail"]["code"] == "expired"


def test_free_and_enterprise_plans_stay_outside_the_paid_checkout(client):
    b, _, _ = make_org(client.app, email="free@ops.example", name="Free Ops", fleet=False)
    assert b.post("/api/billing/checkout", json={"plan_key": "enterprise", "billing_months": 12}).status_code == 422
    assert b.post("/api/billing/checkout", json={"plan_key": "demo", "billing_months": 1}).status_code in (409, 422)
    assert b.get("/api/entitlements").json()["plan_key"] == "demo"


def test_historical_checkouts_keep_their_records_and_are_labelled_test(client):
    from app import db as D
    from app.models_saas import DemoCheckout

    b, _, _ = make_org(client.app, email="history@ops.example", name="History Ops", fleet=False)
    cid = _start(b)["id"]
    _pay(b, cid)
    _complete(b, cid, "history-key-0001")
    with D.session_scope() as s:                         # emulate a checkout completed before v9.2 (no method step)
        row = s.get(DemoCheckout, cid)
        row.payment_method, row.payment_display = None, ""
    h = [x for x in b.get("/api/billing").json()["checkouts"] if x["id"] == cid][0]
    assert h["status"] == "completed" and h["total_paise"] == 49900 and "before v9.2" in h["record_note"]
