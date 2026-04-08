from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, List, Any
from datetime import datetime
from zoneinfo import ZoneInfo
CHICAGO = ZoneInfo("America/Chicago")
from pydantic import BaseModel

router = APIRouter()

# Dependency override in main.py to provide the monitor instance
def get_monitor():
    # Placeholder: this will be replaced by app.dependency_overrides
    pass

@router.get("/status")
async def get_status(monitor = Depends(get_monitor)):
    return {
        "ts": datetime.now(CHICAGO).isoformat(),
        "status": monitor.status,
        "is_running": monitor.is_running,
        "broker_connected": monitor.broker_connected,
        "trading_enabled": monitor.trading_enabled,
        "heartbeat_failures": monitor.heartbeat_failures
    }

@router.post("/monitor/start")
async def start_monitor(monitor = Depends(get_monitor)):
    if monitor.is_running:
        return {"msg": "Monitor already running"}
    # The monitor is usually started by systemd or startup_event in main.py
    # But we can trigger a manual restart here if needed (e.g., after stop)
    import asyncio
    asyncio.create_task(monitor.run_live_monitor())
    return {"msg": "Monitor startup triggered"}

@router.post("/monitor/stop")
async def stop_monitor(monitor = Depends(get_monitor)):
    # monitor is_running = False shuts down subtasks eventually
    monitor.is_running = False
    return {"msg": "Monitor stop requested"}

@router.post("/trading/toggle")
async def toggle_trading(monitor = Depends(get_monitor)):
    new_state = not monitor.trading_enabled
    monitor.set_trading_enabled(new_state)
    state = "enabled" if monitor.trading_enabled else "disabled"
    return {"status": f"Trading {state}", "enabled": monitor.trading_enabled}

@router.post("/trading/reconnect")
async def reconnect_broker(monitor = Depends(get_monitor)):
    # Trigger a fresh initialization of the Schwab client
    import asyncio
    asyncio.create_task(monitor.initialize_schwab_client())
    return {"status": "Reconnect triggered"}

@router.get("/portfolio")
async def get_portfolio(monitor = Depends(get_monitor)):
    # Sim and Live combined
    return {
        "sim": [p.__dict__ for p in monitor.combined_portfolio.positions],
        "live": [p.__dict__ for p in monitor.live_combined_portfolio.positions],
        "sim_cash": monitor.combined_portfolio.cash,
        "live_cash": monitor.live_combined_portfolio.cash
    }

@router.get("/strategies")
async def get_strategies(monitor = Depends(get_monitor)):
    strats = []
    for sid, s in monitor.sub_strategies.items():
        strats.append({
            "sid": sid,
            "start_time": s.trade_start_time.isoformat(),
            "has_traded": s.has_traded_today,
            "positions": [p.__dict__ for p in s.portfolio.positions],
            "cash": s.portfolio.cash
        })
    return strats

@router.get("/orders/working")
async def get_working_orders(monitor = Depends(get_monitor)):
    return monitor.working_orders

@router.get("/trades")
async def get_trades(monitor = Depends(get_monitor)):
    # Return formatted list of trades from portfolios
    all_trades = sorted(monitor.combined_portfolio.trades, key=lambda x: x.timestamp, reverse=True)
    return [
        {
            "ts": t.timestamp.isoformat(),
            "sid": t.strategy_id,
            "purpose": t.purpose.value,
            "credit": t.credit,
            "status": t.status,
            "order_id": t.order_id,
            "legs": [{"sym": l.symbol, "strike": l.strike, "side": l.side, "qty": l.quantity} for l in t.legs]
        }
        for t in all_trades
    ]

@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str, monitor = Depends(get_monitor)):
    success = await monitor.cancel_order(order_id)
    if not success:
        raise HTTPException(status_code=500, detail="Cancel failed")
    return {"msg": f"Order {order_id} cancel requested"}

@router.post("/orders/{order_id}/chase")
async def chase_order(order_id: str, monitor = Depends(get_monitor)):
    success = await monitor.chase_order(order_id)
    if not success:
        raise HTTPException(status_code=500, detail="Chase failed")
    return {"msg": f"Order {order_id} chase/improvement requested"}

@router.post("/orders/cancel_all")
async def cancel_all_orders(monitor = Depends(get_monitor)):
    result = await monitor.cancel_all_orders()
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("msg", "Cancel all failed"))
    return result

@router.get("/session")
async def download_session(monitor = Depends(get_monitor)):
    # Return full session state as JSON (useful for EOD pull without scp)
    return monitor.session_manager.load_session()
