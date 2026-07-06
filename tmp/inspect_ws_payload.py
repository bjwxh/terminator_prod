import asyncio
import websockets
import json

async def main():
    uri = "ws://3.17.36.190:8081/ws"
    try:
        async with websockets.connect(uri) as websocket:
            print("Connected to WebSocket.")
            message = await websocket.recv()
            data = json.loads(message)
            print("SNAPSHOT STATE:")
            # Let's print out the relevant keys:
            print(f"  trading_enabled: {data.get('trading_enabled')}")
            print("  LIVE PORTFOLIO:")
            live = data.get('live', {})
            print(f"    pnl: {live.get('pnl')}")
            print(f"    net_pnl: {live.get('net_pnl')}")
            print(f"    realized: {live.get('realized')}")
            print(f"    unrealized: {live.get('unrealized')}")
            print(f"    cash: {live.get('cash')}")
            print(f"    positions count: {len(live.get('positions', []))}")
            for p in live.get('positions', []):
                print(f"      {p.get('symbol')}: qty={p.get('qty')}, price={p.get('price')}, entry_price={p.get('entry_price')}, day_pnl={p.get('current_day_pnl')}")
            print("  SIM PORTFOLIO:")
            sim = data.get('sim', {})
            print(f"    pnl: {sim.get('pnl')}")
            print(f"    net_pnl: {sim.get('net_pnl')}")
            print(f"    realized: {sim.get('realized')}")
            print(f"    unrealized: {sim.get('unrealized')}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    asyncio.run(main())
