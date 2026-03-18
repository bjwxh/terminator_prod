# live/spt_v4/session_manager.py

import json
import os
import logging
from datetime import datetime, date, time
from typing import Optional, Dict, Any, List
from .models import SubStrategy, Portfolio, OptionLeg, Trade, TradePurpose

class SessionManager:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.logger = logging.getLogger("SessionManager")

    def _validate_session_schema(self, state: Dict[str, Any]) -> bool:
        """Issue 10: Validate required fields in session JSON"""
        required_keys = ['version', 'date', 'sub_strategies', 'combined_portfolio']
        for key in required_keys:
            if key not in state:
                self.logger.error(f"Missing required key '{key}' in session file.")
                return False
        
        # Check sub_strategies is a dict
        if not isinstance(state['sub_strategies'], dict):
            self.logger.error("'sub_strategies' must be a dictionary.")
            return False
            
        return True

    def save_session(self, monitor) -> bool:
        """Save complete state to JSON file"""
        try:
            state = {
                'version': 1,
                'timestamp': datetime.now().isoformat(),
                'date': monitor.startup_time.date().isoformat(),
                'sub_strategies': self._serialize_sub_strategies(monitor.sub_strategies),
                'combined_portfolio': self._serialize_portfolio(monitor.combined_portfolio),
                'live_combined_portfolio': self._serialize_portfolio(monitor.live_combined_portfolio),
                'sent_order_ids': list(monitor.sent_order_ids),
                'order_to_strategy': monitor.order_to_strategy
            }
            
            temp_path = self.file_path + ".tmp"
            with open(temp_path, 'w') as f:
                json.dump(state, f, indent=2)
            os.replace(temp_path, self.file_path)
            return True
        except Exception as e:
            self.logger.error(f"Error saving session: {e}")
            return False

    def load_session(self) -> Optional[Dict[str, Any]]:
        """Load session if valid for today"""
        if not os.path.exists(self.file_path):
            return None
        
        try:
            with open(self.file_path, 'r') as f:
                state = json.load(f)
            
            if state.get('date') != date.today().isoformat():
                self.logger.info("Session file is from a different day. Archiving and starting fresh.")
                # Archive the old file (MMDD format)
                try:
                    file_date = state.get('date', 'unknown')
                    # Convert YYYY-MM-DD to MMDD if possible
                    suffix = file_date.replace('-', '')[-4:]
                    if len(suffix) != 4: suffix = file_date
                    
                    archive_path = self.file_path.replace('.json', f'_{suffix}.json')
                    os.replace(self.file_path, archive_path)
                    self.logger.info(f"Archived old session to {archive_path}")
                except Exception as ex:
                    self.logger.error(f"Failed to archive session: {ex}")
                return None
            
            # Issue 10: Schema validation
            if not self._validate_session_schema(state):
                self.logger.warning("Session file failed schema validation.")
                return None
                
            return state
        except Exception as e:
            self.logger.error(f"Error loading session: {e}")
            return None

    def _serialize_sub_strategies(self, sub_strategies: Dict[str, SubStrategy]) -> Dict[str, Any]:
        result = {}
        for sid, s in sub_strategies.items():
            result[sid] = {
                'sid': s.sid,
                'trade_start_time': s.trade_start_time.strftime('%H:%M:%S'),
                'has_traded_today': s.has_traded_today,
                'init_s_delta': s.init_s_delta,
                'init_l_delta': s.init_l_delta,
                'portfolio': self._serialize_portfolio(s.portfolio)
            }
        return result

    def _serialize_portfolio(self, p: Portfolio) -> Dict[str, Any]:
        return {
            'cash': p.cash,
            'max_margin': p.max_margin,
            'short_call_strike': p.short_call_strike,
            'long_call_strike': p.long_call_strike,
            'short_put_strike': p.short_put_strike,
            'long_put_strike': p.long_put_strike,
            'positions': [self._serialize_leg(l) for l in p.positions],
            'trades': [self._serialize_trade(t) for t in p.trades]
        }

    def _serialize_leg(self, l: OptionLeg) -> Dict[str, Any]:
        return {
            'symbol': l.symbol,
            'strike': l.strike,
            'side': l.side,
            'quantity': l.quantity,
            'delta': l.delta,
            'theta': l.theta,
            'price': l.price,
            'bid_price': l.bid_price,
            'ask_price': l.ask_price
        }

    def _serialize_trade(self, t: Trade) -> Dict[str, Any]:
        return {
            'timestamp': t.timestamp.isoformat(),
            'legs': [self._serialize_leg(l) for l in t.legs],
            'credit': t.credit,
            'commission': t.commission,
            'current_sum_delta': t.current_sum_delta,
            'purpose': t.purpose.value,
            'strategy_id': t.strategy_id,
            'order_id': t.order_id,
            'status': t.status
        }

    def restore_monitor(self, monitor, state: Dict[str, Any]):
        """Restore monitor state from loaded dict"""
        monitor.sent_order_ids = set(state.get('sent_order_ids', []))
        monitor.order_to_strategy = state.get('order_to_strategy', {})

        
        # Restore sub-strategies
        for sid, s_data in state['sub_strategies'].items():
            if sid in monitor.sub_strategies:
                s = monitor.sub_strategies[sid]
                s.has_traded_today = s_data['has_traded_today']
                s.init_s_delta = s_data['init_s_delta']
                s.init_l_delta = s_data['init_l_delta']
                self._restore_portfolio(s.portfolio, s_data['portfolio'])
        
        # Restore combined portfolio
        self._restore_portfolio(monitor.combined_portfolio, state['combined_portfolio'])

    def _restore_portfolio(self, p: Portfolio, p_data: Dict[str, Any]):
        p.cash = p_data['cash']
        p.max_margin = p_data['max_margin']
        p.short_call_strike = p_data['short_call_strike']
        p.long_call_strike = p_data['long_call_strike']
        p.short_put_strike = p_data['short_put_strike']
        p.long_put_strike = p_data['long_put_strike']
        
        p.positions = [self._restore_leg(l) for l in p_data['positions']]
        p.trades = [self._restore_trade(t) for t in p_data['trades']]
        
        # Rebuild position dict
        p._position_dict = {}
        for leg in p.positions:
            key = (leg.symbol, int(round(leg.strike)), leg.side)
            p._position_dict[key] = leg

    def _restore_leg(self, l_data: Dict[str, Any]) -> OptionLeg:
        return OptionLeg(
            symbol=l_data['symbol'],
            strike=l_data['strike'],
            side=l_data['side'],
            quantity=l_data['quantity'],
            delta=l_data['delta'],
            theta=l_data['theta'],
            price=l_data['price'],
            bid_price=l_data['bid_price'],
            ask_price=l_data['ask_price']
        )

    def _restore_trade(self, t_data: Dict[str, Any]) -> Trade:
        return Trade(
            timestamp=datetime.fromisoformat(t_data['timestamp']),
            legs=[self._restore_leg(l) for l in t_data['legs']],
            credit=t_data['credit'],
            commission=t_data['commission'],
            current_sum_delta=t_data.get('current_sum_delta', 0.0),
            purpose=TradePurpose(t_data['purpose']),
            strategy_id=t_data['strategy_id'],
            order_id=t_data.get('order_id'),
            status=t_data.get('status', 'simulation')
        )
