import asyncio
import json
import os
from schwab.auth import easy_client

async def main():
    creds_path = os.path.expanduser("~/.api_keys/schwab/schwab_api.json")
    token_path = os.path.expanduser("~/.api_keys/schwab/schwab_token.json")
    
    print(f"Reading credentials from: {creds_path}")
    print(f"Reading tokens from: {token_path}")
    
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
    
    print("Testing get_account_numbers...")
    resp_numbers = await client.get_account_numbers()
    print(f"Status Code: {resp_numbers.status_code}")
    print(f"Response: {resp_numbers.text}")
    
    print("\nTesting user preferences...")
    session = client.session
    resp_prefs = await session.get("https://api.schwabapi.com/trader/v1/userPreference")
    print(f"Preferences Status Code: {resp_prefs.status_code}")
    print(f"Preferences Response: {resp_prefs.text}")

if __name__ == '__main__':
    asyncio.run(main())
