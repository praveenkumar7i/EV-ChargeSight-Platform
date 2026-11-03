"""v9 real-browser tests (headless Chromium): public website, sign-up, sign-in, onboarding, demo checkout,
subscription expiry, tenant isolation and integration setup. Skipped when Playwright is not installed."""
from __future__ import annotations

import time

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
from tests.test_browser_map import server  # noqa: E402,F401  (reuse the live test server fixture)

PW = "Sunrise-EV-2026!"


@pytest.fixture
def browser(server):  # noqa: F811
    with playwright_sync.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        b.base = server
        yield b
        b.close()


def _page(browser, w=1400, h=950):
    ctx = browser.new_context(viewport={"width": w, "height": h})
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    return pg


def _signup(pg, base, email, org="Sunrise EV Networks"):
    pg.goto(base + "/signup")
    pg.fill("[name=full_name]", "Meera Iyer")
    pg.fill("[name=email]", email)
    pg.fill("[name=password]", PW)
    pg.fill("[name=confirm_password]", PW)
    pg.fill("[name=organisation_name]", org)
    pg.select_option("[name=organisation_type]", "cpo")
    pg.check("[name=accept_terms]")
    pg.click("#suBtn")
    pg.wait_for_url("**/app#onboarding", timeout=20000)
    pg.wait_for_selector(".wizard-steps", timeout=20000)


def test_landing_and_pricing_pages(browser):
    pg = _page(browser)
    pg.goto(browser.base + "/")
    pg.wait_for_selector("#planPreview .plan", timeout=10000)
    assert "Know which charger to fix next" in pg.locator("h1").inner_text()
    assert pg.locator("#planPreview .plan").count() == 5
    for href in ("/signup", "/pricing", "/features", "/enterprise", "/signin", "/privacy", "/terms"):
        assert pg.locator(f'a[href="{href}"]').count() >= 1, href
    pg.goto(browser.base + "/pricing")
    pg.wait_for_selector("#planGrid .plan", timeout=10000)
    grid = pg.locator("#planGrid").inner_text()
    assert pg.locator("#planGrid .plan").count() == 5
    for text in ("₹5,389.20 billed every 12 months", "₹16,189.20 billed every 12 months", "₹53,989.20 billed every 12 months"):
        assert text in grid, text                                                       # server quotes, 12 months
    assert "14,999" not in pg.content()
    pg.click('#period [data-m="6"]')
    assert "₹2,844.30 billed every 6 months" in pg.locator("#planGrid").inner_text()
    pg.click('#period [data-m="1"]')
    assert "Billed monthly: ₹499.00" in pg.locator("#planGrid").inner_text()
    assert "Recommended starting plan" in pg.locator("#planGrid").inner_text()
    pg.fill("#chNum", "4")
    pg.dispatch_event("#chNum", "input")
    assert "Starter" in pg.locator("#calcOut").inner_text()
    pg.fill("#chNum", "20")
    pg.dispatch_event("#chNum", "input")
    assert "Growth" in pg.locator("#calcOut").inner_text()
    pg.fill("#siteNum", "6")
    pg.dispatch_event("#siteNum", "input")
    assert "Professional" in pg.locator("#calcOut").inner_text()
    pg.fill("#chNum", "400")
    pg.dispatch_event("#chNum", "input")
    assert "Enterprise" in pg.locator("#calcOut").inner_text()
    assert "Negotiated" in pg.locator("#compare").inner_text()
    assert not pg.errors


def test_mobile_layout_has_no_horizontal_scroll(browser):
    pg = _page(browser, 390, 844)
    for p in ("/", "/pricing", "/signup"):
        pg.goto(browser.base + p)
        time.sleep(0.8)
        assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), p
    pg.goto(browser.base + "/")
    pg.click("#menuToggle")
    assert pg.locator(".site-nav").is_visible()


def test_signup_validation_signin_errors_and_signout(browser):
    pg = _page(browser)
    pg.goto(browser.base + "/signup")
    pg.click("#suBtn")
    assert "Enter your full name" in pg.locator("#err-full_name").inner_text()
    assert pg.locator("#err-accept_terms").inner_text()
    _signup(pg, browser.base, "meera.signin@sunrise.example")
    pg.click("#userBtn")
    pg.click("#signOut")
    pg.wait_for_url("**/signin?signed_out=1")
    pg.fill("[name=email]", "meera.signin@sunrise.example")
    pg.fill("[name=password]", "wrong-password-1")
    pg.click("#siBtn")
    pg.wait_for_function("document.querySelector('#siMsg').innerText.includes('incorrect')")
    pg.fill("[name=password]", PW)
    pg.click(".pw-toggle")
    assert pg.get_attribute("[name=password]", "type") == "text"
    pg.click("#siBtn")
    pg.wait_for_url("**/app#onboarding", timeout=15000)
    pg.goto(browser.base + "/app#dashboard")
    assert not pg.errors


