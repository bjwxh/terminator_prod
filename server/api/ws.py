import asyncio
import json
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from api.routes import get_monitor
from datetime import datetime
from typing import List, Set, Dict, Any
from core.config import CONFIG

router = APIRouter()
logger = logging.getLogger("API_WS")

class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info(f"Client connected to WebSocket. Total clients: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info(f"Client disconnected from WebSocket. Remaining: {len(self.active_connections)}")

    async def broadcast(self, message: Dict[str, Any]):
        if not self.active_connections:
            return
            
        if not isinstance(message, str):
            json_msg = json.dumps(message)
        else:
            json_msg = message
            
        dead_connections = []
        for connection in list(self.active_connections):
            try:
                await connection.send_text(json_msg)
            except Exception:
                dead_connections.append(connection)
        
        for dc in dead_connections:
            self.disconnect(dc)

manager = ConnectionManager()

async def broadcast_state(monitor):
    """Periodically push monitor state to all connected clients"""
    while True:
        try:
            if not manager.active_connections:
                await asyncio.sleep(1) # Sleep longer if no one listening
                continue
            
            # Prepare detailed state snapshot UNDER LOCK (Bug 6d fix)
            with monitor._data_lock:
                sim_p = monitor.combined_portfolio
                live_p = monitor.live_combined_portfolio
                
                state = {
                    "ts": datetime.now().isoformat(),
                    "spx": monitor._last_spx_price if hasattr(monitor, '_last_spx_price') else None,
                    "status": monitor.status,
                    "broker_connected": monitor.broker_connected,
                    "trading_enabled": monitor.trading_enabled,
                    "heartbeat_failures": monitor.heartbeat_failures,
                    "working_orders": list(monitor.working_orders), # Snapshot list
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
                        "pnl": round(float(sim_p.gross_pnl), 2),
                        "fees": round(float(sim_p.fees), 2),
                        "net_pnl": round(float(sim_p.net_pnl), 2),
                        "realized": round(float(sim_p.realized_pnl), 2),
                        "unrealized": round(float(sim_p.unrealized_pnl), 2),
                        "margin": round(float(sim_p.calculate_standard_margin()), 2),
                        "trades": sim_p.total_contracts,
                        "recent_trades": [
                            {
                                "ts": t.timestamp.isoformat(),
                                "purpose": t.purpose.value,
                                "credit": round(float(t.credit), 2),
                                "strategy": t.strategy_id,
                                "legs": [{"symbol": l.symbol, "qty": l.quantity, "strike": l.strike, "side": l.side} for l in t.legs]
                            } for t in sim_p.trades[-10:]
                        ],
                        "delta": round(float(sim_p.total_delta), 4),
                        "theta": round(float(sim_p.total_theta), 2),
                        "positions": [
                            {
                                "symbol": p.symbol, "strike": p.strike, "side": p.side, "qty": p.quantity, 
                                "pnl": round(float(p.current_day_pnl), 2), "delta": round(float(p.delta), 4),
                                "bid": round(float(p.bid_price), 2), "ask": round(float(p.ask_price), 2)
                            }
                            for p in list(sim_p.positions) # Snapshot list
                        ]
                    },
                    "live": {
                        "pnl": round(float(live_p.gross_pnl), 2),
                        "fees": round(float(live_p.fees), 2),
                        "net_pnl": round(float(live_p.net_pnl), 2),
                        "realized": round(float(live_p.realized_pnl), 2),
                        "unrealized": round(float(live_p.unrealized_pnl), 2),
                        "margin": round(float(live_p.calculate_standard_margin()), 2),
                        "trades": live_p.total_contracts,
                        "recent_trades": [
                            {
                                "ts": t.timestamp.isoformat(),
                                "purpose": t.purpose.value,
                                "credit": round(float(t.credit), 2),
                                "strategy": t.strategy_id,
                                "legs": [{"symbol": l.symbol, "qty": l.quantity, "strike": l.strike, "side": l.side} for l in t.legs]
                            } for t in live_p.trades[-10:]
                        ],
                        "delta": round(float(live_p.total_delta), 4),
                        "theta": round(float(live_p.total_theta), 2),
                        "positions": [
                            {
                                "symbol": p.symbol, "strike": p.strike, "side": p.side, "qty": p.quantity, 
                                "pnl": round(float(p.current_day_pnl), 2), "delta": round(float(p.delta), 4),
                                "bid": round(float(p.bid_price), 2), "ask": round(float(p.ask_price), 2)
                            }
                            for p in list(live_p.positions) # Snapshot list
                        ]
                    },
                    "strategies": {},
                    "logs": list(monitor.logs),
                    "config": CONFIG
                }
                
                # Add sub-strategy level data
                for sid, s in monitor.sub_strategies.items():
                    state["strategies"][sid] = {
                        "pnl": round(float(s.portfolio.current_pnl), 2),
                        "traded": s.has_traded_today,
                        "positions": [
                            {
                                "symbol": p.symbol, "strike": p.strike, "side": p.side, "qty": p.quantity, 
                                "pnl": round(float(p.current_day_pnl), 2), "delta": round(float(p.delta), 4),
                                "bid": round(float(p.bid_price), 2), "ask": round(float(p.ask_price), 2)
                            }
                            for p in list(s.portfolio.positions)
                        ],
                        "history": [
                            {
                                "ts": t.timestamp.isoformat(),
                                "purpose": t.purpose.value,
                                "credit": round(float(t.credit), 2),
                                "legs": [{"symbol": l.symbol, "qty": l.quantity, "strike": l.strike, "side": l.side} for l in t.legs]
                            } for t in s.portfolio.trades[-5:]
                        ]
                    }

            await manager.broadcast({"type": "state_update", "state": state})
            await asyncio.sleep(0.5) # 500ms cadence
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in WS broadcast loop: {e}")
            await asyncio.sleep(1)

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, monitor = Depends(get_monitor)):
    await manager.connect(websocket)
    
    # Send initial history batch to populate charts on connect/refresh
    with monitor._data_lock:
        history = list(monitor.session_history)
    
    await websocket.send_text(json.dumps({
        "type": "history_init",
        "history": history
    }))
    
    try:
        while True:
            # Listen for actions from the client
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                action = msg.get("action")
                
                if action == "confirm_trade":
                    strat_id = msg.get("strat_id")
                    logger.info(f"Manual confirmation received via WS for strategy: {strat_id}")
                    # Ensure monitor handles this safely
                    await monitor.confirm_live_trade(strat_id)
                
                elif action == "dismiss_trade":
                    strat_id = msg.get("strat_id")
                    logger.info(f"Manual dismissal received via WS for strategy: {strat_id}")
                    await monitor.dismiss_live_trade(strat_id)
            
            except json.JSONDecodeError:
                logger.error("Failed to decode JSON message from client")
            except Exception as e:
                logger.error(f"Error processing WS message: {e}")
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)
