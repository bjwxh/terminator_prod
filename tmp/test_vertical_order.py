import json
import os
from schwab.auth import easy_client
from schwab.orders.generic import OrderBuilder
from schwab.orders.common import OrderStrategyType, OrderType, Session, Duration, ComplexOrderStrategyType, OptionInstruction

# Use local paths
cred_file = os.path.expanduser('~/.api_keys/schwab/sli_api.json')
token_file = os.path.expanduser('~/.api_keys/schwab/sli_token.json')
account_id = '22229895'

creds = json.load(open(cred_file))
client = easy_client(
    api_key=creds['api_key'],
    app_secret=creds['api_secret'],
    callback_url=creds['callback_url'],
    token_path=token_file
)

resp = client.get_account_numbers()
account_hash = next(a['hashValue'] for a in resp.json() if a['accountNumber'] == account_id)

qty_units = 2
leg_qty_total = 2 # Total contracts for this leg

builder = OrderBuilder()
builder.set_order_strategy_type(OrderStrategyType.SINGLE)
builder.set_complex_order_strategy_type(ComplexOrderStrategyType.VERTICAL)
builder.set_order_type(OrderType.NET_DEBIT)
builder.set_price("0.10") # Set $0.10 for the test
builder.set_quantity(qty_units)
builder.set_session(Session.NORMAL)
builder.set_duration(Duration.DAY)

# Target: +1 6750C / -1 6760C (Vertical Call Debit Spread)
long_sym = "SPXW  260408C06750000"
short_sym = "SPXW  260408C06760000"

# Fix: Leg quantity must be the TOTAL quantity for that leg (matches top-level set_quantity)
builder.add_option_leg(OptionInstruction.BUY_TO_OPEN, long_sym, leg_qty_total)
builder.add_option_leg(OptionInstruction.SELL_TO_OPEN, short_sym, leg_qty_total)

order_json = builder.build()
print("Generated Order JSON:")
print(json.dumps(order_json, indent=2))

print("\nPlacing order...")
resp = client.place_order(account_hash, order_json)
print(f"Status Code: {resp.status_code}")
if resp.status_code in [200, 201]:
    print("Order placed successfully.")
else:
    print(resp.text)
