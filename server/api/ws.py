import asyncio
import json
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from datetime import datetime
from typing import List, Set

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
            
            # Prepare state snapshot
            # This follows the format in §5.2.2 of PRODUCTION_PLAN.md
            state = {
                "ts": datetime.now().isoformat(),
                "status": monitor.status,
                "broker_connected": monitor.broker_connected,
                "trading_enabled": monitor.trading_enabled,
                "live_pnl": 0.0, # (compute live PNL) 
                "sim_pnl": 0.0, # (compute sim PNL)
                "heartbeat_failures": monitor.heartbeat_failures,
                # Add more fields from monitor as needed for the UI
            }
            
            # Simple PnL computation for now
            # (Need to reach into monitor.combined_portfolio/live_combined_portfolio)
            try:
                # Combined Sim PnL
                sim_gross = monitor.combined_portfolio.cash
                for p in monitor.combined_portfolio.positions:
                    sim_gross += p.price * p.quantity * 100
                state["sim_pnl"] = round(sim_gross, 2)
                
                # Live PnL
                live_gross = monitor.live_combined_portfolio.cash
                for p in monitor.live_combined_portfolio.positions:
                    live_gross += p.price * p.quantity * 100
                state["live_pnl"] = round(live_gross, 2)
            except Exception as e:
                logger.error(f"Error computing PnL for WS: {e}")

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
