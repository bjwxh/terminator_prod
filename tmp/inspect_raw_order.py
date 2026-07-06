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
    
    nums_resp = await client.get_account_numbers()
    account_hash = None
    for acc in nums_resp.json():
        if acc.get('accountNumber') == "22229895":
            account_hash = acc.get('hashValue')
            break
            
    res = await client.get_order(1007018049835, account_hash)
    order = res.json()
    
    legs = order.get("orderLegCollection", [])
    activities = order.get("orderActivityCollection", [])
    
    print("LEGS:")
    for l in legs:
        print(f"  legId={l.get('legId')}, instruction={l.get('instruction')}, quantity={l.get('quantity')}")
        
    print("ACTIVITIES:")
    for activity in activities:
        if activity.get("activityType") == "EXECUTION":
            qty = activity.get("quantity", 0.0)
            exec_legs = activity.get("executionLegs", [])
            print(f"  activityId={activity.get('activityId')}, qty={qty}")
            net_price = 0.0
            for exec_leg in exec_legs:
                leg_id = exec_leg.get("legId")
                price = exec_leg.get("price", 0.0)
                # Find instruction
                instruction = ""
                for l in legs:
                    if l.get("legId") == leg_id:
                        instruction = l.get("instruction", "")
                print(f"    exec_leg legId={leg_id}, price={price}, instruction={instruction}")
                if instruction.startswith("SELL"):
                    net_price += price
                elif instruction.startswith("BUY"):
                    net_price -= price
            print(f"  net_price calculated={net_price}")

if __name__ == '__main__':
    asyncio.run(main())
