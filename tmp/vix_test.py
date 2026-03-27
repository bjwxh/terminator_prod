import asyncio
import json
import logging
import os
import ssl
from datetime import datetime
from schwab.auth import easy_client
from schwab.streaming import StreamClient

# CONFIGURATION
TOKEN_FILE = os.path.expanduser('~/.api_keys/schwab/sli_token.json')
API_KEY_FILE = os.path.expanduser('~/.api_keys/schwab/sli_api.json')

# Logging
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger("VIX_Test")

# SSL BYPASS
ssl._create_default_https_context = ssl._create_unverified_context


latest_vix = None

def handle_vix(msg):
    global latest_vix
    content = msg.get('content', [])
    for entry in content:
        # Field 3 or LAST_PRICE
        val = entry.get('LAST_PRICE') or entry.get('3')
        if val is not None:
            latest_vix = float(val)

async def print_loop():
    while True:
        if latest_vix is not None:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] VIX Streamed Price: {latest_vix:.2f}")
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] VIX: Waiting for stream...")
        await asyncio.sleep(1)

async def main():
    if not os.path.exists(TOKEN_FILE) or not os.path.exists(API_KEY_FILE):
        print("Credentials not found!")
        return

    # Initialize client (Async)
    # Note: We assume creds are managed by the easy_client from token/api paths
    client = easy_client(
        api_key=None,
        app_secret=None,
        callback_url=None,
        token_path=TOKEN_FILE,
        asyncio=True
    )
    
    stream_client = StreamClient(client)
    
    print("Logging into Streamer...")
    await stream_client.login()
    
    # Schwab v1.5.1: Indices come through LEVELONE_EQUITIES
    stream_client.add_level_one_equity_handler(handle_vix)
    
    print("Subscribing to $VIX...")
    await stream_client.level_one_equity_subs(['$VIX'])
    
    # Start the printer task
    asyncio.create_task(print_loop())
    
    print("Streaming started. (Ctrl+C to stop)...")
    
    try:
        while True:
            await stream_client.handle_message()
    except asyncio.CancelledError:
        pass
    finally:
        await stream_client.logout()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nTest stopped.")
