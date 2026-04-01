import asyncio
import os
import sys
import pandas as pd
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import logging

CHICAGO = ZoneInfo("America/Chicago")

# Add project root to sys.path
root_dir = "/Users/fw/Git/terminator_prod"
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from server.core.monitor import LiveTradingMonitor
from server.core.models import TradePurpose, Portfolio, Trade
from server.core.utils import calculate_delta_decay

async def analyze():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    
    # 1. Initialize Monitor
    monitor = LiveTradingMonitor()
    monitor.db_path = "/Users/fw/Git/terminator/data/option_chain.db"
    
    target_date = datetime(2026, 3, 31).date()
    
    print(f"=== PART 1: Analyzing SPX Price Spike around 11:38 ({target_date}) ===")
    
    # We'll run the simulation and stop at 11:50 to see the spike
    start_dt = datetime.combine(target_date, time(8, 30), CHICAGO)
    end_dt = datetime.combine(target_date, time(12, 0), CHICAGO)
    
    history = await monitor._run_historical_simulation(
        start_dt, end_dt, 
        live_trades=[], 
        mode='hard', 
        collect_history=True
    )
    
    df = pd.DataFrame(history)
    df['timestamp'] = pd.to_datetime(df['ts'], format='ISO8601')
    
    # Focus on 11:30 to 11:45
    window = df[(df['timestamp'].dt.time >= time(11, 30)) & (df['timestamp'].dt.time <= time(11, 45))].copy()
    
    print("\nIntraday Data (11:30 - 11:45):")
    cols_to_show = ['ts', 'spx', 'sim_sc_strike', 'sim_sp_strike', 'sim_pnl']
    # Format ts for readability
    window['time'] = window['timestamp'].dt.strftime('%H:%M:%S')
    print(window[['time', 'spx', 'sim_sc_strike', 'sim_sp_strike', 'sim_pnl']].to_string(index=False))
    
    # Detailed check at 11:38
    spike_data = window[window['time'].str.startswith('11:38')]
    if not spike_data.empty:
        print(f"\nSPX at start of 11:38: {spike_data.iloc[0]['spx']:.2f}")
        print(f"SPX at end of 11:38: {spike_data.iloc[-1]['spx']:.2f}")
        change = spike_data.iloc[-1]['spx'] - spike_data.iloc[0]['spx']
        print(f"Change during 11:38: {change:+.2f}")
    
    print("\n=== PART 2: Simulating Gap Logic Hypothesis ===")
    print("Scenario: App disabled at 11:35, re-enabled at 11:45")
    
    # To simulate this, we need the state at 11:35 and 11:45
    # Let's run a fresh monitor for each point to be safe
    
    async def get_state_at(target_time):
        m = LiveTradingMonitor()
        m.db_path = "/Users/fw/Git/terminator/data/option_chain.db"
        end = datetime.combine(target_date, target_time, CHICAGO)
        await m._run_historical_simulation(start_dt, end, live_trades=[], mode='hard', collect_history=False)
        return m
    
    print("Fetching state at 11:35...")
    m_1135 = await get_state_at(time(11, 35))
    pos_1135 = m_1135.combined_portfolio.positions
    
    print("Fetching state at 11:45...")
    m_1145 = await get_state_at(time(11, 45))
    pos_1145 = m_1145.combined_portfolio.positions
    
    print("\nPositions at 11:35 (Frozen Live):")
    for p in pos_1135:
        print(f"  {p.quantity:+.0f} {p.side} {p.strike}")
        
    print("\nPositions at 11:45 (Desired Sim):")
    for p in pos_1145:
        print(f"  {p.quantity:+.0f} {p.side} {p.strike}")
        
    # Now run reconciliation logic
    print("\nTriggering Reconciliation (Gap Sync)...")
    
    # We need to mock the live portfolio in m_1145 to be the 11:35 positions
    # We create a dummy trade to populate the live portfolio with the 'frozen' positions
    m_1145.live_combined_portfolio = Portfolio()
    dummy_trade = Trade(
        timestamp=datetime.now(CHICAGO),
        legs=pos_1135,
        credit=sum(l.price * l.quantity * 100 for l in pos_1135),
        commission=0,
        current_sum_delta=0,
        purpose=TradePurpose.IRON_CONDOR,
        strategy_id="STUCK_POSITIONS"
    )
    m_1145.live_combined_portfolio.add_trade(dummy_trade)
        
    # We also need a snapshot of the option chain at 11:45 for reconciliation pricing
    # We can get it from the DB
    import sqlite3
    db_path = "/Users/fw/Git/terminator/data/option_chain.db"
    conn = sqlite3.connect(db_path)
    ts_1145 = datetime.combine(target_date, time(11, 45), CHICAGO).isoformat()
    # Find the nearest timestamp in DB
    query = "SELECT DISTINCT datetime FROM stock_options WHERE datetime >= ? ORDER BY datetime LIMIT 1"
    db_ts = pd.read_sql(query, conn, params=(ts_1145,)).iloc[0,0]
    
    query = """
        SELECT datetime, strike_price, side, bidprice, askprice, delta, theta, symbol 
        FROM stock_options 
        WHERE datetime = ? AND root_symbol = '$SPX' AND dte = 0
    """
    snap_1145 = pd.read_sql(query, conn, params=(db_ts,))
    snap_1145['mid_price'] = (snap_1145['bidprice'] + snap_1145['askprice']) / 2
    snap_1145['strike_int'] = snap_1145['strike_price'].round().astype(int)
    conn.close()
    
    await m_1145._check_reconciliation(snap_1145)
    
    recon_trade = m_1145.last_reconciliation_trade
    if recon_trade:
        print(f"\nGenerated Trade: {recon_trade.strategy_id} ({recon_trade.purpose})")
        print(f"Total Credit: ${recon_trade.credit/100:.2f}")
        print("Legs:")
        for l in recon_trade.legs:
            print(f"  {l.quantity:+.0f} {l.side} {l.strike} ({getattr(l, 'instruction', 'N/A')})")
            
        print("\nExecution Plan (Division of Orders):")
        plan = recon_trade.execution_plan
        for i, chunk in enumerate(plan['to_submit']):
            print(f"  Order {i+1}:")
            for l in chunk:
                # Instruction is not in chunk legs normally, deduce from side/qty
                print(f"    {l.quantity:+.0f} {l.side} {l.strike}")
    else:
        print("\nNo reconciliation trade generated (Positions match).")

if __name__ == "__main__":
    asyncio.run(analyze())
