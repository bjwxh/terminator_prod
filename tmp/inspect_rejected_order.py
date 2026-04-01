
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

async def fetch_rejected_orders():
    # Setup logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("RejectInspector")

    # Use paths from common CONFIG
    CREDENTIALS_FILE = CONFIG.get('credentials_file')
    TOKEN_FILE = CONFIG.get('token_file')
    ACCOUNT_ID = CONFIG.get('account_id')

    if not os.path.exists(CREDENTIALS_FILE):
        # Retry with schwab/ subfolder as fallback for local dev
        alt_creds = os.path.expanduser('~/.api_keys/schwab/sli_api.json')
        alt_token = os.path.expanduser('~/.api_keys/schwab/sli_token.json')
        if os.path.exists(alt_creds):
             logger.info(f"Using alternate path: {alt_creds}")
             CREDENTIALS_FILE = alt_creds
             TOKEN_FILE = alt_token
        else:
             logger.error(f"Credentials file not found at {CREDENTIALS_FILE}")
             return

    with open(CREDENTIALS_FILE, 'r') as f:
        creds = json.load(f)

    # Initialize Schwab Client
    client = easy_client(
        api_key=creds['api_key'],
        app_secret=creds['api_secret'],
        callback_url=creds.get('callback_url', 'https://127.0.0.1'),
        token_path=TOKEN_FILE,
        asyncio=True,
        enforce_enums=False
    )

    # Get Account Hash
    logger.info("Fetching account numbers...")
    resp = await client.get_account_numbers()
    if resp.status_code != 200:
        logger.error(f"Failed to get account numbers: {resp.status_code} {resp.text}")
        return

    account_hash = None
    for acc in resp.json():
        if acc.get('accountNumber') == ACCOUNT_ID:
            account_hash = acc.get('hashValue')
            break
    
    if not account_hash:
        logger.error(f"Account {ACCOUNT_ID} not found in account list.")
        # Fallback: take the only one if available
        if len(resp.json()) == 1:
            account_hash = resp.json()[0].get('hashValue')
            logger.info(f"Using available account hash: {account_hash}")
        else:
            return

    # Look back for "today" (today 11:48 AM Chicago is earlier today UTC)
    # Chicago is UTC-5. Now is roughly ~17:20 UTC (12:20 Chicago).
    # Fetch orders from the start of the day UTC
    now = datetime.utcnow()
    from_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    logger.info(f"Fetching orders from {from_dt} to {now} (UTC)...")
    
    # Fetch orders
    resp = await client.get_orders_for_account(
        account_hash,
        from_entered_datetime=from_dt,
        to_entered_datetime=now
    )

    if resp.status_code != 200:
        logger.error(f"Failed to fetch orders: {resp.status_code} {resp.text}")
        return

    orders = resp.json()
    logger.info(f"Found {len(orders)} total orders in window.")

    # Filter for Rejected orders
    rejected = [o for o in orders if o.get('status') == 'REJECTED']
    
    # Sort by time to find the 11:48 one
    rejected.sort(key=lambda x: x.get('enteredTime', ''), reverse=True)

    if not rejected:
        logger.info("No rejected orders found in the specified window.")
        return

    logger.info(f"Found {len(rejected)} REJECTED orders.")

    for o in rejected:
        oid = o.get('orderId')
        time = o.get('enteredTime')
        # statusDescription in Schwab is where detailed rejections are kept
        descr = o.get('statusDescription', 'N/A')
        
        # Check orderLegCollection for the 4-leg IC
        legs = o.get('orderLegCollection', [])
        
        print("\n" + "="*80)
        print(f"REJECTED ORDER ID: {oid}")
        print(f"Entered Time (UTC): {time}")
        print(f"Status Description: {descr}")
        
        # Look for explicit rejection reason in the strategy-level or nested fields
        rej_reason = o.get('rejectionReason', 'N/A')
        print(f"Rejection Reason Field: {rej_reason}")
        
        print(f"\nOrder Summary:")
        print(f"Strategy Type: {o.get('orderStrategyType')}")
        print(f"Session: {o.get('session')} | Duration: {o.get('duration')}")
        print(f"Price: {o.get('price', 'N/A')} | Complex: {o.get('complexOrderStrategyType', 'NONE')}")
        
        print(f"\nLegs ({len(legs)}):")
        for i, leg in enumerate(legs):
            instr = leg.get('instruction')
            qty = leg.get('quantity')
            symbol = leg.get('instrument', {}).get('symbol')
            print(f"  [{i+1}] {instr:<15} | Qty: {qty:>3} | {symbol}")
        
        print("="*80)

if __name__ == "__main__":
    asyncio.run(fetch_rejected_orders())
