"""Real-browser tests of the Map and Site Feasibility pages (headless Chromium via Playwright).

They run the actual frontend against a real server on a temporary database. They are skipped
when Playwright or its browser is not installed (pip install playwright && playwright install chromium).
No external service is contacted: without GOOGLE_MAPS_BROWSER_KEY the page uses Leaflet, or the
coordinate plot when the Leaflet CDN is unreachable. Google-specific controls are not covered here.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def server(db_url, settings_env):
    import uvicorn

    from app import db as D
    from app.main import create_app

    settings_env(google_maps_browser_key="", ocm_api_key="", osm_enabled="false")
    D.init_engine(db_url)
    app = create_app(run_simulator_loop=False)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started, "test server did not start"
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(10)


@pytest.fixture
def page(server):
    with playwright_sync.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # browser binaries not installed
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        pg = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.errors = errors
        pg.base = server
        # v9: the console requires a signed-in member. Sign the browser in (shared cookie jar) and give the
        # tests an authenticated HTTP client for their direct API checks.
        import httpx

        from tests.conftest import LEGACY_OWNER, ensure_user

        ensure_user(*LEGACY_OWNER, org_id=1, role="owner", platform_admin=True)
        r = pg.request.post(server + "/api/auth/signin", data={"email": LEGACY_OWNER[0], "password": LEGACY_OWNER[1]})
        assert r.ok, r.text()
        api = httpx.Client(timeout=30)
        tok = api.post(server + "/api/auth/signin", json={"email": LEGACY_OWNER[0], "password": LEGACY_OWNER[1]}).json()["csrf_token"]
        api.headers["X-CSRF-Token"] = tok
        pg.api = api
        yield pg
        api.close()
        browser.close()


def _open_map(page, hash_="#map"):
    page.goto(page.base + "/app" + hash_)
    page.wait_for_selector("#locs .loc", timeout=20000)
    page.wait_for_function("document.querySelector('#mapBox') && !document.querySelector('#mapBox .loading')", timeout=20000)


def _card_count(page, key):
    return int(page.locator(f'#locs [data-area="{key}"] .loc-count').inner_text().split()[0])


def test_locality_selection_updates_markers_list_and_summary(page):
    _open_map(page)
    total = _card_count(page, "")
    assert total > 0 and page.locator("#mlist tr[data-k]").count() == total
    for key in ("whitefield", "electronic_city", "koramangala"):
        page.click(f'#locs [data-area="{key}"]')
        n = _card_count(page, key)
        assert page.locator(f'#locs [data-area="{key}"]').get_attribute("aria-pressed") == "true"
        rows = page.locator("#mlist tr[data-k^='public:']").count()
        assert rows == n, (key, rows, n)
        if n:
            assert int(page.locator("#sumStations").inner_text()) == n
            areas = set(page.locator("#mlist tr[data-k^='public:'] td:nth-child(4)").all_inner_texts())
            assert len(areas) == 1                                       # every row belongs to the chosen locality
        assert f"area={key}" in page.evaluate("location.hash")          # restorable link
    assert not page.errors


def test_marker_and_list_selection_stay_in_sync(page):
    _open_map(page, "#map?area=whitefield")
    page.wait_for_selector("#mlist tr[data-k]", timeout=10000)
    rows = page.locator("#mlist tr[data-k]")
    assert rows.count() >= 2
    # list -> details + marker
    second = rows.nth(1)
    name = second.locator("b").inner_text()
    second.click()
    assert page.locator("#mlist tr[aria-selected='true']").count() == 1
    assert name in page.locator("#detTitle").inner_text()
    assert page.locator("#selCoord").inner_text().startswith("Selected:")
    # keyboard: Enter on another row
    rows.nth(0).focus()
    page.keyboard.press("Enter")
    assert rows.nth(0).locator("b").inner_text() in page.locator("#detTitle").inner_text()
    # marker -> list row
    marker = page.locator("#mapBox image[data-i], #mapBox .leaflet-marker-icon").last
    marker.dispatch_event("click")
    sel = page.locator("#mlist tr[aria-selected='true']")
    assert sel.count() == 1
    assert sel.locator("b").inner_text() in page.locator("#detTitle").inner_text()
    assert not page.errors


def test_operator_filter_and_coordinate_validation(page):
    _open_map(page)
    ops = page.locator("#mop option").all_inner_texts()
    assert len(ops) > 1
    page.select_option("#mop", label=ops[1])
    n = page.locator("#mlist tr[data-k^='public:']").count()
    assert n >= 1 and int(page.locator("#sumStations").inner_text()) == n
    assert set(page.locator("#mlist tr[data-k^='public:'] td:nth-child(3)").all_inner_texts()) == {ops[1]}
    page.fill("#msearch", "13.6, 77.6")
    page.press("#msearch", "Enter")
    page.wait_for_selector(".toast", timeout=5000)
    assert "outside the Bengaluru study area" in page.locator(".toast").last.inner_text()
    page.fill("#msearch", "12.9352, 77.6245")
    page.press("#msearch", "Enter")
    page.wait_for_function("document.querySelector('#selCoord').textContent.includes('12.935200, 77.624500')", timeout=5000)
    assert not page.errors


def test_station_details_show_source_record(page):
    _open_map(page, "#map?area=whitefield")
    page.locator("#mlist tr[data-k^='public:']").first.click()
    det = page.locator("#detail").inner_text().lower()          # headings are upper-cased by CSS
    for needle in ("source record and freshness", "licence", "attribution", "status unknown", "raw stored record"):
        assert needle in det, needle
    assert page.locator("#detail a", has_text="Open source record").count() == 1


# ------------------------------------------------------------------ phase 2: candidate workflow
def _plan_candidate(page, lat, lng, name):
    if page.locator("#mplan").get_attribute("aria-pressed") != "true":
        page.click("#mplan")
    page.fill("#pLat", str(lat))
    page.fill("#pLng", str(lng))
    page.click("#planForm button[type=submit]")
    page.wait_for_selector("#candSave", timeout=10000)
    assert "nearest listed charger" in page.locator("#detail").inner_text().lower()
    page.fill("#cName", name)
    page.click("#cSaveBtn")
    page.wait_for_selector(f"#candTable tr:has-text('{name}')", timeout=10000)


def test_plan_candidate_then_site_feasibility_and_back(page):
    _open_map(page, "#map?area=koramangala")
    page.click("#mplan")
    # invalid coordinates are refused with an explanation
    page.fill("#pLat", "77.6245")
    page.fill("#pLng", "12.9352")
    page.click("#planForm button[type=submit]")
    page.wait_for_selector(".toast", timeout=5000)
    assert "swapped" in page.locator(".toast").last.inner_text()
    _plan_candidate(page, 12.9340, 77.6230, "Kora test site")
    # candidate is on the map list and selected
    assert page.locator("#mlist tr[data-k^='candidate:']").count() == 1
    assert "Kora test site" in page.locator("#detTitle").inner_text()
    # record a user input
    page.fill("#candInputs input[name=n_chargers]", "3")
    page.fill("#candInputs input[name=sessions_per_day]", "28")
    page.click("#cInSave")
    page.wait_for_function("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Inputs saved'))", timeout=5000)
    # continue to Site Feasibility: location and user values prefilled, each with a source
    page.click("#cFeas")
    page.wait_for_selector("#fromMap table", timeout=15000)
    assert page.input_value("input[name=site_name]") == "Kora test site"
    assert abs(float(page.input_value("input[name=lat]")) - 12.934) < 1e-6
    assert abs(float(page.input_value("input[name=lng]")) - 77.623) < 1e-6
    assert page.input_value("input[name=n_chargers]") == "3" and page.input_value("input[name=sessions_per_day]") == "28"
    src = page.locator("#fromMap").inner_text()
    assert "USER-ENTERED" in src and "ASSUMPTION" in src and "Koramangala" in src
    page.wait_for_selector("#out .kpi", timeout=10000)
    assert "initial investment" in page.locator("#out").inner_text().lower()
    # back to map restores locality and candidate
    page.click("#backMap")
    page.wait_for_selector("#locs .loc", timeout=20000)
    page.wait_for_function("document.querySelector('#detTitle').textContent.includes('Kora test site')", timeout=10000)
    assert page.locator('#locs [data-area="koramangala"]').get_attribute("aria-pressed") == "true"
    assert not page.errors


def test_saving_a_proposed_site_requires_confirmation_and_never_overwrites(page, server):
    _h = page.api                                  # v9: authenticated API client

    _open_map(page)
    _plan_candidate(page, 12.9700, 77.7490, "WF save test")
    page.click("#cFeas")
    page.wait_for_selector("#fromMap table", timeout=15000)
    n0 = len(_h.get(server + "/api/investment/sites").json())
    page.click("#save")
    page.wait_for_selector(".modal", timeout=5000)
    page.click("#mNo")                                                   # cancel -> nothing saved
    assert len(_h.get(server + "/api/investment/sites").json()) == n0
    page.click("#save")
    page.click("#mYes")
    page.wait_for_function("document.querySelector('#editing').textContent.includes('Editing saved site')", timeout=5000)
    sites = _h.get(server + "/api/investment/sites").json()
    assert len(sites) == n0 + 1 and sites[-1]["name"] == "WF save test"
    cand = [c for c in _h.get(server + "/api/planning/candidates").json() if c["name"] == "WF save test"][0]
    assert cand["saved_site_id"] == sites[-1]["id"]
    # a second save asks to UPDATE the loaded site; cancelling leaves it untouched
    page.fill("input[name=n_chargers]", "6")
    page.click("#save")
    page.wait_for_selector(".modal", timeout=5000)
    assert "Update saved site" in page.locator(".modal").inner_text()
    page.click("#mNo")
    again = _h.get(server + f"/api/investment/sites").json()
    assert len(again) == n0 + 1 and again[-1]["inputs"]["n_chargers"] != 6
    assert not page.errors


# ------------------------------------------------------------------ phase 3: ranking and financial comparison
def test_rank_candidates_and_compare_finance_in_browser(page, server):
    _h = page.api                                  # v9: authenticated API client

    full = dict(n_chargers=2, charger_type="DC", power_kw=60, sessions_per_day=30, site_area_m2=120,
                grid_available_kva=150, grid_confirmed=True, road_access="main", amenities_observed=24)
    for name, lat, lng, extra in [("Gap site", 13.07, 77.50, {}), ("Dense site", 12.9698, 77.75, {}),
                                  ("Weak demand", 12.9116, 77.6474, {"amenities_observed": 2, "sessions_per_day": 8})]:
        _h.post(server + "/api/planning/candidates", json={"name": name, "lat": lat, "lng": lng, "inputs": {**full, **extra}})
    _h.post(server + "/api/planning/candidates", json={"name": "Bare point", "lat": 12.8452, "lng": 77.6602})
    _open_map(page)
    page.wait_for_selector("#scoreRun", timeout=10000)
    page.click("#scoreRun")
    page.wait_for_selector("#rankTable tbody tr", timeout=10000)
    rows = page.locator("#rankTable tbody tr")
    assert rows.count() == 4
    text = page.locator("#rankTable").inner_text()
    assert "Insufficient data to rank reliably" in text                      # the bare point is not ranked
    assert rows.nth(3).locator("td").first.inner_text().strip() == "—"
    ranks = [rows.nth(i).locator("td").first.inner_text().strip() for i in range(3)]
    assert ranks == ["1", "2", "3"]
    expl = page.locator("#scoreOut details.cand-expl").first.inner_text().lower()
    assert "coverage" in expl and "field checks" in expl and "source data" in expl
    assert "sensitivity" in page.locator("#scoreOut").inner_text().lower()
    # change a weight: the request uses it (ranking re-computed without errors)
    page.locator("#scorePanel details summary").first.click()
    page.locator("#wForm input[name=financial]").fill("0")
    page.click("#scoreRun")
    page.wait_for_selector("#rankTable tbody tr", timeout=10000)
    # Site Feasibility: compare candidates under identical assumptions
    page.goto(page.base + "/app#investment")
    page.wait_for_selector("#candCmpRun", timeout=15000)
    page.click("#candCmpRun")
    page.wait_for_selector("#candCmpTable tbody tr", timeout=10000)
    assert page.locator("#candCmpTable tbody tr").count() == 4
    t = page.locator("#candCmpTable").inner_text()
    assert "Gap site" in t and "NPV" in page.locator("#candCmpTable thead").inner_text()
    assert not page.errors


def test_source_selector_never_shows_unqualified_live(page):
    _open_map(page)
    for value in ("operator_api", "ocpp"):
        page.select_option("#sourceSel", value)
        page.wait_for_timeout(600)
        badge = page.locator("#srcTag").inner_text()
        assert "LIVE" not in badge.replace("NO LIVE DATA", ""), badge          # no live records in this database
        assert badge.strip()                                                     # a real state is still shown
    opts = " ".join(page.locator("header select option").all_inner_texts())
    assert "(live)" not in opts


# ------------------------------------------------------------------ v8: raise a ticket, assignment, technicians
def _open_dash(page):
    page.goto(page.base + "/app#dashboard")
    page.wait_for_selector("#chargers tr[data-id]", timeout=20000)


def test_raise_ticket_from_dashboard_row_and_work_it(page, server):
    _h = page.api                                  # v9: authenticated API client

    _open_dash(page)
    open_before = _h.get(server + "/api/tickets/metrics").json()["open"]
    page.click('#chargers [data-report="SIM-KRM-05"]')
    page.wait_for_selector("#rtForm")
    assert page.input_value("#rtCharger") == "SIM-KRM-05"                      # prefilled from the row
    page.click("#rtSubmit")
    assert "Describe the problem" in page.locator("#rtErrBox").inner_text()    # validation, nothing submitted
    assert _h.get(server + "/api/tickets/metrics").json()["open"] == open_before
    page.select_option("#rtCat", "payment")
    page.select_option("#rtImpact", "unavailable")
    page.fill("#rtDesc", "Card reader dead")
    page.fill("#rtBy", "Browser test")
    page.click("#rtSubmit")
    page.wait_for_selector("#rtResult:not(.hidden)", timeout=10000)
    res = page.locator("#rtResult").inner_text()
    assert "created" in res and "critical" in res and "next action" in res.lower()
    code = res.split("Ticket ")[1].split(" ")[0]
    t = _h.get(server + f"/api/tickets/{code}").json()
    assert t["origin"] == "user_reported" and t["technician_id"]               # auto-assigned in the default simulator
    assert _h.get(server + "/api/tickets/metrics").json()["open"] == open_before + 1
    page.click("#rtOpen")
    page.wait_for_selector(".drawer-b .cand-row", timeout=10000)
    assert "USER_REPORTED" in page.locator(".drawer-h").inner_text()
    body = page.locator(".drawer-b").inner_text().lower()
    for needle in ("severity basis", "sla", "assignment", "audit trail", "priority explanation", "reported problem"):
        assert needle in body, needle
    # reassign to an ineligible technician: conflict confirmation with a reason, recorded as an override
    assert "Test Owner" in page.locator("#actorNote").inner_text()      # v9: actions are recorded as the signed-in user
    target = next(c for c in _h.get(server + f"/api/tickets/{code}/eligible-technicians").json()["candidates"]
                  if not c["eligible"] and not c["safety_blocker"] and c["technician_id"] != t["technician_id"])
    page.click(f'[data-assign="{target["technician_id"]}"]')
    page.wait_for_selector("#pbIn", timeout=5000)
    page.fill("#pbIn", "closest person today")
    page.click("#pbYes")
    page.wait_for_function(f"document.querySelector('.drawer-h') && document.querySelector('.drawer-b') && document.querySelector('.drawer-b').innerText.includes('OVERRIDE')", timeout=10000)
    t2 = _h.get(server + f"/api/tickets/{code}").json()
    assert t2["technician_id"] == target["technician_id"]
    last = [h for h in t2["history"] if h["kind"] == "assignment"][-1]
    assert last["override"] and last["actor"] == "Test Owner" and "closest person today" in last["note"]
    assert not page.errors


def test_technicians_page_and_ticket_list(page, server):
    _h = page.api                                  # v9: authenticated API client

    page.goto(page.base + "/app#technicians")
    page.wait_for_selector("#techList table", timeout=15000)
    assert page.locator("#techList tbody tr").count() == 4
    assert page.locator("#techList").inner_text().count("DEMO") >= 4
    page.select_option('[data-av="TECH-02"]', "off_duty")
    page.wait_for_function("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Availability updated'))", timeout=5000)
    assert {t["id"]: t for t in _h.get(server + "/api/technicians").json()}["TECH-02"]["availability"] == "off_duty"
    page.click("#addTech")
    page.fill("#teF [name=name]", "Registered Tech")
    page.check('#teF [name=skill][value="display"]')
    page.click("#teS")
    page.wait_for_function("document.querySelector('#techList').innerText.includes('Registered Tech')", timeout=5000)
    _h.post(server + "/api/tickets", json={"charger_id": "SIM-HSR-06", "issue_category": "display", "impact": "informational",
                                          "description": "screen dim"})
    page.goto(page.base + "/app#tickets")
    page.wait_for_selector("#tlist table", timeout=15000)
    assert "USER_REPORTED" in page.locator("#tlist").inner_text()
    page.select_option('[data-k="origin"]', "user_reported")
    page.wait_for_timeout(600)
    rows = page.locator("#tlist tbody tr")
    assert rows.count() >= 1 and all("USER_REPORTED" in rows.nth(i).inner_text() for i in range(rows.count()))
    assert not page.errors
