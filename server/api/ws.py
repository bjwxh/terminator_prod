import asyncio
import json
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from api.routes import get_monitor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
CHICAGO = ZoneInfo("America/Chicago")
from typing import List, Set, Dict, Any
from core.config import CONFIG
import socket

import time
import numpy as np

class MonitorEncoder(json.JSONEncoder):
    """Custom JSON encoder to handle numpy types (Bug 20260402 Fix)"""
    def default(self, obj):
        if isinstance(obj, (np.integer, np.int64, np.int32)):
            return int(obj)
        if isinstance(obj, (np.floating, np.float64, np.float32)):
            return float(obj)
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, (datetime, ZoneInfo)):
            return str(obj)
        return super().default(obj)
router = APIRouter()
logger = logging.getLogger("API_WS")
APP_VERSION = str(int(time.time())) # Deploy tracking

class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.tick_count = 0

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
            json_msg = json.dumps(message, cls=MonitorEncoder)
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
    """Periodically push monitor state to all connected clients (PERF-3 fix)"""
    while True:
        try:
            if not manager.active_connections:
                await asyncio.sleep(1)
                continue
            
            # 1. Take FAST snapshot of state under lock
            # Bug 7 Fix: Capture all derived data and lists while under the lock 
            # to prevent runtime errors if lists change size during iteration.
            # 1. Take FAST snapshots of index data outside lock
            vix = monitor._last_vix_price # Atomic float read
            spx = monitor._last_spx_price # Atomic float read
            
            with monitor._data_lock:
                status = monitor.status
                broker_connected = monitor.broker_connected
                trading_enabled = monitor.trading_enabled
                heartbeat_failures = monitor.heartbeat_failures
                working_orders = []
                for o in monitor.working_orders:
                    # 1. Extraction of entered time (Chicago)
                    entered_str = "--:--:--"
                    et = o.get('enteredTime')
                    if et:
                        try:
                            # Schwab timestamp: "2026-03-30T14:15:22Z"
                            edt = datetime.fromisoformat(et.replace('Z', '+00:00')).astimezone(CHICAGO)
                            entered_str = edt.strftime('%H:%M:%S')
                        except: pass

                    # 2. Extract Leg Details
                    legs_coll = o.get('orderLegCollection', [])
                    leg_texts = []
                    total_qty = 0
                    for leg in legs_coll:
                        instr = leg.get('instrument', {})
                        sym = instr.get('symbol', 'N/A')
                        l_qty = int(leg.get('quantity', 0))
                        total_qty += l_qty
                        
                        # Use existing monitor helper to parse strikes/sides
                        parsed = monitor._parse_schwab_symbol(sym)
                        if parsed:
                            instruction = leg.get('instruction', '')
                            prefix = '-' if 'SELL' in instruction else '+'
                            side_ch = parsed['side'][0] # 'C' or 'P'
                            strike = int(parsed['strike'])
                            leg_texts.append(f"{prefix}{l_qty}{side_ch}{strike}")
                        else:
                            leg_texts.append(sym)

                    # 3. Determine Side (Credit/Debit)
                    order_type = o.get('orderType', '')
                    side = "---"
                    if 'CREDIT' in order_type: side = 'credit'
                    elif 'DEBIT' in order_type: side = 'debit'
                    elif legs_coll:
                        # Fallback for individual leg orders
                        side = 'debit' if 'BUY' in legs_coll[0].get('instruction', '') else 'credit'
                    
                    working_orders.append({
                        "time": entered_str,
                        "id": o.get('orderId'), 
                        "symbol": ", ".join(leg_texts),
                        "side": side,
                        "qty": total_qty, 
                        "price": o.get('price'), 
                        "status": o.get('status')
                    })
                logs = list(monitor.logs)
                
                # Helper to snapshot portfolio metrics
                def snap_p(p):
                    return {
                        "pnl": round(float(p.gross_pnl), 2),
                        "fees": round(float(p.fees), 2),
                        "net_pnl": round(float(p.net_pnl), 2),
                        "realized": round(float(p.realized_pnl), 2),
                        "unrealized": round(float(p.unrealized_pnl), 2),
                        "margin": round(float(p.current_margin), 2),
                        "trades": p.total_contracts,
                        "recent_trades": [
                            {
                                "ts": t.timestamp.isoformat(),
                                "purpose": t.purpose.value,
                                "credit": round(float(t.credit), 2),
                                "strategy": t.strategy_id,
                                "legs": [{"symbol": l.symbol, "qty": l.quantity, "strike": l.strike, "side": l.side} for l in t.legs]
                            } for t in p.trades
                        ],
                        "delta": round(float(p.total_delta), 4),
                        "theta": round(float(p.total_theta), 2),
                        "positions": [
                            {
                                "symbol": pos.symbol, "strike": pos.strike, "side": pos.side, "qty": pos.quantity, 
                                "pnl": round(float(pos.current_day_pnl), 2), "delta": round(float(pos.delta), 4),
                                "bid": round(float(pos.bid_price), 2), "ask": round(float(pos.ask_price), 2)
                            }
                            for pos in p.positions
                        ]
                    }

                sim_data = snap_p(monitor.combined_portfolio)
                live_data = snap_p(monitor.live_combined_portfolio)
                
                # Capture sub-strategy state
                # Scalability #4: Split payload into Fast (500ms heartbeat) and Slow (5s detailed) tiers
                strategies_data = {}
                if not hasattr(manager, 'tick_count'): manager.tick_count = 0
                manager.tick_count += 1
                
                # Only send detailed strategy breakdown every 10 ticks
                if manager.tick_count % 10 == 0:
                    for sid, s in monitor.sub_strategies.items():
                        strategies_data[sid] = {
                            "pnl": round(float(s.portfolio.current_pnl), 2),
                            "traded": s.has_traded_today,
                            "positions": [
                                {
                                    "symbol": p.symbol, "strike": p.strike, "side": p.side, "qty": p.quantity, 
                                    "pnl": round(float(p.current_day_pnl), 2), "delta": round(float(p.delta), 4),
                                    "bid": round(float(p.bid_price), 2), "ask": round(float(p.ask_price), 2)
                                }
                                for p in s.portfolio.positions
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
                else:
                    # Fast tier: just send minimal status for each strategy to keep PnL bars alive
                    for sid, s in monitor.sub_strategies.items():
                        strategies_data[sid] = {
                            "pnl": round(float(s.portfolio.current_pnl), 2),
                            "traded": s.has_traded_today
                        }

            # 2. Build and send OUTSIDE the lock
            spx_ts = monitor._last_spx_ts
            latency = 0
            if spx_ts > 0:
                # spx_ts is in milliseconds since epoch
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                latency = max(0, now_ms - spx_ts)
                
            # Resiliency Fix: Include pending trade in state so UI can re-home on refresh
            pending_trade_payload = None
            if monitor.pending_trade:
                try:
                    pending_trade_payload = monitor.get_trade_signal_payload(monitor.pending_trade)
                except Exception:
                    pass # Don't let transient serialization race break the heartbeat

            state = {
                "ts": datetime.now(CHICAGO).isoformat(),
                "exchange_ts": spx_ts, # In ms
                "latency_ms": latency,
                "spx": spx,
                "vix": vix,
                "server_name": socket.gethostname(),
                "status": status,
                "broker_connected": broker_connected,
                "trading_enabled": trading_enabled,
                "heartbeat_failures": heartbeat_failures,
                "working_orders": working_orders,
                "sim": sim_data,
                "live": live_data,
                "strategies": strategies_data,
                "logs": logs,
                "pending_trade": pending_trade_payload,
                "version": APP_VERSION
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
        "history": history,
        "config": CONFIG # P5 Fix: Send config once on connect
    }, cls=MonitorEncoder))
    
    # If there's an active trade awaiting confirmation, re-send signal 
    # so the UI can restore the modal after a page refresh (Bug 14 Fix)
    if monitor.pending_trade:
        payload = monitor.get_trade_signal_payload(monitor.pending_trade)
        payload["is_reconnect"] = True
        await websocket.send_text(json.dumps(payload, cls=MonitorEncoder))
    
    try:
        while True:
            # Listen for actions from the client
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                action = msg.get("action")
                
                if action == "confirm_trade":
                    strat_id = msg.get("strat_id")
                    overrides = msg.get("overrides") # Task #28: List of {idx, price_ea}
                    logger.info(f"Manual confirmation received via WS for strategy: {strat_id} with {len(overrides) if overrides else 0} overrides.")
                    await monitor.confirm_live_trade(strat_id, overrides=overrides)
                    # Broadcast to other clients to close their modals
                    await manager.broadcast({
                        "type": "trade_action",
                        "action": "close_modal",
                        "strat_id": strat_id
                    })
                
                elif action == "dismiss_trade":
                    strat_id = msg.get("strat_id")
                    logger.info(f"Manual dismissal received via WS for strategy: {strat_id}")
                    await monitor.dismiss_live_trade(strat_id)
                    # Broadcast to other clients to close their modals
                    await manager.broadcast({
                        "type": "trade_action",
                        "action": "close_modal",
                        "strat_id": strat_id
                    })
                
                elif action == "toggle_trade_pause":
                    is_paused = msg.get("is_paused", False)
                    monitor.is_trade_timer_paused = is_paused
                    logger.info(f"Trade timer {'PAUSED' if is_paused else 'RESUMED'} via WS")
                    # Sync other clients (Mac vs Mobile)
                    await manager.broadcast({
                        "type": "trade_action",
                        "action": "pause_sync",
                        "is_paused": is_paused
                    })
            
            except json.JSONDecodeError:
                logger.error("Failed to decode JSON message from client")
            except Exception as e:
                logger.error(f"Error processing WS message: {e}")
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)
