"""v9.1 plan catalogue update: idempotent, non-destructive, history-preserving."""
from __future__ import annotations

from sqlalchemy import select

from tests.test_v9_billing import _buy
from tests.test_v9_tenancy import make_org


def _set_v9_0_catalogue(s):
    """Put the plan rows back to the v9.0 seed values (as in a database created by the first v9 release)."""
    from app.models_saas import SubscriptionPlan
    from app.services import settings_store

    for key, v in {"starter": (499900, {"chargers": 25, "users": 3, "integrations": 1}),
                   "growth": (1499900, {"chargers": 100, "users": 15, "integrations": 5}),
                   "demo": (0, {"chargers": 0, "users": 3, "integrations": 0})}.items():
        p = s.get(SubscriptionPlan, key)
        p.monthly_price_paise, p.limits = v
        if key == "starter":
            p.features = ["fleet_dashboard", "tickets", "priority_engine", "manual_assignment", "sla_tracking", "analytics_basic",
                          "reports_standard", "public_map", "simulator", "validation", "integrations"]
    settings_store.put(s, "plan_catalogue", {"version": "9.0"})


def test_catalogue_upgrade_keeps_receipts_grandfathers_active_subscriptions_and_respects_edits(client, tmp_path):
    from app import db as D
    from app.models_saas import DemoCheckout, DemoReceipt, Subscription, SubscriptionEvent, SubscriptionPlan
    from app.services import billing, settings_store

    b, org_b, _ = make_org(client.app, fleet=False)
    # a customer who bought the old Starter plan (v9.0 prices) before the update
    with D.session_scope() as s:
        _set_v9_0_catalogue(s)
        s.get(SubscriptionPlan, "growth").monthly_price_paise = 1234500        # an administrator edited Growth
    from app.services.entitlements import invalidate
    invalidate()
    chk, res = _buy(b, "starter", 1)
    assert res["receipt"]["total"] == "₹4,999.00"
    receipt_no = res["receipt"]["number"]
    with D.session_scope() as s:
        out = billing.upgrade_catalogue(s)
        assert out["updated"] == ["demo", "starter"] and out["customised"] == ["growth"]
        assert s.get(SubscriptionPlan, "starter").monthly_price_paise == 49900
        assert s.get(SubscriptionPlan, "growth").monthly_price_paise == 1234500            # edited row untouched
        assert billing.upgrade_catalogue(s) is None                                       # idempotent
        rec = s.scalar(select(DemoReceipt).where(DemoReceipt.number == receipt_no))
        assert rec.total_paise == 499900                                                   # history never rewritten
        assert s.scalar(select(DemoCheckout).where(DemoCheckout.id == chk["id"])).total_paise == 499900
        sub = s.scalar(select(Subscription).where(Subscription.org_id == org_b))
        assert sub.custom_limits == {"chargers": 25, "users": 3, "integrations": 1} and "integrations" in sub.custom_features
        assert s.scalar(select(SubscriptionEvent).where(SubscriptionEvent.org_id == org_b,
                                                        SubscriptionEvent.kind == "catalogue_grandfathered")) is not None
        info = settings_store.get(s, "plan_catalogue", None)
        assert info["version"] == billing.CATALOGUE_VERSION and info["updated"] == ["demo", "starter"]
    invalidate()
    ent = b.get("/api/entitlements").json()
    assert ent["limits"]["chargers"] == 25 and "integrations" in ent["features"]          # grandfathered until next checkout
    ov = b.get("/api/billing").json()["subscription"]
    assert ov["grandfathered"] and ov["current_period_paid"]["total"] == "₹4,999.00"
    assert ov["next_payment"]["total"] == "₹499.00"                                        # renewal uses the current catalogue
    # the next checkout moves the subscription onto the current catalogue
    _buy(b, "professional", 1, key="idem-after-update")
    ent = b.get("/api/entitlements").json()
    assert ent["limits"] == {"chargers": 100, "users": 15, "sites": 10, "integrations": 10}
    assert not b.get("/api/billing").json()["subscription"]["grandfathered"]


def test_new_database_gets_the_current_catalogue_and_records_the_version(client):
    from app import db as D
    from app.models_saas import SubscriptionPlan
    from app.services import billing, settings_store

    with D.session_scope() as s:
        billing.ensure_catalogue(s)
        keys = [p.key for p in s.scalars(select(SubscriptionPlan).order_by(SubscriptionPlan.sort))]
        assert keys == ["demo", "starter", "growth", "professional", "enterprise"]
        assert settings_store.get(s, "plan_catalogue", None)["version"] == billing.CATALOGUE_VERSION


def test_legacy_demo_workspace_keeps_its_capabilities(client):
    ent = client.get("/api/entitlements").json()
    assert ent["limits"]["sites"] == 100 and {"integrations", "data_import", "reports_expanded", "admin_enhanced"} <= set(ent["features"])
    assert client.get("/api/reports/full.html").status_code == 200
