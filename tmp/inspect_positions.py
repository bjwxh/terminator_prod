
import asyncio
import json
import os
import sys
from schwab.auth import easy_client

async def inspect_account_positions():
    creds_path = os.path.expanduser("~/.api_keys/schwab/sli_api.json")
    token_path = os.path.expanduser("~/.api_keys/schwab/sli_token.json")
    
    with open(creds_path, 'r') as f:
        creds = json.load(f)

    client = easy_client(
        api_key=creds['api_key'],
        app_secret=creds['api_secret'],
        callback_url=creds.get('callback_url', 'https://127.0.0.1'),
        token_path=token_path,
        asyncio=True,
        enforce_enums=False
    )
    
    resp = await client.get_account_numbers()
    target_hash = None
    if resp.status_code == 200:
        for acc in resp.json():
            if str(acc.get('accountNumber')).endswith('9895'):
                target_hash = acc.get('hashValue')
                break
    
    if not target_hash:
        print("ERROR: Could not find account 9895")
        return

    # Fetch with positions
    resp = await client.get_account(target_hash, fields=client.Account.Fields.POSITIONS)
    if resp.status_code == 200:
        data = resp.json()
        positions = data.get('securitiesAccount', {}).get('positions', [])
        print(f"Found {len(positions)} positions.")
        for p in positions:
            # Print keys at top level and keys in instrument
            print(f"\nPosition Keys: {list(p.keys())}")
            instr = p.get('instrument', {})
            print(f"Instrument Keys: {list(instr.keys())}")
            print(f"Sample Data: Symbol={instr.get('symbol')}, LongQ={p.get('longQuantity')}, ShortQ={p.get('shortQuantity')}")
            # Check for flattened keys just in case
            if 'quantity' in p: print(f"ALARM: 'quantity' found at top level: {p['quantity']}")
            if 'symbol' in p: print(f"ALARM: 'symbol' found at top level: {p['symbol']}")
    else:
        print(f"Failed to fetch account: {resp.status_code} {resp.text}")

if __name__ == "__main__":
    asyncio.run(inspect_account_positions())
