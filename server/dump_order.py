import sys
sys.path.append('/Users/fw/Git/terminator_prod')
from schwab.client import Client
from schwab.orders.generic import OrderBuilder
from schwab.orders.common import OrderStrategyType, ComplexOrderStrategyType, OrderType, Session, Duration, OptionInstruction

builder = OrderBuilder()
builder.set_order_strategy_type(OrderStrategyType.SINGLE)
builder.set_complex_order_strategy_type(ComplexOrderStrategyType.IRON_CONDOR)
builder.set_order_type(OrderType.NET_CREDIT)
builder.set_price("1.25")
builder.set_quantity(3)
builder.set_session(Session.NORMAL)
builder.set_duration(Duration.DAY)

builder.add_option_leg(OptionInstruction.SELL_TO_OPEN, "SPXW  260618C04000000", 1)
builder.add_option_leg(OptionInstruction.BUY_TO_OPEN, "SPXW  260618C04050000", 1)
builder.add_option_leg(OptionInstruction.SELL_TO_OPEN, "SPXW  260618P04000000", 1)
builder.add_option_leg(OptionInstruction.BUY_TO_OPEN, "SPXW  260618P03950000", 1)

import json
print(json.dumps(builder.build(), indent=2))
