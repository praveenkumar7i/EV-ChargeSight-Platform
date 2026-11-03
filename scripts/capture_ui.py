import asyncio
import json
import os
import subprocess
import time
import urllib.request
import websockets
import base64

CHROME_PATH = r"C:\Users\prave\AppData\Local\Google\Chrome\Application\chrome.exe"
OUTPUT_DIR = r"C:\Users\prave\Desktop\EV-ChargeSight-Platform\reports\v10_2_screenshots"
os.makedirs(OUTPUT_DIR, exist_ok=True)

async def send_cmd(ws, id_val, method, params=None):
    msg = {"id": id_val, "method": method}
    if params:
        msg["params"] = params
    await ws.send(json.dumps(msg))
    while True:
        raw = await ws.recv()
        resp = json.loads(raw)
        if resp.get("id") == id_val:
            return resp

async def capture(ws, id_val, filename):
    resp = await send_cmd(ws, id_val, "Page.captureScreenshot", {"format": "png"})
    if "result" in resp and "data" in resp["result"]:
        data = base64.b64decode(resp["result"]["data"])
        filepath = os.path.join(OUTPUT_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(data)
        print(f"Captured {filename} ({len(data)} bytes)")
    else:
        print(f"Failed to capture {filename}: {resp}")

async def main():
    profile_dir = os.path.join(OUTPUT_DIR, "chrome_profile_2")
    chrome_proc = subprocess.Popen([
        CHROME_PATH,
        "--headless=new",
        "--remote-debugging-port=9225",
        "--disable-gpu",
        "--window-size=1440,960",
        f"--user-data-dir={profile_dir}"
    ])
    try:
        await asyncio.sleep(2)
        targets_json = urllib.request.urlopen("http://127.0.0.1:9225/json").read()
        targets = json.loads(targets_json)
        ws_url = targets[0]["webSocketDebuggerUrl"]

        async with websockets.connect(ws_url, max_size=25_000_000) as ws:
            cmd_id = 1
            await send_cmd(ws, cmd_id, "Page.enable"); cmd_id += 1
            await send_cmd(ws, cmd_id, "DOM.enable"); cmd_id += 1
            await send_cmd(ws, cmd_id, "Runtime.enable"); cmd_id += 1

            # 1. Navigate to /signin
            await send_cmd(ws, cmd_id, "Page.navigate", {"url": "http://127.0.0.1:8000/signin"}); cmd_id += 1
            await asyncio.sleep(2)

            # 2. Authenticate
            login_js = """
            (async () => {
                const r = await fetch('/api/auth/signin', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({email: 'officer@commercial-test.example', password: 'Password123!', remember: true})
                });
                location.href = '/app#billing';
            })();
            """
            await send_cmd(ws, cmd_id, "Runtime.evaluate", {"expression": login_js, "awaitPromise": True}); cmd_id += 1
            await asyncio.sleep(3)

            # 3. Open checkout modal for Professional
            open_checkout_js = """
            const p = document.querySelector('[data-plan="professional"]');
            if (p) p.click();
            """
            await send_cmd(ws, cmd_id, "Runtime.evaluate", {"expression": open_checkout_js}); cmd_id += 1
            await asyncio.sleep(1.5)
            await capture(ws, cmd_id, "02_checkout_step1_review.png"); cmd_id += 1

            # 4. Step 2 - Choose payment method (default UPI)
            await send_cmd(ws, cmd_id, "Runtime.evaluate", {"expression": "document.querySelector('#go')?.click();"}); cmd_id += 1
            await asyncio.sleep(1.5)
            await capture(ws, cmd_id, "03_checkout_step2_payment_upi.png"); cmd_id += 1

            # Select Card payment option and capture
            select_card_js = """
            const cardRadio = document.querySelector('#m_card');
            if (cardRadio) {
                cardRadio.click();
                cardRadio.dispatchEvent(new Event('change', {bubbles: true}));
            }
            """
            await send_cmd(ws, cmd_id, "Runtime.evaluate", {"expression": select_card_js}); cmd_id += 1
            await asyncio.sleep(1)
            await capture(ws, cmd_id, "04_checkout_step2_payment_cards.png"); cmd_id += 1

            # Select Net Banking and capture
            select_nb_js = """
            const nbRadio = document.querySelector('#m_nb');
            if (nbRadio) {
                nbRadio.click();
                nbRadio.dispatchEvent(new Event('change', {bubbles: true}));
            }
            """
            await send_cmd(ws, cmd_id, "Runtime.evaluate", {"expression": select_nb_js}); cmd_id += 1
            await asyncio.sleep(1)
            await capture(ws, cmd_id, "04b_checkout_step2_payment_netbanking.png"); cmd_id += 1

            # Switch back to card, fill test card & proceed to Step 3
            fill_card_js = """
            const cardRadio = document.querySelector('#m_card');
            cardRadio.click();
            cardRadio.dispatchEvent(new Event('change', {bubbles: true}));
            const testBtn = document.querySelector('[data-card="4111 1111 1111 1111"]');
            if (testBtn) testBtn.click();
            document.querySelector('#payNext')?.click();
            """
            await send_cmd(ws, cmd_id, "Runtime.evaluate", {"expression": fill_card_js}); cmd_id += 1
            await asyncio.sleep(2)
            await capture(ws, cmd_id, "05_checkout_step3_confirm.png"); cmd_id += 1

            # 5. Confirm subscription (Step 4)
            confirm_js = """
            const ack = document.querySelector('#ack');
            if (ack && !ack.checked) ack.click();
            document.querySelector('#confirm')?.click();
            """
            await send_cmd(ws, cmd_id, "Runtime.evaluate", {"expression": confirm_js}); cmd_id += 1
            await asyncio.sleep(3)
            await capture(ws, cmd_id, "06_checkout_step4_confirmed.png"); cmd_id += 1

            print("All modal checkout steps successfully captured!")

    finally:
        chrome_proc.terminate()

if __name__ == "__main__":
    asyncio.run(main())
