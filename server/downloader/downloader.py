#!/usr/bin/env python3
import asyncio
import json
import sqlite3
import os
import logging
from pathlib import Path
from datetime import datetime, date, time as dt_time, timedelta
from zoneinfo import ZoneInfo
CHICAGO = ZoneInfo("America/Chicago")
import time
from typing import Dict, List, Any

from schwab.auth import easy_client

# Simple production logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - DATA_DOWNLOADER - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('downloader.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class MarketDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_database()
    
    def _init_database(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS stock_options (
                        datetime TEXT,
                        root_symbol TEXT,
                        symbol TEXT,
                        strike_price REAL,
                        side TEXT,
                        bidprice REAL,
                        askprice REAL,
                        volume INTEGER,
                        open_interest INTEGER,
                        delta REAL,
                        gamma REAL,
                        vega REAL,
                        theta REAL,
                        iv REAL,
                        dte INTEGER,
                        PRIMARY KEY (datetime, symbol, side)
                    )
                """)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_stock_datetime_side ON stock_options (datetime, side)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_stock_symbol_dte ON stock_options (root_symbol, dte)")
                conn.commit()
                logger.info(f"Database ready at {self.db_path}")
        except Exception as e:
            logger.error(f"Error initializing DB: {e}")
            raise

    def cleanup_old_data(self, days=14):
        """Remove data older than X days to save space"""
        cutoff = (datetime.now(CHICAGO) - timedelta(days=days)).isoformat()
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM stock_options WHERE datetime < ?", (cutoff,))
                removed = cursor.rowcount
                conn.commit()
                if removed > 0:
                    logger.info(f"Cleanup: Removed {removed} records older than {days} days")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

    def insert_options_data(self, options_data: List[Dict[str, Any]]):
        if not options_data: return
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                for o in options_data:
                    cursor.execute("""
                        INSERT OR REPLACE INTO stock_options 
                        (datetime, root_symbol, symbol, strike_price, side, 
                         bidprice, askprice, volume, open_interest, 
                         delta, gamma, vega, theta, iv, dte)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        o['datetime'], o['root_symbol'], o['symbol'], o['strike_price'], o['side'],
                        o['bidprice'], o['askprice'], o['volume'], o['open_interest'],
                        o['delta'], o['gamma'], o['vega'], o['theta'], o['iv'], o['dte']
                    ))
                conn.commit()
                logger.info(f"Inserted {len(options_data)} records")
        except Exception as e:
            logger.error(f"Insert error: {e}")

class OptionsDownloader:
    def __init__(self, config: Dict):
        self.config = config
        self.db = MarketDatabase(config['database_path'])
        self.client = None
        self.is_running = True

    async def initialize_client(self):
        try:
            creds_path = os.path.expanduser("~/.api_keys/schwab/sli_api.json")
            token_path = os.path.expanduser("~/.api_keys/schwab/sli_token.json")
            
            with open(creds_path, 'r') as f:
                creds = json.load(f)
            
            self.client = easy_client(
                api_key=creds['api_key'],
                app_secret=creds['api_secret'],
                callback_url=creds.get('callback_url', 'https://127.0.0.1'),
                token_path=token_path,
                asyncio=True,
                enforce_enums=False
            )
            # Increase timeout for large SPX chains
            self.client.session.timeout = 30.0
            logger.info("Schwab Client initialized")
        except Exception as e:
            logger.error(f"Client init failed: {e}")
            raise

    async def fetch_cycle(self):
        try:
            if not self.client: await self.initialize_client()
            
            now_dt = datetime.now(CHICAGO)
            now_iso = now_dt.isoformat()
            today_date = now_dt.date()
            
            all_records = []
            for item in self.config['symbols']:
                root = item['symbol']
                max_dte = item['max_dte']
                
                logger.info(f"Fetching {root} (DTE 0-{max_dte})...")
                resp = await self.client.get_option_chain(
                    symbol=root,
                    contract_type='ALL',
                    from_date=today_date,
                    to_date=today_date + timedelta(days=max_dte),
                    strike_range=self.config.get('strike_range', 150)
                )
                
                if resp.status_code != 200:
                    logger.error(f"API Error {resp.status_code}: {resp.text}")
                    continue
                
                chain = resp.json()
                
                # Parse Calls and Puts
                for map_key in ['callExpDateMap', 'putExpDateMap']:
                    side = 'CALL' if 'call' in map_key else 'PUT'
                    if map_key not in chain: continue
                    
                    for exp_str, strikes in chain[map_key].items():
                        # exp_str format: "2026-03-19:0"
                        dte = int(exp_str.split(':')[1])
                        if dte > max_dte: continue
                        
                        for strike, contracts in strikes.items():
                            for c in contracts:
                                all_records.append({
                                    'datetime': now_iso,
                                    'root_symbol': root,
                                    'symbol': c.get('symbol'),
                                    'strike_price': float(strike),
                                    'side': side,
                                    'bidprice': c.get('bid'),
                                    'askprice': c.get('ask'),
                                    'volume': c.get('totalVolume', 0),
                                    'open_interest': c.get('openInterest', 0),
                                    'delta': c.get('delta'),
                                    'gamma': c.get('gamma'),
                                    'vega': c.get('vega'),
                                    'theta': c.get('theta'),
                                    'iv': c.get('volatility'),
                                    'dte': dte
                                })
            
            if all_records:
                self.db.insert_options_data(all_records)
                
        except Exception as e:
            logger.error(f"Fetch cycle error: {e}")
            self.client = None # Reset client on error

    async def run(self):
        logger.info("Downloader task started.")
        last_cleanup_day = datetime.now(CHICAGO).date()
        while self.is_running:
            now_dt = datetime.now(CHICAGO)
            today_date = now_dt.date()
            
            # Perform daily cleanup
            if today_date != last_cleanup_day:
                self.db.cleanup_old_data(days=14)
                last_cleanup_day = today_date
                
            now_time = now_dt.time()
            # Market hours: 8:29 AM - 3:16 PM
            if dt_time(8, 29) <= now_time <= dt_time(15, 16):
                start_cycle = time.time()
                await self.fetch_cycle()
                elapsed = time.time() - start_cycle
                sleep_time = max(1, self.config['fetch_interval_seconds'] - elapsed)
                await asyncio.sleep(sleep_time)
            else:
                # Outside market hours, sleep longer
                await asyncio.sleep(60)

async def main():
    config = {
        'fetch_interval_seconds': 30,
        'database_path': '../market_data.db', # Up one level inside server/
        'symbols': [{'symbol': '$SPX', 'max_dte': 0}],
        'strike_range': 150
    }
    downloader = OptionsDownloader(config)
    await downloader.run()

if __name__ == "__main__":
    asyncio.run(main())
