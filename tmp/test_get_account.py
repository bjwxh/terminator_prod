import asyncio
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from schwab.auth import easy_client

# Add server to path for imports if needed
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'server'))

async def test_consolidated_fetch():
    print("--- Schwab Consolidated get_account Test ---")
    
    # 1. Load Credentials
    creds_path = os.path.expanduser("~/.api_keys/schwab/sli_api.json")
    token_path = os.path.expanduser("~/.api_keys/schwab/sli_token.json")
    
    with open(creds_path, 'r') as f:
        creds = json.load(f)

    # 2. Initialize Client
    client = easy_client(
        api_key=creds['api_key'],
        app_secret=creds['api_secret'],
        callback_url=creds.get('callback_url', 'https://127.0.0.1'),
        token_path=token_path,
        asyncio=True,
        enforce_enums=False
    )
    
    # 3. Resolve Account Hash AND Audit Accounts
    print("Listing ALL available accounts...")
    resp = await client.get_account_numbers()
    account_hashes = {}
    if resp.status_code == 200:
        for acc in resp.json():
            num = str(acc.get('accountNumber'))
            h = acc.get('hashValue')
            print(f"  - Account: {num} | Hash: {h[:10]}...")
            account_hashes[num] = h
    
    target_hash = None
    for num, h in account_hashes.items():
        if num.endswith('9895'):
            target_hash = h
            print(f"\nTargeting account ending in 9895 (Hash: {target_hash[:10]}...)")
            break
            
    if not target_hash:
        print(f"ERROR: Could not find hash for account ending in 9895")
        return
    
    # focus on current trading day since 8:30am (chicago time)
    now = datetime.now(timezone.utc)
    # Machine is at -05:00 (CDT), so 8:30 AM Local is 13:30 UTC
    today_start = datetime.now().replace(hour=8, minute=30, second=0, microsecond=0)
    
    print(f"\nFetching orders entered since {today_start.isoformat()} (Chicago Time)...")
    resp_ord = await client.get_orders_for_account(
        target_hash,
        from_entered_datetime=today_start,
        to_entered_datetime=now
    )
    
    if resp_ord.status_code != 200:
        print(f"FAILED: Status {resp_ord.status_code}")
        print(resp_ord.text)
        return

    ord_data = sorted(resp_ord.json(), key=lambda x: x.get('enteredTime', ''), reverse=True)
    
    print("\nAudit: Last 20 Orders (Any Status) with detail:")
    for o in ord_data[:20]:
        print(f"\n  - {o.get('enteredTime')} | {o.get('status')} | ID: {o.get('orderId')}")
        legs = o.get('orderLegCollection', [])
        for leg in legs:
            instr = leg.get('instruction', 'N/A')
            sym = leg.get('instrument', {}).get('symbol', 'N/A')
            qty = leg.get('quantity', 'N/A')
            print(f"    * {instr} {qty} {sym}")
    
    # 5. Verify Data Presence
    print("\n--- RESULTS ---")
    print(f"[ORDERS] Found: {len(ord_data)} total orders in last 7 days")
    
    filled = [o for o in ord_data if o.get('status') == 'FILLED']
    working = [o for o in ord_data if o.get('status') in ['WORKING', 'QUEUED', 'ACCEPTED', 'PENDING_ACTIVATION']]
    
    print(f"  - Filled: {len(filled)}")
    print(f"  - Working: {len(working)}")

    if working:
        print("\n--- WORKING ORDERS ---")
        for o in working:
            status = o.get('status')
            legs = o.get('orderLegCollection', [{}])
            symbol = legs[0].get('instrument', {}).get('symbol', 'N/A')
            qty = o.get('quantity', 'N/A')
            price = o.get('price', o.get('stopPrice', 'MKT'))
            print(f"  * {status} | {symbol} | Qty: {qty} | Price: {price} | ID: {o.get('orderId')}")
    else:
        print("\n(No working orders found in the last 7 days)")

if __name__ == "__main__":
    asyncio.run(test_consolidated_fetch())
