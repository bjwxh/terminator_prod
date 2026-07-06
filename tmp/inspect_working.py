import asyncio
import websockets
import json

async def main():
    uri = "ws://localhost:8081/ws"
    async with websockets.connect(uri) as websocket:
        while True:
            msg = await websocket.recv()
            state = json.loads(msg)
            # Check if it has the state update structure
            if isinstance(state, dict) and state.get("type") == "state_update":
                inner_state = state.get("state", {})
                print("=== WORKING ORDERS ===")
                print(json.dumps(inner_state.get("working_orders"), indent=2))
                break

asyncio.run(main())
