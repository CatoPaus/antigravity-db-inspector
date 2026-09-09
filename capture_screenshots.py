#!/usr/bin/env python3
import asyncio
import base64
import json
import os
import shutil
import subprocess
import time
import urllib.request
import websockets

ARTIFACTS_DIR = "/home/cato/.gemini/antigravity/brain/ba33ae7f-87d6-4ca1-8266-cc4b0e5cc1d2"

async def send_cdp(ws, method, params=None, msg_id=1):
    payload = {"id": msg_id, "method": method}
    if params:
        payload["params"] = params
    await ws.send(json.dumps(payload))
    while True:
        resp = await ws.recv()
        data = json.loads(resp)
        if data.get("id") == msg_id:
            return data

async def capture_screen(ws, filename, msg_id):
    res = await send_cdp(ws, "Page.captureScreenshot", {"format": "png"}, msg_id)
    b64data = res.get("result", {}).get("data", "")
    if b64data:
        dest = os.path.join(ARTIFACTS_DIR, filename)
        with open(dest, "wb") as f:
            f.write(base64.b64decode(b64data))
        print(f"Captured {filename} -> {dest} ({len(b64data)} b64 bytes)")
        return dest
    else:
        print(f"Failed to capture {filename}: {res}")
        return None

async def main():
    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    user_data_dir = "/tmp/chrome_ss_session"
    if os.path.exists(user_data_dir):
        shutil.rmtree(user_data_dir, ignore_errors=True)

    chrome_cmd = [
        "google-chrome",
        "--headless=new",
        "--remote-debugging-port=9333",
        "--remote-allow-origins=*",
        "--window-size=1440,920",
        "--hide-scrollbars",
        "--no-sandbox",
        "--disable-gpu",
        f"--user-data-dir={user_data_dir}",
        "about:blank"
    ]
    proc = subprocess.Popen(chrome_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("Started Chrome PID", proc.pid)
    time.sleep(1.5)

    try:
        req = urllib.request.urlopen("http://127.0.0.1:9333/json/list", timeout=3)
        targets = json.loads(req.read().decode("utf-8"))
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise RuntimeError("No page target found!")
        ws_url = pages[0]["webSocketDebuggerUrl"]
        print("Connecting to CDP WS:", ws_url)

        async with websockets.connect(ws_url, max_size=50*1024*1024) as ws:
            msg_id = 1
            await send_cdp(ws, "Page.enable", {}, msg_id)
            msg_id += 1
            await send_cdp(ws, "Runtime.enable", {}, msg_id)
            msg_id += 1

            # 1. Navigate to Inspector Home
            print("1. Navigating to http://localhost:8990...")
            await send_cdp(ws, "Page.navigate", {"url": "http://localhost:8990"}, msg_id)
            msg_id += 1
            await asyncio.sleep(2.5)

            # Screenshot 1: Databases Overview
            await capture_screen(ws, "01_databases_overview.png", msg_id)
            msg_id += 1

            # 2. Switch to Bloat Scanner tab
            print("2. Switching to Bloat Scanner tab...")
            await send_cdp(ws, "Runtime.evaluate", {"expression": "showTab('bloat')"}, msg_id)
            msg_id += 1
            await asyncio.sleep(2.0)

            # Screenshot 2: Bloat Scanner
            await capture_screen(ws, "02_bloat_scanner.png", msg_id)
            msg_id += 1

            # 3. Open current database detail view
            print("3. Opening database detail view...")
            await send_cdp(ws, "Runtime.evaluate", {
                "expression": "openDatabase('ba33ae7f-87d6-4ca1-8266-cc4b0e5cc1d2.db', 'desktop')"
            }, msg_id)
            msg_id += 1
            await asyncio.sleep(2.0)

            # Screenshot 3: Steps Detail View
            await capture_screen(ws, "03_steps_detail.png", msg_id)
            msg_id += 1

            # 4. Open Step 419 Protobuf inspector modal
            print("4. Opening Step 419 Protobuf modal...")
            await send_cdp(ws, "Runtime.evaluate", {
                "expression": "inspectStep('ba33ae7f-87d6-4ca1-8266-cc4b0e5cc1d2.db', 'desktop', 419)"
            }, msg_id)
            msg_id += 1
            await asyncio.sleep(2.0)

            # Screenshot 4: Step Modal
            await capture_screen(ws, "04_protobuf_inspector_modal.png", msg_id)
            msg_id += 1

        print("All screenshots successfully captured!")
    finally:
        proc.terminate()
        proc.wait()
        print("Chrome closed.")

if __name__ == "__main__":
    asyncio.run(main())
