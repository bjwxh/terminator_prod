import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import json
import asyncio
from datetime import datetime

app = FastAPI()

# Mock data for preview
MOCK_STATE = {
    "ts": datetime.now().isoformat(),
    "status": "PREVIEW MODE",
    "broker_connected": True,
    "trading_enabled": False,
    "heartbeat_failures": 0,
    "sim": {
        "pnl": 12450.50,
        "fees": 125.40,
        "net_pnl": 12325.10,
        "trades": 45,
        "delta": 0.124,
        "theta": -840.50,
        "positions": [
            {"symbol": "SPX   260318C05900000", "strike": 5900, "side": "CALL", "qty": -10, "pnl": 1200.0},
            {"symbol": "SPX   260318P05800000", "strike": 5800, "side": "PUT", "qty": -10, "pnl": 850.0}
        ]
    },
    "live": {
        "pnl": 11840.20,
        "fees": 315.42,
        "net_pnl": 11524.78,
        "trades": 42,
        "delta": -0.045,
        "theta": -1140.20,
        "positions": [
            {"symbol": "SPX   260318C05900000", "strike": 5900, "side": "CALL", "qty": -10, "pnl": 1150.0},
            {"symbol": "SPX   260318P05800000", "strike": 5800, "side": "PUT", "qty": -10, "pnl": 820.0}
        ]
    },
    "strategies": {
        "strat_0900": {"pnl": 1450.20, "traded": True, "positions": 4},
        "strat_1000": {"pnl": -240.50, "traded": True, "positions": 4},
        "strat_1100": {"pnl": 0.00, "traded": False, "positions": 0}
    },
    "logs": [
        "15:30:00 - INFO - Monitor Started",
        "15:30:05 - INFO - Reconciling portfolio...",
        "15:30:10 - WARNING - Slippage detected: 0.15",
        "15:30:15 - ERROR - Connection dropped (Retrying...)"
    ]
}

@app.websocket("/ws")
async def websocket_endpoint(websocket):
    await websocket.accept()
    while True:
        await websocket.send_text(json.dumps(MOCK_STATE))
        await asyncio.sleep(1)

app.mount("/", StaticFiles(directory="server/static", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
