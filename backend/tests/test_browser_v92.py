"""v9.2 real-browser checks (headless Chromium): navy authentication pages only, unchanged console theme, no grey
form fields, wider sidebar, My Fleet empty state vs Presentation Demo, TEST payment step."""
from __future__ import annotations

import time

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
from tests.test_browser_map import server  # noqa: E402,F401
from tests.test_browser_saas import PW, _page, _signup, browser  # noqa: E402,F401

NAVY = "rgb(11, 18, 32)"            # #0b1220 auth page background
AUTH_FIELD = "rgb(13, 23, 38)"      # #0d1726 auth input background
PUBLIC_PAPER = "rgb(245, 247, 250)"  # unchanged light public pages
CONSOLE_BG = "rgb(12, 17, 27)"      # unchanged console background (#0c111b)
CONSOLE_SURFACE = "rgb(20, 27, 39)"  # unchanged console surface (#141b27)


def _bg(pg, sel="body"):
    return pg.evaluate(f"getComputedStyle(document.querySelector({sel!r})).backgroundColor")


def test_only_authentication_pages_use_the_navy_design(browser):
    pg = _page(browser)
    for path in ("/signin", "/signup", "/forgot-password", "/reset-password?token=x"):
        pg.goto(browser.base + path)
        pg.wait_for_selector(".auth-card")
        assert pg.evaluate("document.body.classList.contains('auth-page')"), path
        assert _bg(pg) == NAVY, path
        assert _bg(pg, ".auth-card") == "rgb(17, 28, 48)", path                       # #111c30 panel
        assert _bg(pg, ".auth-card input:not([type=checkbox])") == AUTH_FIELD, path
    for path in ("/", "/pricing", "/features", "/terms"):
        pg.goto(browser.base + path)
        pg.wait_for_selector("main section, main .page-head", timeout=10000)
        assert not pg.evaluate("document.body.classList.contains('auth-page')"), path
        assert _bg(pg) == PUBLIC_PAPER, path                                            # public pages unchanged
    assert not pg.errors


def test_auth_controls_still_work_with_the_new_design(browser):
    pg = _page(browser)
    pg.goto(browser.base + "/signin")
    pg.click("#siBtn")
    assert pg.locator("#err-email").inner_text() and pg.locator("#err-password").inner_text()      # validation visible
    assert pg.evaluate("getComputedStyle(document.querySelector('#err-email')).color") == "rgb(252, 165, 165)"
    pg.fill("[name=password]", "abc")
    pg.click(".pw-toggle")
    assert pg.get_attribute("[name=password]", "type") == "text"
    pg.goto(browser.base + "/signin")
    pg.wait_for_selector("[name=email]")
    pg.keyboard.press("Tab")
    for _ in range(12):
        if pg.evaluate("document.activeElement && document.activeElement.name") == "email":
            break
        pg.keyboard.press("Tab")
    outline = pg.evaluate("getComputedStyle(document.activeElement).boxShadow")
    assert "59, 130, 246" in outline                                                  # visible blue focus ring
    assert not pg.errors


def test_console_theme_inputs_and_sidebar_after_sign_in(browser):
    pg = _page(browser, 1400, 900)
    _signup(pg, browser.base, "meera.theme@sunrise.example", org="Theme Check Org")
    assert not pg.evaluate("document.body.classList.contains('auth-page')")
    assert _bg(pg) == CONSOLE_BG and _bg(pg, ".topbar") == CONSOLE_SURFACE              # console theme unchanged
    pg.wait_for_selector("[name=primary_contact_email]")
    fields = pg.evaluate("""[...document.querySelectorAll('#view input:not([type=checkbox]):not([type=radio]), #view select')]
                            .map(i => [i.name, i.type, getComputedStyle(i).backgroundColor])""")
    assert fields and all(bg == CONSOLE_SURFACE for _, _, bg in fields), fields          # no grey/white fields (e-mail included)
    # sidebar: ~260 px, labels on one line, every route still present
    assert pg.evaluate("document.querySelector('.nav').getBoundingClientRect().width") == 260
    heights = pg.evaluate("[...document.querySelectorAll('.nav a[data-route]:not(.hidden)')].map(a => [a.textContent.trim(), a.getBoundingClientRect().height])")
    assert all(h < 40 for _, h in heights), heights
    assert "Simulator & event timeline" in [t for t, _ in heights]
    assert pg.locator(".nav a[data-route]").count() == 17  # v10.3 added historical sessions route
    # disabled and read-only fields are styled on purpose, not left grey
    pg.evaluate("""() => { const i = document.createElement('input'); i.type = 'email'; i.disabled = true; i.id = 'probe';
                           document.querySelector('#view').appendChild(i); }""")
    assert pg.evaluate("getComputedStyle(document.querySelector('#probe')).borderStyle") == "dashed"
    # mobile: off-canvas navigation, no horizontal scroll
    pg.set_viewport_size({"width": 390, "height": 844})
    pg.goto(browser.base + "/app#dashboard")
    time.sleep(1.5)
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    assert pg.evaluate("document.querySelector('.nav').getBoundingClientRect().right") <= 1
    pg.click("#menuBtn")
    time.sleep(0.4)
    assert pg.evaluate("document.querySelector('.nav').getBoundingClientRect().left") >= 0
    assert pg.locator("#wsSwitch .ws-btn").is_visible()
    assert not pg.errors


