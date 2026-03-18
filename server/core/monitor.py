# live/spt_v4/monitor.py

import asyncio
import logging
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, date, time, timedelta, timezone
from typing import Dict, List, Tuple, Optional, Set
from collections import defaultdict, OrderedDict
import threading
import traceback
import queue
from zoneinfo import ZoneInfo
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import json
import os
import math
from pathlib import Path
import schwab
from schwab.auth import easy_client
from schwab import utils as schwab_utils
from schwab.orders.options import (
    option_buy_to_open_limit, option_buy_to_close_limit,
    option_sell_to_open_limit, option_sell_to_close_limit,
    bull_put_vertical_open, bear_call_vertical_open,
    bull_put_vertical_close, bear_call_vertical_close
)
from schwab.orders.common import (
    OrderStrategyType, OrderType, Session, Duration, 
    ComplexOrderStrategyType, OptionInstruction
)
from schwab.orders.generic import OrderBuilder

from .config import CONFIG
from .models import SubStrategy, Portfolio, OptionLeg, Trade, TradePurpose, TradeStats
from .utils import calculate_delta_decay
from .session_manager import SessionManager

try:
    from ..notifications import notify_all
except (ImportError, ValueError):
    # Fallback for standalone or testing
    async def notify_all(*args, **kwargs): pass

