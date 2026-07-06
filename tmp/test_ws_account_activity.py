import asyncio
import json
import logging
import sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from schwab.auth import easy_client
from schwab.streaming import StreamClient

CHICAGO = ZoneInfo("America/Chicago")

# Setup clean visual logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AccountActivityTest")

def handle_account_activity(msg):
    """Callback for all real-time order and account events from Schwab."""
    print("\n" + "="*80)
    print(f"🔔 [RECEIVED ACCOUNT ACTIVITY EVENT] - {datetime.now(CHICAGO).strftime('%Y-%m-%d %H:%M:%S')} CST")
    print("="*80)
    
    # Beautify the raw payload
    print(json.dumps(msg, indent=2))
    
    # Try parsing high-value fields
    try:
        content = msg.get("content", [])
        for entry in content:
            msg_type = entry.get("MESSAGE_TYPE") or entry.get("message-type") or entry.get("1")
            msg_data = entry.get("MESSAGE_DATA") or entry.get("message-data") or entry.get("2")
            
            print(f"\n👉 Event Type: {msg_type}")
            if msg_data:
                print(f"👉 Event Details: {msg_data}")
    except Exception as e:
        print(f"Error parsing details: {e}")
    
    print("="*80 + "\n")

async def main():
    home_dir = Path.home()
    credentials_dir = home_dir / ".api_keys" / "schwab"
    
    credentials_path = credentials_dir / "schwab_api.json"
    token_path = credentials_dir / "schwab_token.json"
    account_id = "43293551"
    
    if not credentials_path.exists():
        logger.error(f"Credentials not found at {credentials_path}")
        return
        
    logger.info(f"Initializing client for account {account_id}...")
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
    
    logger.info("Starting robust auto-reconnection loop...")
    logger.info("Press Ctrl+C to stop manually, or close the task when done.")
    
    while True:
        stream_client = None
        try:
            logger.info("Initializing Streaming Client...")
            stream_client = StreamClient(client, account_id=account_id)
            
            logger.info("Logging into Streamer...")
            await stream_client.login()
            
            # Register the callback handler BEFORE subscribing
            logger.info("Registering Account Activity handler...")
            stream_client.add_account_activity_handler(handle_account_activity)
            
            # Subscribe to Account Activity feed
            logger.info(f"Subscribing to ACCT_ACTIVITY feed for account {account_id}...")
            await stream_client.account_activity_sub()
            
            logger.info("Subscription sent successfully! Listening for events...")
            
            # Message processing loop
            while True:
                await stream_client.handle_message()
                
        except asyncio.CancelledError:
            logger.info("Monitoring task cancelled.")
            break
        except KeyboardInterrupt:
            logger.info("Monitoring stopped by keyboard interrupt.")
            break
        except Exception as e:
            logger.error(f"Error in streaming loop: {e}")
            logger.info("Attempting auto-reconnect in 5 seconds...")
            await asyncio.sleep(5)
        finally:
            if stream_client:
                logger.info("Closing active stream connection...")
                try:
                    await stream_client.logout()
                except:
                    pass

    logger.info("Stream connection cleanly closed. Exiting.")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Exited.")