def test_my_fleet_empty_state_and_presentation_demo_in_the_browser(browser):
    pg = _page(browser)
    _signup(pg, browser.base, "meera.present@sunrise.example", org="Present Check Org")
    pg.goto(browser.base + "/app#dashboard")
    pg.wait_for_selector(".empty-ws", timeout=15000)
    assert "no chargers yet" in pg.locator(".empty-ws").inner_text()
    pg.wait_for_function("document.querySelector('#simChipText').innerText.includes('no simulated fleet')", timeout=10000)
    assert "Every charger is clear" not in pg.locator("#alerts").inner_text()
    pg.click("#wsSwitch .ws-btn")
    pg.wait_for_selector(".pres-banner", timeout=120000)
    pg.wait_for_selector("#kpis .kpi .val", timeout=30000)
    s = pg.request.get(browser.base + "/api/fleet/summary?source=simulation").json()
    assert s["total_chargers"] == 40 and len(s["sites"]) == 6
    assert pg.locator("#wsSwitch").inner_text().count("Presentation Demo") == 1
    assert pg.locator("#cRestore").count() == 1                                    # restore offered in the demo workspace
    pg.click("#wsSwitch .ws-btn")                                                 # Back to My Fleet
    pg.wait_for_selector(".empty-ws", timeout=30000)
    assert pg.locator("#cRestore").count() == 0                                   # never offered in My Fleet
    assert not pg.errors


def test_checkout_payment_step_refuses_real_cards_in_the_browser(browser):
    pg = _page(browser)
    posted = []
    pg.on("request", lambda r: posted.append(r.post_data or "") if r.method == "POST" else None)
    _signup(pg, browser.base, "meera.card@sunrise.example", org="Card Check Org")
    pg.goto(browser.base + "/app#billing")
    pg.wait_for_selector('[data-plan="starter"]', timeout=10000)
    pg.click('[data-m="6"]')
    pg.click('[data-plan="starter"]')
    pg.wait_for_selector("#bf")
    assert "₹2,844.30" in pg.locator(".co-summary").inner_text()
    assert _bg(pg, "#bf [name=billing_address]") == CONSOLE_SURFACE                  # was a grey native field
    pg.click("#go")
    pg.wait_for_selector("#pmForm")
    assert pg.locator("[name=cvv], [name=otp], [name=pin]").count() == 0             # never requested
    pg.check("[name=method][value=credit_card]")
    pg.fill("[name=card_name]", "Meera Iyer")
    pg.fill("[name=card_number]", "4242424242424242")
    pg.fill("[name=card_expiry]", "1230")
    pg.click("#payNext")
    pg.wait_for_function("document.querySelector('#err-card_number').innerText.length > 0")
    assert not any("4242424242424242" in p for p in posted)                        # refused before it left the browser
    pg.click('[data-card="4111 1111 1111 1111"]')
    pg.click("#payNext")
    pg.wait_for_selector("#ack")
    assert "Test credit card · Visa ending 1111" in pg.locator(".modal").inner_text()
    assert pg.is_disabled("#confirm")
    pg.check("#ack")
    pg.click("#confirm")
    pg.wait_for_selector("#toBilling", timeout=10000)
    text = pg.locator(".modal").inner_text()
    assert "Order Confirmed" in text and "subscription is now active" in text
    assert not pg.errors
