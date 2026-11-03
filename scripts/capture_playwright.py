"""
Playwright-based screenshot capture for ChargeSight Historical Sessions page.
Uses proper page-load events (no arbitrary sleep), waits for chart containers.
"""
import os, sys, json, pathlib

# ── paths ───────────────────────────────────────────────────────────────────
PROJECT = pathlib.Path(__file__).parent.parent
ARTIFACT_DIR = pathlib.Path(r"C:\Users\prave\.gemini\antigravity\brain\01d1b498-9017-471a-b5e6-4ec0f3bc2ea5")
REPORTS_DIR  = PROJECT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

BASE_URL = "http://127.0.0.1:8000"
DB_PATH  = str(PROJECT / "data" / "chargesight.db")

# ── get a session token for org 2 ────────────────────────────────────────────
sys.path.insert(0, str(PROJECT / "backend"))

# Bootstrap the app so db.py initialises properly
from app.db import init_engine, SessionLocal
from app.models_saas import User
from app.services.accounts import create_session   # returns (token_str, AuthSession)

init_engine()   # uses default db path from config / env
db = SessionLocal()

user = db.query(User).filter(User.email == "praveenkumar1372005@gmail.com").first()
if not user:
    print("ERROR: user not found"); db.close(); sys.exit(1)

token, _sess = create_session(db, user, remember=True, org_id=2)
db.commit()   # MUST commit so the server can load the session
db.close()
print(f"Session token obtained: {token[:20]}...")

# ── playwright capture ───────────────────────────────────────────────────────
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

def save(page, name: str):
    for d in (ARTIFACT_DIR, REPORTS_DIR):
        p = d / name
        page.screenshot(path=str(p), full_page=False)
        print(f"  Saved: {p}")

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx     = browser.new_context(viewport={"width": 1440, "height": 900})
    page    = ctx.new_page()

    # ── set auth cookie before navigating ───────────────────────────────────
    ctx.add_cookies([{
        "name":     "cs_session",
        "value":    token,
        "domain":   "127.0.0.1",
        "path":     "/",
        "httpOnly": True,
        "secure":   False,
        "sameSite": "Lax",
    }])

    # ── navigate and wait for network idle ──────────────────────────────────
    print("Navigating to historical page …")
    page.goto(f"{BASE_URL}/app#historical", wait_until="networkidle", timeout=60_000)

    # wait for the KPI section to appear (rendered by JS)
    try:
        page.wait_for_selector("#histKpis", timeout=30_000)
        print("KPI element found — page is rendered.")
    except PWTimeout:
        print("WARNING: KPI element not found within 30s — taking screenshot anyway.")

    # short pause to let Chart.js / barChart finish painting
    page.wait_for_timeout(2000)

    # ── screenshot 1: all charts (no filter) ────────────────────────────────
    print("Screenshot 1: all charts (no filter)")
    save(page, "08_historical_sessions_all_charts.png")

    # ── apply hotel filter and take screenshot 2 ────────────────────────────
    print("Applying Location = hotel filter …")
    try:
        loc_sel = page.locator("#filterLocation")
        loc_sel.select_option("hotel")
        page.wait_for_load_state("networkidle", timeout=15_000)
        page.wait_for_timeout(1500)
        print("Screenshot 2: hotel filter applied")
        save(page, "09_historical_sessions_filtered_hotel.png")
    except Exception as e:
        print(f"WARNING: hotel filter step failed: {e}")

    # ── screenshot 3: scroll down to see more charts ─────────────────────────
    print("Scrolling down for lower charts …")
    page.evaluate("window.scrollTo(0, 800)")
    page.wait_for_timeout(1000)
    save(page, "10_historical_sessions_lower_charts.png")

    browser.close()
    print("\nAll screenshots saved successfully.")

