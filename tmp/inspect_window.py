
import asyncio
import os
import json
import logging
import sys
from datetime import datetime, timedelta

# Ensure we can import from server/core
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from schwab.auth import easy_client
from server.core.config import CONFIG

async def inspect_window():
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("WindowInspector")

    # Use paths from CONFIG
    CREDENTIALS_FILE = os.path.expanduser('~/.api_keys/schwab/sli_api.json')
    TOKEN_FILE = os.path.expanduser('~/.api_keys/schwab/sli_token.json')
    ACCOUNT_ID = CONFIG.get('account_id')

    with open(CREDENTIALS_FILE, 'r') as f:
        creds = json.load(f)

    client = easy_client(
        api_key=creds['api_key'],
        app_secret=creds['api_secret'],
        callback_url=creds.get('callback_url', 'https://127.0.0.1'),
        token_path=TOKEN_FILE,
        asyncio=True,
        enforce_enums=False
    )

    resp = await client.get_account_numbers()
    account_hash = [acc.get('hashValue') for acc in resp.json() if acc.get('accountNumber') == ACCOUNT_ID][0]

    # Target window: 11:48 Chicago -> 16:48 UTC
    start = datetime(2026, 3, 31, 16, 40)
    end = datetime(2026, 3, 31, 17, 20)
    
    print(f"Inspecting ALL orders from {start} to {end} UTC...")
    
    resp = await client.get_orders_for_account(
        account_hash,
        from_entered_datetime=start,
        to_entered_datetime=end
    )

    if resp.status_code != 200:
        print(f"Error: {resp.status_code} {resp.text}")
        return

    orders = resp.json()
    orders.sort(key=lambda x: x.get('enteredTime', ''))

    for o in orders:
        time = o.get('enteredTime')
        status = o.get('status')
        oid = o.get('orderId')
        descr = o.get('statusDescription', '')
        
        print(f"[{time}] ID: {oid} | Status: {status:<10} | Desc: {descr[:50]}")
        if status == 'REJECTED' or 'Power' in descr:
             legs = o.get('orderLegCollection', [])
             print(f"   -> Legs({len(legs)}): " + " | ".join([f"{l.get('instruction')} {l.get('instrument', {}).get('symbol')}" for l in legs]))

if __name__ == "__main__":
    asyncio.run(inspect_window())
