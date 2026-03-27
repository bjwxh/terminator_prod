import asyncio
import json
import os
from schwab.auth import easy_client

TOKEN_FILE = os.path.expanduser('~/.api_keys/schwab/sli_token.json')
API_KEY_FILE = os.path.expanduser('~/.api_keys/schwab/sli_api.json')

async def main():
    with open(API_KEY_FILE, 'r') as f:
        creds = json.load(f)

    client = easy_client(
        api_key=creds['api_key'],
        app_secret=creds['api_secret'],
        callback_url=creds.get('callback_url', 'https://127.0.0.1:8183'),
        token_path=TOKEN_FILE,
        asyncio=True
    )
    
    symbols = ['$SPX', '$VIX', 'SPY', 'VIX']
    print(f"Checking quotes for {symbols}...")
    r = await client.get_quotes(symbols)
    if r.status_code == 200:
        data = r.json()
        for sym, quote in data.items():
            # Check the 'assetMainType' or equivalent
            asset_type = quote.get('assetMainType', 'Unknown')
            last_price = quote.get('quote', {}).get('lastPrice') or quote.get('lastPrice')
            print(f"Symbol: {sym} | Type: {asset_type} | Last: {last_price}")
    else:
        print(f"Error: {r.status_code} - {r.text}")

if __name__ == "__main__":
    asyncio.run(main())
