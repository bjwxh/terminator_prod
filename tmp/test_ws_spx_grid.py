import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from zoneinfo import ZoneInfo

from schwab.auth import easy_client
from schwab.streaming import StreamClient

CHICAGO = ZoneInfo("America/Chicago")

# Setup clean minimal logging to avoid interfering with our table output
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SPXGridTest")

# State storage for live quotes
# format: strike -> {'C': {bid, ask, mid, ts}, 'P': {bid, ask, mid, ts}}
live_chain = defaultdict(lambda: {'C': {'bid': None, 'ask': None, 'mid': None, 'ts': None},
                                  'P': {'bid': None, 'ask': None, 'mid': None, 'ts': None}})

def handle_option_update(msg):
    """Receive live WebSocket messages and update our in-memory data store."""
    content = msg.get('content', [])
    for entry in content:
        symbol = entry.get('key')
        if not symbol:
            continue
        
        # Parse symbol to extract side, strike
        # Example format: 'SPXW  260522C07480000'
        try:
            symbol_clean = symbol.strip()
            parts = symbol_clean.split()
            if len(parts) < 2:
                continue
            
            code = parts[-1]
            side_char = code[-9]  # 'C' or 'P'
            strike_str = code[-8:]  # '07480000'
            strike = int(strike_str) / 1000.0
            
            bid = entry.get('BID_PRICE') or entry.get('2')
            ask = entry.get('ASK_PRICE') or entry.get('3')
            
            # Update data
            side = 'C' if side_char == 'C' else 'P'
            if bid is not None:
                live_chain[strike][side]['bid'] = float(bid)
            if ask is not None:
                live_chain[strike][side]['ask'] = float(ask)
            
            # Compute mid price
            b = live_chain[strike][side]['bid']
            a = live_chain[strike][side]['ask']
            if b is not None and a is not None:
                live_chain[strike][side]['mid'] = (b + a) / 2.0
            
            live_chain[strike][side]['ts'] = datetime.now(CHICAGO).strftime('%H:%M:%S')
        except Exception as e:
            pass

def print_pricing_grid():
    """Print a beautiful side-by-side Call/Put options grid to the terminal."""
    # Find all strikes in our range (7280 to 7650 in steps of 5)
    strikes = sorted(list(range(7280, 7655, 5)))
    
    # Terminal ANSI codes for formatting
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    CLEAR_SCREEN = "\033[H\033[J" # Clear terminal and home cursor
    
    print(CLEAR_SCREEN, end="")
    print(f"{BOLD}{HEADER}========================================================================================={ENDC}")
    print(f"{BOLD}{HEADER}                   📊 SPX 0DTE REAL-TIME OPTIONS CHAIN GRID (MAY 22)                     {ENDC}")
    print(f"  Time: {datetime.now(CHICAGO).strftime('%Y-%m-%d %H:%M:%S')} CST | Streaming: {len(strikes)} strikes | Update Cadence: 5s")
    print(f"{BOLD}{HEADER}========================================================================================={ENDC}")
    print(f" {BOLD}            CALLS (7480 - 7650)            |        |             PUTS (7280 - 7475)          {ENDC}")
    print(f"--------------------------------------------+--------+-------------------------------------------")
    print(f"  Bid      Ask      Mid      Time   Status  | Strike |  Status  Time     Mid      Bid      Ask   ")
    print(f"--------------------------------------------+--------+-------------------------------------------")
    
    for strike in strikes:
        strike_f = float(strike)
        c = live_chain[strike_f]['C']
        p = live_chain[strike_f]['P']
        
        # Format Calls (only for strikes 7480 to 7650)
        if 7480 <= strike <= 7650:
            c_bid_f = f"{c['bid']:7.2f}" if c['bid'] is not None else "   -   "
            c_ask_f = f"{c['ask']:7.2f}" if c['ask'] is not None else "   -   "
            c_mid_f = f"{c['mid']:7.2f}" if c['mid'] is not None else "   -   "
            c_ts_f = f"{c['ts']}" if c['ts'] is not None else "--:--:--"
            c_status = f"{OKGREEN}LIVE{ENDC}" if c['ts'] is not None else "STALE"
        else:
            c_bid_f, c_ask_f, c_mid_f, c_ts_f, c_status = "   -   ", "   -   ", "   -   ", "        ", "  -  "
            
        # Format Puts (only for strikes 7280 to 7475)
        if 7280 <= strike <= 7475:
            p_bid_f = f"{p['bid']:7.2f}" if p['bid'] is not None else "   -   "
            p_ask_f = f"{p['ask']:7.2f}" if p['ask'] is not None else "   -   "
            p_mid_f = f"{p['mid']:7.2f}" if p['mid'] is not None else "   -   "
            p_ts_f = f"{p['ts']}" if p['ts'] is not None else "--:--:--"
            p_status = f"{OKBLUE}LIVE{ENDC}" if p['ts'] is not None else "STALE"
        else:
            p_bid_f, p_ask_f, p_mid_f, p_ts_f, p_status = "   -   ", "   -   ", "   -   ", "        ", "  -  "
            
        # Highlight ATM strike/region if we detect mid-prices (rough estimate)
        strike_line = f" {strike:5d} "
        if strike == 7480 or strike == 7475:
            strike_line = f"{BOLD}{WARNING}➔{strike:4d} {ENDC}"
            
        print(f" {c_bid_f}  {c_ask_f}  {c_mid_f}  {c_ts_f}  {c_status}  |{strike_line}|  {p_status}  {p_ts_f}  {p_mid_f}  {p_bid_f}  {p_ask_f} ")
        
    print(f"{BOLD}{HEADER}========================================================================================={ENDC}")
    print(f" Note: '➔' indicates the boundary between the Call and Put target strike zones.")

