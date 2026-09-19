# copy of tools/ws_smoke.py for archive
#!/usr/bin/env python3
"""Archived copy of ws_smoke.py"""

import argparse
import asyncio
import sys

try:
    import websockets
except Exception as e:
    print("NO_WEBSOCKETS_MODULE", e)
    sys.exit(2)

async def run(uri: str, timeout: int) -> int:
    try:
        print("TRY_CONNECT", uri)
        async with websockets.connect(uri) as ws:
            print("CONNECTED")
            try:
                await ws.send('{"type":"smoke_ping"}')
                print("SENT_PING")
            except Exception as ex:
                print("SEND_ERROR", type(ex).__name__, ex)
                return 1
            try:
                await asyncio.wait_for(asyncio.sleep(0.2), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            return 0
    except Exception as ex:
        print("CONNECT_ERROR", type(ex).__name__, ex)
        return 1

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--uri', default='ws://127.0.0.1:8765')
    p.add_argument('--timeout', type=int, default=5)
    args = p.parse_args()
    rc = asyncio.run(run(args.uri, args.timeout))
    sys.exit(rc)
