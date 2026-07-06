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
        print("Available accounts:")
        print(nums_resp.json())
        return
        
    res = await client.get_account(account_hash, fields=['positions'])
    if res.status_code == 200:
        data = res.json()
        positions = data.get('securitiesAccount', {}).get('positions', [])
        print("POSITIONS:")
        for pos in positions:
            instr = pos.get('instrument', {})
            sym = instr.get('symbol')
            qty = pos.get('longQuantity', 0) - pos.get('shortQuantity', 0)
            mv = pos.get('marketValue', 0)
            avg_p = pos.get('averagePrice', 0)
            day_pnl = pos.get('currentDayProfitLoss', 0)
            print(f"  {sym}: qty={qty}, mv={mv}, avg_price={avg_p}, day_pnl={day_pnl}")
        
        # Get today's starting value or total PnL
        bal = data.get('securitiesAccount', {}).get('currentBalances', {})
        print("BALANCES:")
        print(f"  Liquidation Value: {bal.get('liquidationValue')}")
        print(f"  Cash Balance: {bal.get('cashBalance')}")
    else:
        print(f"Failed: {res.status_code} - {res.text}")

if __name__ == '__main__':
    asyncio.run(main())
