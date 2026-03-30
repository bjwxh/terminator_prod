# live/terminator/models.py

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import List, Optional, Dict, Tuple, Set
import pandas as pd

class TradePurpose(Enum):
    IRON_CONDOR = "iron_condor"       # Initial entry
    REBALANCE_SHORT = "rebalance_short"  # Adjusting short leg
    REBALANCE_LONG = "rebalance_long"    # Adjusting long leg
    REBALANCE_NEW = "rebalance_new"      # Adding new spread (one side expired)
    EXIT = "exit"                         # Closing at end of day
    RECONCILIATION = "reconciliation"    # Syncing live with sim


@dataclass
class OptionLeg:
    symbol: str
    strike: float
    side: str        # "CALL" or "PUT"
    quantity: int    # Positive = long, Negative = short
    delta: float = 0.0
    theta: float = 0.0
    price: float = 0.0 # Current Mid
    entry_price: float = 0.0 # Cost basis
    bid_price: float = 0.0
    ask_price: float = 0.0
    target_delta: float = 0.0
    current_day_pnl: float = 0.0


    @property
    def mid_price(self) -> float:
        if self.bid_price > 0 and self.ask_price > 0:
            return (self.bid_price + self.ask_price) / 2
        return self.price

@dataclass
class Trade:
    timestamp: datetime
    legs: List[OptionLeg]
    credit: float
    commission: float
    current_sum_delta: float
    purpose: TradePurpose
    strategy_id: str
    order_id: Optional[str] = None
    status: str = "simulation" # "simulation", "pending", "filled", "cancelled"
    constituent_trades: List['Trade'] = field(default_factory=list)

