import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
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
print(f"Session token generated for org {sess.org_id}")

async def send_cmd(ws, id_val, method, params=None):
    msg = {"id": id_val, "method": method}
    if params:
        msg["params"] = params
    await ws.send(json.dumps(msg))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=20.0)
        resp = json.loads(raw)
        if resp.get("id") == id_val:
            return resp

async def save_screenshot(ws, cmd_id, filename):
    resp = await send_cmd(ws, cmd_id, "Page.captureScreenshot", {"format": "png"})
    if "result" in resp and "data" in resp["result"]:
        data = base64.b64decode(resp["result"]["data"])
        p1 = os.path.join(ARTIFACT_DIR, filename)
        p2 = os.path.join(REPORTS_DIR, filename)
        with open(p1, "wb") as f:
            f.write(data)
        with open(p2, "wb") as f:
            f.write(data)
        print(f"Saved screenshot: {filename} ({len(data)} bytes)")
        return p1
    print(f"Failed to capture {filename}: {resp}")
    return None

async def run():
    temp_dir = tempfile.mkdtemp()
    proc = subprocess.Popen([
        CHROME_PATH,
        "--headless=new",
        "--remote-debugging-port=9336",
        f"--user-data-dir={temp_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--window-size=1440,1100"
    ])
    try:
        await asyncio.sleep(2)
        data = urllib.request.urlopen("http://127.0.0.1:9336/json", timeout=5).read()
        targets = json.loads(data)
        ws_url = targets[0]["webSocketDebuggerUrl"]

        async with websockets.connect(ws_url, max_size=25_000_000) as ws:
            cmd = 1
            await send_cmd(ws, cmd, "Page.enable"); cmd += 1
            await send_cmd(ws, cmd, "Network.enable"); cmd += 1
            await send_cmd(ws, cmd, "Runtime.enable"); cmd += 1

            # 1. Set authentication cookie on URL
            c_resp = await send_cmd(ws, cmd, "Network.setCookie", {
                "name": "cs_session",
                "value": raw_token,
                "url": "http://127.0.0.1:8000",
                "httpOnly": True,
                "sameSite": "Lax"
            }); cmd += 1
            print("Cookie set result:", c_resp.get("result"))

            # 2. Navigate to Historical view
            await send_cmd(ws, cmd, "Page.navigate", {"url": "http://127.0.0.1:8000/app#historical"}); cmd += 1
            await asyncio.sleep(4)

            # 3. Check page state and verify DOM
            check_js = """
            (() => {
                const kpis = Array.from(document.querySelectorAll('#histKpis .kpi')).map(k => ({
                    lbl: k.querySelector('.lbl')?.innerText?.trim(),
                    val: k.querySelector('.val')?.innerText?.trim(),
                    sub: k.querySelector('.sub')?.innerText?.trim(),
                }));
                const errBox = document.querySelector('.error-box')?.innerText || null;
                const hourlyBars = document.querySelectorAll('#hourlyChart svg').length;
                const typeRows = document.querySelectorAll('#typeChart .hbar-row').length;
                const locRows = document.querySelectorAll('#locChart .hbar-row').length;
                const compRows = document.querySelectorAll('#compChart .hbar-row').length;
                const durRows = document.querySelectorAll('#durationChart .hbar-row').length;
                const demRows = document.querySelectorAll('#demandChart .hbar-row').length;
                const tableRows = document.querySelectorAll('#histTable tbody tr').length;
                const countHint = document.querySelector('#countHint')?.innerText;
                const url = location.href;

                return {
                    url,
                    errBox,
                    kpis,
                    hourlyBars,
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
            eval_res = await send_cmd(ws, cmd, "Runtime.evaluate", {"expression": check_js, "returnByValue": True}); cmd += 1
            metrics = eval_res.get("result", {}).get("value", {})
            print("Initial DOM Verification:")
            print(json.dumps(metrics, indent=2))

            # 4. Save initial full dashboard screenshot
            await save_screenshot(ws, cmd, "08_historical_sessions_all_charts.png"); cmd += 1

            # 5. Apply filter: select location 'hotel'
            filter_js = """
            (() => {
                const sel = document.querySelector('#filterLocation');
                if (sel) {
                    sel.value = 'hotel';
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                }
            })()
            """
            await send_cmd(ws, cmd, "Runtime.evaluate", {"expression": filter_js}); cmd += 1
            await asyncio.sleep(2)

            filter_eval = await send_cmd(ws, cmd, "Runtime.evaluate", {"expression": check_js, "returnByValue": True}); cmd += 1
            filtered_metrics = filter_eval.get("result", {}).get("value", {})
            print("Filtered DOM Result (Location = hotel):")
            print(json.dumps(filtered_metrics, indent=2))

            # 6. Save filtered screenshot
            await save_screenshot(ws, cmd, "09_historical_sessions_filtered_hotel.png"); cmd += 1

    finally:
        proc.terminate()
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(run())
