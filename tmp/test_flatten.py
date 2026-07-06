import json
from datetime import datetime, timezone
import collections

# Define dummy objects to match server/core structure
class OptionLeg:
    def __init__(self, symbol, strike, side, quantity, price, entry_price):
        self.symbol = symbol
        self.strike = strike
        self.side = side
        self.quantity = quantity
        self.price = price
        self.entry_price = entry_price

class Trade:
    def __init__(self, timestamp, legs, credit, commission, current_sum_delta, purpose, strategy_id, order_id, status):
        self.timestamp = timestamp
        self.legs = legs
        self.credit = credit
        self.commission = commission
        self.current_sum_delta = current_sum_delta
        self.purpose = purpose
        self.strategy_id = strategy_id
        self.order_id = order_id
        self.status = status
        self._filled_units = 0.0

def _parse_schwab_symbol(symbol):
    # SPXW  260521C07460000 -> SPXW date call strike
    # Let's mock a simple parser
    # symbol looks like "SPXW  260521C07460000" or similar
    try:
        parts = symbol.split()
        if len(parts) >= 2:
            sym_part = parts[0]
            desc = parts[1]
            # extract strike (last 8 digits represent strike * 1000)
            strike_val = float(desc[-8:]) / 1000.0
            side = 'CALL' if 'C' in desc else 'PUT'
            return {'strike': strike_val, 'side': side}
    except Exception as e:
        pass
    return {'strike': 7000.0, 'side': 'CALL'}

def _flatten_orders(orders, parent_order_id=None):
    flattened = []
    for o in orders:
        children = o.get('childOrderStrategies', [])
        current_id = str(o.get('orderId', ''))
        strat_type = o.get('orderStrategyType', '')
        
        # Keep trace of the top parent order ID.
        top_parent_id = parent_order_id if parent_order_id is not None else current_id
        
        if children:
            if strat_type == 'FLATTEN':
                # Keep parent AND children for FLATTEN strategies
                if parent_order_id is not None:
                    o['_parent_order_id'] = parent_order_id
                flattened.append(o)
                flattened.extend(_flatten_orders(children, parent_order_id=top_parent_id))
            else:
                # Apply parent-skipping rule for other strategies
                flattened.extend(_flatten_orders(children, parent_order_id=top_parent_id))
        else:
            if parent_order_id is not None:
                o['_parent_order_id'] = parent_order_id
            flattened.append(o)
    return flattened

