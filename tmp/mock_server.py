import asyncio
import json
import logging
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
import uvicorn
import os

app = FastAPI()

# Mount static files from the repository
script_dir = os.path.dirname(os.path.abspath(__file__))
static_path = os.path.join(os.path.dirname(script_dir), "server", "static")
app.mount("/", StaticFiles(directory=static_path, html=True), name="static")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            state = {
                "broker_connected": True,
                "trading_enabled": True,
                "sim": {
                    "margin": 15000.0,
                    "trades": 12,
                    "net_pnl": 1245.50,
                    "delta": 0.052,
                    "fees": 5.0,
                    "pnl": 1250.50, # Gross
                    "realized": 800.00,
                    "unrealized": 450.50,
                    "theta": -85.20,
                    "recent_trades": [],
                    "positions": []
                },
                "live": {
                    "margin": 14200.0,
                    "trades": 10,
                    "net_pnl": 1090.20,
                    "delta": 0.041,
                    "fees": 10.0,
                    "pnl": 1100.20, # Gross
                    "realized": 700.00,
                    "unrealized": 400.20,
                    "theta": -78.50,
                    "recent_trades": [],
                    "positions": []
                },
                "working_orders": [],
                "strategies": {
                    "IC_PREEM": {
                        "pnl": 450.0,
                        "traded": True,
                        "positions": [{"symbol": "L1", "strike": 4800, "side": "CALL", "qty": -3, "pnl": 100, "delta": -0.05, "bid": 2.50, "ask": 2.70}],
                        "history": [
                            {"ts": datetime.now().isoformat(), "purpose": "iron_condor", "credit": 5000, "legs": [
                                {"symbol": "L1", "qty": -3, "strike": 4800, "side": "CALL"}
                            ]}
                        ]
                    }
                },
                "stats": {
                    "total_trades": 22, "winners": 15, "losers": 7, "win_rate": 0.68,
                    "total_pnl": 2350.70, "max_drawdown": -450.0, "avg_duration": 45.5
                },
                "logs": ["INFO: Update 9 fields system ready."],
                "config": {"trading_enabled": True}
            }
            await websocket.send_text(json.dumps(state))
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9000)
