#!/usr/bin/env python3
# archived copy of tools/test_ws_client.py
import asyncio,sys
try:
    import websockets
except Exception as e:
    print("NO_WEBSOCKETS_MODULE", e)
    sys.exit(2)

async def main():
    uri='ws://127.0.0.1:8765'
    try:
        print('TRY_CONNECT', uri)
        async with websockets.connect(uri) as ws:
            print('CONNECTED')
            await asyncio.sleep(0.2)
    except Exception as e:
        print('CONNECT_ERROR', type(e).__name__, str(e))
        sys.exit(1)

if __name__ == '__main__':
    asyncio.run(main())