class Portfolio:
    def __init__(self):
        self.positions: List[OptionLeg] = []
        self.cash: float = 0.0
        self.trades: List[Trade] = []
        
        self.max_margin: float = 0.0
        self.starting_market_value: float = 0.0
        
        # Strike tracking for reconstruction
        self.short_call_strike: Optional[float] = None
        self.long_call_strike: Optional[float] = None
        self.short_put_strike: Optional[float] = None
        self.long_put_strike: Optional[float] = None
        
        self._position_dict: Dict[Tuple[str, int, str], OptionLeg] = {}
        self._margin_dirty: bool = True
        self._cached_margin: float = 0.0

    @property
    def total_delta(self) -> float:
        return sum(p.delta * p.quantity for p in self.positions)

    @property
    def total_theta(self) -> float:
        return sum(p.theta * p.quantity for p in self.positions)

    @property
    def realized_pnl(self) -> float:
        """Net Realized PnL (Credits from closed legs - commissions for those legs)"""
        # In this session model, cash reflects entries. 
        # For intuitive realized/unrealized, we consider:
        # Realized = Total cash - entry credits of open positions - fees
        open_entry_credits = sum(p.entry_price * abs(p.quantity) * 100 for p in self.positions if p.quantity < 0)
        open_entry_costs = sum(p.entry_price * abs(p.quantity) * 100 for p in self.positions if p.quantity > 0)
        # Note: self.cash is the sum of ALL credits (entry and exit)
        # So Realized (closed legs) = self.cash - (entry credits of currently open shorts) + (entry costs of currently open longs) - fees
        return self.cash - open_entry_credits + open_entry_costs - self.fees

    @property
    def unrealized_pnl(self) -> float:
        """Gain/Loss since entry for open positions"""
        return sum((p.price - p.entry_price) * p.quantity * 100 for p in self.positions)

    @property
    def gross_pnl(self) -> float:
        """Total PnL before fees (Realized Gains + Unrealized Gains)"""
        # Unrealized is gain since entry. 
        # Realized (including entry credits) is self.cash - commissions.
        # So Gross = Cash + sum((price-entry)*qty*100) - current_open_entries
        # Actually simpler: Gross = Net + Fees
        # To avoid recursion, let's use the core formula:
        # Net = Cash + sum(price*qty*100) - Fees
        # Gross = Cash + sum(price*qty*100)
        return self.cash + sum(p.price * p.quantity * 100 for p in self.positions)

    @property
    def fees(self) -> float:
        """Total commissions for the current session"""
        return sum(t.commission for t in self.trades)

    @property
    def net_pnl(self) -> float:
        """Total PnL after fees (Gross PnL - Fees)"""
        return self.gross_pnl - self.fees

    @property
    def current_pnl(self) -> float:
        """Compatibility property for net_pnl"""
        return self.net_pnl

    @property
    def total_contracts(self) -> int:
        """Total number of contracts traded (sum of abs quantities across all legs)"""
        return sum(sum(abs(l.quantity) for l in t.legs) for t in self.trades)

    @property
    def current_margin(self) -> float:
        if self._margin_dirty:
            self._cached_margin = self.calculate_standard_margin()
            self.max_margin = max(self.max_margin, self._cached_margin)
            self._margin_dirty = False
        return self._cached_margin

    def calculate_standard_margin(self) -> float:
        """
        Calculate the standard Schwab Reg-T margin requirement for the portfolio.
        Evaluates net exposure at each strike point to find maximum potential risk.
        Formula: MAX(Total Call Side Risk, Total Put Side Risk) * 100
        """
        calls = [p for p in self.positions if p.side == 'CALL']
        puts = [p for p in self.positions if p.side == 'PUT']
        
        def calculate_side_risk(legs, side):
            if not legs: return 0.0
            
            # Find all potential vulnerability points (the strikes themselves)
            strikes = sorted(list(set([l.strike for l in legs])))
            # Add boundary strikes to check for uncapped risk
            test_strikes = [strikes[0] - 1.0] + strikes + [strikes[-1] + 1.0]
            
            max_risk = 0.0
            for ts in test_strikes:
                intrinsic_val = 0.0
                for l in legs:
                    if l.side == 'CALL':
                        # Call Intrinsic: Max(0, Price - Strike)
                        val = max(0.0, ts - l.strike)
                    else:
                        # Put Intrinsic: Max(0, Strike - Price)
                        val = max(0.0, l.strike - ts)
                    intrinsic_val += val * l.quantity
                
                # Risk is the net loss (negative of intrinsic value)
                risk = -intrinsic_val
                if risk > max_risk:
                    max_risk = risk
            
            return max_risk

        call_risk = calculate_side_risk(calls, 'CALL')
        put_risk = calculate_side_risk(puts, 'PUT')
        return max(call_risk, put_risk) * 100


    def _update_strikes(self, trade: Trade):
        """Scan current positions to update strike trackers for reconstruction."""
        self.short_call_strike = None
        self.long_call_strike = None
        self.short_put_strike = None
        self.long_put_strike = None
        
        for p in self.positions:
            if p.side == 'CALL':
                if p.quantity < 0: self.short_call_strike = p.strike
                elif p.quantity > 0: self.long_call_strike = p.strike
            elif p.side == 'PUT':
                if p.quantity < 0: self.short_put_strike = p.strike
                elif p.quantity > 0: self.long_put_strike = p.strike

    def _update_position(self, leg: OptionLeg):
        """Atomic-ish update for list and dict"""
        key = (leg.symbol, int(round(leg.strike)), leg.side)
        if key not in self._position_dict:
            self.positions.append(leg)
            self._position_dict[key] = leg
        # If already in dict, it's already in list (reference same object)

    def _remove_position(self, key: Tuple[str, int, str]):
        """Atomic-ish removal for list and dict"""
        if key in self._position_dict:
            leg = self._position_dict[key]
            if leg in self.positions:
                self.positions.remove(leg)
            del self._position_dict[key]

    def add_trade(self, trade: Trade):
        self.cash += trade.credit
        for leg in trade.legs:
            # Use integer strike keys for stability
            key = (leg.symbol, int(round(leg.strike)), leg.side)
            if key in self._position_dict:
                existing = self._position_dict[key]
                
                # If adding to same side, update weighted average price
                if (existing.quantity > 0 and leg.quantity > 0) or (existing.quantity < 0 and leg.quantity < 0):
                    total_q = existing.quantity + leg.quantity
                    if total_q != 0:
                        existing.entry_price = (existing.entry_price * existing.quantity + leg.price * leg.quantity) / total_q
                    existing.quantity = total_q
                else:
                    # Closing or partially closing
                    existing.quantity += leg.quantity
                
                if existing.quantity == 0:
                    self._remove_position(key)
            else:
                new_leg = OptionLeg(
                    symbol=leg.symbol,
                    strike=leg.strike,
                    side=leg.side,
                    quantity=leg.quantity,
                    delta=leg.delta,
                    theta=leg.theta,
                    price=leg.price, 
                    entry_price=leg.price, # Set initial entry price
                    bid_price=leg.bid_price,
                    ask_price=leg.ask_price
                )
                self._update_position(new_leg)
        
        self._update_strikes(trade)
        self.trades.append(trade)
        self._margin_dirty = True
        # Max margin is updated lazily via the property or explicitly if needed, 
        # but for safety we trigger a check here.
        _ = self.current_margin 

    def get_all_deltas(self, snap: Optional[pd.DataFrame] = None) -> Dict[str, float]:
        sc_d = lc_d = sp_d = lp_d = 0.0
        found_c = found_p = False
        
        for p in self.positions:
            val = p.delta * p.quantity
            if p.side == 'CALL':
                if p.quantity < 0: sc_d += val
                else: lc_d += val
                found_c = True
            else:
                if p.quantity < 0: sp_d += val
                else: lp_d += val
                found_p = True
        
        # Synthetic reconstruction for flat sides if snapshot provided
        if snap is not None:
            if not found_c and self.short_call_strike is not None:
                mask_s = (snap['strike_price'].round().astype(int) == int(round(self.short_call_strike))) & (snap['side'] == 'CALL')
                mask_l = (snap['strike_price'].round().astype(int) == int(round(self.long_call_strike))) & (snap['side'] == 'CALL')
                if not snap[mask_s].empty and not snap[mask_l].empty:
                    sc_d = -snap[mask_s]['delta'].iloc[0]
                    lc_d = snap[mask_l]['delta'].iloc[0]
            
            if not found_p and self.short_put_strike is not None:
                mask_sp = (snap['strike_price'].round().astype(int) == int(round(self.short_put_strike))) & (snap['side'] == 'PUT')
                mask_lp = (snap['strike_price'].round().astype(int) == int(round(self.long_put_strike))) & (snap['side'] == 'PUT')
                if not snap[mask_sp].empty and not snap[mask_lp].empty:
                    sp_d = -snap[mask_sp]['delta'].iloc[0]
                    lp_d = snap[mask_lp]['delta'].iloc[0]
                
        return {
            'abs_short_call_delta': abs(sc_d),
            'abs_long_call_delta': abs(lc_d),
            'abs_short_put_delta': abs(sp_d),
            'abs_long_put_delta': abs(lp_d)
        }

@dataclass
class TradeStats:
    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    avg_duration_minutes: float = 0.0
    
    @property
    def win_rate(self) -> float:
        if self.total_trades == 0: return 0.0
        return self.winners / self.total_trades

@dataclass
class SubStrategy:
    sid: str
    trade_start_time: time
    portfolio: Portfolio = field(default_factory=Portfolio)
    has_traded_today: bool = False
    
    # Delta targets
    init_s_delta: float = 0.0
    init_l_delta: float = 0.0
    unit_size: int = 1

    def __post_init__(self):
        # We'll initialize these from config in the monitor
        pass
