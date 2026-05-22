import asyncio
import os
import sys
import pandas as pd
from datetime import date, datetime, time

# Add project root to sys.path
sys.path.append(os.getcwd())

from server.core.monitor import LiveTradingMonitor, CHICAGO

async def main():
    target_date = date(2026, 5, 21)
    db_path = "/Users/fw/data/options/options_20260521.db"
    
    start_dt = datetime.combine(target_date, time(8, 30), CHICAGO)
    end_dt = datetime.combine(target_date, time(15, 0), CHICAGO)
    
    monitor = LiveTradingMonitor()
    monitor.config['db_path'] = db_path
    monitor.db_path = db_path
    
    print("Running simulation and capturing strategy details...")
    history = await monitor._run_historical_simulation(start_dt, end_dt, collect_history=True)
    
    print("\n=========================================================================")
    # Print sub-strategy results
    for sid, s in monitor.sub_strategies.items():
        print(f"Strategy: {sid}")
        p = s.portfolio
        print(f"  Trades Count: {len(p.trades)}")
        print(f"  Gross PnL: ${p.gross_pnl:,.2f}")
        print(f"  Fees: ${p.fees:,.2f}")
        print(f"  Net PnL: ${p.net_pnl:,.2f}")
        print("  Trades Executed:")
        for t in p.trades:
            print(f"    - {t.timestamp.strftime('%H:%M:%S')} | {t.purpose.value:<15} | Credit/Cost: ${t.credit:+.2f} | Comm: ${t.commission:.2f}")
            for leg in t.legs:
                action = "BUY" if leg.quantity > 0 else "SELL"
                print(f"      {action} {abs(leg.quantity)}x {leg.symbol} | Strike: {leg.strike} | Mid/Entry: {leg.entry_price:.2f}")
        print("-" * 73)
        
    print("\n=========================================================================")
    print("Combined Simulation Portfolio Summary:")
    print(f"Total Trades: {len(monitor.combined_portfolio.trades)}")
    print(f"Gross PnL: ${monitor.combined_portfolio.gross_pnl:,.2f}")
    print(f"Fees: ${monitor.combined_portfolio.fees:,.2f}")
    print(f"Net PnL: ${monitor.combined_portfolio.net_pnl:,.2f}")
    print("=========================================================================\n")

if __name__ == "__main__":
    asyncio.run(main())
