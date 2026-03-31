
import asyncio
import json
import logging
import os
import ssl
from datetime import datetime
from zoneinfo import ZoneInfo
from schwab.auth import easy_client
from schwab.streaming import StreamClient

# CONFIGURATION
TOKEN_FILE = os.path.expanduser('~/.api_keys/schwab/sli_token.json')
API_KEY_FILE = os.path.expanduser('~/.api_keys/schwab/sli_api.json')
CHICAGO = ZoneInfo("America/Chicago")

# SSL BYPASS
ssl._create_default_https_context = ssl._create_unverified_context

def handle_msg(msg):
    # LEVELONE_EQUITIES response structure
    content = msg.get('content', [])
    for entry in content:
        # Entry usually contains 'key' ($SPX, $VIX) and numbered fields
        key = entry.get('key')
        
        # In Schwab 1.5.1 LEVELONE_EQUITIES:
        # Field 1: QUOTE_TIME or TRADE_TIME (ms)
        # Field 3: LAST_PRICE
        ts_val = entry.get('1') or entry.get('QUOTE_TIME') or entry.get('TRADE_TIME')
        price = entry.get('3') or entry.get('LAST_PRICE')
        
        local_now = datetime.now(CHICAGO)
        
        print(f"[{local_now.strftime('%H:%M:%S.%f')}] Symbol: {key}")
        print(f"  RAW: {entry}")
        
        if ts_val:
            try:
                # Schwab timestamps are usually in ms since epoch
                ext_ts = datetime.fromtimestamp(float(ts_val)/1000, CHICAGO)
                diff = (local_now - ext_ts).total_seconds()
                print(f"  Exchange TS: {ext_ts.strftime('%H:%M:%S.%f')} (Diff: {diff:.3f}s)")
            except Exception as e:
                print(f"  Exchange TS (Raw Parsing Error): {ts_val} - {e}")
        
        if price:
            print(f"  Price: {price}")
        print("-" * 50)

async def main():
    if not os.path.exists(TOKEN_FILE) or not os.path.exists(API_KEY_FILE):
        print(f"Credentials not found at {TOKEN_FILE} or {API_KEY_FILE}!")
        return

    # Load API Key for easy_client
    with open(API_KEY_FILE, 'r') as f:
        api_data = json.load(f)
        api_key = api_data['api_key']

    # Initialize client (Async)
    client = easy_client(
        api_key=api_key,
        app_secret=None,
        callback_url=None,
        token_path=TOKEN_FILE,
        asyncio=True
    )
    
    stream_client = StreamClient(client)
    
    print("Logging into Streamer...")
    await stream_client.login()
    
    # Schwab v1.5.1: Indices come through LEVELONE_EQUITIES
    stream_client.add_level_one_equity_handler(handle_msg)
    
    symbols = ['$SPX', '$VIX']
    print(f"Subscribing to {symbols}...")
    await stream_client.level_one_equity_subs(symbols)
    
    print("Streaming started. Listening for 30 seconds (Ctrl+C to stop)...")
    
    try:
        # Listen for 30 seconds
        start_time = datetime.now()
        while (datetime.now() - start_time).total_seconds() < 30:
            await stream_client.handle_message()
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        print("Logging out...")
        await stream_client.logout()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nTest stopped.")
