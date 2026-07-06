import asyncio
import json
import os
import sys
import logging
from datetime import date
from pathlib import Path
from schwab.auth import easy_client
from schwab.streaming import StreamClient

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TestSchwabWS")

async def main():
    # 1. Load credentials and initialize Schwab Client
    home_dir = Path.home()
    credentials_dir = home_dir / ".api_keys" / "schwab"
    credentials_path = credentials_dir / "schwab_api.json"
    token_path = credentials_dir / "schwab_token.json"
    
    if not credentials_path.exists():
        logger.error(f"Credentials not found at {credentials_path}")
        return
    
    logger.info("Initializing Schwab client...")
    with open(credentials_path, 'r') as f:
        creds = json.load(f)
        
    client = easy_client(
        api_key=creds['api_key'],
        app_secret=creds['api_secret'],
        callback_url=creds.get('callback_url', 'https://127.0.0.1'),
        token_path=str(token_path),
        asyncio=True,
        enforce_enums=False
    )
    
    # 2. Fetch a few active SPX 0DTE option symbols
    logger.info("Fetching SPX 0DTE option chain to find active symbols...")
    today = date.today()
    resp = await client.get_option_chain(
        '$SPX', 
        strike_range=10, # Fetch a narrow strike range around the ATM strike
        from_date=today,
        to_date=today
    )
    
    if resp.status_code != 200:
        logger.error(f"Failed to fetch option chain: {resp.status_code} {resp.text}")
        return
        
    data = resp.json()
    option_symbols = []
    
    # Extract option symbols
    for side in ['callExpDateMap', 'putExpDateMap', 'callStrategyChain', 'putStrategyChain']:
        chain = data.get(side)
        if not chain:
            continue
        
        # Traverse the map: Expiration -> Strike -> List[Option] or Strike -> Expiration -> List[Option]
        for first_key, first_val in chain.items():
            if isinstance(first_val, dict):
                for second_key, second_val in first_val.items():
                    if isinstance(second_val, list):
                        for opt in second_val:
                            symbol = opt.get('symbol')
                            if symbol and len(option_symbols) < 5:
                                option_symbols.append(symbol)
                    elif isinstance(second_val, dict):
                        # Handle deep nesting if any
                        for third_key, third_val in second_val.items():
                            if isinstance(third_val, list):
                                for opt in third_val:
                                    symbol = opt.get('symbol')
                                    if symbol and len(option_symbols) < 5:
                                        option_symbols.append(symbol)

    if not option_symbols:
        logger.error("No active SPX 0DTE option contracts found in the chain.")
        return
        
    logger.info(f"Found active SPX 0DTE option symbols for testing: {option_symbols}")
    
    # 3. Setup streaming client
    logger.info("Logging into Schwab streaming client...")
    stream_client = StreamClient(client)
    await stream_client.login()
    
    # Define message handlers
    def handle_option_update(msg):
        logger.info(f"🔴 [OPTION UPDATE RECEIVER] Raw WebSocket message: {json.dumps(msg, indent=2)}")
        content = msg.get('content', [])
        for entry in content:
            key = entry.get('key')
            bid = entry.get('BID_PRICE') or entry.get('2')
            ask = entry.get('ASK_PRICE') or entry.get('3')
            last = entry.get('LAST_PRICE') or entry.get('4')
            logger.info(f"🎯 Option: {key} | Bid: {bid} | Ask: {ask} | Last: {last}")

    def handle_raw_message(msg):
        # Fallback raw message logger to see any metadata/heartbeats
        if msg.get('notify') or msg.get('response'):
            logger.info(f"ℹ️ Stream Control Message: {json.dumps(msg)}")
            
    # Add handlers
    stream_client.add_level_one_option_handler(handle_option_update)
    
    # 4. Subscribe to the option symbols
    logger.info(f"Subscribing to LEVELONE_OPTIONS for {option_symbols}...")
    await stream_client.level_one_option_subs(option_symbols)
    
    logger.info("Subscription sent. Starting message loop (running for 30 seconds)...")
    
    # Run the stream client's message loop
    run_duration = 30 # seconds
    start_time = asyncio.get_event_loop().time()
    
    try:
        while asyncio.get_event_loop().time() - start_time < run_duration:
            # handle_message awaits and processes a message from the websocket
            await stream_client.handle_message()
    except asyncio.CancelledError:
        logger.info("Stream processing loop cancelled.")
    except Exception as e:
        logger.error(f"Error in stream processing loop: {e}")
    finally:
        logger.info("Logging out and closing stream client...")
        try:
            await stream_client.logout()
        except Exception as e:
            logger.warning(f"Error logging out: {e}")
        logger.info("Test finished.")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
