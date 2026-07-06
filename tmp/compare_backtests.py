import asyncio
import os
import sys
import json
import subprocess
from datetime import datetime, date
import pytz

CHICAGO = pytz.timezone('America/Chicago')

# Add project root to sys.path
sys.path.append(os.getcwd())

from server.core.config import CONFIG
from server.core.monitor import LiveTradingMonitor

async def main():
    target_date = date(2026, 6, 18)
    db_path = f"/Users/fw/data/options/options_20260618.db"
    
    # 1. Load config from terminator_rust/config.json
    with open("terminator_rust/config.json", "r") as f:
        rust_config = json.load(f)
        
    # Override python config dict
    for k, v in rust_config.items():
        CONFIG[k] = v
        
    CONFIG['db_path'] = db_path
    
    # Instantiate monitor with the config
    monitor = LiveTradingMonitor(config=CONFIG)
    monitor.db_path = db_path
    
    # Determine the end time based on the database or current time
    now_ct = datetime.now(CHICAGO)
    start_dt = CHICAGO.localize(datetime.combine(target_date, datetime.strptime("08:30:00", "%H:%M:%S").time()))
    # Rust uses Utc::now() converted to Chicago tz. Let's match it.
    end_dt = now_ct
    
    print(f"Running Python simulation from {start_dt} to {end_dt}...")
    
    # Run the simulation
    history = await monitor._run_historical_simulation(
        start_dt, 
        end_dt, 
        live_trades=[], 
        mode='hard', 
        collect_history=True
    )
    
    py_portfolio = monitor.combined_portfolio
    py_gross = py_portfolio.gross_pnl
    py_fees = py_portfolio.total_contracts * CONFIG['commission_per_contract'] * 2 # Wait, let's verify how fees are calculated in Rust/Python
    # Let's check the portfolio net pnl directly
    py_net = py_portfolio.net_pnl
    py_trades_count = len(py_portfolio.trades)
    
    print("\n=======================================================")
    print("           PYTHON BACKTEST RESULTS SUMMARY             ")
    print("=======================================================")
    print(f"Total Trades: {py_trades_count}")
    print(f"Gross PnL: ${py_gross:.2f}")
    print(f"Fees: ${py_portfolio.fees:.2f}")
    print(f"Net PnL: ${py_net:.2f}")
    print("=======================================================\n")
    
    # Print Python trades details
    py_trade_list = []
    for t in py_portfolio.trades:
        legs_str = "  |  ".join([
            f"{'BUY' if l.quantity > 0 else 'SELL'} {l.strike} {l.side} @{l.price:.2f}(Δ{l.delta:.3f})"
            for l in t.legs
        ])
        # Format similar to Rust
        ts_str = t.timestamp
        if hasattr(ts_str, 'strftime'):
            ts_str = ts_str.strftime("%Y-%m-%d %H:%M:%S")
        ts_trunc = ts_str[:19].replace(" ", "T")
        py_trade_list.append({
            'sid': t.strategy_id,
            'action': t.purpose.name if hasattr(t.purpose, 'name') else t.purpose,
            'ts': ts_trunc,
            'credit': round(t.credit, 2),
            'commission': round(t.commission, 2),
            'legs': [(l.quantity, l.strike, l.side, round(l.price, 2)) for l in t.legs]
        })

    # 2. Run Rust backtest and get output
    print("\nRunning Rust backtest...")
    rust_res = subprocess.run(
        ["cargo", "run", "--bin", "backtest_today"],
        cwd="terminator_rust",
        env={**os.environ, "CONFIG_PATH": "config.json"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    if rust_res.returncode != 0:
        print("Rust backtest failed:")
        print(rust_res.stderr)
        return
        
    rust_stdout = rust_res.stdout
    print("Rust backtest completed successfully.")
    
    # Parse Rust trades
    rust_trades = []
    for line in rust_stdout.splitlines():
        if "credit=$" in line and "strat_" in line:
            # Parse strategy_id, type/action, time, credit, commission, and legs
            parts = line.strip().split()
            # e.g., ["strat_0835", "REBAL", "2026-06-18T11:38:13", "credit=$", "-117.50", "commission=$2.26", ...]
            sid = parts[0]
            action = parts[1]
            ts = parts[2]
            # credit is parts[4], commission is parts[5].split('$')[-1]
            try:
                credit = float(parts[4])
                comm = float(parts[5].split("$")[-1])
                rust_trades.append({
                    'sid': sid,
                    'action': action,
                    'ts': ts,
                    'credit': credit,
                    'commission': comm,
                    'line': line.strip()
                })
            except Exception as e:
                pass
                
    # Write normalized trades to files for comparison
    py_lines = []
    for pt in sorted(py_trade_list, key=lambda x: (x['sid'], x['ts'], x['credit'])):
        py_lines.append(f"{pt['sid']} | {pt['ts']} | {pt['action']:15} | credit={pt['credit']:8.2f} | comm={pt['commission']:.2f}")
        
    rust_parsed_trades = []
    for rt in sorted(rust_trades, key=lambda x: (x['sid'], x['ts'], x['credit'])):
        rust_parsed_trades.append(f"{rt['sid']} | {rt['ts']} | {rt['action']:15} | credit={rt['credit']:8.2f} | comm={rt['commission']:.2f}")
        
    with open("tmp/trades_py.txt", "w") as f:
        f.write("\n".join(py_lines) + "\n")
        
    with open("tmp/trades_rust.txt", "w") as f:
        f.write("\n".join(rust_parsed_trades) + "\n")
        
    print(f"Saved {len(py_lines)} Python trades to tmp/trades_py.txt")
    print(f"Saved {len(rust_parsed_trades)} Rust trades to tmp/trades_rust.txt")
    
    # Run diff command to show mismatches
    print("\n--- Diff of Python vs Rust Trades ---")
    diff_res = subprocess.run(
        ["diff", "-u", "tmp/trades_py.txt", "tmp/trades_rust.txt"],
        stdout=subprocess.PIPE,
        text=True
    )
    print(diff_res.stdout)
            
    # Parse Rust totals from stdout
    rust_gross = 0.0
    rust_fees = 0.0
    rust_net = 0.0
    for line in rust_stdout.splitlines():
        if "Sim Total gross PnL" in line:
            rust_gross = float(line.split("$")[-1].strip())
        elif "Sim Total fees" in line:
            rust_fees = float(line.split("$")[-1].strip())
        elif "Sim Total net PnL" in line:
            rust_net = float(line.split("$")[-1].strip())

    # Compare
    gross_match = abs(py_gross - rust_gross) < 0.01
    fees_match = abs(py_portfolio.fees - rust_fees) < 0.01
    net_match = abs(py_net - rust_net) < 0.01
    
    print("\n=== COMPARISON RESULT ===")
    print(f"Gross PnL Match: {gross_match} (Py: {py_gross:.2f}, Rust: {rust_gross:.2f})")
    print(f"Fees Match:      {fees_match} (Py: {py_portfolio.fees:.2f}, Rust: {rust_fees:.2f})")
    print(f"Net PnL Match:   {net_match} (Py: {py_net:.2f}, Rust: {rust_net:.2f})")
    
    if gross_match and fees_match and net_match:
        print("\nSUCCESS: Python and Rust backtest results are 100% matched!")
    else:
        print("\nFAILURE: Backtest results mismatch!")

if __name__ == "__main__":
    asyncio.run(main())
