import json
import urllib.request
import os
import datetime

token_path = "/Users/fw/.api_keys/schwab/sli_token.json"
with open(token_path) as f:
    token_data = json.load(f)
access_token = token_data.get("access_token")

# Get today's dates
now = datetime.datetime.now(datetime.timezone.utc)
from_time = now.replace(hour=0, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%S.000Z")
to_time = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
account_hash = "D2C87DB71EFD23C4712F2BA8906DF7B099831CF8" # I need to get the account hash! Let's get it via account numbers endpoint!

req = urllib.request.Request("https://api.schwabapi.com/trader/v1/accounts/accountNumbers", headers={"Authorization": f"Bearer {access_token}"})
with urllib.request.urlopen(req) as response:
    accounts = json.loads(response.read().decode())
    hash_val = next(acc["hashValue"] for acc in accounts if acc["accountNumber"] == "22229895")

req = urllib.request.Request(f"https://api.schwabapi.com/trader/v1/accounts/{hash_val}/orders?fromEnteredTime={from_time}&toEnteredTime={to_time}&status=FILLED", headers={"Authorization": f"Bearer {access_token}"})
with urllib.request.urlopen(req) as response:
    orders = json.loads(response.read().decode())
    for o in orders:
        print("ORDER ID:", o.get("orderId"))
        print("Limit Price:", o.get("price"))
        print("OrderActivityCollection:")
        print(json.dumps(o.get("orderActivityCollection", []), indent=2))
        print("-------------")