async def main():
    # 1. Load credentials and initialize Schwab Client
    home_dir = Path.home()
    credentials_dir = home_dir / ".api_keys" / "schwab"
    credentials_path = credentials_dir / "schwab_api.json"
    token_path = credentials_dir / "schwab_token.json"
    
    if not credentials_path.exists():
        print(f"Error: Credentials not found at {credentials_path}")
        return
    
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
    
    # 2. Build list of option symbols
    # Calls: 7480 to 7650 in increments of 5
    call_strikes = list(range(7480, 7655, 5))
    # Puts: 7280 to 7475 in increments of 5
    put_strikes = list(range(7280, 7480, 5))
    
    symbols = []
    # Schwab Option Symbol Format: SPXW  YYMMDDS00000000
    for strike in call_strikes:
        # format strike: e.g. 7480 * 1000 = 7480000 -> formatted to 8 digits = '07480000'
        sym = f"SPXW  260522C{strike * 1000:08d}"
        symbols.append(sym)
        
    for strike in put_strikes:
        sym = f"SPXW  260522P{strike * 1000:08d}"
        symbols.append(sym)
        
    print(f"Generated {len(symbols)} SPX 0DTE option symbols (Calls: {len(call_strikes)}, Puts: {len(put_strikes)})")
    
    # 3. Setup Streaming client
    stream_client = StreamClient(client)
    await stream_client.login()
    
    # Add handler for Option updates
    stream_client.add_level_one_option_handler(handle_option_update)
    
    # Subscribe to symbols
    print(f"Subscribing to {len(symbols)} option symbols on WebSocket stream...")
    await stream_client.level_one_option_subs(symbols)
    
    # Define tasks
    async def stream_loop():
        try:
            while True:
                await stream_client.handle_message()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Stream error: {e}")
            
    async def display_loop():
        # Wait 2 seconds for initial subscriptions to buffer quotes before first display
        await asyncio.sleep(2)
        start_time = time.time()
        # Run for exactly 60 seconds (1 minute)
        while time.time() - start_time < 60:
            print_pricing_grid()
            await asyncio.sleep(5)
            
    # Launch stream handler in background
    stream_task = asyncio.create_task(stream_loop())
    
    # Run the display loop for 1 minute
    await display_loop()
    
    # Clean shutdown
    stream_task.cancel()
    try:
        await stream_task
    except asyncio.CancelledError:
        pass
        
    print("\nLogging out from Schwab stream...")
    await stream_client.logout()
    print("Done. Grid testing completed.")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
