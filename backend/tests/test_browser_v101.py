"""v10.1 real-browser checks: visible Sign In / Sign Up, Open Workspace and Start Free Demo flows, safe redirects,
signed-in handling without loops, sign-out and Back navigation."""
from __future__ import annotations

import time
from urllib.parse import urlparse

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
from tests.test_browser_map import server  # noqa: E402,F401
from tests.test_browser_saas import PW, _page, _signup, browser  # noqa: E402,F401


def _signin(pg, base, email, path="/signin"):
    pg.goto(base + path)
    pg.fill("[name=email]", email)
    pg.fill("[name=password]", PW)
    pg.click("#siBtn")


def test_landing_header_shows_sign_in_sign_up_and_open_workspace(browser):
    pg = _page(browser)
    pg.goto(browser.base + "/")
    hdr = pg.locator("#siteHeader")
    assert hdr.locator('a[href="/signin"]').inner_text() == "Sign in"
    assert hdr.locator('a[href="/signup"]').inner_text() == "Sign up"
    assert hdr.locator('a:has-text("Open workspace")').get_attribute("href") == "/signin?next=%2Fapp"
    hdr.locator('a[href="/signin"]').click()
    pg.wait_for_url("**/signin")
    for sel in ("[name=email]", "[name=password]", ".pw-toggle", 'a[href="/forgot-password"]', 'a[href="/signup"]'):
        assert pg.locator(sel).count() >= 1, sel
    pg.goto(browser.base + "/")
    pg.locator('#siteHeader a[href="/signup"]').click()
    pg.wait_for_url("**/signup")
    assert pg.locator("#suBtn").is_visible() and pg.locator('form a[href="/signin"]').count() == 1
    # mobile: the same links in the accessible menu
    pg.set_viewport_size({"width": 390, "height": 844})
    pg.goto(browser.base + "/")
    pg.click("#menuToggle")
    assert pg.get_attribute("#menuToggle", "aria-expanded") == "true"
    assert pg.locator('#siteHeader a[href="/signin"]').is_visible() and pg.locator('#siteHeader a[href="/signup"]').is_visible()
    assert not pg.errors


def test_open_workspace_and_protected_views_require_sign_in(browser):
    pg = _page(browser)
    _signup(pg, browser.base, "meera.ws@sunrise.example", org="Workspace Flow Org")
    pg.click("#userBtn")
    pg.click("#signOut")
    pg.wait_for_url("**/signin?signed_out=1")
    pg.goto(browser.base + "/")
    pg.click('#siteHeader a:has-text("Open workspace")')
    pg.wait_for_url("**/signin?next=%2Fapp")
    # a deep link keeps its view across the sign-in round trip
    pg.goto(browser.base + "/app#billing")
    pg.wait_for_url("**/signin?next=/app#billing")
    pg.fill("[name=email]", "meera.ws@sunrise.example")                      # sign in on the redirected page itself
    pg.fill("[name=password]", PW)
    pg.click("#siBtn")
    pg.wait_for_url(lambda u: urlparse(u).path == "/app" and urlparse(u).fragment == "billing", timeout=15000)
    # the authenticated header offers the workspace directly
    pg.goto(browser.base + "/")
    assert pg.locator('#siteHeader a:has-text("Open workspace")').get_attribute("href") == "/app"
    assert "Signed in as" in pg.locator("#siteHeader").inner_text()
    # visiting Sign In while signed in shows a notice instead of a redirect loop
    pg.goto(browser.base + "/signin")
    pg.wait_for_selector("#siSwitch")
    assert "already signed in" in pg.locator(".auth-card").inner_text() and pg.url.endswith("/signin")
    # sign out from the public header, then Back cannot show the console
    pg.goto(browser.base + "/")
    pg.click("#hdrSignOut")
    pg.wait_for_selector('#siteHeader a[href="/signup"]', timeout=15000)           # page reloaded in the signed-out state
    assert pg.request.get(browser.base + "/api/auth/me").status == 401
    pg.goto(browser.base + "/app#dashboard")
    pg.wait_for_url("**/signin?next=/app#dashboard")
    pg.go_back()
    time.sleep(0.6)
    assert "/app" not in pg.url.split("?")[0] or pg.locator("#siBtn").count() == 1
    assert not pg.errors


def test_redirect_parameter_cannot_leave_the_site(browser):
    pg = _page(browser)
    _signup(pg, browser.base, "meera.redir@sunrise.example", org="Redirect Org")
    pg.click("#userBtn")
    pg.click("#signOut")
    pg.wait_for_url("**/signin?signed_out=1")
    for bad in ("https://evil.example/app", "//evil.example/app", "/app.evil.example", "/\\\\evil.example"):
        _signin(pg, browser.base, "meera.redir@sunrise.example", path="/signin?next=" + bad)
        pg.wait_for_url(lambda u: urlparse(u).path == "/app", timeout=15000)      # path check, not a substring of the query
        assert urlparse(pg.url).netloc == urlparse(browser.base).netloc, (bad, pg.url)
        pg.click("#userBtn")
        pg.click("#signOut")
        pg.wait_for_url("**/signin?signed_out=1")
    assert not pg.errors


def test_invalid_credentials_show_an_error_and_do_not_sign_in(browser):
    pg = _page(browser)
    _signup(pg, browser.base, "meera.badpw@sunrise.example", org="Bad Password Org")
    pg.click("#userBtn")
    pg.click("#signOut")
    pg.wait_for_url("**/signin?signed_out=1")
    pg.fill("[name=email]", "meera.badpw@sunrise.example")
    pg.fill("[name=password]", "not-the-password-1")
    pg.click("#siBtn")
    pg.wait_for_function("document.querySelector('#siMsg').innerText.includes('incorrect')")
    assert pg.request.get(browser.base + "/api/auth/me").status == 401
    assert not pg.errors


def test_start_free_demo_requires_an_account_then_opens_the_private_demo(browser):
    pg = _page(browser)
    pg.goto(browser.base + "/")
    pg.click('.hero a:has-text("Start free demo")')
    pg.wait_for_url("**/demo")
    text = pg.locator(".auth-card").inner_text()
    assert "SIMULATED" in text and pg.locator('a[href="/signup?intent=demo"]').count() == 1
    assert pg.request.get(browser.base + "/api/auth/me").status == 401                  # nobody was signed in silently
    pg.click('a[href="/signup?intent=demo"]')
    pg.wait_for_url("**/signup?intent=demo")
    pg.fill("[name=full_name]", "Meera Iyer")
    pg.fill("[name=email]", "meera.demo@sunrise.example")
    pg.fill("[name=password]", PW)
    pg.fill("[name=confirm_password]", PW)
    pg.fill("[name=organisation_name]", "Demo Flow Org")
    pg.select_option("[name=organisation_type]", "cpo")
    pg.check("[name=accept_terms]")
    pg.click("#suBtn")
    pg.wait_for_url(lambda u: urlparse(u).path == "/app", timeout=120000)
    pg.wait_for_selector(".pres-banner", timeout=60000)
    me = pg.request.get(browser.base + "/api/auth/me").json()
    assert me["organisation"]["workspace_kind"] == "presentation" and me["entitlements"]["plan_key"] == "demo"
    assert {o["workspace_kind"] for o in me["organisations"]} == {"customer", "presentation"}
    assert not pg.errors
