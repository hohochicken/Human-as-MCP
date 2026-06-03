import asyncio, json, datetime, sys, os

LOG = r"G:\ws_callback.log"
WS = "ws://127.0.0.1:4350/ws"

async def main():
    import websockets
    while True:
        try:
            async with websockets.connect(WS, ping_interval=30, ping_timeout=10) as ws:
                with open(LOG, "a", encoding="utf-8") as f:
                    f.write(f"READY|{datetime.datetime.now():%H:%M:%S}\n")
                    f.flush()
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    ts = datetime.datetime.now().strftime("%H:%M:%S")
                    line = f"CALLBACK|{ts}|{json.dumps(data, ensure_ascii=False)}"
                    with open(LOG, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                        f.flush()
        except asyncio.CancelledError:
            break
        except Exception as e:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(f"RECONNECT|{e}\n")
                f.flush()
            await asyncio.sleep(5)

asyncio.run(main())
