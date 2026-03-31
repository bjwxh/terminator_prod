
import sys
import os
import pandas as pd
from datetime import datetime, time
from typing import List, Dict

# Ensure we can import from server/core
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from server.core.monitor import LiveTradingMonitor, TradePurpose
from server.core.models import SubStrategy, OptionLeg, Trade, Portfolio
from server.core.config import CONFIG

class MockMonitor(LiveTradingMonitor):
    def __init__(self, config):
        self.config = config
        self.sub_strategies = {}
        self.logger = type('MockLogger', (), {'info': print, 'debug': lambda *a: None, 'error': print, 'warning': print})()
        self._data_lock = type('MockLock', (), {'__enter__': lambda s: s, '__exit__': lambda s, *a: None})()
        self.combined_portfolio = Portfolio()
        self.live_combined_portfolio = Portfolio()
        self.client = None
        self._option_cache = {}
        self.working_orders = []

    def set_sub_strategies(self, strats: List[SubStrategy]):
        self.sub_strategies = {s.sid: s for s in strats}

def create_mock_snapshot(strikes: List[float]) -> pd.DataFrame:
    data = []
    for s in strikes:
        for side in ['CALL', 'PUT']:
            data.append({
                'strike_price': float(s),
                'side': side,
                'delta': 0.15 if side == 'CALL' else -0.15,
                'mid_price': 2.50,
                'bid_price': 2.45,
                'ask_price': 2.55,
                'symbol': f"SPX_{int(s)}_{side}",
                'theta': -1.5
            })
    return pd.DataFrame(data)

def run_test_case(name: str, strat_trades: Dict[str, List[Trade]], monitor_config=CONFIG):
    print(f"\n{'='*20} {name} {'='*20}")
    monitor = MockMonitor(monitor_config)
    
    # Consolidation Step
    netted_list = monitor.net_trades(strat_trades)
    if not netted_list:
        print("No trades generated.")
        return

    netted = netted_list[0]
    print(f"Total Portfolio Discrepancy (Netted Legs):")
    for l in netted.legs:
        print(f"  - {l.side} {l.strike}: {l.quantity} contracts")

    # Execution Planning (Chunking)
    plan = monitor.create_execution_plan(netted)
    
    chunks = plan['to_submit']
    print(f"\nFinal Order Chunks for Schwab (Max 4 UNIQUE legs per order):")
    if not chunks:
        print("  (Empty - Likely already matched by working orders)")
    for i, c in enumerate(chunks):
        # We need to roll the legs for the final order display
        rolled = monitor._roll_legs(c)
        print(f"Order #{i+1}:")
        for l in rolled:
            print(f"  -> {l.side} {l.strike} | Size: {abs(l.quantity)} | Instruction: {getattr(l, 'instruction', 'N/A')}")

# --- SETUP TEST DATA ---

# Case 1: 3 Strategies entering 1-unit ICs at once
# Each strat has 4 legs. Total = 12 unit-legs but only 4 UNIQUE legs.
t1 = Trade(datetime.now(), [
    OptionLeg("SPX_6400_PUT", 6400, "PUT", 1), OptionLeg("SPX_6410_PUT", 6410, "PUT", -1),
    OptionLeg("SPX_6500_CALL", 6500, "CALL", -1), OptionLeg("SPX_6510_CALL", 6510, "CALL", 1)
], 0, 0, 0, TradePurpose.IRON_CONDOR, "s1")
trades_entry = {"s1": [t1], "s2": [t1], "s3": [t1]}

# Case 2: 3 Strategies rolling SAME Put side (6450 -> 6440)
# Each strat: +1 6450, -1 6440. Total: +3 6450, -3 6440.
t2 = Trade(datetime.now(), [
    OptionLeg("SPX_6450_PUT", 6450, "PUT", 1),
    OptionLeg("SPX_6440_PUT", 6440, "PUT", -1)
], 0,0,0, TradePurpose.REBALANCE_SHORT, "s")
trades_roll_same = {"s1": [t2], "s2": [t2], "s3": [t2]}

# Case 3: Mixed Roll (8 unique legs total)
# Strat 1: Roll Put 6450 -> 6440 (+1, -1)
# Strat 2: Roll Call 6550 -> 6560 (+1, -1)
# Strat 3: Entry new IC (4 different strikes)
t3_p = Trade(datetime.now(), [OptionLeg("SPX_6450_PUT", 6450, "PUT", 1), OptionLeg("SPX_6440_PUT", 6440, "PUT", -1)], 0,0,0, TradePurpose.REBALANCE_SHORT, "s1")
t3_c = Trade(datetime.now(), [OptionLeg("SPX_6550_CALL", 6550, "CALL", 1), OptionLeg("SPX_6560_CALL", 6560, "CALL", -1)], 0,0,0, TradePurpose.REBALANCE_SHORT, "s2")
t3_e = Trade(datetime.now(), [
    OptionLeg("SPX_6000_PUT", 6000, "PUT", 1), OptionLeg("SPX_6010_PUT", 6010, "PUT", -1),
    OptionLeg("SPX_6100_CALL", 6100, "CALL", -1), OptionLeg("SPX_6110_CALL", 6110, "CALL", 1)
],0,0,0, TradePurpose.IRON_CONDOR, "s3")
trades_mixed = {"s1": [t3_p], "s2": [t3_c], "s3": [t3_e]}

# Case 4: 12 UNIQUE legs (3 ICs at different strikes)
# Should result in 3 separate 4nd leg orders.
t4_a = Trade(datetime.now(), [OptionLeg("S1", 100, "PUT", 1), OptionLeg("S2", 110, "PUT", -1), OptionLeg("S3", 120, "CALL", -1), OptionLeg("S4", 130, "CALL", 1)], 0,0,0, TradePurpose.IRON_CONDOR, "a")
t4_b = Trade(datetime.now(), [OptionLeg("S5", 200, "PUT", 1), OptionLeg("S6", 210, "PUT", -1), OptionLeg("S7", 220, "CALL", -1), OptionLeg("S8", 230, "CALL", 1)], 0,0,0, TradePurpose.IRON_CONDOR, "b")
t4_c = Trade(datetime.now(), [OptionLeg("S9", 300, "PUT", 1), OptionLeg("S10", 310, "PUT", -1), OptionLeg("S11", 320, "CALL", -1), OptionLeg("S12", 330, "CALL", 1)], 0,0,0, TradePurpose.IRON_CONDOR, "c")
trades_12legs = {"a": [t4_a], "b": [t4_b], "c": [t4_c]}

# Run tests
if __name__ == "__main__":
    run_test_case("3-STRAT CONSOLIDATED ENTRY (SAME IC)", trades_entry)
    run_test_case("3-STRAT CONSOLIDATED ROLL (SAME STRIKE)", trades_roll_same)
    run_test_case("MULTI-STRAT MIXED (8 UNIQUE LEGS)", trades_mixed)
    run_test_case("12 UNIQUE LEGS (3 DIFFERENT ICs)", trades_12legs)
