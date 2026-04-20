
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

def handle_option_msg(msg):
    """
    LEVELONE_OPTIONS response structure handler
    """
    content = msg.get('content', [])
    for entry in content:
        key = entry.get('key') # Ticker
        
        # Mapping for LEVELONE_OPTIONS (Standard fields in schwab-py)
        # 1: BID_PRICE
        # 2: ASK_PRICE
        # 3: LAST_PRICE
        # 8: TOTAL_VOLUME
        
        bid = entry.get('1') or entry.get('BID_PRICE')
        ask = entry.get('2') or entry.get('ASK_PRICE')
        last = entry.get('3') or entry.get('LAST_PRICE')
        volume = entry.get('8') or entry.get('TOTAL_VOLUME')
        
        local_now = datetime.now(CHICAGO)
        
        print(f"[{local_now.strftime('%H:%M:%S.%f')}] Symbol: {key}")
        if bid and ask:
            print(f"  Quote: {bid} / {ask}")
        if last:
            print(f"  Last: {last}")
        if volume:
            print(f"  Volume: {volume}")
        print("-" * 50)

async def main():
    if not os.path.exists(TOKEN_FILE) or not os.path.exists(API_KEY_FILE):
        print(f"Credentials not found at {TOKEN_FILE} or {API_KEY_FILE}!")
        return

    with open(API_KEY_FILE, 'r') as f:
        api_data = json.load(f)
        api_key = api_data['api_key']
        app_secret = api_data['api_secret']

    # Initialize client
    client = easy_client(
        api_key=api_key,
        app_secret=app_secret,
        callback_url='https://127.0.0.1',
        token_path=TOKEN_FILE,
        asyncio=True
    )
    
    # 1. Fetch the exact symbol for today's 6830 Call
    print("Fetching today's SPX 0DTE chain...")
    today = datetime.now(CHICAGO).date()
    # Note: SPXW is the root for weeklies
    resp = await client.get_option_chain(
        symbol='$SPX', 
        strike=6830,
        from_date=today,
        to_date=today
    )
    
    if resp.status_code != 200:
        print(f"Failed to fetch chain: {resp.status_code} - {resp.text}")
        return
        
    chain = resp.json()
    option_symbol = None
    
    # Locate the symbol in callExpDateMap
    call_map = chain.get('callExpDateMap', {})
    for exp_str, strikes in call_map.items():
        if '6830.0' in strikes:
            contracts = strikes['6830.0']
            if contracts:
                option_symbol = contracts[0]['symbol']
                break
    
    if not option_symbol:
        print("Could not find the 6830 Call contract for today. Printing available strikes...")
        # Fallback to construction or just print what's available
        for exp_str, strikes in call_map.items():
            print(f"Available strikes for {exp_str}: {list(strikes.keys())[:5]}...")
        return

    print(f"Found symbol: {option_symbol}")
    
    # 2. Setup Streaming
    stream_client = StreamClient(client)
    
    print("Logging into Streamer...")
    await stream_client.login()
    
    stream_client.add_level_one_option_handler(handle_option_msg)
    
    print(f"Subscribing to {option_symbol}...")
    await stream_client.level_one_option_subs([option_symbol])
    
    print("Streaming started. Listening (Ctrl+C to stop)...")
    
    try:
        while True:
            await stream_client.handle_message()
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
        pass
