import json
import os
import sys
from datetime import datetime, time, date

sys.path.append(os.getcwd())
from server.core.monitor import SubStrategy
from server.core.config import CONFIG
from eod.eod_report import LiveTradingMonitor
import pandas as pd

with open("tmp/snapshots_20260617.json") as f:
    snapshots = json.load(f)

monitor = LiveTradingMonitor(CONFIG, "dummy.db")

for time_str, records in snapshots.items():
    df = pd.DataFrame(records)
    df['mid_price'] = (df['bidprice'] + df['askprice']) / 2.0
    monitor._option_cache = {}
    
    hour, min = map(int, time_str.split(':'))
    ts = datetime.combine(date(2026, 6, 17), time(hour, min))
    
    s = SubStrategy(sid=f"strat_{time_str.replace(':', '')}", trade_start_time=time(hour, min))
    s.init_s_delta = 0.175
    s.init_l_delta = 0.025
    s.units = 1
    
    trade = monitor._check_entry(s, df, ts)
    if trade:
        print(f"\n[{time_str}] Python Trade:")
        print(f"  Net Credit: ${trade.credit:.2f}")
        for leg in trade.legs:
            action = "BUY" if leg.quantity > 0 else "SELL"
            print(f"    {action} {abs(leg.quantity)}x {leg.symbol} (Strike: {leg.strike}, Mid: {leg.price:.2f}, Delta: {leg.delta:.4f})")
    else:
        print(f"\n[{time_str}] No trade")
