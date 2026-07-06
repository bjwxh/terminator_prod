import sys
import os
import asyncio
import json
from pathlib import Path
from schwab.auth import easy_client

async def main():
    home_dir = Path.home()
    credentials_dir = home_dir / ".api_keys" / "schwab"
    
    with open(credentials_dir / "schwab_api.json", 'r') as f:
        creds = json.load(f)
        
    client = easy_client(
        api_key=creds['api_key'],
        app_secret=creds['api_secret'],
        callback_url=creds.get('callback_url', 'https://127.0.0.1'),
        token_path=str(credentials_dir / "schwab_token.json"),
        asyncio=True,
        enforce_enums=False
    )
    
    # Get option chain
    from datetime import date
    today = date.today()
    res = await client.get_option_chain('$SPX', strike_count=2, from_date=today, to_date=today)
    if res.status_code == 200:
        data = res.json()
        # Find a contract
        calls = data.get('callExpDateMap', {})
        for exp, strikes in calls.items():
            for strike, contracts in strikes.items():
                print(json.dumps(contracts[0], indent=2))
                return
    else:
        print(f"Failed: {res.status_code} - {res.text}")

if __name__ == '__main__':
    asyncio.run(main())
