import asyncio
import os
import sys
import json
import logging
from datetime import datetime, date, timezone, timedelta

# Ensure we are in the project root
sys.path.append(os.getcwd())

from server.core.monitor import LiveTradingMonitor, CHICAGO
from server.core.config import CONFIG
from eod.eod_report import run_simulation
from schwab.auth import easy_client

async def main():
    target_date_str = '2026-03-23'
    target_date = date.fromisoformat(target_date_str)
    
    # Absolute DB path to avoid monitor.py resolution issues
    db_path = os.path.abspath('server/market_data_0323.db')
    
    # Initialize Monitor
    custom_config = CONFIG.copy()
    custom_config['db_path'] = db_path
    monitor = LiveTradingMonitor(config=custom_config)
    
    # 1. Pull Real Trades from Schwab
    print(f"Fetching Schwab trades for {target_date_str}...")
    
    # Credentials
    with open(os.path.expanduser('~/.api_keys/schwab/sli_api.json'), 'r') as f:
        creds = json.load(f)
        app_key = creds['api_key']
        app_secret = creds['api_secret']
        callback_url = creds['callback_url']

    client = easy_client(
        api_key=app_key,
        app_secret=app_secret,
        callback_url=callback_url,
        token_path=os.path.expanduser('~/.api_keys/schwab/sli_token.json')
    )
    
    resp = client.get_account_numbers()
    account_hash = resp.json()[0]['hashValue']
    
    # Range for 03-23
    start_dt = datetime(2026, 3, 23, 8, 30, tzinfo=CHICAGO).astimezone(timezone.utc)
    end_dt = datetime(2026, 3, 23, 15, 30, tzinfo=CHICAGO).astimezone(timezone.utc)
    
    resp_ord = client.get_orders_for_account(
        account_hash,
        from_entered_datetime=start_dt,
        to_entered_datetime=end_dt
    )
    
    if resp_ord.status_code != 200:
        print(f"Error fetching Schwab orders: {resp_ord.status_code}")
        return

    ord_data = resp_ord.json()
    filled_orders = [o for o in ord_data if o.get('status') == 'FILLED']
    print(f"Found {len(filled_orders)} filled orders.")
    
    # Convert to Trade objects and add to monitor (to calculate real pnl)
    monitor.client = client
    monitor.account_hash = account_hash
    real_trades = []
    for o in filled_orders:
        trades = monitor._convert_order_to_trade(o)
        real_trades.extend(trades)
        monitor.live_combined_portfolio.trades.extend(trades)
    
    monitor.live_combined_portfolio.cash = sum(t.credit for t in monitor.live_combined_portfolio.trades)

    # Calculate Real PnL using the monitor's portfolio
    rp = monitor.live_combined_portfolio
    real_gross_pnl = rp.gross_pnl
    real_net_pnl = rp.net_pnl
    real_contracts = rp.total_contracts
    real_trades_count = len(filled_orders)

    # 2. Run Simulation
    print(f"Running simulation for {target_date_str}...")
    # Pass absolute db_path AND real_trades for bootstrapping
    sim_data = await run_simulation(target_date, db_path=db_path)
    
    # 3. Format CSV Row
    # date,sim_trades,sim_gross_pnl,sim_net_pnl,real_trades,real_gross_pnl,real_net_pnl,sim_contracts,real_contracts
    
    row = [
        target_date_str,
        sim_data['sim_trades'],
        sim_data['sim_gross_pnl'],
        sim_data['sim_net_pnl'],
        real_trades_count,
        round(real_gross_pnl, 2),
        round(real_net_pnl, 2),
        float(sim_data['sim_contracts']),
        float(real_contracts)
    ]
    
    print("\nRECONSTRUCTED ROW:")
    print(",".join(map(str, row)))

if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR) # Suppress noise
    asyncio.run(main())
