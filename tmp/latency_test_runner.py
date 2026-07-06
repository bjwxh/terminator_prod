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
from schwab.orders.generic import OrderBuilder
from schwab.orders.common import OrderStrategyType, OrderType, Session, Duration, OptionInstruction

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("LatencyTest")

TOKEN_FILE = os.path.expanduser('~/.api_keys/schwab/sli_token.json')
API_KEY_FILE = os.path.expanduser('~/.api_keys/schwab/sli_api.json')
CHICAGO = ZoneInfo("America/Chicago")

ssl._create_default_https_context = ssl._create_unverified_context

async def get_client():
    if not os.path.exists(TOKEN_FILE) or not os.path.exists(API_KEY_FILE):
        logger.error(f"Credentials not found at {TOKEN_FILE} or {API_KEY_FILE}!")
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

async def run_pricing_test(client):
    logger.info("--- Starting Pricing Feed Latency Test ---")
    
    # 1. Fetch Option symbol for 7350 PUT
    today = date.today()
    logger.info(f"Fetching option chain for strike 7350, expiry {today}...")
    resp = await client.get_option_chain(
        symbol='$SPX',
        strike=7350,
        from_date=today,
        to_date=today
    )
    
    if resp.status_code != 200:
        logger.error(f"Failed to fetch option chain: {resp.status_code} - {resp.text}")
        return None
        
    chain = resp.json()
    put_map = chain.get('putExpDateMap', {})
    option_symbol = None
    for exp_str, strikes in put_map.items():
        if '7350.0' in strikes:
            contracts = strikes['7350.0']
            if contracts:
                option_symbol = contracts[0]['symbol']
                break
                
    if not option_symbol:
        logger.warning("Could not find the 7350 PUT contract for today. Let's find any put around the current price...")
        # Get whatever put strike is available
        for exp_str, strikes in put_map.items():
            for strike_str, contracts in strikes.items():
                if contracts:
                    option_symbol = contracts[0]['symbol']
                    logger.info(f"Selected fallback symbol: {option_symbol}")
                    break
            if option_symbol:
                break
                
    if not option_symbol:
        logger.error("No option symbol found at all.")
        return None

    # Step A: Stream index $SPX to measure latency
    latencies = []
    stream_client = StreamClient(client)
    await stream_client.login()
    
    def handle_equity_update(msg):
        content = msg.get('content', [])
        for entry in content:
            # quote time or trade time in millis
            ts_val = entry.get('QUOTE_TIME_MILLIS') or entry.get('TRADE_TIME_MILLIS') or entry.get('REGULAR_MARKET_TRADE_MILLIS')
            if ts_val:
                try:
                    local_now = datetime.now(CHICAGO)
                    ext_ts = datetime.fromtimestamp(float(ts_val)/1000, CHICAGO)
                    diff = (local_now - ext_ts).total_seconds()
                    if 0 <= diff < 10:
                        latencies.append(diff)
                except Exception:
                    pass

    stream_client.add_level_one_equity_handler(handle_equity_update)
    logger.info("Subscribing to index $SPX for latency tracking...")
    await stream_client.level_one_equity_subs(['$SPX'])
    
    logger.info("Index streaming started. Collecting data for 30 seconds...")
    start_time = time.time()
    try:
        while time.time() - start_time < 30:
            await stream_client.handle_message()
    except Exception as e:
        logger.error(f"Error during index stream: {e}")
    finally:
        try:
            await stream_client.logout()
        except Exception:
            pass

    await asyncio.sleep(2)

    # Step B: Stream option to measure message count / frequency
    option_update_count = 0
    stream_client_opt = StreamClient(client)
    await stream_client_opt.login()
    
    def handle_option_update(msg):
        nonlocal option_update_count
        content = msg.get('content', [])
        option_update_count += len(content)

    stream_client_opt.add_level_one_option_handler(handle_option_update)
    logger.info(f"Subscribing to option {option_symbol} for tick count...")
    await stream_client_opt.level_one_option_subs([option_symbol])
    
    logger.info("Option streaming started. Collecting data for 30 seconds...")
    start_time = time.time()
    try:
        while time.time() - start_time < 30:
            await stream_client_opt.handle_message()
    except Exception as e:
        logger.error(f"Error during option stream: {e}")
    finally:
        try:
            await stream_client_opt.logout()
        except Exception:
            pass
        
    if latencies:
        avg_lat = sum(latencies) / len(latencies)
        latencies.sort()
        p90_lat = latencies[int(len(latencies) * 0.90)]
        p99_lat = latencies[int(len(latencies) * 0.99)]
    else:
        avg_lat, p90_lat, p99_lat = float('nan'), float('nan'), float('nan')
        
    results = {
        "avg_pricing_latency_sec": avg_lat,
        "p90_pricing_latency_sec": p90_lat,
        "p99_pricing_latency_sec": p99_lat,
        "pricing_samples": len(latencies),
        "option_updates_received": option_update_count
    }
    logger.info(f"Pricing results: {results}")
    return results

