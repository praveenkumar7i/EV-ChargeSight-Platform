import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
import websockets

from app.db import SessionLocal
from app.models_saas import User
from app.services.accounts import create_session

CHROME_PATH = r"C:\Users\prave\AppData\Local\Google\Chrome\Application\chrome.exe"
ARTIFACT_DIR = r"C:\Users\prave\.gemini\antigravity\brain\01d1b498-9017-471a-b5e6-4ec0f3bc2ea5"
REPORTS_DIR = r"C:\Users\prave\Desktop\EV-ChargeSight-Platform\reports"
os.makedirs(REPORTS_DIR, exist_ok=True)

# Generate valid session for Praveen Kumar Y on Org 2 (which holds 72,841 records)
db = SessionLocal()
u = db.query(User).filter_by(email="praveenkumar1372005@gmail.com").first()
raw_token, sess = create_session(db, u, remember=True, org_id=2)
db.commit()
db.close()
print("Session token generated for org", sess.org_id)

async def cmd(ws, id_val, method, params=None):
    msg = {"id": id_val, "method": method}
    if params:
        msg["params"] = params
    await ws.send(json.dumps(msg))
    while True:
        raw = await ws.recv()
        resp = json.loads(raw)
        if resp.get("id") == id_val:
            return resp

async def run():
    td = tempfile.mkdtemp()
    proc = subprocess.Popen([
        CHROME_PATH,
        "--headless=new",
        "--remote-debugging-port=9339",
        f"--user-data-dir={td}",
        "--no-first-run",
        "--disable-gpu",
        "--window-size=1440,1100"
    ])
    try:
        await asyncio.sleep(2)
        targets = json.loads(urllib.request.urlopen("http://127.0.0.1:9339/json").read())
        ws_url = targets[0]["webSocketDebuggerUrl"]

        async with websockets.connect(ws_url, max_size=25_000_000) as ws:
            c = 1
            await cmd(ws, c, "Page.enable"); c += 1
            await cmd(ws, c, "Runtime.enable"); c += 1

            # Set session cookie
            c_res = await cmd(ws, c, "Network.setCookie", {
                "name": "cs_session",
                "value": raw_token,
                "url": "http://127.0.0.1:8000"
            }); c += 1
            print("Cookie set:", c_res)

            # Navigate to historical page
            await cmd(ws, c, "Page.navigate", {"url": "http://127.0.0.1:8000/app#historical"}); c += 1
            await asyncio.sleep(4)

            # Evaluate KPIs and charts in page
            check_js = """
            (() => {
                const kpis = Array.from(document.querySelectorAll('#histKpis .kpi')).map(k => ({
                    lbl: k.querySelector('.lbl')?.innerText?.trim(),
                    val: k.querySelector('.val')?.innerText?.trim(),
                    sub: k.querySelector('.sub')?.innerText?.trim(),
                }));
                const errBox = document.querySelector('.error-box')?.innerText || null;
                const hourlySvg = document.querySelectorAll('#hourlyChart svg').length;
                const typeRows = document.querySelectorAll('#typeChart .hbar-row').length;
                const locRows = document.querySelectorAll('#locChart .hbar-row').length;
                const compRows = document.querySelectorAll('#compChart .hbar-row').length;
                const durRows = document.querySelectorAll('#durationChart .hbar-row').length;
                const demRows = document.querySelectorAll('#demandChart .hbar-row').length;
                const tableRows = document.querySelectorAll('#histTable tbody tr').length;
                const countHint = document.querySelector('#countHint')?.innerText;

                return {
                    url: location.href,
                    errBox,
                    kpis,
                    hourlySvg,
                    typeRows,
                    locRows,
                    compRows,
                    durRows,
                    demRows,
                    tableRows,
                    countHint
                };
            })()
            """
            eval_res = await cmd(ws, c, "Runtime.evaluate", {"expression": check_js, "returnByValue": True}); c += 1
            metrics = eval_res["result"]["result"]["value"]
            print("Page Evaluation Metrics:")
            print(json.dumps(metrics, indent=2))

            # Screenshot 1: All charts and KPIs
            ss1 = await cmd(ws, c, "Page.captureScreenshot", {"format": "png"}); c += 1
            png1 = base64.b64decode(ss1["result"]["data"])
            p1_art = os.path.join(ARTIFACT_DIR, "08_historical_sessions_all_charts.png")
            p1_rep = os.path.join(REPORTS_DIR, "08_historical_sessions_all_charts.png")
            with open(p1_art, "wb") as f:
                f.write(png1)
            with open(p1_rep, "wb") as f:
                f.write(png1)
            print(f"Captured 08_historical_sessions_all_charts.png ({len(png1)} bytes)")

            # Apply filter: Location = 'hotel'
            filter_js = """
            (() => {
                const sel = document.querySelector('#filterLocation');
                if (sel) {
                    sel.value = 'hotel';
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                }
            })()
            """
            await cmd(ws, c, "Runtime.evaluate", {"expression": filter_js}); c += 1
            await asyncio.sleep(2)

            filter_eval = await cmd(ws, c, "Runtime.evaluate", {"expression": check_js, "returnByValue": True}); c += 1
            filtered_metrics = filter_eval["result"]["result"]["value"]
            print("Filtered Evaluation Metrics (Location = hotel):")
            print(json.dumps(filtered_metrics, indent=2))

            # Screenshot 2: Filtered view
            ss2 = await cmd(ws, c, "Page.captureScreenshot", {"format": "png"}); c += 1
            png2 = base64.b64decode(ss2["result"]["data"])
            p2_art = os.path.join(ARTIFACT_DIR, "09_historical_sessions_filtered_hotel.png")
            p2_rep = os.path.join(REPORTS_DIR, "09_historical_sessions_filtered_hotel.png")
            with open(p2_art, "wb") as f:
                f.write(png2)
            with open(p2_rep, "wb") as f:
                f.write(png2)
            print(f"Captured 09_historical_sessions_filtered_hotel.png ({len(png2)} bytes)")

    finally:
        proc.terminate()
        try:
            shutil.rmtree(td, ignore_errors=True)
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(run())

