import asyncio
import json
import websockets

async def main():
    uri = "ws://localhost:8081/ws"
    async with websockets.connect(uri) as ws:
        for _ in range(5):
            msg = await ws.recv()
            data = json.loads(msg)
            if data.get('type') == 'state_update':
                live = data['state']['live']
                print("NEW STATE:")
                print(f"  net_pnl: {live.get('net_pnl')}")
                print(f"  unrealized: {live.get('unrealized')}")
                print(f"  realized: {live.get('realized')}")
                print(f"  fees: {live.get('fees')}")
                break

asyncio.run(main())
