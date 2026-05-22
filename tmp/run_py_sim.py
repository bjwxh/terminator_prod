import asyncio
import os
import sys
import json
import logging
from datetime import date, datetime, time

# Configure logging to stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)

# Add project root to sys.path
sys.path.append(os.getcwd())

from server.core.monitor import LiveTradingMonitor, CHICAGO
from eod.eod_report import run_simulation

# Dictionary to capture option chain snapshots
snapshots = {}

# Monkeypatch LiveTradingMonitor._check_entry to capture snapshots
original_check_entry = LiveTradingMonitor._check_entry

def hooked_check_entry(self, s, snap, ts):
    time_str = ts.strftime("%H:%M")
    if time_str in ["09:01", "09:31", "10:01", "10:31"]:
        if time_str not in snapshots:
            logging.info(f"Capturing options chain snapshot at {time_str} ({ts})")
            # Select required columns and copy
            cols = ['datetime', 'strike_price', 'side', 'bidprice', 'askprice', 'delta', 'theta', 'symbol']
            records = snap[cols].copy()
            records['datetime'] = records['datetime'].astype(str)
            snapshots[time_str] = records.to_dict(orient='records')
    return original_check_entry(self, s, snap, ts)

LiveTradingMonitor._check_entry = hooked_check_entry

async def main():
    target_date = date(2026, 5, 21)
    db_path = "/Users/fw/data/options/options_20260521.db"
    
    print(f"Running Python simulation for {target_date} using {db_path}...")
    sim_data = await run_simulation(target_date, db_path=db_path)
    
    print("\n=======================================================")
    print("           PYTHON SIMULATION RESULTS SUMMARY           ")
    print("=======================================================")
    print(f"Total Trades: {sim_data['sim_trades']}")
    print(f"Total Contracts: {sim_data['sim_contracts']}")
    print(f"Gross PnL: ${sim_data['sim_gross_pnl']:.2f}")
    print(f"Net PnL: ${sim_data['sim_net_pnl']:.2f}")
    print("=======================================================\n")
    
    # Save the snapshots
    snapshot_path = "tmp/snapshots_20260521.json"
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
    with open(snapshot_path, "w") as f:
        json.dump(snapshots, f, indent=2)
    print(f"Successfully captured and saved {len(snapshots)} snapshots to {snapshot_path}")
    
    # Print sub-strategy entry breakdown
    history = sim_data['history']
    print(f"History records found: {len(history)}")

if __name__ == "__main__":
    asyncio.run(main())