def _convert_order_to_trade(order) -> list:
    trades = []
    
    leg_fill_prices = {}
    leg_exec_qty = {}
    activities = order.get('orderActivityCollection', [])
    actual_net_cash = 0.0
    has_execution = False
    
    if activities:
        leg_id_to_instr = {str(l.get('legId')): l.get('instruction') for l in order.get('orderLegCollection', [])}
        for activity in activities:
            if activity.get('activityType') == 'EXECUTION':
                for exec_leg in activity.get('executionLegs', []):
                    lid = str(exec_leg.get('legId'))
                    ep = exec_leg.get('price', 0.0)
                    eq = exec_leg.get('quantity', 0)
                    
                    leg_fill_prices[lid] = ep
                    leg_exec_qty[lid] = leg_exec_qty.get(lid, 0) + eq
                    
                    instr = leg_id_to_instr.get(lid, '')
                    multiplier = 1.0 if 'SELL' in instr else -1.0
                    actual_net_cash += (ep * eq * multiplier)
                    has_execution = True

    legs = []
    for oleg in order.get('orderLegCollection', []):
        instr = oleg.get('instrument', {})
        symbol = instr.get('symbol', '')
        is_spx = (instr.get('underlyingSymbol') in ['$SPX', 'SPX', '$SPXW', 'SPXW'] or 
                  symbol.startswith('$SPX') or symbol.startswith('SPX'))
        
        if is_spx:
            parsed = _parse_schwab_symbol(symbol)
            if parsed:
                instruction = oleg.get('instruction', '')
                lid = str(oleg.get('legId'))
                
                qty = int(leg_exec_qty.get(lid, oleg.get('quantity', 0)))
                signed_qty = qty if 'BUY' in instruction else -qty
                
                fill_p = leg_fill_prices.get(lid, order.get('price', 0))
                
                legs.append(OptionLeg(
                    symbol=symbol,
                    strike=parsed['strike'],
                    side=parsed['side'],
                    quantity=signed_qty,
                    price=fill_p,
                    entry_price=fill_p
                ))
    
    if not legs:
        return []

    close_time_str = order.get('closeTime') or order.get('enteredTime')
    order_id_key = str(order.get('orderId', ''))
    
    if has_execution:
        credit = actual_net_cash * 100
    else:
        order_type = order.get('orderType', '')
        raw_price = order.get('price', 0)
        multiplier = 100 
        if order_type == 'NET_DEBIT':
            credit = -raw_price * multiplier
        elif order_type == 'NET_CREDIT':
            credit = raw_price * multiplier
        else:
            credit = (raw_price * multiplier) if legs[0].quantity < 0 else (-raw_price * multiplier)

    comm_per_contract = 1.13
    est_commission = comm_per_contract * sum(abs(l.quantity) for l in legs)
    
    order_legs = order.get('orderLegCollection', [])
    all_open = all(oleg.get('instruction', '').endswith('_OPEN') for oleg in order_legs)
    any_open = any(oleg.get('instruction', '').endswith('_OPEN') for oleg in order_legs)
    
    purpose = "EXIT" # Simple dummy
    strategy_id = "BROKER"

    trades.append(Trade(
        timestamp=close_time_str,
        legs=legs,
        credit=credit,
        commission=est_commission,
        current_sum_delta=0,
        purpose=purpose,
        strategy_id=strategy_id,
        order_id=order_id_key,
        status="filled"
    ))
    
    return trades

def run_test():
    with open("tmp/today_orders.json", "r") as f:
        orders = json.load(f)
        
    # Let's see the list of filled top-level orders and their strategy types
    print("TOP-LEVEL FILLED ORDERS:")
    for o in orders:
        if o.get('status') == 'FILLED':
            oid = o.get('orderId')
            qty = o.get('quantity')
            filled = o.get('filledQuantity')
            strat = o.get('orderStrategyType')
            c_strat = o.get('complexOrderStrategyType')
            has_children = len(o.get('childOrderStrategies', [])) > 0
            print(f"  ID: {oid} | Qty: {qty} | Filled: {filled} | Strat: {strat} | Complex: {c_strat} | HasChildren: {has_children}")

    flattened = _flatten_orders(orders)
    print(f"\nTotal raw orders in file: {len(orders)}")
    print(f"Total flattened orders: {len(flattened)}")
    
    print("\nFlattened orders detail:")
    for o in flattened:
        oid = o.get('orderId')
        parent_id = o.get('_parent_order_id')
        qty = o.get('quantity')
        filled_qty = o.get('filledQuantity')
        status = o.get('status')
        strategy = o.get('complexOrderStrategyType')
        strat_type = o.get('orderStrategyType')
        print(f"ID: {oid} | Parent: {parent_id} | Qty: {qty} | Filled: {filled_qty} | Status: {status} | Strategy: {strategy} | Type: {strat_type}")
        
    filled_flattened = [o for o in flattened if o.get('filledQuantity', 0) > 0]
    print(f"\nFilled flattened orders count: {len(filled_flattened)}")
    
    total_debits = 0
    total_commission = 0
    for o in filled_flattened:
        trades = _convert_order_to_trade(o)
        for t in trades:
            print(f"TRADE -> ID: {t.order_id} | Legs: {len(t.legs)} | Credit: {t.credit} | Comm: {t.commission} | Strat: {t.strategy_id}")
            for leg in t.legs:
                print(f"  Leg: {leg.symbol} | Qty: {leg.quantity} | Price: {leg.price}")
            if t.credit < 0:
                total_debits += t.credit
            total_commission += t.commission
            
    print(f"\nTotal Debits: {total_debits}")
    print(f"Total Commission: {total_commission}")

if __name__ == "__main__":
    run_test()
