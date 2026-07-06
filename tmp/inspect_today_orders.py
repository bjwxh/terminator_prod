import sys
import os
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from schwab.auth import easy_client

async def main():
    home_dir = Path.home()
    credentials_dir = home_dir / ".api_keys" / "schwab"
    
    with open(credentials_dir / "sli_api.json", 'r') as f:
        creds = json.load(f)
        
    client = easy_client(
        api_key=creds['api_key'],
        app_secret=creds['api_secret'],
        callback_url=creds.get('callback_url', 'https://127.0.0.1'),
        token_path=str(credentials_dir / "sli_token.json"),
        asyncio=True,
        enforce_enums=False
    )
    
    # We want to check our target account's hash and positions
    nums_resp = await client.get_account_numbers()
    account_hash = None
    if nums_resp.status_code == 200:
        for acc in nums_resp.json():
            if acc.get('accountNumber') == "22229895":
                account_hash = acc.get('hashValue')
                break
                
    if not account_hash:
        print("Target account hash not found!")
        return
        
    from datetime import date
    today = date.today()
    res = await client.get_orders_for_account(
        account_hash, 
        from_entered_datetime=datetime.combine(today, datetime.min.time()), 
        to_entered_datetime=datetime.now(), 
        status='FILLED'
    )
    if res.status_code == 200:
        orders = res.json()
        print(f"Total filled orders today: {len(orders)}")
        total_realized_cash = 0.0
        for o in orders:
            order_id = o.get('orderId')
            close_time = o.get('closeTime')
            price = o.get('price')
            # For options, price is per share, so multiply by 100 * quantity
            # Wait, let's look at the legs to see direction and quantity
            legs = o.get('orderLegCollection', [])
            leg_desc = ", ".join([f"{l.get('instruction')} {l.get('quantity')} {l.get('instrument', {}).get('symbol')}" for l in legs])
            print(f"  Order {order_id} at {close_time}: price={price}, legs=[{leg_desc}]")
    else:
        print(f"Failed: {res.status_code} - {res.text}")

if __name__ == '__main__':
    asyncio.run(main())
