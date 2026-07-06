import asyncio
import json
import httpx
from datetime import datetime
from pathlib import Path

CREDS_PATH = Path.home() / ".api_keys" / "schwab" / "schwab_token.json"

async def main():
    with open(CREDS_PATH) as f:
        token = json.load(f)
    
    today = datetime.now().strftime("%Y-%m-%d")
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.schwabapi.com/marketdata/v1/chains",
            headers={"Authorization": f"Bearer {token['token']['access_token']}"},
            params={
                "symbol": "$SPX",
                "strikeCount": "1",
                "fromDate": today,
                "toDate": today,
            }
        )
        data = resp.json()
        
        # Extract one call symbol
        try:
            exp_date = list(data['callExpDateMap'].keys())[0]
            strike = list(data['callExpDateMap'][exp_date].keys())[0]
            symbol = data['callExpDateMap'][exp_date][strike][0]['symbol']
            print(f"REST API Symbol Format: '{symbol}'")
        except Exception as e:
            print("Error parsing:", e)

if __name__ == "__main__":
    asyncio.run(main())