def test_demo_onboarding_checkout_receipt_and_isolation(browser):
    pg = _page(browser)
    _signup(pg, browser.base, "meera.flow@sunrise.example")
    pg.fill("[name=sites_estimate]", "4")
    pg.fill("[name=chargers_estimate]", "30")
    pg.click("#next")
    pg.wait_for_selector("[name=uc]")
    pg.check('[value="fleet_monitoring"]')
    pg.click("#next")
    pg.wait_for_selector("[name=mode]")
    pg.check('[name=mode][value="demo"]')
    pg.click("#mkDemo")
    pg.wait_for_selector("#modeDetail .note.ok", timeout=90000)
    pg.click("#next")
    pg.wait_for_selector("#slaF")
    pg.click("#next")
    pg.wait_for_selector(".checklist")
    assert "SIMULATED" in pg.locator(".checklist").inner_text()
    pg.click("#next")
    pg.click("#next")
    pg.wait_for_function("location.hash === '#dashboard'")
    # tenant isolation in the UI: only this organisation's simulated fleet and tickets
    pg.goto(browser.base + "/app#tickets")
    pg.wait_for_selector("table", timeout=15000)
    time.sleep(1)
    text = pg.locator("#view").inner_text()
    assert "TKT-SUNR" in text and "TKT-00001" not in text
    # integration setup without credentials shows the truthful state
    pg.goto(browser.base + "/app#integrations")
    pg.wait_for_selector("#conns", timeout=10000)
    time.sleep(1)
    assert "does not include configured integrations" in pg.locator("#conns").inner_text()
    # demo checkout
    pg.goto(browser.base + "/app#billing")
    pg.wait_for_selector('[data-plan="growth"]', timeout=10000)
    pg.click('[data-m="1"]')
    pg.click('[data-plan="growth"]')
    pg.wait_for_selector("#bf")
    assert "₹1,499.00" in pg.locator(".modal").inner_text()
    pg.click("#go")
    pg.wait_for_selector("#pmForm")                                   # v10.2: professional payment step before confirmation
    assert "Select a Payment Option" in pg.locator(".modal").inner_text()
    pg.click('[data-upi="success@test"]')
    pg.click("#payNext")
    pg.wait_for_selector("#ack")
    assert "Review Subscription Order" in pg.locator(".modal").inner_text()
    assert pg.is_disabled("#confirm")
    pg.check("#ack")
    pg.click("#confirm")
    pg.wait_for_selector("#toBilling", timeout=10000)
    assert "Order Confirmed" in pg.locator(".modal").inner_text()
    pg.click("#toBilling")
    pg.wait_for_selector("[data-rec]", timeout=10000)
    pg.click("[data-rec]")
    pg.wait_for_selector(".receipt")
    assert "Receipt" in pg.locator(".receipt").inner_text() and "PAID" in pg.locator(".receipt").inner_text()
    pg.click("#cl")
    # now on Growth: add an operator API connection without credentials -> Not configured
    pg.goto(browser.base + "/app#integrations")
    pg.wait_for_selector("#newConn", timeout=10000)
    pg.fill("#newConn [name=name]", "Operator feed")
    pg.click("#newConn button")
    pg.wait_for_selector("[data-conn]", timeout=10000)
    assert "Not configured" in pg.locator("[data-conn]").inner_text()
    assert not pg.errors


def test_subscription_expiry_makes_the_workspace_read_only(browser, server):  # noqa: F811
    import httpx

    from tests.conftest import LEGACY_OWNER, ensure_user

    pg = _page(browser)
    _signup(pg, browser.base, "meera.expiry@sunrise.example", org="Expiry Test Org")
    me = pg.request.get(browser.base + "/api/auth/me").json()
    org_id, csrf = me["organisation"]["id"], me["csrf_token"]
    h = {"X-CSRF-Token": csrf, "Content-Type": "application/json"}
    chk = pg.request.post(browser.base + "/api/billing/checkout", data='{"plan_key":"starter","billing_months":1}', headers=h).json()
    pg.request.post(browser.base + f"/api/billing/checkout/{chk['id']}/payment-method",
                    data='{"method":"upi","upi_app":"gpay","upi_id":"success@test"}', headers=h)
    pg.request.post(browser.base + f"/api/billing/checkout/{chk['id']}/complete", data='{"acknowledge_simulation":true}',
                    headers={**h, "Idempotency-Key": "browser-expiry-1"})
    ensure_user(*LEGACY_OWNER, org_id=1, role="owner", platform_admin=True)
    adm = httpx.Client()
    adm.headers["X-CSRF-Token"] = adm.post(server + "/api/auth/signin", json={"email": LEGACY_OWNER[0], "password": LEGACY_OWNER[1]}).json()["csrf_token"]
    for action in ("reach_renewal_date", "end_grace_period"):
        assert adm.post(server + f"/api/admin/organisations/{org_id}/simulate-time", json={"action": action, "reason": "browser test"}).status_code == 200
    pg.goto(browser.base + "/app#dashboard")
    pg.wait_for_selector("#acctBanner .note.warn", timeout=15000)
    assert "Read-only workspace" in pg.locator("#acctBanner").inner_text()
    assert "expired" in pg.locator(".plan-chip").inner_text().lower()
    adm.close()
    assert not pg.errors