async def run_order_test(client):
    logger.info("--- Starting Order Routing Latency Test ---")
    
    # 1. Fetch Option symbol for 7400 PUT
    today = date.today()
    logger.info(f"Fetching option chain for strike 7400, expiry {today}...")
    resp = await client.get_option_chain(
        symbol='$SPX',
        strike=7400,
        from_date=today,
        to_date=today
    )
    
    if resp.status_code != 200:
        logger.error(f"Failed to fetch option chain: {resp.status_code} - {resp.text}")
        return None
        
    chain = resp.json()
    put_map = chain.get('putExpDateMap', {})
    option_symbol = None
    for exp_str, strikes in put_map.items():
        if '7400.0' in strikes:
            contracts = strikes['7400.0']
            if contracts:
                option_symbol = contracts[0]['symbol']
                break
                
    if not option_symbol:
        logger.warning("Could not find 7400 PUT. Using fallback search...")
        for exp_str, strikes in put_map.items():
            for strike_str, contracts in strikes.items():
                if contracts:
                    option_symbol = contracts[0]['symbol']
                    break
            if option_symbol:
                break
                
    if not option_symbol:
        logger.error("No option symbol found for order test.")
        return None
        
    logger.info(f"Selected option symbol for order test: {option_symbol}")
    
    # Get Account numbers
    resp = await client.get_account_numbers()
    if resp.status_code != 200:
        logger.error(f"Failed to get account numbers: {resp.status_code}")
        return None
        
    account_id = '22229895'
    account_hash = next(a['hashValue'] for a in resp.json() if a['accountNumber'] == account_id)
    
    # Run 5 rounds of submit & cancel
    place_times = []
    cancel_times = []
    
    for i in range(5):
        logger.info(f"Round {i+1}/5 - Placing order...")
        builder = OrderBuilder()
        builder.set_order_strategy_type(OrderStrategyType.SINGLE)
        builder.set_order_type(OrderType.LIMIT)
        builder.set_price("1.00") # Bidding $1.00 for 7400 PUT
        builder.set_session(Session.NORMAL)
        builder.set_duration(Duration.DAY)
        builder.add_option_leg(OptionInstruction.BUY_TO_OPEN, option_symbol, 1)
        
        order_json = builder.build()
        
        t0 = time.perf_counter()
        place_resp = await client.place_order(account_hash, order_json)
        t1 = time.perf_counter()
        
        if place_resp.status_code not in [200, 201]:
            logger.error(f"Failed to place order: {place_resp.status_code} - {place_resp.text}")
            continue
            
        place_time = t1 - t0
        place_times.append(place_time)
        
        # Extract order ID from Location header
        location = place_resp.headers.get("Location", "")
        order_id = location.split("/")[-1]
        
        logger.info(f"Placed order ID: {order_id} in {place_time:.3f}s. Cancelling...")
        
        t2 = time.perf_counter()
        cancel_resp = await client.cancel_order(int(order_id), account_hash)
        t3 = time.perf_counter()
        
        if cancel_resp.status_code not in [200, 201]:
            logger.error(f"Failed to cancel order: {cancel_resp.status_code} - {cancel_resp.text}")
        else:
            cancel_time = t3 - t2
            cancel_times.append(cancel_time)
            logger.info(f"Cancelled order ID: {order_id} in {cancel_time:.3f}s")
            
        await asyncio.sleep(10)
        
    results = {
        "avg_order_placement_sec": sum(place_times) / len(place_times) if place_times else float('nan'),
        "avg_order_cancellation_sec": sum(cancel_times) / len(cancel_times) if cancel_times else float('nan'),
        "placement_rounds": len(place_times),
        "cancellation_rounds": len(cancel_times)
    }
    logger.info(f"Order results: {results}")
    return results

async def main():
    client = await get_client()
    
    pricing_res = await run_pricing_test(client)
    await asyncio.sleep(5)
    order_res = await run_order_test(client)
    
    final_res = {
        "timestamp": datetime.now().isoformat(),
        "pricing": pricing_res,
        "ordering": order_res
    }
    
    print("\n=== FINAL RESULTS JSON ===")
    print(json.dumps(final_res, indent=2))
    print("==========================")

if __name__ == '__main__':
    asyncio.run(main())
