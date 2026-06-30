# tmp/latency_test_runner_ws.py
import asyncio
import json
import logging
import os
import ssl
import sys
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo
from schwab.auth import easy_client
from schwab.streaming import StreamClient

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("WSLatency")

TOKEN_FILE = os.path.expanduser('~/.api_keys/schwab/sli_token.json')
API_KEY_FILE = os.path.expanduser('~/.api_keys/schwab/sli_api.json')
CHICAGO = ZoneInfo("America/Chicago")

ssl._create_default_https_context = ssl._create_unverified_context

async def get_client():
    if not os.path.exists(TOKEN_FILE) or not os.path.exists(API_KEY_FILE):
        logger.error(f"Credentials not found!")
        sys.exit(1)
        
    with open(API_KEY_FILE, 'r') as f:
        api_data = json.load(f)
        api_key = api_data['api_key']
        app_secret = api_data['api_secret']
        callback_url = api_data.get('callback_url', 'https://127.0.0.1')
        
    client = easy_client(
        api_key=api_key,
        app_secret=app_secret,
        callback_url=callback_url,
        token_path=TOKEN_FILE,
        asyncio=True,
        enforce_enums=False
    )
    return client

async def resolve_option_symbols(client):
    today = date.today()
    logger.info(f"Fetching SPX option chain for today ({today})...")
    resp = await client.get_option_chain(symbol='$SPX', from_date=today, to_date=today)
    if resp.status_code != 200:
        logger.error(f"Failed to fetch option chain: {resp.status_code}")
        return None, None
        
    chain = resp.json()
    call_map = chain.get('callExpDateMap', {})
    put_map = chain.get('putExpDateMap', {})
    
    call_symbol = None
    put_symbol = None
    
    # Resolve 7510 Call
    for exp_str, strikes in call_map.items():
        if '7510.0' in strikes:
            contracts = strikes['7510.0']
            if contracts:
                call_symbol = contracts[0]['symbol']
                break
    
    # Resolve 7419 Put
    for exp_str, strikes in put_map.items():
        if '7419.0' in strikes:
            contracts = strikes['7419.0']
            if contracts:
                put_symbol = contracts[0]['symbol']
                break
                
    if not call_symbol:
        logger.warning("Could not find 7510 Call. Finding closest strike call...")
        for exp_str, strikes in call_map.items():
            sorted_strikes = sorted([float(s) for s in strikes.keys()])
            if sorted_strikes:
                closest_strike = min(sorted_strikes, key=lambda x: abs(x - 7510))
                call_symbol = strikes[f"{closest_strike:.1f}"][0]['symbol']
                logger.info(f"Selected fallback call strike {closest_strike}: {call_symbol}")
                break

    if not put_symbol:
        logger.warning("Could not find 7419 Put. Finding closest strike put...")
        for exp_str, strikes in put_map.items():
            sorted_strikes = sorted([float(s) for s in strikes.keys()])
            if sorted_strikes:
                closest_strike = min(sorted_strikes, key=lambda x: abs(x - 7419))
                put_symbol = strikes[f"{closest_strike:.1f}"][0]['symbol']
                logger.info(f"Selected fallback put strike {closest_strike}: {put_symbol}")
                break

    return call_symbol, put_symbol

async def run_latency_test(client, call_sym, put_sym):
    logger.info(f"Targeting Call: {call_sym} | Put: {put_sym}")
    latencies = []
    stream_client = StreamClient(client)
    await stream_client.login()
    
    def handle_option_update(msg):
        # We can extract the timestamp from the response
        # In Schwab stream message, the timestamp is at root: {"notify": [...], "timestamp": ...} or in responses
        # Or in the level one option entry
        # Let's get the root-level timestamp
        ts_val = msg.get('timestamp')
        if not ts_val:
            # check inside response or notify
            content = msg.get('content', [])
            if content and isinstance(content, list):
                ts_val = content[0].get('52') or content[0].get('QUOTE_TIME')
        
        if ts_val:
            try:
                local_now = datetime.now(CHICAGO)
                ext_ts = datetime.fromtimestamp(float(ts_val)/1000, CHICAGO)
                diff = (local_now - ext_ts).total_seconds()
                # Exclude any massive outliers or timezone mismatches
                if 0 <= diff < 10:
                    latencies.append(diff)
            except Exception:
                pass

    stream_client.add_level_one_option_handler(handle_option_update)
    logger.info("Subscribing to LEVELONE_OPTIONS...")
    await stream_client.level_one_option_subs([call_sym, put_sym])
    
    logger.info("Starting stream collection for 2 minutes (120 seconds)...")
    start_time = time.time()
    try:
        while time.time() - start_time < 120:
            await stream_client.handle_message()
    except Exception as e:
        logger.error(f"Stream error: {e}")
    finally:
        try:
            await stream_client.logout()
        except Exception:
            pass

    if latencies:
        avg_lat = sum(latencies) / len(latencies)
        latencies.sort()
        p90_lat = latencies[int(len(latencies) * 0.90)]
        p99_lat = latencies[int(len(latencies) * 0.99)]
        logger.info(f"Results collected: samples={len(latencies)}, avg={avg_lat:.4f}s, p90={p90_lat:.4f}s, p99={p99_lat:.4f}s")
    else:
        avg_lat, p90_lat, p99_lat = float('nan'), float('nan'), float('nan')
        logger.error("No latency samples collected during the 2 minute window.")
        
    return {
        "avg_latency": avg_lat,
        "p90_latency": p90_lat,
        "p99_latency": p99_lat,
        "samples": len(latencies)
    }

async def main():
    client = await get_client()
    call_sym, put_sym = await resolve_option_symbols(client)
    if not call_sym or not put_sym:
        logger.error("Could not resolve option symbols.")
        return
        
    res = await run_latency_test(client, call_sym, put_sym)
    print("\n=== LATENCY RESULTS JSON ===")
    print(json.dumps(res, indent=2))
    print("============================")

if __name__ == '__main__':
    asyncio.run(main())