# Mocking Schwab until actual client is ready
# In reality, this would use schwab.client.AsyncClient
class LiveTradingMonitor:
    def __init__(self, config=None, trade_signal_callback=None):
        self.config = config or CONFIG
        self.trade_signal_callback = trade_signal_callback
        self.logger = logging.getLogger("LiveTradingMonitor")
        
        # Initialize sub-strategies
        self.sub_strategies: Dict[str, SubStrategy] = {}
        self._initialize_sub_strategies()
        
        # Combined portfolios
        self.combined_portfolio = Portfolio()
        self.live_combined_portfolio = Portfolio()
        
        # Schwab Client
        self.client = None
        self.account_hash = None
        self.account_id = str(self.config.get('account_id'))
        self.credentials_file = self.config.get('credentials_file', 'schwab_api.json')
        self.token_file = self.config.get('token_file', 'schwab_token.json')
        self.last_reconciliation_trade = None # Shared state for dynamic GUI sync
        
        # Thread safety
        self._data_lock = threading.RLock()
        self._order_lock = threading.Lock()
        self.sent_order_ids: Set[str] = set()
        self.order_to_strategy: Dict[str, str] = {} # Map order_id -> strategy_id
        self.suggested_strategy_trades: Dict[str, datetime] = {} # sid -> last_suggest_time
        self.active_order_signals: Set[str] = set() # Track strategy_ids currently in order_queue
        self.working_strategy_ids: Set[str] = set() # Track strategy_ids with working orders at broker
        self.live_closed_day_pnl = 0.0
        
        self.trading_enabled = False # New toggle for live trading vs sim only
        self.working_orders = [] # Shared cache for GUI
        self.awaiting_broker_sync = False # Flag for in-flight orders
        
        # Broker Heartbeat State
        self.broker_connected = False
        self.heartbeat_failures = 0
        self.heartbeat_running = False

        # Session persistence
        self.startup_time = datetime.now()
        self.forced_shutdown_requested = False
        self.session_manager = SessionManager(self.config['session_file_path'])
        
        self.is_running = False
        self._stop_event = asyncio.Event()
        self.reconciliation_event = asyncio.Event() # Triggered by sim trades
        self._recon_task = None
        self.status = "Stopped" 
        self._option_cache = OrderedDict() # Issue 11: LRU-style cache
        self.order_queue = queue.Queue()
        self.stats = TradeStats() # Enhancement 2: Trading Statistics
        
        # Resolve DB path relative to project root if it's relative
        db_path = self.config.get('db_path', 'data/spx_0dte.db')
        if not os.path.isabs(db_path):
            root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
            db_path = os.path.join(root, db_path)
        self.config['db_path'] = db_path
        self.logger.info(f"Monitor initialized with DB: {db_path}")

    def _initialize_sub_strategies(self):
        start_dt = datetime.strptime(self.config['portfolio_start_time'], '%H:%M:%S')
        end_dt = datetime.strptime(self.config['portfolio_end_time'], '%H:%M:%S')
        interval = timedelta(minutes=self.config['portfolio_interval_minutes'])
        
        curr = start_dt
        while curr <= end_dt:
            t = curr.time()
            sid = f"strat_{t.strftime('%H%M')}"
            s = SubStrategy(sid=sid, trade_start_time=t)
            
            # Delta targets (calculated from config)
            s.init_s_delta = self.config['initial_sum_delta'] / 2
            s.init_l_delta = max((self.config['initial_sum_delta'] / 2) - self.config['init_wing_delta'], 
                                self.config.get('min_long_delta', 0.025))
            
            self.sub_strategies[sid] = s
            curr += interval

    async def run_live_monitor(self):
        """Main loop for live monitoring using TaskGroup (Python 3.11+)"""
        self.is_running = True
        self.logger.info("Starting live monitor thread...")
        self.status = "Initializing..."
        
        try:
            async with asyncio.TaskGroup() as tg:
                # 0. Startup notification
                await notify_all(self.config, "Monitor Started", title="SPT v4 Live")
                
                # 1. Start Support Tasks
                tg.create_task(self.run_broker_heartbeat())
                tg.create_task(self._run_health_check_server()) # Enhancement 1: Health Check
                
                # 2. Main Strategy Logic (30s cadence)
                tg.create_task(self._monitoring_loop())
                
                # 3. Dedicated Reconciliation Logic (Unified GAP_SYNC)
                tg.create_task(self.run_reconciliation_loop())

                # 4. High-Frequency Broker Sync (5s cadence)
                tg.create_task(self._broker_sync_loop())
                
        except Exception as e:
            if not isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                self.status = f"Fatal Error: {str(e)}"
                self.logger.error(f"Fatal task failure in TaskGroup: {e}\n{traceback.format_exc()}")
            self.is_running = False
            asyncio.create_task(notify_all(self.config, f"Monitor Stopped: {self.status}", title="SPT v4 Live", priority="urgent"))

    async def run_reconciliation_loop(self):
        """Unified path for execution: Waits for 'pokes' or runs periodically on timeout."""
        self.logger.info("Reconciliation Loop started.")
        # Periodic reconciliation interval (seconds) - runs even without sim trade pokes
        periodic_interval = self.config.get('check_interval_minutes', 0.5) * 60

        while self.is_running:
            try:
                # Wait for a 'poke' from simulation trades OR timeout for periodic check
                try:
                    await asyncio.wait_for(self.reconciliation_event.wait(), timeout=periodic_interval)
                    self.reconciliation_event.clear()

                    # BATCHING WINDOW: Wait for more trades to arrive (only on poke, not timeout)
                    window = self.config.get('trade_batching_window_seconds', 0)
                    if window > 0:
                        self.logger.info(f"Reconciliation Poked: Waiting {window}s batching window for additional signals.")
                        await asyncio.sleep(window)
                        # Clear event again in case trades arrived during sleep
                        self.reconciliation_event.clear()
                except asyncio.TimeoutError:
                    # Periodic check - no batching window needed
                    pass

                # Trigger the actual sync logic
                # Note: fetch fresh snap for most accurate reconciliation
                snap = await self.get_live_options_data()
                if snap is not None:
                    await self._check_reconciliation(snap)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in reconciliation loop: {e}")
                await asyncio.sleep(1)

    async def _monitoring_loop(self):
        """Isolated monitoring logic extracted for TaskGroup context"""
        try:
            now = datetime.now()
            catch_up_start = datetime.combine(now.date(), time(8, 30))
            
            session_state = self.session_manager.load_session()
            if session_state:
                self.logger.info("Found existing session for today. Restoring...")
                self.session_manager.restore_monitor(self, session_state)
                if 'timestamp' in session_state:
                    try:
                        file_ts = datetime.fromisoformat(session_state['timestamp'].replace('Z', '+00:00'))
                        if file_ts.date() == now.date():
                            catch_up_start = max(catch_up_start, file_ts)
                            self.logger.info(f"Resuming catch-up from session timestamp: {catch_up_start}")
                    except Exception as e:
                        self.logger.warning(f"Could not parse session timestamp: {e}")
            
            if now > catch_up_start:
                self.status = "Catching up..."
                await self._run_historical_simulation(catch_up_start, now)
            
            self.status = "Running"

            while self.is_running:
                # 0. Overnight Sentinel Check (Requirement #3)
                now_check = datetime.now()
                if now_check.date() > self.startup_time.date() and now_check.time() >= time(8, 0):
                    self.logger.warning("Overnight session detected (8:00 AM threshold). Initiating forced shutdown.")
                    self.trading_enabled = False
                    self.is_running = False
                    self.status = "Forced Shutdown"
                    self.session_manager.save_session(self)
                    self.forced_shutdown_requested = True
                    break

                # Runs at 30s cadence (from config check_interval_minutes)
                await self._monitor_step()
                self._update_stats() # Enhancement 2: Update session stats
                
                try:
                    interval = self.config['check_interval_minutes'] * 60
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                    break
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            self.logger.info("Monitoring loop cancelled.")
            raise
        except Exception as e:
            self.logger.error(f"Error in monitoring loop: {e}\n{traceback.format_exc()}")
            raise

    async def _sync_broker_data(self):
        """Fetch positions, recent trades, and working orders. Clears awaiting_broker_sync flag."""
        if not self.client or not self.account_hash:
            return

        try:
            # 1. Fetch live SPX positions from Broker
            broker_positions = await self.get_live_positions()
            
            # 2. Fetch live recent trades from Broker
            broker_trades = await self.get_live_trades()
            
            # 3. Fetch working orders to track status
            working_orders = await self.get_working_orders()

            with self._data_lock:
                if broker_positions is not None:
                    self._update_live_portfolio(broker_positions)

                if broker_trades is not None:
                    self.live_combined_portfolio.trades = broker_trades
                    # Recalculate live cash from trades to show PnL correctly
                    self.live_combined_portfolio.cash = sum(t.credit - t.commission for t in broker_trades)

                if working_orders is not None:
                    self.working_orders = working_orders # Cache for GUI
                    self.working_strategy_ids = {
                        self.order_to_strategy[str(o['orderId'])] 
                        for o in working_orders 
                        if str(o.get('orderId')) in self.order_to_strategy
                    }
                    
                    # CLEARING FLAG: If we were waiting for sync, clear it now that we have fresh data
                    if self.awaiting_broker_sync:
                        self.awaiting_broker_sync = False
                        self.logger.info("Fresh broker data received. Clearing awaiting_broker_sync flag.")

        except Exception as e:
            self.logger.error(f"Error in _sync_broker_data: {e}")

    async def _broker_sync_loop(self):
        """High-frequency loop to keep account state fresh (5s)"""
        self.logger.info("Broker Sync Loop started (5s cadence).")
        initial_sync_done = False
        while self.is_running:
            try:
                # Sync if market is open OR if we haven't done an initial sync yet (useful for after-hours start)
                now = datetime.now()
                if self.is_market_open(now) or self.status == "Initializing..." or not initial_sync_done:
                    await self._sync_broker_data()
                    initial_sync_done = True
                
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in broker_sync_loop: {e}")
                await asyncio.sleep(5)

    async def _run_health_check_server(self):
        """Enhancement 1: Lightweight HTTP server for external monitoring tools"""
        async def handle_health(reader, writer):
            try:
                # Read request (just consume it)
                await reader.read(4096)
                with self._data_lock:
                    status_data = {
                        "status": self.status,
                        "broker_connected": self.broker_connected,
                        "heartbeat_failures": self.heartbeat_failures,
                        "is_running": self.is_running,
                        "live_pnl": self.live_closed_day_pnl,
                        "sim_pnl": self.combined_portfolio.cash + sum(l.price * l.quantity * 100 for l in self.combined_portfolio.positions),
                        "timestamp": datetime.now().isoformat()
                    }
                res_body = json.dumps(status_data).encode()
                response = (
                    f"HTTP/1.1 200 OK\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(res_body)}\r\n"
                    f"Connection: close\r\n\r\n"
                ).encode() + res_body
                writer.write(response)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        port = self.config.get('health_check_port', 8081) # Use 8081 to avoid common 8080 conflicts
        try:
            server = await asyncio.start_server(handle_health, '127.0.0.1', port)
            self.logger.info(f"Health check server listening on 127.0.0.1:{port}")
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            self.logger.info("Health check server shutting down.")
            raise
        except Exception as e:
            self.logger.error(f"Failed to start health check server: {e}")

    def _update_stats(self):
        """Enhancement 2: Calculate session statistics"""
        with self._data_lock:
            # Stats for SubStrategies
            total = 0
            winners = 0
            losers = 0
            total_dur = 0.0
            
            for sid, s in self.sub_strategies.items():
                if not s.has_traded_today: continue
                # Calculate PnL for this strategy
                mv = sum(l.price * l.quantity * 100 for l in s.portfolio.positions)
                pnl = s.portfolio.cash + mv
                
                # Only count as 'finished' for win rate if positions are flat or day is late
                is_closed = not s.portfolio.positions
                if is_closed:
                    total += 1
                    if pnl > 0: winners += 1
                    elif pnl < 0: losers += 1
                
                # Duration: simple estimate (entry time to current or exit)
                if s.portfolio.trades:
                    entry_ts = s.portfolio.trades[0].timestamp
                    last_ts = s.portfolio.trades[-1].timestamp if is_closed else datetime.now()
                    dur = (last_ts - entry_ts).total_seconds() / 60.0
                    total_dur += dur

            self.stats.total_trades = total
            self.stats.winners = winners
            self.stats.losers = losers
            self.stats.total_pnl = self.combined_portfolio.cash + sum(l.price * l.quantity * 100 for l in self.combined_portfolio.positions)
            if total > 0:
                self.stats.avg_duration_minutes = total_dur / len([s for s in self.sub_strategies.values() if s.has_traded_today])
            
            # Drawdown calculation
            # Simplified: track daily peak simulated equity vs current
            equity = self.stats.total_pnl
            if not hasattr(self, '_peak_equity'): self._peak_equity = equity
            self._peak_equity = max(self._peak_equity, equity)
            dd = self._peak_equity - equity
            self.stats.max_drawdown = max(self.stats.max_drawdown, dd)

    async def run_broker_heartbeat(self):
        """Dedicated loop to check broker connection every 5s"""
        self.heartbeat_running = True
        self.logger.info("Broker heartbeat loop started.")
        
        import random
        backoff = 1.0
        max_backoff = 60.0
        
        while self.heartbeat_running:
            try:
                if not self.client:
                    await self.initialize_schwab_client()
                    if not self.client:
                        # Init failed, let the exception block handle the delay/backoff
                        raise ConnectionError("Schwab client failed to initialize (check network/VPN/DNS)")
                
                # Use get_account_numbers as it was fastest in tests (~230ms)
                resp = await self.client.get_account_numbers()
                if resp.status_code == 200:
                    self.broker_connected = True
                    self.heartbeat_failures = 0
                    backoff = 1.0 # Reset backoff on success
                else:
                    raise Exception(f"Status {resp.status_code}")
                
                await asyncio.sleep(self.config.get('heartbeat_interval_seconds', 5))
                
            except Exception as e:
                self.heartbeat_failures += 1
                if self.heartbeat_failures == 3: # Send alert only on initial failure threshold
                    self.broker_connected = False
                    await notify_all(self.config, "Broker Connection Lost!", title="SPT v4 Critical", priority="urgent")
                    self.client = None # Reset client to force a fresh initialization attempt next cycle
                elif self.heartbeat_failures > 3:
                     self.broker_connected = False
                     self.client = None
                
                self.logger.error(f"Heartbeat failed (attempt {self.heartbeat_failures}): {e}")
                
                # Issue 7: Exponential backoff with jitter
                sleep_time = min(backoff + random.uniform(0, 0.1 * backoff), max_backoff)
                await asyncio.sleep(sleep_time)
                backoff = min(backoff * 2, max_backoff)

    async def initialize_schwab_client(self, credentials_file: Optional[str] = None, token_file: Optional[str] = None):
        """Initialize Schwab API client using local keys"""
        try:
            home_dir = Path.home()
            credentials_dir = home_dir / ".api_keys" / "schwab"
            
            # Use provided files or fall back to monitor attributes
            cf = credentials_file or self.credentials_file
            tf = token_file or self.token_file
            
            credentials_path = credentials_dir / cf
            token_path = credentials_dir / tf
            
            with open(credentials_path, 'r') as f:
                creds = json.load(f)
            
            self.client = easy_client(
                api_key=creds['api_key'],
                app_secret=creds['api_secret'],
                callback_url=creds.get('callback_url', 'https://127.0.0.1'),
                token_path=str(token_path),
                asyncio=True,
                enforce_enums=False
            )
            
            # Set timeout on the underlying httpx session to handle large SPX chain responses
            self.client.session.timeout = 30.0
            
            # Issue 19: Add timeout to initial verification call
            try:
                nums_resp = await asyncio.wait_for(self.client.get_account_numbers(), timeout=10.0)
            except asyncio.TimeoutError:
                self.logger.error("Timed out waiting for Schwab account numbers during initialization.")
                return
            
            if nums_resp.status_code == 200:
                for acc in nums_resp.json():
                    if str(acc.get('accountNumber')) == self.account_id:
                        self.account_hash = acc.get('hashValue')
                        break
            
            if not self.account_hash:
                self.logger.error(f"Could not resolve hash for account {self.account_id}")
            else:
                self.logger.info(f"Schwab client initialized for account {self.account_id}")
                
        except Exception as e:
            msg = str(e).lower()
            # Detect common DNS/Network error strings (especially when switching VPNs)
            if "nodename nor servname" in msg or "[errno 8]" in msg or "temporary failure" in msg:
                self.logger.error("Failed to initialize Schwab client: Network or DNS resolution failure (Check VPN connection).")
            else:
                self.logger.error(f"Failed to initialize Schwab client: {e}")
            self.client = None

    async def switch_account(self, account_id: str, credentials_file: str, token_file: str):
        """Switch to a different Schwab account/API profile"""
        self.logger.info(f"Switching to account {account_id} with {credentials_file}...")
        self.account_id = account_id
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.account_hash = None # Trigger re-resolve
        await self.initialize_schwab_client()

    def _parse_schwab_symbol(self, symbol: str) -> Optional[Dict]:
        """
        Robustly parse Schwab option symbols.
        Format Example: 'SPXW  240114C06960000'
        """
        try:
            # Basic cleanup
            symbol = symbol.strip()
            pts = symbol.split()
            if len(pts) < 2: return None
            
            code = pts[-1]
            # Strike is always the last 8 digits
            strike_str = code[-8:]
            side_char = code[-9]
            # Date is before the side character
            # We don't strictly need date for 0DTE logic since we know it's today
            
            side = 'CALL' if side_char == 'C' else 'PUT'
            strike = float(strike_str) / 1000.0
            
            return {
                'strike': strike,
                'side': side,
                'root': pts[0]
            }
        except Exception as e:
            self.logger.warning(f"Failed to parse symbol {symbol}: {e}")
            return None

    def is_market_open(self, now: datetime) -> bool:
        """Check if current time is within market hours (8:30 - 15:00)"""
        start_time = datetime.strptime(self.config['start_time'], '%H:%M:%S').time()
        end_time = datetime.strptime(self.config['end_time'], '%H:%M:%S').time()
        
        # Check if it's a weekday
        if now.weekday() >= 5: # Saturday or Sunday
            return False
            
        return start_time <= now.time() <= end_time

    async def _monitor_step(self):
        """Single iteration of checking for trades (30s cadence)"""
        now = datetime.now()
        if not self.is_market_open(now):
            self.logger.debug(f"Outside market hours ({now.time()}). Skipping monitor step.")
            return

        if not self.client:
            await self.initialize_schwab_client()
            if not self.client: return

        self.logger.debug(f"Monitor step (Pricing/Strategy) at {now}")
        
        # NOTE: Position and Trade Sync now handled by _broker_sync_loop (5s)
        broker_positions = self.live_combined_portfolio.positions
        broker_trades = self.live_combined_portfolio.trades

        # 2c. Calculate Session Baseline (Starting Market Value)
        # We need (Qty_at_open * Prev_Close) for all symbols.
        
        # Issue 14: Pull SNAP early and use it for pricing to avoid redundant API calls
        snap = await self.get_live_options_data()
        if snap is None or snap.empty:
            self.logger.warning("Failed to fetch option chain. Aborting monitor step to avoid stale state.")
            return

        # Create a quote dict from the snap for portfolios
        snap_quotes = {}
        for _, row in snap.iterrows():
            snap_quotes[row['symbol']] = {
                'bid': row['bidprice'],
                'ask': row['askprice'],
                'mid': row['mid_price'],
                'delta': row['delta'],
                'theta': row['theta']
            }
        
        with self._data_lock:
            # Group fills by symbol for qty tracking
            symbol_net_qty_fills = defaultdict(int)
            for t in broker_trades:
                for l in t.legs:
                    symbol_net_qty_fills[l.symbol] += l.quantity

            all_relevant_symbols = set(symbol_net_qty_fills.keys())
            for p in broker_positions: all_relevant_symbols.add(p.symbol)
            
            if all_relevant_symbols:
                # Only fetch what's NOT in snap_quotes
                missing = [s for s in all_relevant_symbols if s not in snap_quotes]
                quotes = snap_quotes.copy()
                if missing:
                    extra = await self.fetch_quotes(missing)
                    quotes.update(extra)
                
                starting_value = 0.0
                for symbol in all_relevant_symbols:
                    # Robust symbol lookup for quotes to get prev_close (fallback for whitespace differences)
                    q = quotes.get(symbol)
                    if not q:
                        norm_sym = " ".join(symbol.split())
                        for sym, data in quotes.items():
                            if " ".join(sym.split()) == norm_sym:
                                q = data
                                break
                    
                    # Some Schwab API versions nest the quote data
                    q_data = q.get('quote', q) if isinstance(q, dict) else {}
                    prev_close = q_data.get('closePrice', 0) if isinstance(q_data, dict) else 0
                    
                    curr_pos = next((p for p in broker_positions if p.symbol == symbol), None)
                    curr_qty = curr_pos.quantity if curr_pos else 0
                    
                    start_qty = curr_qty - symbol_net_qty_fills[symbol]
                    starting_value += (start_qty * prev_close * 100)
                    
                self.live_combined_portfolio.starting_market_value = starting_value
                self._update_all_pricing(quotes, snap=snap)
                
                mv = sum(l.price * l.quantity * 100 for l in self.live_combined_portfolio.positions)
                cash = self.live_combined_portfolio.cash
                day_pnl = (mv + cash) - starting_value
                self.logger.debug(f"PnL Calc: MV={mv:.2f}, Cash={cash:.2f}, StartMV={starting_value:.2f}, DayPnL={day_pnl:.2f}")
        
        # 3. Update Quotes and Deltas for all active positions (Already updated in step 2c via snap)
        # 4. Strategy logic (snap already fetched)
        ts = datetime.now()
        t_time = ts.time()
        start_time_obj = time(8, 30)
        end_time_obj = time(15, 0)
        
        # Target decay
        decay_c = calculate_delta_decay(ts, 'CALL', self.config['initial_sum_delta']/2, start_time_obj, end_time_obj)
        decay_p = calculate_delta_decay(ts, 'PUT', self.config['initial_sum_delta']/2, start_time_obj, end_time_obj)
        t_short = (decay_c + decay_p) / 2
        spx = self.estimate_spx_price(snap)

        t_trades = defaultdict(list)
        
        with self._data_lock:
            for sid, s in self.sub_strategies.items():
                if t_time < s.trade_start_time:
                    continue
                
                # Skip if there's already a pending signal for this strategy in the GUI
                if sid in self.active_order_signals:
                    continue

                
                # Update individual strategy position pricing/deltas from snap
                for p in s.portfolio.positions:
                    r = snap[(snap['strike_price'].round().astype(int) == int(round(p.strike))) & (snap['side'] == p.side)]
                    if not r.empty:
                        p.delta = r['delta'].iloc[0]
                        p.price = r['mid_price'].iloc[0]
                        p.theta = float(r['theta'].iloc[0]) if not pd.isna(r['theta'].iloc[0]) else 0.0
                
                # Check for entry
                if not s.has_traded_today and t_time < end_time_obj:
                    trade = self._check_entry(s, snap, ts)
                    if trade:
                        # NOTE: In Live mode, we DON'T update the portfolio yet!
                        # We wait for the user to confirm in the GUI.
                        t_trades[sid].append(trade)
                
                # Check for rebalance/exit
                elif s.has_traded_today:
                    if t_time >= end_time_obj and s.portfolio.positions:
                        trade = self._create_exit_trade(s, snap, ts, spx)
                        if trade:
                            t_trades[sid].append(trade)
                    elif t_time < end_time_obj:
                        trades = self._check_rebalance(s, snap, ts, t_short)
                        for tr in trades:
                            t_trades[sid].append(tr)

            # 4. Fast Model Update: Simulation state is updated immediately
            if t_trades:
                self.logger.info(f"Fast Model Update: Executing {len(t_trades)} strategy signals in simulation.")
                
                # Update sub-strategies first
                for sid, trades in t_trades.items():
                    if sid in self.sub_strategies:
                        for tr in trades:
                            self.sub_strategies[sid].portfolio.add_trade(tr)
                            self.sub_strategies[sid].has_traded_today = True

                # Net and update combined simulation portfolio
                netted = self.net_trades(t_trades)
                for nt in netted:
                    self.combined_portfolio.add_trade(nt)
                
                # POKE: Signal that reconciliation is needed immediately
                self.reconciliation_event.set()

        # 5. Reconciliation (Simulation vs reality)
        # Handled by run_reconciliation_loop() when poked via reconciliation_event.
        # Removed direct call here to prevent race condition causing duplicate order confirmations.

        # 6. Save Session
        self.session_manager.save_session(self)


    def _round_to_tick(self, price: float, num_legs: int = 1) -> float:
        """
        Round price to the nearest tick for SPX.
        - Spreads/Condors (2+ legs): $0.05 increments.
        - Single Legs: $0.05 if < $3.00, $0.10 if >= $3.00.
        """
        if num_legs > 1:
            tick = 0.05
        else:
            tick = 0.10 if price >= 3.0 else 0.05
        return round(round(price / tick) * tick, 2)

    async def execute_net_trade(self, trade: Trade):
        """Execute the plan attached to the trade object, minimizing churn."""
        if not self.client or not self.account_hash:
            self.logger.error("Cannot execute trade: Schwab client or account hash not initialized.")
            trade.status = "failed"
            return

        # ALWAYS regenerate the execution plan right before execution to deduplicate 
        # against manual orders placed while the confirmation window was open.
        self.logger.info("Refreshing broker state and execution plan for final verification...")
        self.working_orders = await self.get_working_orders()
        plan = self.create_execution_plan(trade)

        try:
            # 1. Cancel Outdated Orders
            for wo in plan['to_cancel']:
                wid = str(wo.get('orderId'))
                self.logger.info(f"Cancelling outdated order {wid}...")
                await self.cancel_order(wid)

            # 2. Submit New Chunks
            needed_chunks = plan['to_submit']
            if not needed_chunks:
                self.logger.info("No new chunks to submit. Execution complete.")
                trade.status = "filled"
                return

            self.logger.info(f"Executing {len(needed_chunks)} new chunks for trade {trade.purpose}.")
            
            all_success = True
            for i, chunk_legs in enumerate(needed_chunks):
                # Aggregate unrolled legs before submission to avoid Schwab duplicate leg error
                rolled_chunk_legs = self._roll_legs(chunk_legs)
                num_legs = len(rolled_chunk_legs)
                
                # GCD-based Unitization (Issue Fix)
                # Determine if we can treat this as N units of a base structure
                leg_qtys = [abs(l.quantity) for l in rolled_chunk_legs]
                num_units = leg_qtys[0]
                for q in leg_qtys[1:]:
                    num_units = math.gcd(num_units, q)
                
                # Calculate chunk credit/price (TOTAL dollar amount)
                total_chunk_credit = sum(-l.quantity * l.price for l in rolled_chunk_legs) * 100
                total_mid_price = abs(total_chunk_credit / 100.0)
                
                # Price per unit for the broker
                unit_mid_price = total_mid_price / num_units
                offset = self.config.get('order_offset', 0.0)
                
                # Apply offset and tick rounding to UNIT price
                if total_chunk_credit >= 0:
                    raw_price = unit_mid_price + offset
                else:
                    raw_price = max(0.0, unit_mid_price - offset)

                ticked_price = self._round_to_tick(raw_price, num_legs=num_legs)
                price_str = f"{ticked_price:.2f}"
                
                builder = None
                self.logger.info(f"Order {i+1}/{len(needed_chunks)}: {num_legs} legs x {num_units} units, Unit Mid: {unit_mid_price:.2f}, Ticked: {price_str}")
                
                if num_legs == 1:
                    leg = rolled_chunk_legs[0]
                    instruction = getattr(leg, 'instruction', None)
                    if not instruction:
                        if leg.quantity > 0: instruction = "BUY_TO_CLOSE" if trade.purpose == TradePurpose.EXIT else "BUY_TO_OPEN"
                        else: instruction = "SELL_TO_CLOSE" if trade.purpose == TradePurpose.EXIT else "SELL_TO_OPEN"
                    
                    builder = OrderBuilder()
                    builder.set_order_strategy_type(OrderStrategyType.SINGLE)
                    builder.set_order_type(OrderType.LIMIT)
                    builder.set_price(price_str)
                    builder.set_quantity(num_units)
                    builder.set_session(Session.NORMAL)
                    builder.set_duration(Duration.DAY)
                    builder.add_option_leg(getattr(OptionInstruction, instruction), leg.symbol, abs(leg.quantity) // num_units)
                
                elif num_legs == 4:
                    # Try to see if it's an Iron Condor
                    sc = next((l for l in rolled_chunk_legs if l.side == 'CALL' and l.quantity < 0), None)
                    lc = next((l for l in rolled_chunk_legs if l.side == 'CALL' and l.quantity > 0), None)
                    sp = next((l for l in rolled_chunk_legs if l.side == 'PUT' and l.quantity < 0), None)
                    lp = next((l for l in rolled_chunk_legs if l.side == 'PUT' and l.quantity > 0), None)
                    
                    if all([sc, lc, sp, lp]):
                        builder = OrderBuilder()
                        builder.set_order_strategy_type(OrderStrategyType.SINGLE)
                        builder.set_complex_order_strategy_type(ComplexOrderStrategyType.IRON_CONDOR)
                        builder.set_order_type(OrderType.NET_CREDIT if total_chunk_credit > 0 else OrderType.NET_DEBIT)
                        builder.set_price(price_str)
                        builder.set_quantity(num_units)
                        builder.set_session(Session.NORMAL)
                        builder.set_duration(Duration.DAY)
                        
                        builder.add_option_leg(OptionInstruction.SELL_TO_OPEN if trade.purpose != TradePurpose.EXIT else OptionInstruction.BUY_TO_CLOSE, sc.symbol, abs(sc.quantity) // num_units)
                        builder.add_option_leg(OptionInstruction.BUY_TO_OPEN if trade.purpose != TradePurpose.EXIT else OptionInstruction.SELL_TO_CLOSE, lc.symbol, abs(lc.quantity) // num_units)
                        builder.add_option_leg(OptionInstruction.SELL_TO_OPEN if trade.purpose != TradePurpose.EXIT else OptionInstruction.BUY_TO_CLOSE, sp.symbol, abs(sp.quantity) // num_units)
                        builder.add_option_leg(OptionInstruction.BUY_TO_OPEN if trade.purpose != TradePurpose.EXIT else OptionInstruction.SELL_TO_CLOSE, lp.symbol, abs(lp.quantity) // num_units)

                if not builder:
                    # Generic CUSTOM builder for any 2-4 legs
                    builder = OrderBuilder()
                    builder.set_order_strategy_type(OrderStrategyType.SINGLE)
                    builder.set_complex_order_strategy_type(ComplexOrderStrategyType.CUSTOM)
                    builder.set_order_type(OrderType.NET_CREDIT if total_chunk_credit >= 0 else OrderType.NET_DEBIT)
                    builder.set_price(price_str)
                    builder.set_quantity(num_units)
                    builder.set_session(Session.NORMAL)
                    builder.set_duration(Duration.DAY)
                    
                    for leg in rolled_chunk_legs:
                        inst_str = getattr(leg, 'instruction', None)
                        if not inst_str:
                            if leg.quantity > 0: inst_str = "BUY_TO_CLOSE" if trade.purpose == TradePurpose.EXIT else "BUY_TO_OPEN"
                            else: inst_str = "SELL_TO_CLOSE" if trade.purpose == TradePurpose.EXIT else "SELL_TO_OPEN"
                        builder.add_option_leg(getattr(OptionInstruction, inst_str), leg.symbol, abs(leg.quantity) // num_units)

                # Detailed leg logging
                leg_details = []
                for lg in rolled_chunk_legs:
                    leg_details.append(f"{lg.symbol}: {lg.quantity}x @ Mid {lg.price:.2f} (Strike: {lg.strike})")
                self.logger.info(f"  Legs: {', '.join(leg_details)}")
                self.logger.info(f"  Net Amount (TOTAL): ${abs(total_chunk_credit):.2f} {'Credit' if total_chunk_credit >= 0 else 'Debit'}")

                order = builder.build()
                resp = await self.client.place_order(self.account_hash, order)
                if resp.status_code in [200, 201]:
                    from schwab import utils as schwab_utils
                    order_id = schwab_utils.Utils(self.client, self.account_hash).extract_order_id(resp)
                    self.logger.info(f"Trade executed successfully! Order ID: {order_id}")
                    if order_id:
                        self.order_to_strategy[str(order_id)] = trade.strategy_id
                else:
                    self.logger.error(f"Trade execution failed: {resp.status_code} {resp.text}")
                    all_success = False

            if all_success:
                trade.status = "filled"
                self.awaiting_broker_sync = True # Track in-flight sync
                self.logger.info("Live trade execution complete; Simulation updated. Awaiting broker sync.")
                # Trade fill notification
                msg = f"Trade filled: {trade.strategy_id} {trade.purpose.value} (Net: ${trade.credit/100.0:.2f})"
                asyncio.create_task(notify_all(self.config, msg, title="Trade Alert"))
                self.session_manager.save_session(self)
            else:
                trade.status = "failed"

        except Exception as e:
            self.logger.error(f"Error executing trade: {e}")
            self.logger.error(traceback.format_exc())
            trade.status = "error"


    async def get_live_options_data(self) -> Optional[pd.DataFrame]:
        """Fetch full SPX 0DTE option chain and format as DataFrame"""
        if not self.client: return None
        
        try:
            # Explicitly filter for 0DTE and limited strike range to prevent 'Body buffer overflow'
            today = date.today()
            resp = await self.client.get_option_chain(
                '$SPX', 
                strike_range=100,
                from_date=today,
                to_date=today
            )
            if resp.status_code != 200:
                self.logger.error(f"Failed to fetch option chain: {resp.status_code}")
                return None
                
            data = resp.json()
            recs = []
            
            # Schwab returns either 'callStrategyChain' (Strike -> Exp -> Options)
            # or 'callExpDateMap' (Exp -> Strike -> Options)
            for side in ['CALL', 'PUT']:
                sc_key = 'callStrategyChain' if side == 'CALL' else 'putStrategyChain'
                em_key = 'callExpDateMap' if side == 'CALL' else 'putExpDateMap'
                
                strategy_chain = data.get(sc_key)
                exp_map = data.get(em_key)
                
                if strategy_chain:
                    # Strike -> Expiration -> List[Option]
                    for strike_str, exps in strategy_chain.items():
                        strike = float(strike_str)
                        for exp_str, options in exps.items():
                            for opt in options:
                                if opt.get('daysToExpiration', 0) == 0:
                                    recs.append(self._parse_opt_rec(opt, strike, side))
                
                elif exp_map:
                    # Expiration -> Strike -> List[Option]
                    for exp_str, strikes in exp_map.items():
                        for strike_str, options in strikes.items():
                            strike = float(strike_str)
                            for opt in options:
                                if opt.get('daysToExpiration', 0) == 0:
                                    recs.append(self._parse_opt_rec(opt, strike, side))
            
            df = pd.DataFrame(recs)
            if not df.empty:
                # Issue 15: Filter early to reduce memory usage
                df = df.dropna(subset=['delta'])
                if 'theta' in df.columns:
                    df['theta'] = df['theta'].fillna(0.0)
            return df
        except Exception as e:
            self.logger.error(f"Error fetching live options data: {e}")
            return None

    def _parse_opt_rec(self, opt: Dict, strike: float, side: str) -> Dict:
        """Helper to parse a single option contract record"""
        return {
            'symbol': opt.get('symbol'),
            'strike_price': strike,
            'side': side,
            'bidprice': opt.get('bid', 0),
            'askprice': opt.get('ask', 0),
            'mid_price': (opt.get('bid', 0) + opt.get('ask', 0)) / 2,
            'delta': opt.get('delta', 0),
            'theta': opt.get('theta', 0)
        }

    async def fetch_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """Fetch real-time quotes from Schwab"""
        if not self.client or not symbols: return {}
        
        try:
            resp = await self.client.get_quotes(symbols)
            if resp.status_code != 200: return {}
            
            data = resp.json()
            quotes = {}
            for sym, q in data.items():
                # Schwab API might return quote under a 'quote' key or directly
                quote_data = q.get('quote', q)
                # Schwab API version dependent: delta sometimes in 'greeks' or directly in 'quote'
                greeks = q.get('greeks', {})
                quotes[sym] = {
                    'bid': quote_data.get('bidPrice', 0),
                    'ask': quote_data.get('askPrice', 0),
                    'mid': (quote_data.get('bidPrice', 0) + quote_data.get('askPrice', 0)) / 2,
                    'delta': greeks.get('delta', quote_data.get('delta', 0)),
                    'theta': greeks.get('theta', quote_data.get('theta', 0))
                }
            return quotes
        except Exception as e:
            self.logger.error(f"Error fetching quotes: {e}")
            return {}

    def _update_all_pricing(self, quotes: Dict[str, Dict], snap: Optional[pd.DataFrame] = None):
        """Update both sim and live portfolios with latest quotes using robust matching"""
        for portfolio in [self.combined_portfolio, self.live_combined_portfolio]:
            for p in portfolio.positions:
                found = False
                
                # 1. Try Strike/Side matching against snap (most robust for SPX 0DTE)
                if snap is not None and not snap.empty:
                    # Match by integer strike and side
                    r = snap[(snap['strike_price'].round().astype(int) == int(round(p.strike))) & (snap['side'] == p.side)]
                    if not r.empty:
                        row = r.iloc[0]
                        p.bid_price = row['bidprice']
                        p.ask_price = row['askprice']
                        p.price = row['mid_price']
                        p.delta = row['delta']
                        p.theta = float(row['theta']) if not pd.isna(row['theta']) else 0.0
                        found = True
                
                # 2. Fallback to Symbol matching in quotes (for non-SPX or snapshot misses)
                if not found:
                    # Try exact symbol
                    q = quotes.get(p.symbol)
                    if not q:
                        # Try normalized symbol (remove extra spaces)
                        norm_sym = " ".join(p.symbol.split())
                        for sym, data in quotes.items():
                            if " ".join(sym.split()) == norm_sym:
                                q = data
                                break
                    
                    if q:
                        p.bid_price = q.get('bid', 0)
                        p.ask_price = q.get('ask', 0)
                        p.price = q.get('mid', 0)
                        p.delta = q.get('delta', 0)
                        p.theta = q.get('theta', 0)

    async def get_live_positions(self) -> List[Dict]:
        """Fetch current SPX positions from Broker"""
        if not self.client or not self.account_hash: return None
        
        try:
            resp = await self.client.get_account(self.account_hash, fields=self.client.Account.Fields.POSITIONS)
            if resp.status_code != 200:
                self.logger.error(f"Failed to fetch account positions: {resp.status_code}")
                return None
            
            positions = resp.json().get('securitiesAccount', {}).get('positions', [])
            spx_pos = []
            for pos in positions:
                instr = pos.get('instrument', {})
                if instr.get('assetType') == 'OPTION' and (instr.get('underlyingSymbol') == '$SPX' or instr.get('symbol').startswith('SPX')):
                    sym = instr.get('symbol')
                    parsed = self._parse_schwab_symbol(sym)
                    if not parsed: continue
                    
                    qty = pos.get('longQuantity', 0) - pos.get('shortQuantity', 0)
                    spx_pos.append({
                        'symbol': sym,
                        'strike': parsed['strike'],
                        'side': parsed['side'],
                        'quantity': int(qty),
                        'price': pos.get('marketValue', 0) / (qty * 100) if qty != 0 else 0,
                        'bid': pos.get('marketValue', 0) / (qty * 100) if qty != 0 else 0,
                        'ask': pos.get('marketValue', 0) / (qty * 100) if qty != 0 else 0,
                    'avg_price': pos.get('averagePrice', 0),
                    'current_day_pnl': pos.get('currentDayProfitLoss', 0)
                    })
            return spx_pos
        except Exception as e:
            self.logger.error(f"Error fetching live positions: {e}")
            return None # Return None to indicate failure

    async def get_live_trades(self) -> List[Trade]:
        """Fetch recent filled orders from Schwab and convert to Trade objects"""
        if not self.client or not self.account_hash: return None
        try:
            # Fetch for today
            from_time = datetime.combine(date.today(), time(0, 0))
            resp = await self.client.get_orders_for_account(self.account_hash, from_entered_datetime=from_time, status=self.client.Order.Status.FILLED)
            if resp.status_code != 200:
                self.logger.error(f"Failed to fetch account orders: {resp.status_code}")
                return None
            
            orders = resp.json()
            trades = []
            self.logger.info(f"Fetched {len(orders)} filled orders from Schwab.")
            for order in orders:
                legs = []
                for oleg in order.get('orderLegCollection', []):
                    instr = oleg.get('instrument', {})
                    if instr.get('underlyingSymbol') == '$SPX' or instr.get('symbol').startswith('SPX'):
                        sym = instr.get('symbol')
                        parsed = self._parse_schwab_symbol(sym)
                        if parsed:
                            legs.append(OptionLeg(
                                symbol=sym,
                                strike=parsed['strike'],
                                side=parsed['side'],
                                quantity=int(oleg.get('quantity', 0)) if oleg.get('instruction').startswith('BUY') else -int(oleg.get('quantity', 0)),
                                price=order.get('price', 0),
                                entry_price=order.get('price', 0)
                            ))
                
                if legs:
                    # ... (rest of the logic remains the same)
                    # (I'll keep it concise for the replacement)
                    close_time_str = order.get('closeTime') or order.get('enteredTime')
                    if close_time_str:
                        ts = datetime.fromisoformat(close_time_str.replace('Z', '+00:00'))
                    else:
                        ts = datetime.now(timezone.utc)

                    order_id_key = str(order.get('orderId', ''))
                    strategy_id = self.order_to_strategy.get(order_id_key, "BROKER")
                    
                    # Prefer actual execution price from orderActivityCollection (Issue Fix)
                    activities = order.get('orderActivityCollection', [])
                    actual_net_cash = 0.0
                    has_execution = False
                    
                    if activities:
                        leg_id_to_instr = {l.get('legId'): l.get('instruction') for l in order.get('orderLegCollection', [])}
                        for activity in activities:
                            if activity.get('activityType') == 'EXECUTION':
                                for exec_leg in activity.get('executionLegs', []):
                                    exec_p = exec_leg.get('price', 0.0)
                                    exec_q = exec_leg.get('quantity', 0)
                                    lid = exec_leg.get('legId')
                                    instr = leg_id_to_instr.get(lid, '')
                                    multiplier = 1.0 if 'SELL' in instr else -1.0
                                    actual_net_cash += (exec_p * exec_q * multiplier)
                                    has_execution = True
                    
                    if has_execution:
                        credit = actual_net_cash * 100
                    else:
                        order_type = order.get('orderType', '')
                        raw_price = order.get('price', 0)
                        multiplier = abs(legs[0].quantity) * 100
                        if order_type == 'NET_DEBIT':
                            credit = -raw_price * multiplier
                        elif order_type == 'NET_CREDIT':
                            credit = raw_price * multiplier
                        else:
                            if legs[0].quantity > 0: credit = -raw_price * multiplier
                            else: credit = raw_price * multiplier

                    comm_per_contract = self.config.get('commission_per_contract', 1.13)
                    est_commission = comm_per_contract * sum(abs(l.quantity) for l in legs)
                    any_open = any(oleg.get('instruction', '').endswith('_OPEN') for oleg in order.get('orderLegCollection', []))
                    purpose = TradePurpose.RECONCILIATION if any_open else TradePurpose.EXIT

                    trades.append(Trade(
                        timestamp=ts.astimezone(ZoneInfo("America/Chicago")),
                        legs=legs,
                        credit=credit,
                        commission=est_commission,
                        current_sum_delta=0,
                        purpose=purpose,
                        strategy_id=strategy_id,
                        order_id=order_id_key,
                        status="filled"
                    ))

            self.logger.info(f"Processed {len(trades)} SPX trades for display.")
            trades.sort(key=lambda x: x.timestamp)
            return trades
        except Exception as e:
            self.logger.error(f"Error fetching live trades: {e}\n{traceback.format_exc()}")
            return None

    async def get_working_orders(self) -> List[Dict]:
        """Fetch currently working/pending orders from Schwab"""
        if not self.client or not self.account_hash: return None
        try:
            # We want working/pending orders
            statuses = [
                self.client.Order.Status.WORKING,
                self.client.Order.Status.PENDING_ACTIVATION,
                self.client.Order.Status.AWAITING_MANUAL_REVIEW,
                self.client.Order.Status.PENDING_CANCEL
            ]
            
            # get_orders_for_account with multiple statuses
            # Schwab API allows querying by status. 
            # If the library doesn't support a list of statuses easily, we might need to iterate or check docs.
            # Assuming it supports a single status at a time or we can just fetch all and filter.
            # Let's fetch for today and filter manually for speed/robustness if needed, 
            # but API filtering is better.
            
            # Try fetching ALL orders for today and filtering
            from_time = datetime.combine(date.today(), time(0, 0))
            resp = await self.client.get_orders_for_account(self.account_hash, from_entered_datetime=from_time)
            if resp.status_code != 200:
                self.logger.error(f"Failed to fetch account orders: {resp.status_code}")
                return None
            
            all_orders = resp.json()
            working_statuses = ['WORKING', 'PENDING_ACTIVATION', 'AWAITING_MANUAL_REVIEW', 'QUEUED']
            working_orders = [o for o in all_orders if o.get('status') in working_statuses]
            return working_orders
        except Exception as e:
            self.logger.error(f"Error fetching working orders: {e}")
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order by ID"""
        if not self.client or not self.account_hash: return False
        try:
            # The Schwab API cancel_order method takes (order_id, account_hash)
            # based on the error message suggesting account_hash was passed as order_id.
            resp = await self.client.cancel_order(int(order_id), self.account_hash)
            if resp.status_code in [200, 201, 204]:
                self.logger.info(f"Order {order_id} cancelled successfully.")
                # Cleanup internal tracking
                if order_id in self.order_to_strategy:
                    sid = self.order_to_strategy.pop(order_id)
                    if sid in self.working_strategy_ids:
                        self.working_strategy_ids.remove(sid)
                return True
            else:
                self.logger.error(f"Failed to cancel order {order_id}: {resp.status_code} {resp.text}")
                return False
        except Exception as e:
            self.logger.error(f"Error cancelling order {order_id}: {e}")
            return False

    def send_email_alert(self, trade: Trade, report_data: Dict):
        """Send trade alert via email (blocking call, should be run in thread)"""
        if not self.config.get('email_alerts_enabled', True):
            return
            
        config_path = self.config.get('email_config_path')
        if not config_path or not os.path.exists(config_path):
            self.logger.warning(f"Email config file not found at {config_path} - email alerts disabled")
            return
        
        try:
            with open(config_path, 'r') as f:
                email_config = json.load(f)
            
            # Ensure v5a structure if needed
            if 'from_email' not in email_config and 'sender_email' in email_config:
                email_config['from_email'] = email_config['sender_email']
                email_config['password'] = email_config['sender_password']
                email_config['smtp_server'] = 'smtp.gmail.com'
                email_config['smtp_port'] = 587

            recipients = self.config.get('email_recipients', [])
            if not recipients:
                self.logger.warning("No email recipients configured, skipping alert")
                return

            msg = MIMEMultipart()
            msg['From'] = email_config['from_email']
            msg['To'] = ", ".join(recipients)
            msg['Subject'] = "Terminaotr Alert: New Trade"
            
            # Create email body
            body = self._format_alert_body(trade, report_data)
            msg.attach(MIMEText(body, 'plain'))
            
            with smtplib.SMTP(email_config['smtp_server'], email_config['smtp_port']) as server:
                server.starttls()
                server.login(email_config['from_email'], email_config['password'])
                server.send_message(msg)
            
            self.logger.info(f"Trade alert email sent to {len(recipients)} recipients")
            
        except Exception as e:
            self.logger.error(f"Failed to send trade alert email: {e}")

    def _format_alert_body(self, trade: Trade, data: Dict) -> str:
        """Format the alert email body as requested by USER"""
        lines = []
        lines.append("NEW TRADE SIGNAL DETECTED")
        lines.append("=" * 40)
        lines.append(f"Strategy: {trade.strategy_id}")
        lines.append(f"Purpose: {trade.purpose.value}")
        lines.append(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")
        
        # Sort legs: PUT first, then by Strike
        sorted_new_legs = sorted(trade.legs, key=lambda x: (0 if x.side == 'PUT' else 1, x.strike))
        
        lines.append("NEW TRADE INFO:")
        for leg in sorted_new_legs:
            side = "SHORT" if leg.quantity < 0 else "LONG"
            lines.append(f"  {side} {leg.side} {leg.strike} x{abs(leg.quantity)}")
        
        net_impact = trade.credit / 100.0
        impact_text = f"NET CREDIT: ${net_impact:.2f}" if net_impact >= 0 else f"NET DEBIT: ${abs(net_impact):.2f}"
        lines.append(f"Total Net: {impact_text}")
        lines.append("")
        
        lines.append("CURRENT POSITIONS:")
        live_pos = data.get('live_positions', [])
        sim_pos = data.get('sim_positions', [])
        
        def sort_positions(positions):
            return sorted(positions, key=lambda x: (0 if x.side == 'PUT' else 1, x.strike))

        lines.append("  [LIVE]")
        if not live_pos: 
            lines.append("    No active positions")
        else:
            for p in sort_positions(live_pos):
                lines.append(f"    {p.side} {p.strike} x{p.quantity}")
            
        lines.append("  [SIM]")
        if not sim_pos: 
            lines.append("    No active positions")
        else:
            for p in sort_positions(sim_pos):
                lines.append(f"    {p.side} {p.strike} x{p.quantity}")
        lines.append("")
        
        lines.append("PERFORMANCE SUMMARY:")
        lines.append("  [Simulation]")
        lines.append(f"    Gross PnL: ${data.get('sim_gross_pnl', 0.0):.2f}")
        lines.append(f"    Fees:      ${data.get('sim_fees', 0.0):.2f}")
        lines.append(f"    Net PnL:   ${data.get('sim_net_pnl', 0.0):.2f}")
        lines.append(f"    Trades:    {data.get('sim_trades', 0)}")
        lines.append(f"    Margin:    ${data.get('sim_margin', 0.0):,.2f}")
        lines.append("")
        lines.append("  [Live]")
        lines.append(f"    Gross PnL: ${data.get('live_gross_pnl', 0.0):.2f}")
        lines.append(f"    Fees:      ${data.get('live_fees', 0.0):.2f}")
        lines.append(f"    Net PnL:   ${data.get('live_net_pnl', 0.0):.2f}")
        lines.append(f"    Trades:    {data.get('live_trades', 0)}")
        lines.append(f"    Margin:    ${data.get('live_margin', 0.0):,.2f}")
        lines.append("")
        
        lines.append("Action required: Please confirm or dismiss the trade in the Terminaotr UI.")
        return "\n".join(lines)

    def signal_completed(self, trade: Trade):
        """Remove strategy and its constituents from active signals set once GUI is done with it"""
        if trade.strategy_id in self.active_order_signals:
            self.active_order_signals.remove(trade.strategy_id)
        
        for ct in trade.constituent_trades:
            if ct.strategy_id in self.active_order_signals:
                self.active_order_signals.remove(ct.strategy_id)



    def _update_live_portfolio(self, broker_positions: List[Dict]):
        """Update live_combined_portfolio with actual broker positions"""
        self.live_combined_portfolio.positions = []
        for bp in broker_positions:
            self.live_combined_portfolio.positions.append(OptionLeg(
                symbol=bp['symbol'],
                strike=bp['strike'],
                side=bp['side'],
                quantity=bp['quantity'],
                price=bp['price'],
                entry_price=bp.get('avg_price', bp['price']), 
                bid_price=bp['bid'],
                ask_price=bp['ask'],
                current_day_pnl=bp.get('current_day_pnl', 0.0)
            ))
        # Recalculate margin
        self.live_combined_portfolio.max_margin = self.live_combined_portfolio.calculate_standard_margin()

    async def _check_reconciliation(self, snap: pd.DataFrame):
        """Compare simulated combined portfolio with live broker reality and suggest syncing trades"""
        if self.awaiting_broker_sync:
            self.logger.info("Skipping reconciliation: Awaiting broker sync of recent submission.")
            return
        # Issue 9: Always run reconciliation logic to track divergence
        # But only suggest/queue if trading enabled? 
        # Actually, Issue 9 says "Always run reconciliation logic regardless of trading_enabled flag."

        sim_pos = self.combined_portfolio.positions
        live_pos = self.live_combined_portfolio.positions
        
        sim_dict = { (int(round(p.strike)), p.side): p.quantity for p in sim_pos }
        live_dict = { (int(round(p.strike)), p.side): p.quantity for p in live_pos }
        
        all_keys = set(sim_dict.keys()) | set(live_dict.keys())
        needed_adjustments = []
        
        for k in all_keys:
            sq = sim_dict.get(k, 0)
            lq = live_dict.get(k, 0)
            diff = sq - lq
            if diff != 0:
                # Bypass broker restriction: do not close and open in one order.
                # If flipping (e.g., -1 to +1), only go to 0 first.
                if (lq < 0 and sq > 0) or (lq > 0 and sq < 0):
                    self.logger.info(f"Flipping detected for {k[0]}{k[1]}: Live {lq}, Sim {sq}. Clipping to 0 first.")
                    diff = -lq
                
                needed_adjustments.append((k[0], k[1], diff))
        
        if not needed_adjustments:
            self.logger.debug("Reconciliation passed: Sim matches Live")
            with self._data_lock:
                self.last_reconciliation_trade = None
            return

        # Use a lock to check queue state without blocking
        with self._order_lock:
            in_queue = any(t.purpose == TradePurpose.RECONCILIATION for t in list(self.order_queue.queue))

        self.logger.info(f"RECONCILIATION DISCREPANCY: Found {len(needed_adjustments)} legs mismatch. Generating Gap Sync Trade.")
        
        # Build Reconciliation Trade
        legs = []
        total_credit = 0.0
        
        for strike, side, qty in needed_adjustments:
            # Find in snap
            r = snap[(snap['strike_price'].round().astype(int) == strike) & (snap['side'] == side)]
            if r.empty:
                self.logger.error(f"Cannot sync leg {strike}{side}: Not found in option chain.")
                continue
                
            symbol = r['symbol'].iloc[0]
            price = r['mid_price'].iloc[0]
            delta = r['delta'].iloc[0]
            # Handle possible NaN in theta
            theta_val = r['theta'].iloc[0] if 'theta' in r.columns else 0.0
            theta = float(theta_val) if not pd.isna(theta_val) else 0.0
            
            # Determine instruction based on live position (lq)
            lq = live_dict.get((strike, side), 0)
            if qty > 0: # Needs to BUY
                inst = "BUY_TO_CLOSE" if lq < 0 else "BUY_TO_OPEN"
            else: # Needs to SELL
                inst = "SELL_TO_CLOSE" if lq > 0 else "SELL_TO_OPEN"
            
            leg = OptionLeg(
                symbol=symbol,
                strike=float(strike),
                side=side,
                quantity=int(qty),
                delta=delta,
                theta=theta,
                price=price
            )
            # Store instruction temporarily in a custom attribute for execute_net_trade
            leg.instruction = inst
            legs.append(leg)
            # credit = sum(-quantity * price) * 100
            total_credit += (-qty * price * 100)

        if legs:
            recon_trade = Trade(
                timestamp=datetime.now(),
                legs=legs,
                credit=total_credit,
                commission=len(legs) * self.config.get('commission_per_contract', 1.13),
                current_sum_delta=0.0,
                purpose=TradePurpose.RECONCILIATION,
                strategy_id="GAP_SYNC"
            )
            
            # ATTACH EXECUTION PLAN - This allows the GUI to see what would happen
            plan = self.create_execution_plan(recon_trade)
            recon_trade.execution_plan = plan
            
            with self._data_lock:
                self.last_reconciliation_trade = recon_trade

            # Issue 9: Only put in queue if trading is enabled and not already queued
            if self.trading_enabled:
                if not in_queue:
                    # Filter out if the plan is actually empty (already sync'd by manual order)
                    if not plan['to_submit'] and not plan['to_cancel']:
                        self.logger.info("Reconciliation GAP_SYNC generated, but broker already has matching orders. Suppressing pop.")
                        return

                    self.logger.info(f"Adding Reconciliation Trade to order queue: {len(legs)} legs")
                    self.order_queue.put(recon_trade)
                else:
                    self.logger.debug("Updated reconciliation trade available for GUI sync.")
            else:
                self.logger.warning(f"Sim divergence detected but trading is disabled. GAP_SYNC updated for {len(legs)} legs.")


    async def _run_historical_simulation(self, start_dt: datetime, end_dt: datetime, live_trades: List[Trade] = None, collect_history: bool = False) -> List[Dict]:
        """Run backtester logic on historical data. If live_trades provided, replays them into live_combined_portfolio."""
        history = []
        pending_live_trades = sorted(live_trades or [], key=lambda t: t.timestamp)
        sim_date_str = start_dt.date().isoformat()
        db_path = self.config['db_path']
        
        # If testing after market close, extend end_dt to include the whole day's data
        market_end_today = datetime.combine(start_dt.date(), time(15, 0))
        effective_end_dt = max(end_dt, market_end_today)

        from contextlib import closing
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                query = """
                    SELECT datetime, strike_price, side, bidprice, askprice, delta, theta, symbol 
                    FROM stock_options 
                    WHERE date(datetime) = ? AND root_symbol = '$SPX' AND dte = 0 
                    AND datetime BETWEEN ? AND ?
                    ORDER BY datetime
                """
                data = pd.read_sql_query(query, conn, params=(sim_date_str, start_dt.isoformat(), effective_end_dt.isoformat()))
        except Exception as e:
            self.logger.error(f"Database error during historical simulation: {e}")
            return

        if data.empty:
            self.logger.warning(f"No historical data available for simulation catch-up on {sim_date_str}")
            return
        
        self.logger.info(f"Loaded {len(data)} snapshots for {sim_date_str}. Processing...")

        data['datetime'] = pd.to_datetime(data['datetime'])
        data['mid_price'] = (data['bidprice'] + data['askprice']) / 2
        data = data.dropna(subset=['delta', 'bidprice', 'askprice'])
        
        groups = data.groupby('datetime')
        self._option_cache = {}
        
        start_time_obj = time(8, 30)
        end_time_obj = time(15, 0)

        for ts, snap in groups:
            snap = snap.reset_index(drop=True)
            t_time = ts.time()
            t_trades = defaultdict(list)
            
            # Apply any live trades that occurred at or before this timestamp
            while pending_live_trades and pending_live_trades[0].timestamp.replace(tzinfo=None) <= ts.replace(tzinfo=None):
                lt = pending_live_trades.pop(0)
                # Map leg prices to current snap for accuracy if possible
                for l in lt.legs:
                    lsnap = snap[(snap['strike_price'].round().astype(int) == int(round(l.strike))) & (snap['side'] == l.side)]
                    if not lsnap.empty:
                        l.price = lsnap['mid_price'].iloc[0]
                        l.delta = lsnap['delta'].iloc[0]
                self.live_combined_portfolio.add_trade(lt)

            spx = self.estimate_spx_price(snap)
            
            # Target decay for rebalance logic
            decay_c = calculate_delta_decay(ts, 'CALL', self.config['initial_sum_delta']/2, start_time_obj, end_time_obj)
            decay_p = calculate_delta_decay(ts, 'PUT', self.config['initial_sum_delta']/2, start_time_obj, end_time_obj)
            t_short = (decay_c + decay_p) / 2
            
            for sid, s in self.sub_strategies.items():
                if t_time < s.trade_start_time:
                    continue
                
                # Update positions
                for p in s.portfolio.positions:
                    r = snap[(snap['strike_price'].round().astype(int) == int(round(p.strike))) & (snap['side'] == p.side)]
                    if not r.empty:
                        p.delta = r['delta'].iloc[0]
                        p.price = r['mid_price'].iloc[0]
                        p.theta = float(r['theta'].iloc[0]) if not pd.isna(r['theta'].iloc[0]) else 0.0
                
                # Check for entry
                if not s.has_traded_today and t_time < end_time_obj:
                    trade = self._check_entry(s, snap, ts)
                    if trade:
                        s.portfolio.add_trade(trade)
                        t_trades[sid].append(trade)
                        s.has_traded_today = True
                
                # Check for rebalance/exit
                elif s.has_traded_today:
                    if t_time >= end_time_obj and s.portfolio.positions:
                        trade = self._create_exit_trade(s, snap, ts, spx)
                        if trade:
                            s.portfolio.add_trade(trade)
                            t_trades[sid].append(trade)
                    elif t_time < end_time_obj:
                        trades = self._check_rebalance(s, snap, ts, t_short)
                        for tr in trades:
                            s.portfolio.add_trade(tr)
                            t_trades[sid].append(tr)

            if t_trades:
                for mt in self.net_trades(t_trades):
                    self.combined_portfolio.add_trade(mt)

            # Update pricing for the aggregated portfolios to ensure accurate PnL/Deltas
            for port in [self.combined_portfolio, self.live_combined_portfolio]:
                for p in port.positions:
                    r = snap[(snap['strike_price'].round().astype(int) == int(round(p.strike))) & (snap['side'] == p.side)]
                    if not r.empty:
                        p.delta = r['delta'].iloc[0]
                        p.price = r['mid_price'].iloc[0]
                        p.theta = float(r['theta'].iloc[0]) if not pd.isna(r['theta'].iloc[0]) else 0.0

            if collect_history:
                sim_d = self.combined_portfolio.get_all_deltas(snap)
                live_d = self.live_combined_portfolio.get_all_deltas(snap)
                
                sim_mv = sum(l.price * l.quantity * 100 for l in self.combined_portfolio.positions)
                live_mv = sum(l.price * l.quantity * 100 for l in self.live_combined_portfolio.positions)
                
                history.append({
                    'timestamp': ts,
                    'spx': spx,
                    'sim_sc_strike': float(self.combined_portfolio.short_call_strike) if self.combined_portfolio.short_call_strike else None,
                    'sim_sp_strike': float(self.combined_portfolio.short_put_strike) if self.combined_portfolio.short_put_strike else None,
                    'live_sc_strike': float(self.live_combined_portfolio.short_call_strike) if self.live_combined_portfolio.short_call_strike else None,
                    'live_sp_strike': float(self.live_combined_portfolio.short_put_strike) if self.live_combined_portfolio.short_put_strike else None,
                    'sim_sc_delta': sim_d['abs_short_call_delta'],
                    'sim_sp_delta': sim_d['abs_short_put_delta'],
                    'live_sc_delta': live_d['abs_short_call_delta'],
                    'live_sp_delta': live_d['abs_short_put_delta'],
                    'sim_pnl': self.combined_portfolio.cash + sim_mv,
                    'live_pnl': self.live_combined_portfolio.cash + live_mv
                })

        self.logger.info(f"Historical simulation complete. Combined Portfolio Trades: {len(self.combined_portfolio.trades)}")
        return history

    def estimate_spx_price(self, snap: pd.DataFrame) -> Optional[float]:
        c, p = snap[snap['side'] == 'CALL'], snap[snap['side'] == 'PUT']
        if c.empty or p.empty: return None
        dc, dp = c.dropna(subset=['delta']), p.dropna(subset=['delta'])
        if dc.empty or dp.empty: return None
        # Find closest to 0.5 delta
        ac = dc.iloc[(dc['delta'] - 0.5).abs().argsort()[:1]]
        ap = dp.iloc[(dp['delta'] + 0.5).abs().argsort()[:1]]
        if ac.empty or ap.empty: return None
        kc, kp, pc, pp = ac['strike_price'].iloc[0], ap['strike_price'].iloc[0], ac['mid_price'].iloc[0], ap['mid_price'].iloc[0]
        return (kc + (pc - pp) + kp + (pc - pp)) / 2

    def _find_option(self, snap: pd.DataFrame, target: float, side: str, timestamp: datetime, max_diff: float = None, short_strike: float = None):
        target = target if side == 'CALL' else -abs(target)
        cache_key = (timestamp, round(target, 4), side, max_diff, short_strike)
        if cache_key in self._option_cache: return self._option_cache[cache_key]
        
        opts = snap[(snap['side'] == side) & (snap['delta'].notna())]
        if opts.empty: return None
        
        if max_diff is not None and short_strike is not None:
            if side == 'CALL':
                opts = opts[opts['strike_price'] <= short_strike + max_diff]
            else:
                opts = opts[opts['strike_price'] >= short_strike - max_diff]
        
        if opts.empty: return None
        
        deltas = opts['delta'].values
        if side == 'CALL':
            if target <= deltas.max():
                mask = deltas >= target
                res = opts.iloc[np.where(mask)[0][np.argmin(np.abs(deltas[mask] - target))]]
            else: res = opts.iloc[np.argmin(np.abs(deltas - target))]
        else:
            if target >= deltas.min():
                mask = deltas <= target
                res = opts.iloc[np.where(mask)[0][np.argmin(np.abs(deltas[mask] - target))]]
            else: res = opts.iloc[np.argmin(np.abs(deltas - target))]
        
        # Issue 11: Keep cache bounded (LRU behavior with popitem(last=False))
        if len(self._option_cache) > 2000:
            while len(self._option_cache) > 1000:
                self._option_cache.popitem(last=False)
                
        self._option_cache[cache_key] = res
        return res

    def _check_entry(self, s: SubStrategy, snap: pd.DataFrame, ts: datetime) -> Optional[Trade]:
        max_diff = self.config['max_spread_diff']
        start_time_obj = time(8, 30)
        end_time_obj = time(15, 0)
        
        sc_t = calculate_delta_decay(ts, 'CALL', s.init_s_delta, start_time_obj, end_time_obj)
        sp_t = calculate_delta_decay(ts, 'PUT', s.init_s_delta, start_time_obj, end_time_obj)
        lc_t = calculate_delta_decay(ts, 'CALL', s.init_l_delta, start_time_obj, end_time_obj)
        lp_t = calculate_delta_decay(ts, 'PUT', s.init_l_delta, start_time_obj, end_time_obj)
        
        st, lt = (sc_t + sp_t)/2, (lc_t + lp_t)/2
        
        sc = self._find_option(snap, st, 'CALL', ts)
        sp = self._find_option(snap, -st, 'PUT', ts)
        if sc is not None and sp is not None:
            lc = self._find_option(snap, lt, 'CALL', ts, max_diff=max_diff, short_strike=sc['strike_price'])
            lp = self._find_option(snap, -lt, 'PUT', ts, max_diff=max_diff, short_strike=sp['strike_price'])
            
            if all([sc is not None, lc is not None, sp is not None, lp is not None]):
                legs = [
                    OptionLeg(sc['symbol'], sc['strike_price'], 'CALL', -1, sc['delta'], sc['theta'], price=sc['mid_price'], target_delta=st),
                    OptionLeg(lc['symbol'], lc['strike_price'], 'CALL', 1, lc['delta'], lc['theta'], price=lc['mid_price'], target_delta=lt),
                    OptionLeg(sp['symbol'], sp['strike_price'], 'PUT', -1, sp['delta'], sp['theta'], price=sp['mid_price'], target_delta=-st),
                    OptionLeg(lp['symbol'], lp['strike_price'], 'PUT', 1, lp['delta'], lp['theta'], price=lp['mid_price'], target_delta=-lt)
                ]
                credit = sum(-l.quantity * l.price for l in legs) * 100
                comm = self.config.get('commission_per_contract', 1.13) * len(legs)
                return Trade(ts, legs, credit, comm, (sc_t + sp_t), TradePurpose.IRON_CONDOR, s.sid)
        return None

    def _check_rebalance(self, s: SubStrategy, snap: pd.DataFrame, ts: datetime, t_short: float) -> List[Trade]:
        trades = []
        deltas = s.portfolio.get_all_deltas(snap)
        start_time_obj = time(8, 30)
        end_time_obj = time(15, 0)
        
        decay_cl = calculate_delta_decay(ts, 'CALL', s.init_l_delta, start_time_obj, end_time_obj)
        decay_pl = calculate_delta_decay(ts, 'PUT', s.init_l_delta, start_time_obj, end_time_obj)
        t_long = (decay_cl + decay_pl) / 2
        
        max_diff = self.config['max_spread_diff']
        
        for side in ['CALL', 'PUT']:
            sn_needs = abs(deltas[f'abs_short_{side.lower()}_delta'] - t_short) > self.config['rebalance_threshold']
            ln_needs = abs(deltas[f'abs_long_{side.lower()}_delta'] - t_long) > self.config['long_leg_rebalance_delta_threshold']
            
            # Width cap rebalance
            if not ln_needs:
                if side == 'CALL' and s.portfolio.short_call_strike and s.portfolio.long_call_strike:
                    if (s.portfolio.long_call_strike - s.portfolio.short_call_strike) > max_diff:
                        ln_needs = True
                elif side == 'PUT' and s.portfolio.short_put_strike and s.portfolio.long_put_strike:
                    if (s.portfolio.short_put_strike - s.portfolio.long_put_strike) > max_diff:
                        ln_needs = True
            
            on_side = [p for p in s.portfolio.positions if p.side == side]
            tr = None
            if not on_side and (sn_needs or ln_needs):
                # Only if currently empty on this side
                tr = self._create_new_spread_trade(s, snap, t_short, t_long, side, ts)
            elif sn_needs:
                tr = self._create_rebalance_trade(s, snap, t_short, side, ts, True, t_long=t_long)
            elif ln_needs:
                tr = self._create_rebalance_trade(s, snap, t_long, side, ts, False)
            
            if tr:
                trades.append(tr)
        return trades

    def _create_new_spread_trade(self, s: SubStrategy, snap: pd.DataFrame, st: float, lt: float, side: str, ts: datetime) -> Optional[Trade]:
        max_diff = self.config['max_spread_diff']
        opt_s = self._find_option(snap, st, side, ts)
        if opt_s is None: return None
        opt_l = self._find_option(snap, lt, side, ts, max_diff=max_diff, short_strike=opt_s['strike_price'])
        if opt_l is None: return None
        
        legs = [
            OptionLeg(opt_s['symbol'], opt_s['strike_price'], side, -1, opt_s['delta'], opt_s.get('theta', 0), price=opt_s['mid_price'], target_delta=st if side == 'CALL' else -st),
            OptionLeg(opt_l['symbol'], opt_l['strike_price'], side, 1, opt_l['delta'], opt_l.get('theta', 0), price=opt_l['mid_price'], target_delta=lt if side == 'CALL' else -lt)
        ]
        credit = sum(-lg.quantity*lg.price for lg in legs)*100
        return Trade(ts, legs, credit, 0, st + (lt if side == 'PUT' else 0), TradePurpose.REBALANCE_NEW, s.sid) # st is approx half sum delta

    def _create_rebalance_trade(self, s: SubStrategy, snap: pd.DataFrame, target: float, side: str, ts: datetime, is_short: bool, t_long: float = None) -> Optional[Trade]:
        ex_legs = [p for p in s.portfolio.positions if p.side == side and ((is_short and p.quantity < 0) or (not is_short and p.quantity > 0))]
        if not ex_legs: return None
        ex = ex_legs[0]
        
        max_diff = self.config['max_spread_diff']
        
        if is_short:
            new_s = self._find_option(snap, target, side, ts)
            if new_s is None: return None
            
            legs = [
                OptionLeg(ex.symbol, ex.strike, ex.side, -ex.quantity, ex.delta, ex.theta, price=ex.price, target_delta=ex.target_delta),
                OptionLeg(new_s['symbol'], new_s['strike_price'], side, -1, new_s['delta'], new_s.get('theta',0), price=new_s['mid_price'], target_delta=target if side == 'CALL' else -target)
            ]
            
            # Width cap check
            l_legs = [p for p in s.portfolio.positions if p.side == side and p.quantity > 0]
            if l_legs:
                l_ex = l_legs[0]
                width = abs(new_s['strike_price'] - l_ex.strike)
                if width > max_diff and t_long is not None:
                    new_l = self._find_option(snap, t_long, side, ts, max_diff=max_diff, short_strike=new_s['strike_price'])
                    if new_l is not None:
                        legs.append(OptionLeg(l_ex.symbol, l_ex.strike, l_ex.side, -l_ex.quantity, l_ex.delta, l_ex.theta, price=l_ex.price, target_delta=l_ex.target_delta))
                        legs.append(OptionLeg(new_l['symbol'], new_l['strike_price'], side, 1, new_l['delta'], new_l.get('theta',0), price=new_l['mid_price'], target_delta=t_long if side == 'CALL' else -t_long))
            
            credit = sum(-l.quantity * l.price for l in legs) * 100
            comm = self.config.get('commission_per_contract', 1.13) * len(legs)
            if abs(credit) <= self.config['min_credit']*100: return None
            return Trade(ts, legs, credit, comm, target * 2, TradePurpose.REBALANCE_SHORT, s.sid)
        else:
            ss_legs = [p for p in s.portfolio.positions if p.side == side and p.quantity < 0]
            short_strike = ss_legs[0].strike if ss_legs else None
            new_l = self._find_option(snap, target, side, ts, max_diff=max_diff, short_strike=short_strike)
            if new_l is None: return None
            
            legs = [
                OptionLeg(ex.symbol, ex.strike, ex.side, -ex.quantity, ex.delta, ex.theta, price=ex.price, target_delta=ex.target_delta),
                OptionLeg(new_l['symbol'], new_l['strike_price'], side, 1, new_l['delta'], new_l.get('theta',0), price=new_l['mid_price'], target_delta=target if side == 'CALL' else -target)
            ]
            credit = sum(-l.quantity * l.price for l in legs) * 100
            comm = self.config.get('commission_per_contract', 1.13) * len(legs)
            return Trade(ts, legs, credit, comm, target * 2, TradePurpose.REBALANCE_LONG, s.sid)

    def _create_exit_trade(self, s: SubStrategy, snap: pd.DataFrame, ts: datetime, spx: Optional[float]) -> Optional[Trade]:
        closing_legs = []
        for p in s.portfolio.positions:
            is_itm = (p.side == 'CALL' and spx and spx > p.strike) or (p.side == 'PUT' and spx and spx < p.strike)
            exit_price = 0.0
            if is_itm:
                r = snap[(snap['strike_price'].round().astype(int) == int(round(p.strike))) & (snap['side'] == p.side)]
                exit_price = r['mid_price'].iloc[0] if not r.empty else p.price
            closing_legs.append(OptionLeg(p.symbol, p.strike, p.side, -p.quantity, p.delta, p.theta, price=exit_price))
        
        if not closing_legs: return None
        credit = sum(-l.quantity*l.price for l in closing_legs)*100
        return Trade(ts, closing_legs, credit, 0, 0, TradePurpose.EXIT, s.sid)

    def _unroll_legs(self, legs: List[OptionLeg]) -> List[OptionLeg]:
        """Unroll legs and sort deterministically for repeatable chunking."""
        unrolled = []
        # Sort incoming legs by side then strike to ensure deterministic unrolling
        sorted_input = sorted(legs, key=lambda x: (0 if x.side == 'PUT' else 1, x.strike))
        
        for leg in sorted_input:
            qty = int(abs(leg.quantity))
            direction = 1 if leg.quantity > 0 else -1
            for _ in range(qty):
                unit_leg = OptionLeg(
                    symbol=leg.symbol, strike=leg.strike, side=leg.side,
                    quantity=direction, delta=leg.delta, theta=leg.theta,
                    price=leg.price, entry_price=leg.price,
                    bid_price=leg.bid_price, ask_price=leg.ask_price
                )
                if hasattr(leg, 'instruction'):
                    unit_leg.instruction = leg.instruction
                unrolled.append(unit_leg)
        return unrolled

    def _roll_legs(self, legs: List[OptionLeg]) -> List[OptionLeg]:
        """Aggregate unrolled unit legs back into grouped legs by symbol."""
        agg = {}
        for l in legs:
            inst = getattr(l, 'instruction', None)
            k = (l.symbol, inst)
            if k not in agg:
                agg[k] = OptionLeg(
                    symbol=l.symbol, strike=l.strike, side=l.side,
                    quantity=l.quantity, delta=l.delta, theta=l.theta,
                    price=l.price, entry_price=l.entry_price,
                    bid_price=l.bid_price, ask_price=l.ask_price
                )
                if inst: agg[k].instruction = inst
            else:
                agg[k].quantity += l.quantity
        return list(agg.values())

    def _get_smart_chunks(self, legs: List[OptionLeg]) -> List[List[OptionLeg]]:
        """DETERMINISTIC CHUNKING: Always form the same ICs for the same set of legs."""
        unrolled = self._unroll_legs(legs)
        if not unrolled: return []
        if len(unrolled) <= 4: return [unrolled]

        ics = []
        remaining = list(unrolled) # Already sorted by Side/Strike from _unroll_legs
        
        # Greedily pull out Iron Condors in a deterministic way
        while len(remaining) >= 4:
            # Find the first available Short Put (usually the foundation of our structure)
            sp = next((l for l in remaining if l.side == 'PUT' and l.quantity < 0), None)
            if not sp: break
            
            # Find partners to fulfill the IC structure: LP, SC, LC
            lp = next((l for l in remaining if l.side == 'PUT' and l.quantity > 0), None)
            sc = next((l for l in remaining if l.side == 'CALL' and l.quantity < 0), None)
            lc = next((l for l in remaining if l.side == 'CALL' and l.quantity > 0), None)
            
            if all([sp, lp, sc, lc]):
                combo = [sp, lp, sc, lc]
                ics.append(combo)
                # Remove these specific instances
                remaining.remove(sp); remaining.remove(lp);
                remaining.remove(sc); remaining.remove(lc);
            else:
                break # Cannot form full IC anymore
        
        # Any remaining legs are grouped into 4-leg chunks
        chunks = ics
        for i in range(0, len(remaining), 4):
            chunks.append(remaining[i:i+4])
            
        return chunks

    def _get_legs_signature(self, legs: List[OptionLeg]) -> Set[Tuple[str, int]]:
        """Return a set of (symbol, quantity) for content-based matching."""
        rolled = self._roll_legs(legs)
        return {(l.symbol.strip(), l.quantity) for l in rolled if l.quantity != 0}

    def _get_broker_order_signature(self, wo: Dict) -> Set[Tuple[str, int]]:
        """Extract (symbol, quantity) signature from a Schwab broker order."""
        sig = set()
        for leg in wo.get('orderLegCollection', []):
            symbol = leg.get('instrument', {}).get('symbol', '').strip()
            qty = int(leg.get('quantity', 0))
            inst = leg.get('instruction', '')
            if 'SELL' in inst: qty = -qty
            sig.add((symbol, qty))
        return sig

    def create_execution_plan(self, trade: Trade) -> Dict:
        """Create a plan of what to keep, cancel, and submit based on broker state."""
        chunks = self._get_smart_chunks(trade.legs)
        to_submit = []
        matched_broker_orders = []
        matched_ids = set()
        
        # 1. Match Chunks
        current_working = list(self.working_orders)
        for chunk in chunks:
            match_idx = self._find_sig_match(chunk, current_working)
            if match_idx is not None:
                wo = current_working.pop(match_idx)
                matched_broker_orders.append(wo)
                matched_ids.add(str(wo.get('orderId')))
            else:
                to_submit.append(chunk)

        # 2. Find orders to CANCEL
        to_cancel = []
        for wo in self.working_orders:
            wid = str(wo.get('orderId'))
            if wid in matched_ids: continue
            if not self._is_spx_0dte_order(wo): continue
            
            # If it belongs to strategy or is a recon overlap, and not matched -> kill it.
            if self.order_to_strategy.get(wid) == trade.strategy_id or trade.purpose == TradePurpose.RECONCILIATION:
                to_cancel.append(wo)

        return {
            'to_keep': matched_broker_orders,
            'to_submit': to_submit,
            'to_cancel': to_cancel
        }

    def _find_sig_match(self, chunk: List[OptionLeg], working_orders: List[Dict]) -> Optional[int]:
        """Find index of an order in working_orders that exactly matches chunk signature."""
        chunk_sig = self._get_legs_signature(chunk)
        for i, wo in enumerate(working_orders):
            if self._get_broker_order_signature(wo) == chunk_sig:
                return i
        return None


    def _is_spx_0dte_order(self, wo: Dict) -> bool:
        """Check if order is SPX 0DTE."""
        legs = wo.get('orderLegCollection', [])
        if not legs: return False
        for l in legs:
            instr = l.get('instrument', {})
            if instr.get('assetType') == 'OPTION' and (instr.get('underlyingSymbol') == '$SPX' or instr.get('symbol').startswith('SPX')):
                # Simple check for today's date in symbol or common 0DTE patterns
                return True
        return False

    def _check_against_working_orders(self, trade: Trade) -> Tuple[str, List[Dict]]:
        """CONTENT-BASED MATCHING (Cases 1-6)"""
        if not self.working_orders:
            return "NONE", []

        trade_sig = self._get_legs_signature(trade.legs)
        if not trade_sig: return "NONE", []

        matching_orders = []
        is_exact = False
        is_subset = False
        is_modification = False

        for wo in self.working_orders:
            if not self._is_spx_0dte_order(wo):
                continue

            wo_sig = self._get_broker_order_signature(wo)
            if not wo_sig: continue

            # Case 1-4: Exact Match
            if wo_sig == trade_sig:
                is_exact = True
                matching_orders.append(wo)
                break

            # Case 6: Subset (Working order is part of the new trade)
            if wo_sig.issubset(trade_sig):
                is_subset = True
                matching_orders.append(wo)
                continue

            # Case 5/6 Modification: If same strategy or general conflict
            wid = str(wo.get('orderId', ''))
            if self.order_to_strategy.get(wid) == trade.strategy_id:
                is_modification = True
                matching_orders.append(wo)

        if is_exact: return "EXACT", matching_orders
        if is_subset: return "SUBSET", matching_orders
        if is_modification: return "MODIFICATION", matching_orders
        
        return "NONE", []

    def net_trades(self, trade_map: Dict[str, List[Trade]]) -> List[Trade]:
        if not trade_map: return []

        agg = defaultdict(lambda: {'q': 0, 'm': None})
        total_credit = 0.0
        all_constituents = []

        # 1. Collect all trades and sum their STATIC credits
        for sid, trades in trade_map.items():
            for t in trades:
                total_credit += t.credit
                all_constituents.append(t)
                for l in t.legs:
                    k = (l.symbol, int(round(l.strike)), l.side)
                    agg[k]['q'] += l.quantity
                    agg[k]['m'] = l

        # 2. Net the legs
        net_legs = [
            OptionLeg(v['m'].symbol, v['m'].strike, v['m'].side, v['q'],
                      v['m'].delta, v['m'].theta, price=v['m'].price)
            for v in agg.values() if v['q'] != 0
        ]

        # 3. Commission based on NET contracts (broker reality)
        comm = self.config.get('commission_per_contract', 1.13) * sum(abs(l.quantity) for l in net_legs)

        # 4. If truly nothing happened, skip
        if not net_legs and total_credit == 0:
            return []

        # 5. Take metadata from first trade
        first_strat_id = list(trade_map.keys())[0]
        first_t = trade_map[first_strat_id][0]

        netted_trade = Trade(
            timestamp=first_t.timestamp,
            legs=net_legs,
            credit=total_credit,
            commission=comm,
            current_sum_delta=first_t.current_sum_delta,
            purpose=first_t.purpose,
            strategy_id="GAP_SYNC",
            constituent_trades=all_constituents
        )
        return [netted_trade]
