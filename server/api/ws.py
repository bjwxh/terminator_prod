import asyncio
import json
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from datetime import datetime
from typing import List, Set
from core.config import CONFIG

router = APIRouter()
logger = logging.getLogger("API_WS")

# Track active connections
active_connections: Set[WebSocket] = set()

async def broadcast_state(monitor):
    """Periodically push monitor state to all connected clients"""
    while True:
        try:
            if not active_connections:
                await asyncio.sleep(1) # Sleep longer if no one listening
                continue
            
            # Prepare detailed state snapshot
            sim_p = monitor.combined_portfolio
            live_p = monitor.live_combined_portfolio
            
            state = {
                "ts": datetime.now().isoformat(),
                "status": monitor.status,
                "broker_connected": monitor.broker_connected,
                "trading_enabled": monitor.trading_enabled,
                "heartbeat_failures": monitor.heartbeat_failures,
                "working_orders": monitor.working_orders,
                "stats": {
                    "total_trades": monitor.stats.total_trades,
                    "winners": monitor.stats.winners,
                    "losers": monitor.stats.losers,
                    "win_rate": monitor.stats.win_rate,
                    "total_pnl": round(monitor.stats.total_pnl, 2),
                    "max_drawdown": round(monitor.stats.max_drawdown, 2),
                    "avg_duration": round(monitor.stats.avg_duration_minutes, 1)
                },
                "sim": {
                    "pnl": round(sim_p.current_pnl, 2),
                    "fees": round(sum(t.commission for t in sim_p.trades), 2),
                    "net_pnl": round(sim_p.current_pnl - sum(t.commission for t in sim_p.trades), 2),
                    "trades": len(sim_p.trades),
                    "recent_trades": [
                        {
                            "ts": t.timestamp.isoformat(),
                            "purpose": t.purpose.value,
                            "credit": round(t.credit, 2),
                            "strategy": t.strategy_id,
                            "legs": [{"symbol": l.symbol, "qty": l.quantity, "strike": l.strike, "side": l.side} for l in t.legs]
                        } for t in sim_p.trades[-10:]
                    ],
                    "delta": round(sim_p.total_delta, 4),
                    "theta": round(sim_p.total_theta, 2),
                    "positions": [
                        {"symbol": p.symbol, "strike": p.strike, "side": p.side, "qty": p.quantity, "pnl": round(p.current_day_pnl, 2)}
                        for p in sim_p.positions
                    ]
                },
                "live": {
                    "pnl": round(live_p.current_pnl, 2),
                    "fees": round(sum(t.commission for t in live_p.trades), 2),
                    "net_pnl": round(live_p.current_pnl - sum(t.commission for t in live_p.trades), 2),
                    "trades": len(live_p.trades),
                    "recent_trades": [
                        {
                            "ts": t.timestamp.isoformat(),
                            "purpose": t.purpose.value,
                            "credit": round(t.credit, 2),
                            "strategy": t.strategy_id,
                            "legs": [{"symbol": l.symbol, "qty": l.quantity, "strike": l.strike, "side": l.side} for l in t.legs]
                        } for t in live_p.trades[-10:]
                    ],
                    "delta": round(live_p.total_delta, 4),
                    "theta": round(live_p.total_theta, 2),
                    "positions": [
                        {"symbol": p.symbol, "strike": p.strike, "side": p.side, "qty": p.quantity, "pnl": round(p.current_day_pnl, 2)}
                        for p in live_p.positions
                    ]
                },
                "strategies": {},
                "logs": list(monitor.logs),
                "config": CONFIG
            }
            
            # Add sub-strategy level data
            for sid, s in monitor.sub_strategies.items():
                state["strategies"][sid] = {
                    "pnl": round(s.portfolio.current_pnl, 2),
                    "traded": s.has_traded_today,
                    "positions": [
                        {"symbol": p.symbol, "strike": p.strike, "side": p.side, "qty": p.quantity, "pnl": round(p.current_day_pnl, 2)}
                        for p in s.portfolio.positions
                    ],
                    "history": [
                        {
                            "ts": t.timestamp.isoformat(),
                            "purpose": t.purpose.value,
                            "credit": round(t.credit, 2),
                            "legs": [{"symbol": l.symbol, "qty": l.quantity, "strike": l.strike, "side": l.side} for l in t.legs]
                        } for t in s.portfolio.trades[-5:]
                    ]
                }

            message = json.dumps(state)
            
            # Broadcast to all
            dead_connections = []
            for connection in active_connections:
                try:
                    await connection.send_text(message)
                except Exception:
                    dead_connections.append(connection)
            
            for dc in dead_connections:
                active_connections.remove(dc)
                
            await asyncio.sleep(0.5) # 500ms cadence
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in WS broadcast loop: {e}")
            await asyncio.sleep(1)

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)
    logger.info(f"Client connected to WebSocket. Total clients: {len(active_connections)}")
    try:
        while True:
            # Keep connection alive, listen for any messages from client
            data = await websocket.receive_text()
            # Handle client commands if any
    except WebSocketDisconnect:
        active_connections.remove(websocket)
        logger.info(f"Client disconnected from WebSocket. Remaining: {len(active_connections)}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        if websocket in active_connections:
            active_connections.remove(websocket)
