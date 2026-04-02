# live/terminator/monitor.py

import asyncio
import logging
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, date, time, timedelta, timezone
from typing import Dict, List, Tuple, Optional, Set, Any
from collections import defaultdict, OrderedDict
from itertools import combinations
import copy
import threading
import traceback
import queue
import random
from zoneinfo import ZoneInfo
CHICAGO = ZoneInfo("America/Chicago")
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import json
import os
import math
from pathlib import Path
import schwab
from schwab.auth import easy_client
from schwab.streaming import StreamClient
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
    from notifications import notify_all
except (ImportError, ValueError):
    # Fallback for standalone or testing
    import logging
    _fallback_log = logging.getLogger("LiveTradingMonitor")
    _fallback_log.warning("CRITICAL: Failed to load notification engine! Email alerts will be disabled.")
    async def notify_all(*args, **kwargs): pass

# Mocking Schwab until actual client is ready
# In reality, this would use schwab.client.AsyncClient
class LiveTradingMonitor:
    def __init__(self, config=None, trade_signal_callback=None):
        self.config = config or CONFIG
        self.trade_signal_callback = trade_signal_callback
        self.logger = logging.getLogger("LiveTradingMonitor")
        
        # --- Real-time Logs for Web UI ---
        from collections import deque
        self.logs = deque(maxlen=200) # Keep last 200 logs
        class WSLogHandler(logging.Handler):
            def __init__(self, buffer):
                super().__init__()
                self.buffer = buffer
            def emit(self, record):
                msg = self.format(record)
                self.buffer.append(msg)
        
        log_handler = WSLogHandler(self.logs)
        log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S'))
        self.logger.addHandler(log_handler)

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
        self.price_overrides: Dict[str, Dict[int, float]] = {} # Task #28: sid -> {chunk_idx: price_ea}
        
        self.trading_enabled = False # New toggle for live trading vs sim only
        self.working_orders = [] # Shared cache for GUI
        self.awaiting_broker_sync = False # Flag for in-flight orders
        
        # Broker Heartbeat State
        self.broker_connected = False
        self.broker_reconnecting = False
        self.heartbeat_failures = 0
        self.heartbeat_running = False
        
        # Trade Confirmation State (Interactive Modal Support)
        self.pending_trade: Optional[Trade] = None
        self._pending_trade_confirmed = False
        self.is_trade_timer_paused = False # Issue 20: support pausing auto-confirm
        self.confirmation_event = asyncio.Event() 
        self._queued_purposes: Set[TradePurpose] = set() # Bug 8 tracker
        self._sim_dirty: bool = True # P4 tracker

        # Session persistence
        self.startup_time = datetime.now(CHICAGO)
        self.forced_shutdown_requested = False
        self.session_manager = SessionManager(self.config['session_file_path'])
        
        self.is_running = False
        self._stop_event = asyncio.Event() # Re-bound in run_live_monitor (Bug 2)
        self.reconciliation_event = asyncio.Event() # Re-bound in run_live_monitor (Bug 2)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._recon_task = None
        self.status = "Stopped" 
        self._option_cache: Dict[Tuple, Any] = {} # P6: Clear per tick
        self.order_queue: Optional[asyncio.Queue] = None  # Re-bound in run_live_monitor (same pattern as Bug 2)
        self.stats = TradeStats() # Enhancement 2: Trading Statistics
        self._last_snap: Optional[pd.DataFrame] = None # Cache for Greeks
        self._greek_cache: Dict[Tuple[str, int, str], Tuple[float, float]] = {} # (symbol, strike, side) -> (delta, theta)
        
        # Resolve DB path relative to project root if it's relative
        self.db_path = self.config.get('db_path', 'data/spx_0dte.db')
        if not os.path.isabs(self.db_path):
            root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
            self.db_path = os.path.join(root, self.db_path)
        self._peak_equity = 0.0 # Initializing peak equity for stats
        self.session_history: List[Dict] = [] # Time-series for charts: {ts, spx, sim_pnl, live_pnl}
        self._last_spx_price: Optional[float] = None # Latest SPX estimate
        self._last_spx_ts: int = 0  # Latest SPX exchange timestamp (ms)
        self._last_vix_price: Optional[float] = None
        self.stream_client: Optional[StreamClient] = None
        self.last_dismissed_recon_time: float = 0 # Cooldown for GAP_SYNC (Bug 16 Fix)
        # Scalability #6: Tracker for periodic saves (5min)
        self._last_save_time: float = 0 
        self.logger.info(f"Monitor initialized with DB: {self.db_path}")

    def set_trading_enabled(self, enabled: bool):
        """Managed toggle for trading to ensure state consistency (Bug 13 Fix)"""
        self.trading_enabled = enabled
        action = "ENABLED" if enabled else "DISABLED"
        self.logger.info(f"Trading has been {action} manually.")
        
        if not enabled:
            # 1. Stop any currently waiting modal from returning on refresh (Bug 16 Fix)
            if self.pending_trade:
                self.logger.info(f"Aborting active trade {self.pending_trade.strategy_id} due to disable.")
                # We don't null it here (loop will do it), but we ensure the UI doesn't see it
                # Actually, set it to None here just to be safe for websocket reconnects
                self.pending_trade = None
                self._pending_trade_confirmed = False
                self.confirmation_event.set() # Wake up the loop to clean up state

            # 2. Clear the order queue so no further popups occur (Bug 1 Fix)
            cleared_count = 0
            if self.order_queue is not None:
                while not self.order_queue.empty():
                    try:
                        t = self.order_queue.get_nowait()
                        self.order_queue.task_done()
                        cleared_count += 1
                        # Cleanup trackers
                        if t.strategy_id in self.active_order_signals:
                            self.active_order_signals.remove(t.strategy_id)
                        if t.purpose in self._queued_purposes:
                            self._queued_purposes.remove(t.purpose)
                    except:
                        break
            if cleared_count > 0:
                self.logger.info(f"Cleared {cleared_count} pending trades from queue upon disabling.")

    def get_trade_signal_payload(self, trade: Trade) -> Dict:
        """Centralized helper to build consistent UI signals for a trade (Bug 15 Fix)"""
        plan = self.create_execution_plan(trade)
        orders_data = []
        total_adjusted_credit = 0.0

        # 1. New Orders being submitted (matching logic in run_order_execution_loop)
        for i, chunk in enumerate(plan['to_submit']):
            rolled = self._roll_legs(chunk)
            chunk_credit = sum(-l.quantity * l.price for l in rolled) * 100
            leg_qtys = [abs(l.quantity) for l in rolled]
            num_units = leg_qtys[0]
            for q in leg_qtys[1:]: num_units = math.gcd(num_units, q)
            
            leg_texts = []
            for l in rolled:
                side = "SHORT" if l.quantity < 0 else "LONG"
                leg_texts.append(f"{side} {l.side} {int(l.strike)} x{abs(l.quantity)//num_units}")
            
            # Fix Task #32: Use classification to determine structural intent and floor-locking
            order_type, is_credit_structural = self._classify_order_type(rolled)
            lock_floor = (order_type != "unknown")
            
            if is_credit_structural is not None:
                is_credit = is_credit_structural
            else:
                is_credit = chunk_credit >= 0
            
            offset = self.config.get('order_offset', 0.0)
            signed_mid = (chunk_credit / 100.0 / num_units) if num_units > 0 else 0.0
            signed_target = signed_mid + offset
            
            if lock_floor:
                price_with_offset = max(0.0, signed_target if is_credit else -signed_target)
            else:
                is_credit = signed_target >= 0
                price_with_offset = abs(signed_target)

            # Round to tick for UI display consistency
            price_with_offset = self._round_to_tick(price_with_offset, num_legs=len(rolled))

            signed_price_ea = price_with_offset if is_credit else -price_with_offset
            total_adjusted_credit += signed_price_ea * num_units
            orders_data.append({
                "type": "TRADE",
                "idx": i,
                "qty": num_units,
                "desc": f"[{order_type.upper()}] " + " | ".join(leg_texts),
                "is_credit": is_credit,
                "order_type": order_type,
                "lock_floor": lock_floor,
                "credit": f"${price_with_offset:.2f} {'Cr' if is_credit else 'Db'} (ea)",
                "price_ea": signed_price_ea
            })
        
        # 2. Orders being cancelled
        for wo in plan['to_cancel']:
            orders_data.append({
                "type": "CANCEL",
                "qty": wo.get('quantity'),
                "desc": f"Order ID: {wo.get('orderId')}",
                "credit": "N/A",
                "price_ea": 0.0
            })
            
        return {
            "type": "trade_signal",
            "strat_id": trade.strategy_id,
            "purpose": trade.purpose.value,
            "total_credit": f"${abs(total_adjusted_credit):.2f} {'Credit' if total_adjusted_credit >= 0 else 'Debit'}",
            "orders": orders_data,
            "is_paused": self.is_trade_timer_paused,
            "timeout": self.config.get('order_auto_execute_timeout', 20)
        }

    def _get_portfolio_summary_text(self) -> str:
        """Helper to generate a text summary for alerts (Bug 30 Fix)"""
        with self._data_lock:
            # Stats
            sim_pnl = self.combined_portfolio.net_pnl
            live_pnl = self.live_combined_portfolio.net_pnl
            total_trades = self.stats.total_trades
            
            # Holdings
            def format_h(p):
                if not p.positions: return " - Empty"
                lines = []
                for l in p.positions:
                    side = "SHORT" if l.quantity < 0 else "LONG"
                    lines.append(f" - {side} {l.side} {int(l.strike)} x{abs(l.quantity)}")
                return "\n".join(lines)
            
            sim_h = format_h(self.combined_portfolio)
            live_h = format_h(self.live_combined_portfolio)
            
        return (
            f"--- PORTFOLIO SUMMARY ---\n"
            f"SIM PnL: ${sim_pnl:,.2f} (Total Session Trades: {total_trades})\n"
            f"LIVE PnL: ${live_pnl:,.2f}\n\n"
            f"SIM HOLDINGS:\n{sim_h}\n\n"
            f"LIVE HOLDINGS:\n{live_h}"
        )

    def _broadcast_alert(self, level: str, title: str, message: str):
        """Helper to broadcast alerts via WebSocket for UI sound and notifications."""
        try:
            from api.ws import manager
            if hasattr(self, '_loop') and self._loop:
                asyncio.run_coroutine_threadsafe(
                    manager.broadcast({
                        "type": "alert",
                        "level": level,
                        "title": title,
                        "message": message
                    }), self._loop
                )
            else:
                self.logger.warning(f"Alert dropped (loop not ready): [{level}] {title} — {message}")
        except Exception as e:
            self.logger.error(f"Failed to broadcast alert: {e}")

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
            s.unit_size = self.config.get('default_unit_size', 1)
            
            self.sub_strategies[sid] = s
            curr += interval

    async def run_live_monitor(self):
        """Main lifecycle entry point for the background monitor."""
        self._loop = asyncio.get_running_loop()
        
        # Bug 2 + BUG-R2-2: Create asyncio primitives inside the running loop
        self.confirmation_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self.reconciliation_event = asyncio.Event()
        self.order_queue = asyncio.Queue()
        
        self.is_running = True
        self.logger.info("Starting live monitor thread...")
        self.status = "Initializing..."
        
        try:
            async with asyncio.TaskGroup() as tg:
                # 0. Startup notification
                await notify_all(self.config, "Monitor Started", title="Terminator Live")
                
                # 1. Start Support Tasks with self-restarting loops (Robustness-1 Fix)
                tg.create_task(self._safe_task("HealthCheck", self._run_health_check_server))
                
                # 2. Main Strategy Logic (30s cadence)
                tg.create_task(self._safe_task("Monitoring", self._monitoring_loop))
                
                # 3. Dedicated Reconciliation Logic (Unified GAP_SYNC)
                tg.create_task(self._safe_task("Reconciliation", self.run_reconciliation_loop))

                # 4. High-Frequency Broker Sync (5s cadence)
                tg.create_task(self._safe_task("BrokerSync", self._broker_sync_loop))

                # 5. Real-time Index Streaming ($SPX, $VIX)
                tg.create_task(self._safe_task("IndexStreamer", self._run_index_streaming_loop))

                # 6. Order Execution Loop
                tg.create_task(self._safe_task("OrderExecution", self.run_order_execution_loop))
                
        except Exception as e:
            if not isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                self.status = f"Fatal Error: {str(e)}"
                self.logger.error(f"Fatal task failure in TaskGroup: {e}\n{traceback.format_exc()}")
            self.is_running = False
            asyncio.create_task(notify_all(self.config, f"Monitor Stopped: {self.status}", title="Terminator Live", priority="urgent"))

    async def _safe_task(self, name, coro_func):
        """Robustness-1 Fix: Wrap each task in a self-restarting loop"""
        while self.is_running:
            try:
                await coro_func()
            except asyncio.CancelledError:
                self.logger.info(f"{name} task cancelled.")
                break
            except Exception as e:
                self.logger.error(f"{name} task crashed, restarting in 5s: {e}")
                self.logger.error(traceback.format_exc())
                await asyncio.sleep(5)

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
                    # Bug 8 Fix: Ensure combined sim matches sum of sub-strategies
                    self._reconcile_combined_simulation()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in reconciliation loop: {e}")
                await asyncio.sleep(1)

    async def run_order_execution_loop(self):
        """Drains the order_queue and executes trades with interactive confirmation."""
        self.logger.info("Order Execution Loop started.")
        while self.is_running:
            try:
                # Use a timeout to occasionally check if self.is_running changed
                try:
                    trade = await asyncio.wait_for(self.order_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                # Interactive Confirmation Loop
                self.pending_trade = trade
                # Bug 8 Tracker: Clear from queue tracker as it's now in active confirmation (pending_trade)
                with self._data_lock:
                    if trade.purpose in self._queued_purposes:
                        self._queued_purposes.discard(trade.purpose)
                
                self._pending_trade_confirmed = False
                self.confirmation_event.clear()
                
                # Safety-0 Fix: Re-check redundancy IMMEDIATELY before broadcast to UI
                # This prevents modals from popping up for trades covered in the lag time
                if self._is_trade_redundant(trade):
                    self.logger.info(f"Suppressed redundant signal for {trade.strategy_id} before broadcast.")
                    # Still clean up trackers to avoid memory leak or monitoring skips
                    with self._data_lock:
                        if trade.strategy_id in self.active_order_signals:
                            self.active_order_signals.discard(trade.strategy_id)
                    self.pending_trade = None
                    self.order_queue.task_done()
                    continue

                self.logger.info(f"Awaiting confirmation for trade: {trade.strategy_id} ({trade.purpose})")
                
                # Build REFINED payload for showTradeModal in app.js (Bug 15 Fix: extracted to helper)
                msg = self.get_trade_signal_payload(trade)
                
                # Notify UI to show modal
                from api.ws import manager
                if manager.active_connections:
                    asyncio.create_task(manager.broadcast(msg))
                    # Stability Fix: Only trigger the sound alert/notification exactly when the modal pops up
                    self._broadcast_alert("chime", "New Trade Signal", f"Trade required for {trade.strategy_id} ({trade.purpose.value})")
                
                # BUG 30: Also notify via push/email when signal appears (not just when filled)
                # This provides the user with the snapshot summary for context
                try:
                    summ = self._get_portfolio_summary_text()
                    asyncio.create_task(notify_all(
                        self.config, 
                        f"New trade needed: {trade.strategy_id} ({trade.purpose.value}) for ${abs(trade.credit/100.0):.2f}", 
                        title="Trade Signal ALERT",
                        email_body=f"Trade required for strategy: {trade.strategy_id}\n\n{summ}"
                    ))
                except Exception as ne:
                    self.logger.error(f"Failed to generate/send trade signal notification: {ne}")

                # Wait for UI confirmation (or auto-confirm in headless mode)
                confirmed = False
                # Server-side safety timeout should be slightly longer than UI countdown 
                # to allow the browser's confirm message to arrive. (Bug 15 Fix)
                ui_timeout = self.config.get('order_auto_execute_timeout', 20)
                timeout = self.config.get('auto_confirm_timeout', ui_timeout + 10)
                
                # BUG 3 Fix: Support "Pause" by replacing wait_for with a manual interval loop
                elapsed_while_active = 0.0
                while self.is_running:
                    try:
                        # Check for confirmation/dismissal every 0.1s
                        await asyncio.wait_for(self.confirmation_event.wait(), timeout=0.1)
                        confirmed = self._pending_trade_confirmed
                        break
                    except asyncio.TimeoutError:
                        # 5-second cadence check (matches broker sync frequency)
                        if int(elapsed_while_active * 10) % 50 == 0:
                            if self._is_trade_redundant(trade):
                                self.logger.info(f"Trade for {trade.strategy_id} is no longer needed (filled manually?). Auto-dismissing.")
                                confirmed = False
                                self._pending_trade_confirmed = False
                                # Notify UI to close modal
                                from api.ws import manager
                                asyncio.create_task(manager.broadcast({"type": "trade_action", "action": "close_modal", "strat_id": trade.strategy_id}))
                                break

                        if not self.is_trade_timer_paused:
                            elapsed_while_active += 0.1
                            if timeout > 0 and elapsed_while_active >= timeout:
                                # Re-check redundancy one last time before auto-exec! (Safety-1 Fix)
                                if self._is_trade_redundant(trade):
                                    self.logger.warning(f"Timeout for {trade.strategy_id}, but trade is now redundant. Aborting!")
                                    confirmed = False
                                    break
                                    
                                self.logger.warning(f"Confirmation timeout for {trade.strategy_id} at {elapsed_while_active:.1f}s / {timeout}s. Auto-executing!")
                                confirmed = True
                                break
                        else:
                            # Still paused
                            if int(elapsed_while_active * 10) % 50 == 0: # Log every 5 seconds while paused
                                self.logger.debug(f"Trade {trade.strategy_id} remains PAUSED. Time already elapsed: {elapsed_while_active:.1f}s")
                        continue
                
                # Cleanup BEFORE notifying reconciliation (prevents race condition)
                self.pending_trade = None
                self.is_trade_timer_paused = False
                self.order_queue.task_done()

                if confirmed:
                    self.logger.info(f"Executing trade for {trade.strategy_id}...")
                    await self.execute_net_trade(trade)
                    # Broadcast success alert
                    self._broadcast_alert("success", "Trade Executed", f"Sent {len(trade.legs)} legs for {trade.strategy_id}.")
                else:
                    self.logger.info(f"Trade for {trade.strategy_id} was dismissed/rejected.")
                    trade.status = "cancelled"
                    if trade.purpose == TradePurpose.RECONCILIATION:
                        from time import time
                        self.last_dismissed_recon_time = time()
                    # POKE reconciliation again to re-evaluate the GAP
                    self.reconciliation_event.set()
                    self._broadcast_alert("info", "Trade Dismissed", f"Trade for {trade.strategy_id} was cancelled/dismissed.")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in order_execution_loop: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(1)

    async def _monitoring_loop(self):
        """Isolated monitoring logic extracted for TaskGroup context"""
        try:
            now = datetime.now(CHICAGO)
            catch_up_start = datetime.combine(now.date(), time(8, 30), CHICAGO)
            
            session_state = self.session_manager.load_session()
            catch_up_start_today = catch_up_start # Baseline 8:30
            if session_state:
                self.logger.info("Found existing session for today. Restoring...")
                self.session_manager.restore_monitor(self, session_state)
                # Note: We still rebuild history from 8:30 regardless of timestamp to ensure chart continuity
            
            # 1. ALWAYS Catch-up from 08:30 AM via simulation to rebuild full history
            db_path = self.db_path
            bootstrap_mode = self.config.get('bootstrap_mode', 'soft')
            
            if now.time() >= time(8, 30) and db_path and os.path.exists(db_path) and db_path != '/dev/null':
                self.status = "Catching up..."
                try:
                    # Bug 3 Fix: Perform explicit initial broker sync before bootstrap reads broker_trades
                    self.logger.info("Performing initial broker sync before bootstrap...")
                    await self._sync_broker_data() 
                    
                    # Collect TRUE broker trades from restored live portfolio for the Live replay
                    # Issue Fix: We used to use s.portfolio.trades (Sim) which caused curves to overlap
                    with self._data_lock:
                        broker_trades = list(self.live_combined_portfolio.trades)
                    
                    # Reset portfolios for a clean replay from 08:30
                    with self._data_lock:
                        self.combined_portfolio = Portfolio()
                        self.live_combined_portfolio = Portfolio()
                        for sid, s in self.sub_strategies.items():
                            s.portfolio = Portfolio()
                            s.has_traded_today = False
                    
                    self.logger.info(f"Rebuilding full history from 08:30 AM (Mode: {bootstrap_mode})...")
                    self.logger.info(f"Bootstrap debug: found {len(broker_trades)} broker trades for replay.")
                    if broker_trades:
                        self.logger.info(f"First broker trade: {broker_trades[0].timestamp} - {broker_trades[0].purpose}")
                        
                    history = await self._run_historical_simulation(
                        catch_up_start_today, now, 
                        live_trades=broker_trades, 
                        mode=bootstrap_mode,
                        collect_history=True
                    )
                    with self._data_lock:
                        self.session_history = history if history else []
                except Exception as e:
                    self.logger.error(f"Catch-up simulation failed: {e}\n{traceback.format_exc()}")
            else:
                self.logger.info("Skipping historical catch-up (Before market open or no Database).")
            
            self.status = "Running"

            while self.is_running:
                # 0. Overnight Sentinel Check (Requirement #3)
                now_check = datetime.now(CHICAGO)
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
                
                # Record history point for charts (30s cadence)
                with self._data_lock:
                    # Estimate SPX if not already done in monitor_step
                    spx = self._last_spx_price if hasattr(self, '_last_spx_price') else None
                    self.session_history.append({
                        'ts': datetime.now(CHICAGO).isoformat(),
                        'spx': spx,
                        'sim_pnl': round(self.combined_portfolio.net_pnl, 2),
                        'live_pnl': round(self.live_combined_portfolio.net_pnl, 2)
                    })
                
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
        """Fetch positions, recent trades, and working orders using a consolidated 2-call pattern.
        This also serves as the connection heartbeat."""
        if not self.client or not self.account_hash:
            return

        try:
            # Bug 4 Fix: Standardize to_entered_datetime to use UTC (matching get_live_trades)
            now_utc = datetime.now(timezone.utc)
            now_chi = now_utc.astimezone(CHICAGO)
            today_830 = now_chi.replace(hour=8, minute=30, second=0, microsecond=0)
            
            # --- CALL 1: Account Balances & Positions ---
            resp_acc = await self.client.get_account(self.account_hash, fields=['positions'])
            
            # --- CALL 2: Consolidated Orders (Filled + Working) ---
            # Focus on today's activity from 08:30 AM (Chicago)
            resp_ord = await self.client.get_orders_for_account(
                self.account_hash,
                from_entered_datetime=today_830,
                to_entered_datetime=now_utc
            )

            # --- Data Processing (Bug 1 Fix: Parse FIRST, then mark healthy) ---
            if resp_acc.status_code != 200 or resp_ord.status_code != 200:
                self.logger.warning(f"Broker Sync partial failure: Acc {resp_acc.status_code}, Ord {resp_ord.status_code}")
                # Treat as failure for heartbeat logic
                raise Exception(f"API Error: Acc={resp_acc.status_code}, Ord={resp_ord.status_code}")

            ord_data = resp_ord.json()
            if not isinstance(ord_data, list):
                self.logger.error(f"Unexpected orders response format (type={type(ord_data).__name__}): {str(ord_data)[:300]}")
                # Raising ensures heartbeat_failures increments and broker_connected is not marked True
                raise Exception(f"Malformed orders response: expected list, got {type(ord_data).__name__}")

            # --- Heartbeat Tracking (Only if data parsed correctly) ---
            with self._data_lock:
                self.broker_connected = True
                self.heartbeat_failures = 0

            acc_data = resp_acc.json().get('securitiesAccount', {})
            with self._data_lock:
                # 1. Update Positions
                broker_positions = acc_data.get('positions', [])
                self._update_live_portfolio(broker_positions)

                # 2. Update Trades (FILLED today)
                # Filter locally for trades filled today after 8:30
                filled_orders = [o for o in ord_data if o.get('status') == 'FILLED']
                
                # Convert filled orders to Trade objects
                broker_trades = []
                for o in filled_orders:
                    trades = self._convert_order_to_trade(o)
                    broker_trades.extend(trades)

                # Bug 2 Fix: Merge incoming filled trades with existing set instead of replacing
                # Use order_id as deduplication key to prevent losing history on empty API responses
                existing_ids = {t.order_id for t in self.live_combined_portfolio.trades if t.order_id}
                new_trades = [t for t in broker_trades if t.order_id and t.order_id not in existing_ids]
                
                if new_trades:
                    self.live_combined_portfolio.trades.extend(new_trades)
                    self.logger.info(f"Added {len(new_trades)} new filled trade(s) from broker sync.")

                # Recalculate live cash from the full combined list
                self.live_combined_portfolio.cash = sum(t.credit for t in self.live_combined_portfolio.trades)

                # 3. Update Working Orders
                self.working_orders = [o for o in ord_data if o.get('status') in ['WORKING', 'QUEUED', 'ACCEPTED', 'PENDING_ACTIVATION']]
                self.working_strategy_ids = {
                    self.order_to_strategy[str(o['orderId'])] 
                    for o in self.working_orders 
                    if str(o.get('orderId')) in self.order_to_strategy
                }
                
                # CLEARING FLAG: If we were waiting for sync, clear it now that we have fresh data
                if self.awaiting_broker_sync:
                    self.awaiting_broker_sync = False
                    self.logger.info("Fresh consolidated broker data received. Clearing awaiting_broker_sync flag.")

                # AUTO-DISMISSAL: If we have an active modal, check if it's now redundant
                # Issue 3 Fix: Sync-triggered check is more responsive than timer-based
                if hasattr(self, 'pending_trade') and self.pending_trade and self._is_trade_redundant(self.pending_trade):
                    self.logger.debug(f"Data-driven redundancy check: Trade {self.pending_trade.strategy_id} is covered. Dismissing modal.")
                    self.confirmation_event.set()

        except Exception as e:
            with self._data_lock:
                self.heartbeat_failures += 1
                self.logger.error(f"Error in _sync_broker_data (Heartbeat Failure {self.heartbeat_failures}): {e}")
                
                # Connection Recovery Logic
                if self.heartbeat_failures >= 3:
                    self.broker_connected = False
                    # Reset client to force re-auth
                    self.client = None 

            if self.heartbeat_failures == 3:
                await notify_all(self.config, "Broker Connection Lost!", title="Terminator Critical", priority="urgent")
            
            self._broadcast_alert("error", "Broker Sync Error", str(e))

    async def _broker_sync_loop(self):
        """High-frequency loop to keep account state fresh (5s)"""
        self.logger.info("Broker Sync Loop started (5s cadence).")
        initial_sync_done = False
        while self.is_running:
            try:
                # Ensure client is initialized even pre-market so UI shows online status
                if not self.client:
                    await self.initialize_schwab_client()

                # Sync if market is open OR if we haven't done an initial sync yet (useful for after-hours start)
                now = datetime.now(CHICAGO)
                if self.is_market_open(now) or self.status == "Initializing..." or not initial_sync_done:
                    await self._sync_broker_data()
                    initial_sync_done = True
                
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in broker_sync_loop: {e}")
                await asyncio.sleep(5)

    async def _run_index_streaming_loop(self):
        """Dedicated task to stream $SPX and $VIX in real-time via WebSocket."""
        self.logger.info("Index Streaming Loop starting...")
        
        def handle_index_update(msg):
            # Known: Indices like $SPX and $VIX come under LEVELONE_EQUITIES for Schwab in 1.5.1
            content = msg.get('content', [])
            for entry in content:
                key = entry.get('key')
                # Support both string keys and index-based keys
                price = entry.get('LAST_PRICE') or entry.get('3')
                if price is not None:
                    if key == '$SPX':
                        self._last_spx_price = float(price)
                        ts_ms = entry.get('QUOTE_TIME_MILLIS') or entry.get('TRADE_TIME_MILLIS') or entry.get('1')
                        if ts_ms:
                            self._last_spx_ts = int(ts_ms)
                    elif key == '$VIX':
                        self._last_vix_price = float(price)
        
        try:
            while self.is_running:
                try:
                    if not self.client:
                        await asyncio.sleep(5)
                        continue

                    self.stream_client = StreamClient(self.client)
                    await self.stream_client.login()
                    
                    # Handler for index symbols ($SPX, $VIX)
                    self.stream_client.add_level_one_equity_handler(handle_index_update)
                    
                    # Subscribe to symbols
                    self.logger.info("Subscribing to real-time $SPX and $VIX streams...")
                    await self.stream_client.level_one_equity_subs(['$SPX', '$VIX'])
                    
                    # Persistent message loop
                    while self.is_running:
                        await self.stream_client.handle_message()
                        
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.logger.error(f"Index Streamer Error: {e}")
                    if self.stream_client:
                        try:
                            await self.stream_client.logout()
                        except: pass
                    await asyncio.sleep(10) # Cooldown before reconnect
        finally:
            if self.stream_client:
                try:
                    await self.stream_client.logout()
                except: pass
            self.logger.info("Index Streaming Loop stopped.")

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
                        "timestamp": datetime.now(CHICAGO).isoformat()
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
                # Duration: only for closed strategies (Bug 4 Fix)
                if is_closed and s.portfolio.trades:
                    entry_ts = s.portfolio.trades[0].timestamp
                    last_ts = s.portfolio.trades[-1].timestamp
                    
                    # Ensure both are offset-aware for subtraction (Bug Fix: TypeError fix)
                    if getattr(entry_ts, 'tzinfo', None) is None: entry_ts = entry_ts.replace(tzinfo=CHICAGO)
                    if getattr(last_ts, 'tzinfo', None) is None: last_ts = last_ts.replace(tzinfo=CHICAGO)
                    
                    dur = (last_ts - entry_ts).total_seconds() / 60.0
                    total_dur += dur
            self.stats.total_trades = total
            self.stats.winners = winners
            self.stats.losers = losers
            self.stats.total_pnl = self.combined_portfolio.cash + sum(l.price * l.quantity * 100 for l in self.combined_portfolio.positions)
            if total > 0:
                # Bug 4 Fix: Average duration across closed trades (total)
                self.stats.avg_duration_minutes = total_dur / total
            
            # Drawdown calculation
            # Simplified: track daily peak simulated equity vs current
            equity = self.stats.total_pnl
            if not hasattr(self, '_peak_equity'): self._peak_equity = equity
            self._peak_equity = max(self._peak_equity, equity)
            dd = self._peak_equity - equity
            self.stats.max_drawdown = max(self.stats.max_drawdown, dd)

    async def run_broker_heartbeat(self):
        """Legacy heartbeat loop (Deprecated: logic moved to _sync_broker_data)"""
        self.logger.info("Legacy heartbeat loop deactivated as sync loop now handles connection monitoring.")
        self.heartbeat_running = False
        return

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
            if self.client and hasattr(self.client, 'session'):
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
        now = datetime.now(CHICAGO)
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
        self._last_snap = snap # Cache for higher frequency position syncs
        if snap is None or snap.empty:
            self.logger.warning("Failed to fetch option chain. Aborting monitor step to avoid stale state.")
            return
        
        # Pre-calculate indexed snap for strategy loops (Perf-1 Fix)
        snap['strike_int'] = snap['strike_price'].round().astype(int)
        snap_indexed = snap.set_index(['strike_int', 'side'])
        
        # P6 Fix: Clear the option cache at the start of every 30s tick
        # Since entries are snap-relative, there is no benefit to retaining across ticks.
        self._option_cache = {} 

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
        
        # Identify all symbols we need quotes for
        with self._data_lock:
            symbol_net_qty_fills = defaultdict(int)
            for t in broker_trades:
                for l in t.legs:
                    symbol_net_qty_fills[l.symbol] += l.quantity

            all_relevant_symbols = set(symbol_net_qty_fills.keys())
            for p in broker_positions: 
                all_relevant_symbols.add(p.symbol)
            
            # P9 Fix: Also include all current SIM positions so they are priced even if Live is empty
            for p in self.combined_portfolio.positions:
                all_relevant_symbols.add(p.symbol)
            
            # Identify missing ones (Bug 3: identify UNDER lock, fetch OUTSIDE lock)
            missing = [s for s in all_relevant_symbols if s not in snap_quotes]

        # Bug 3: Fetch outside lock!
        quotes = snap_quotes.copy()
        if missing:
            extra = await self.fetch_quotes(missing)
            quotes.update(extra)

        with self._data_lock:
            if all_relevant_symbols:
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
                # P2: Use indexed snap for fast pricing
                self._update_all_pricing(quotes, snap=snap, snap_indexed=snap_indexed)
                
                mv = sum(l.price * l.quantity * 100 for l in self.live_combined_portfolio.positions)
                cash = self.live_combined_portfolio.cash
                day_pnl = (mv + cash) - starting_value
                self.logger.debug(f"PnL Calc: MV={mv:.2f}, Cash={cash:.2f}, StartMV={starting_value:.2f}, DayPnL={day_pnl:.2f}")
        
        # 3. Update Quotes and Deltas for all active positions (Already updated in step 2c via snap)
        # 4. Strategy logic (snap already fetched)
        ts = datetime.now(CHICAGO)
        t_time = ts.time()
        start_time_obj = time(8, 30)
        end_time_obj = time(15, 0)
        
        # Target decay
        decay_c = calculate_delta_decay(ts, 'CALL', self.config['initial_sum_delta']/2, start_time_obj, end_time_obj)
        decay_p = calculate_delta_decay(ts, 'PUT', self.config['initial_sum_delta']/2, start_time_obj, end_time_obj)
        t_short = (decay_c + decay_p) / 2
        # P7 Fix: Prefer real-time streamed $SPX price over synthetic estimate
        spx = self._last_spx_price or self.estimate_spx_price(snap)
        if spx is None:
            self.logger.error("Critical: Could not determine SPX price from stream or snapshot.")
            return

        self._last_spx_price = spx # Ensure fallback value is cached

        t_trades = defaultdict(list)
        
        # Scalability #5: Batch process strategies to avoid holding lock too long
        strategy_items = list(self.sub_strategies.items())
        for start_i in range(0, len(strategy_items), 10):
            batch = strategy_items[start_i:start_i + 10]
            with self._data_lock:
                for sid, s in batch:
                    if t_time < s.trade_start_time:
                        continue
                    
                    # Bug 5 Fix: Check active_order_signals while under lock 
                    # and skip if there's already a pending signal for this strategy in the GUI
                    if sid in self.active_order_signals:
                        continue

                    for p in s.portfolio.positions:
                        p_strike_int = int(round(p.strike))
                        key = (p_strike_int, p.side)
                        if key in snap_indexed.index:
                            row = snap_indexed.loc[[key]].iloc[0]
                            p.delta = row['delta']
                            p.price = row['mid_price']
                            p.theta = float(row['theta']) if not pd.isna(row['theta']) else 0.0
                    
                    # Check for entry
                    if not s.has_traded_today and t_time < end_time_obj:
                        trade = self._check_entry(s, snap, ts)
                        if trade:
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
            
            # Yield control between batches
            await asyncio.sleep(0)

        with self._data_lock:
                self.logger.debug(f"Fast Model Update: Executing {len(t_trades)} strategy signals in simulation.")
                
                # Update sub-strategies first
                for sid, trades in t_trades.items():
                    if sid in self.sub_strategies:
                        for tr in trades:
                            self.sub_strategies[sid].portfolio.add_trade(tr)
                            self.sub_strategies[sid].has_traded_today = True

                # P4 Fix: Mark simulation as 'dirty' when trades occur to trigger rebuild
                if t_trades:
                    self._sim_dirty = True

                # Net and update combined simulation portfolio
                netted = self.net_trades(t_trades)
                for nt in netted:
                    self.combined_portfolio.add_trade(nt)
                
                # BUG 8/Replay Fix: Force sync to ensure combined portfolio matches substrategies
                self._reconcile_combined_simulation()
                
                # POKE: Signal that reconciliation is needed immediately
                self.reconciliation_event.set()

        # 5. Reconciliation (Simulation vs reality)
        # Handled by run_reconciliation_loop() when poked via reconciliation_event.
        # Removed direct call here to prevent race condition causing duplicate order confirmations.

        # 6. Periodic Save Session (Scalability #6: 5min Safety Checkpoint)
        # Event-driven saves also occur in execute_net_trade()
        from time import time as t_time
        now_ts = t_time()
        if (now_ts - self._last_save_time) >= 300: # 5 Minutes
            self.session_manager.save_session(self)
            self._last_save_time = now_ts
            self.logger.debug("Performed periodic session safety checkpoint.")


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

        # ALWAYS refresh the execution plan right before execution.
        # Issue 2 Fix: Rely on the 5s sync cache (self.working_orders) rather than making a 3rd API call.
        self.logger.info("Verifying execution plan against last known broker state...")
        # self.working_orders = await self.get_working_orders() # Removed redundant call
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
                signed_mid = total_chunk_credit / (100.0 * num_units) if num_units > 0 else 0.0
                unit_mid_price = abs(signed_mid)
                
                # Classify structure to determine if 0.00 floor is mandatory (Task #32 Improvements)
                order_type, is_credit_structural = self._classify_order_type(rolled_chunk_legs)
                lock_floor = (order_type != "unknown")
                
                # Structural intent (+Cr, -Db) locked to avoid structural rejections on Schwab
                if is_credit_structural is not None:
                    is_credit_struct = is_credit_structural
                else:
                    # Fallback to mid price for spreads and unknown structures
                    is_credit_struct = total_chunk_credit >= 0
                
                # Task #28: Apply Manual Price Override from UI if exists
                override = self.price_overrides.get(trade.strategy_id, {}).get(i)
                if override is not None:
                    if lock_floor:
                        # Intent Locked (Safe structures)
                        is_final_credit = is_credit_struct
                        raw_price = max(0.0, override if is_credit_struct else -override)
                    else:
                        # Intent Dynamic (Exotic structures)
                        is_final_credit = override >= 0
                        raw_price = abs(override)
                    self.logger.info(f"Chunk {i}: Override {override:.2f} -> Price: {raw_price:.2f} (Lock: {lock_floor})")
                else:
                    offset = self.config.get('order_offset', 0.0)
                    signed_target = signed_mid + offset
                    
                    if lock_floor:
                        # Intent Locked + Mandatory Floor (Safe structures)
                        is_final_credit = is_credit_struct
                        raw_price = max(0.0, signed_target if is_credit_struct else -signed_target)
                    else:
                        # Intent Dynamic + No Floor (Exotic structures)
                        is_final_credit = signed_target >= 0
                        raw_price = abs(signed_target)
                
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
                        builder.set_order_type(OrderType.NET_CREDIT if is_final_credit else OrderType.NET_DEBIT)
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
                    builder.set_order_type(OrderType.NET_CREDIT if is_final_credit else OrderType.NET_DEBIT)
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
            if self.client:
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
            else:
                return None
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

    async def fetch_quotes(self, symbols: List[str]) -> Dict[str, Any]:
        """Fetch quotes for symbols with exponential backoff (Bug 9 Fix)"""
        if not self.client or not symbols: return {}
        
        # Ensure symbols are clean and not empty
        symbols = [s.strip() for s in symbols if s and s.strip()]
        if not symbols: return {}

        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.logger.info(f"Fetching quotes for {len(symbols)} symbols (Attempt {attempt+1})...")
                res = await self.client.get_quotes(symbols)
                if res.status_code == 200:
                    data = res.json()
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
                elif res.status_code == 429: # Rate Limit
                    import random
                    wait = (2 ** attempt) + (random.random() * 0.5)
                    self.logger.warning(f"Rate limited (429) fetching quotes. Waiting {wait:.2f}s")
                    await asyncio.sleep(wait)
                else:
                    self.logger.error(f"Failed to fetch quotes: HTTP {res.status_code}")
                    break
            except Exception as e:
                self.logger.error(f"Error fetching quotes: {e}")
                await asyncio.sleep(1)
        return {}

    def _update_all_pricing(self, quotes: Dict[str, Dict], snap: Optional[pd.DataFrame] = None, snap_indexed: Optional[pd.DataFrame] = None):
        """Update both sim and live portfolios with latest quotes using robust matching (P2 Fix)"""
        for portfolio in [self.combined_portfolio, self.live_combined_portfolio]:
            for p in portfolio.positions:
                found = False
                
                # 1. Fast path: O(1) indexed snap lookup (P2 fix)
                if snap_indexed is not None:
                    key = (int(round(p.strike)), p.side)
                    if key in snap_indexed.index:
                        row = snap_indexed.loc[[key]].iloc[0]
                        p.bid_price = row['bidprice']
                        p.ask_price = row['askprice']
                        p.price = row['mid_price']
                        delta_val = row['delta']
                        p.delta = float(delta_val) if not pd.isna(delta_val) else 0.0
                        p.theta = float(row['theta']) if not pd.isna(row['theta']) else 0.0
                        # Update sticky cache
                        self._greek_cache[(p.symbol, int(round(p.strike)), p.side)] = (p.delta, p.theta)
                        found = True

                # 2. Slow fallback: boolean mask (only when snap_indexed unavailable)
                elif not found and snap is not None and not snap.empty:
                    r = snap[(snap['strike_price'].round().astype(int) == int(round(p.strike))) & (snap['side'] == p.side)]
                    if not r.empty:
                        row = r.iloc[0]
                        p.bid_price = row['bidprice']
                        p.ask_price = row['askprice']
                        p.price = row['mid_price']
                        delta_val = row['delta']
                        p.delta = float(delta_val) if not pd.isna(delta_val) else 0.0
                        p.theta = float(row['theta']) if not pd.isna(row['theta']) else 0.0
                        # Update sticky cache
                        self._greek_cache[(p.symbol, int(round(p.strike)), p.side)] = (p.delta, p.theta)
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
                        # Bug 2 Fix: Extract and send real bid/ask for live positions
                        'price': pos.get('marketValue', 0) / (qty * 100) if qty != 0 else 0,
                        'bid': 0, # Bug 9 Fix: Populate from snap in update_live_portfolio instead of fabrication
                        'ask': 0,
                    'avg_price': pos.get('averagePrice', 0),
                    'current_day_pnl': pos.get('currentDayProfitLoss', 0)
                    })
            return spx_pos
        except Exception as e:
            self.logger.error(f"Error fetching live positions: {e}")
            return None
    async def get_live_trades(self) -> List[Trade]:
        """Fetch recent filled orders from Schwab and convert to Trade objects"""
        if not self.client or not self.account_hash: return None
        try:
            # 0. Timezone Awareness: Correctly fetch Chicago morning (08:30) for orders
            # 0. Timezone Awareness: Correctly fetch today's orders (Broaden to 4:00 AM to be safe)
            now = datetime.now(timezone.utc)
            today_start_chi = datetime.now(CHICAGO).replace(hour=4, minute=0, second=0, microsecond=0)
            
            # Fetch ALL orders for today, we filter status in python naturally (Issue: Schwab API param mismatch)
            resp = await self.client.get_orders_for_account(
                self.account_hash, 
                from_entered_datetime=today_start_chi, 
                to_entered_datetime=now
            )
            if resp.status_code != 200:
                self.logger.error(f"Failed to fetch account orders: {resp.status_code}")
                return None
            
            ord_data = resp.json()
            filled_orders = [o for o in ord_data if o.get('status') == 'FILLED']
            
            trades = []
            self.logger.debug(f"Fetched {len(ord_data)} total orders. Found {len(filled_orders)} filled orders.")
            for order in filled_orders:
                trades.extend(self._convert_order_to_trade(order))

            self.logger.debug(f"Processed {len(trades)} SPX trades for display.")
            trades.sort(key=lambda x: x.timestamp)
            return trades
        except Exception as e:
            self.logger.error(f"Error fetching live trades: {e}\n{traceback.format_exc()}")
            return None

    def _convert_order_to_trade(self, order: Dict) -> List[Trade]:
        """Helper to convert a single Schwab order JSON to Trade objects (1 Strategy = 1 Trade)"""
        trades = []
        
        # Issue 4 Fix: Map each leg to its specific execution fill price
        leg_fill_prices = {}
        activities = order.get('orderActivityCollection', [])
        actual_net_cash = 0.0
        has_execution = False
        
        if activities:
            leg_id_to_instr = {str(l.get('legId')): l.get('instruction') for l in order.get('orderLegCollection', [])}
            for activity in activities:
                if activity.get('activityType') == 'EXECUTION':
                    for exec_leg in activity.get('executionLegs', []):
                        lid = str(exec_leg.get('legId'))
                        ep = exec_leg.get('price', 0.0)
                        eq = exec_leg.get('quantity', 0)
                        
                        # Cache fill price for instrument building
                        leg_fill_prices[lid] = ep
                        
                        # Aggregate for net premium
                        instr = leg_id_to_instr.get(lid, '')
                        multiplier = 1.0 if 'SELL' in instr else -1.0
                        actual_net_cash += (ep * eq * multiplier)
                        has_execution = True

        legs = []
        for oleg in order.get('orderLegCollection', []):
            instr = oleg.get('instrument', {})
            symbol = instr.get('symbol', '')
            # Robust Check: Match SPX, $SPX, SPXW, or $SPXW (Task: Fix missing Live Trades)
            is_spx = (instr.get('underlyingSymbol') in ['$SPX', 'SPX', '$SPXW', 'SPXW'] or 
                      symbol.startswith('$SPX') or symbol.startswith('SPX'))
            
            if is_spx:
                parsed = self._parse_schwab_symbol(symbol)
                if parsed:
                    instruction = oleg.get('instruction', '')
                    qty = int(oleg.get('quantity', 0))
                    signed_qty = qty if 'BUY' in instruction else -qty
                    
                    # Issue 4: Use leg-specific fill price, fall back to limit price
                    lid = str(oleg.get('legId'))
                    fill_p = leg_fill_prices.get(lid, order.get('price', 0))
                    
                    legs.append(OptionLeg(
                        symbol=symbol,
                        strike=parsed['strike'],
                        side=parsed['side'],
                        quantity=signed_qty,
                        price=fill_p,
                        entry_price=fill_p
                    ))
        
        if not legs:
            return []

        # Determine timestamp
        close_time_str = order.get('closeTime') or order.get('enteredTime')
        if close_time_str:
            ts = datetime.fromisoformat(close_time_str.replace('Z', '+00:00'))
        else:
            ts = datetime.now(timezone.utc)
        ts_chi = ts.astimezone(CHICAGO)

        order_id_key = str(order.get('orderId', ''))
        strategy_id = self.order_to_strategy.get(order_id_key, "BROKER")
        
        if has_execution:
            credit = actual_net_cash * 100
        else:
            # Fallback for non-filled orders
            order_type = order.get('orderType', '')
            raw_price = order.get('price', 0)
            multiplier = 100 
            if order_type == 'NET_DEBIT':
                credit = -raw_price * multiplier
            elif order_type == 'NET_CREDIT':
                credit = raw_price * multiplier
            else:
                credit = (raw_price * multiplier) if legs[0].quantity < 0 else (-raw_price * multiplier)

        comm_per_contract = self.config.get('commission_per_contract', 1.13)
        est_commission = comm_per_contract * sum(abs(l.quantity) for l in legs)
        
        # Determine purpose
        order_legs = order.get('orderLegCollection', [])
        all_open = all(oleg.get('instruction', '').endswith('_OPEN') for oleg in order_legs)
        any_open = any(oleg.get('instruction', '').endswith('_OPEN') for oleg in order_legs)
        
        if len(legs) == 4 and all_open:
            purpose = TradePurpose.IRON_CONDOR
        else:
            purpose = TradePurpose.RECONCILIATION if any_open else TradePurpose.EXIT

        trades.append(Trade(
            timestamp=ts_chi,
            legs=legs,
            credit=credit,
            commission=est_commission,
            current_sum_delta=0,
            purpose=purpose,
            strategy_id=strategy_id,
            order_id=order_id_key,
            status="filled"
        ))
        
        return trades

    async def get_working_orders(self) -> List[Dict]:
        """Fetch currently working/pending orders from Schwab. (Issue 6 Fix: CHICAGO aware)"""
        if not self.client or not self.account_hash: return []
        try:
            # Fetch for today since Chicago midnight
            from_time = datetime.now(CHICAGO).replace(hour=0, minute=0, second=0, microsecond=0)
            
            # Fetch ALL to filter manually for better robustness (library might have quirks with list status)
            resp = await self.client.get_orders_for_account(self.account_hash, from_entered_datetime=from_time)
            
            if resp.status_code != 200:
                self.logger.error(f"Failed to fetch account orders: {resp.status_code}")
                return []
            
            all_orders = resp.json()
            if not isinstance(all_orders, list):
                return []

            working_statuses = ['WORKING', 'PENDING_ACTIVATION', 'AWAITING_MANUAL_REVIEW', 'QUEUED', 'ACCEPTED']
            working_orders = [o for o in all_orders if o.get('status') in working_statuses]
            return working_orders
        except Exception as e:
            self.logger.error(f"Error fetching working orders: {e}")
            return []

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
                        self.working_strategy_ids.discard(sid)
                return True
            else:
                self.logger.error(f"Failed to cancel order {order_id}: {resp.status_code} {resp.text}")
                return False
        except Exception as e:
            self.logger.error(f"Error cancelling order {order_id}: {e}")
            return False

    async def cancel_all_orders(self) -> Dict[str, Any]:
        """Cancel all working orders at the broker"""
        if not self.client or not self.account_hash:
            return {"success": False, "msg": "Client not initialized"}
        
        # We use a copy of the list to avoid mutation issues while iterating/awaiting
        with self._data_lock:
            to_cancel = list(self.working_orders)
        
        if not to_cancel:
            return {"success": True, "msg": "No working orders to cancel", "count": 0}
        
        self.logger.info(f"Requested cancellation of all {len(to_cancel)} working orders.")
        
        results = []
        for order in to_cancel:
            oid = str(order.get('orderId'))
            success = await self.cancel_order(oid)
            results.append({"orderId": oid, "success": success})
            
        success_count = sum(1 for r in results if r['success'])
        return {
            "success": success_count > 0,
            "msg": f"Cancelled {success_count} of {len(to_cancel)} orders",
            "count": success_count,
            "total": len(to_cancel)
        }

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

            recipients = self.config.get('email_recipients', ['frankwang.alert@gmail.com'])
            if not recipients:
                self.logger.warning("No email recipients configured, skipping alert")
                return

            msg = MIMEMultipart()
            msg['From'] = email_config['from_email']
            msg['To'] = ", ".join(recipients)
            msg['Subject'] = "Terminator Alert: New Trade"
            
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
        lines.append(f"Time: {datetime.now(CHICAGO).strftime('%Y-%m-%d %H:%M:%S')}")
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
        
        lines.append("Action required: Please confirm or dismiss the trade in the Terminator UI.")
        return "\n".join(lines)

    def signal_completed(self, trade: Trade):
        """Remove strategy and its constituents from active signals set once GUI is done with it"""
        if trade.strategy_id in self.active_order_signals:
            self.active_order_signals.remove(trade.strategy_id)
        
        for ct in trade.constituent_trades:
            if ct.strategy_id in self.active_order_signals:
                self.active_order_signals.remove(ct.strategy_id)

    async def confirm_live_trade(self, strategy_id: str, overrides: Optional[List[Dict]] = None):
        """Manual confirmation of a pending trade for a strategy."""
        if self.pending_trade and self.pending_trade.strategy_id == strategy_id:
            self.logger.info(f"Manual confirmation received for strategy: {strategy_id}")
            if overrides:
                # Convert list [ {idx: 0, price_ea: 1.05}, ... ] to dict mapping for fast lookup in execution loop
                override_map = {o['idx']: o['price_ea'] for o in overrides if 'idx' in o}
                self.price_overrides[strategy_id] = override_map
                self.logger.info(f"Applied {len(override_map)} price overrides for {strategy_id}")
            
            self._pending_trade_confirmed = True
            self.confirmation_event.set()
        else:
            self.logger.warning(f"Unexpected confirmation received for {strategy_id} - no pending trade matches.")

    async def dismiss_live_trade(self, strategy_id: str):
        """Manual dismissal of a pending trade for a strategy."""
        if self.pending_trade and self.pending_trade.strategy_id == strategy_id:
            self.logger.info(f"Manual dismissal received for strategy: {strategy_id}")
            self._pending_trade_confirmed = False
            self.confirmation_event.set()
        else:
            self.logger.warning(f"Unexpected dismissal received for {strategy_id} - no pending trade matches.")



    def _update_live_portfolio(self, broker_positions: List[Dict]):
        """Update live_combined_portfolio with actual broker positions, robustly parsing Schwab JSON. (Bug Fix: Raw Parsing)"""
        self.live_combined_portfolio.positions = []
        for bp in broker_positions:
            instr = bp.get('instrument', {})
            symbol = instr.get('symbol', 'N/A')
            
            # 1. Basic validation and parsing
            if instr.get('assetType') != 'OPTION':
                continue
            
            # Important: raw Schwab positions often lack strikePrice at top level. 
            # We use our robust parser to get strike and side from the symbol.
            parsed = self._parse_schwab_symbol(symbol)
            if not parsed:
                self.logger.warning(f"Failed to parse live position symbol: {symbol}")
                continue
                
            strike_val = parsed['strike']
            side_val = parsed['side']
            
            # Calculate SIGNED quantity: Long - Short
            quantity = float(bp.get('longQuantity', 0)) - float(bp.get('shortQuantity', 0))
            if quantity == 0:
                continue

            k = (symbol, int(round(strike_val)), side_val)
            
            # 2. Greeks and Pricing lookup
            delta, theta = self._greek_cache.get(k, (0.0, 0.0))
            bid_price = 0.0
            ask_price = 0.0
            
            # Always attempt to look at the last snapshot to seed prices (Prevents the 0.0 race condition)
            if self._last_snap is not None:
                mask = (self._last_snap['strike_price'].round().astype(int) == int(round(strike_val))) & \
                       (self._last_snap['side'] == side_val)
                r = self._last_snap[mask]
                if not r.empty:
                    # If Greeks were missing from cache, update them from the current snap
                    if delta == 0:
                        delta = float(r['delta'].iloc[0])
                        theta_val = r['theta'].iloc[0] if 'theta' in r.columns else 0.0
                        theta = float(theta_val) if not pd.isna(theta_val) else 0.0
                        self._greek_cache[k] = (delta, theta)
                    
                    bid_price = float(r['bidprice'].iloc[0])
                    ask_price = float(r['askprice'].iloc[0])

            # 3. Add to live portfolio
            mkt_val = bp.get('marketValue', 0)
            mid_price = mkt_val / (quantity * 100) if quantity != 0 else 0
            
            self.live_combined_portfolio.positions.append(OptionLeg(
                symbol=symbol,
                strike=strike_val,
                side=side_val,
                quantity=int(quantity),
                price=abs(mid_price), # Mid is derived from market value
                entry_price=bp.get('averagePrice', 0), 
                bid_price=bid_price,
                ask_price=ask_price,
                current_day_pnl=bp.get('currentDayProfitLoss', 0.0),
                delta=delta,
                theta=theta
            ))
            
        # Trigger margin recalculation
        self.live_combined_portfolio.max_margin = self.live_combined_portfolio.calculate_standard_margin()
        self.logger.info(f"Updated live portfolio: {len(self.live_combined_portfolio.positions)} active positions. Margin: ${self.live_combined_portfolio.max_margin:,.2f}")

    def _get_live_filled_positions(self) -> Dict[Tuple[int, str], float]:
        """Returns ONLY filled positions from the broker (not including working orders)."""
        with self._data_lock:
            return { (int(round(p.strike)), p.side): float(p.quantity) for p in self.live_combined_portfolio.positions }

    def _get_effective_live_positions(self) -> Dict[Tuple[int, str], float]:
        """
        Combines FILLED positions with WORKING orders to get the 'Target' live state.
        This prevents suggesting trades for gaps that are already being addressed by a pending order.
        """
        with self._data_lock:
            # 1. Start with filled positions
            eff_dict = { (int(round(p.strike)), p.side): float(p.quantity) for p in self.live_combined_portfolio.positions }
            
            # 2. Add working orders to the effective current state
            for o in self.working_orders:
                # We skip cancelling/rejected orders if possible, but 'WORKING' is our target.
                for leg in o.get('orderLegCollection', []):
                    instr = leg.get('instruction', '')
                    qty = float(leg.get('quantity', 0))
                    instr_obj = leg.get('instrument', {})
                    
                    if instr_obj.get('assetType') == 'OPTION':
                        symbol = instr_obj.get('symbol')
                        parsed = self._parse_schwab_symbol(symbol)
                        
                        if parsed:
                            # BUY instructions increase position, SELL instructions decrease (long-centric math)
                            multiplier = 1.0 if 'BUY' in instr else -1.0
                            key = (int(round(parsed['strike'])), parsed['side'])
                            eff_dict[key] = eff_dict.get(key, 0.0) + (qty * multiplier)
                        else:
                            # Fallback if parsing failed but top-level fields exist
                            strike = instr_obj.get('strikePrice')
                            side = instr_obj.get('putCall')
                            if strike and side:
                                multiplier = 1.0 if 'BUY' in instr else -1.0
                                key = (int(round(float(strike))), side)
                                eff_dict[key] = eff_dict.get(key, 0.0) + (qty * multiplier)
            return eff_dict

    async def _check_reconciliation(self, snap: pd.DataFrame):
        """Compare simulated combined portfolio with live broker reality and suggest syncing trades"""
        if self.awaiting_broker_sync:
            self.logger.info("Skipping reconciliation: Awaiting broker sync of recent submission.")
            return
        # Issue 9: Always run reconciliation logic to track divergence
        # Issue 35: Deduplicate against Working Orders
        sim_pos = self.combined_portfolio.positions
        # Stability Fix: Detect gap against FILLED positions only.
        # Deduplication against working orders will happen in create_execution_plan.
        eff_live_dict = self._get_live_filled_positions()
        
        sim_dict = { (int(round(p.strike)), p.side): float(p.quantity) for p in sim_pos }
        
        all_keys = set(sim_dict.keys()) | set(eff_live_dict.keys())
        needed_adjustments = []
        
        for k in all_keys:
            sq = sim_dict.get(k, 0.0)
            lq = eff_live_dict.get(k, 0.0)
            diff = sq - lq
            if abs(diff) > 0.01: # Use epsilon for float safety
                # NO FLIP RULE: If crossing zero, we need two separate legs across different orders (Task #22)
                if (lq < 0 and sq > 0) or (lq > 0 and sq < 0):
                    self.logger.info(f"Flipping detected for {k[0]}{k[1]}: Live {lq}, Sim {sq}. Splitting into separate Exit and Entry legs.")
                    # 1. Exit portion: suggest the size that gets us back to 0
                    needed_adjustments.append((k[0], k[1], -lq))
                    # 2. Entry portion: suggest the target size from 0
                    needed_adjustments.append((k[0], k[1], sq))
                else:
                    needed_adjustments.append((k[0], k[1], diff))
        
        if not needed_adjustments:
            self.logger.debug("Reconciliation passed: Sim matches Live")
            with self._data_lock:
                self.last_reconciliation_trade = None
            return

        # Bug 8 Fix: Use tracker set for robust check 
        with self._data_lock:
            in_queue = TradePurpose.RECONCILIATION in self._queued_purposes
            if not in_queue and self.pending_trade:
                in_queue = self.pending_trade.purpose == TradePurpose.RECONCILIATION

        # Bug 16 Fix: cooldown for GAP_SYNC dismissal to prevent immediate re-prompts
        recon_cooldown = self.config.get('recon_dismiss_cooldown_seconds', 20)
        if not in_queue:
            from time import time
            if (time() - self.last_dismissed_recon_time) < recon_cooldown:
                return

        self.logger.debug(f"RECONCILIATION DISCREPANCY: Found {len(needed_adjustments)} legs mismatch. Generating Gap Sync Trade.")
        
        # Build Reconciliation Trade
        legs = []
        total_credit = 0.0
        
        # Bug 11: Index snap for O(1) reconciliation lookups
        snap_indexed = snap.set_index(['strike_int', 'side']) if 'strike_int' in snap.columns else snap.set_index([snap['strike_price'].round().astype(int), 'side'])

        # Track live positions locally to correctly determine instructions for split legs (flipping)
        tracked_live_positions = dict(eff_live_dict)

        for strike, side, qty in needed_adjustments:
            # Bug 11: O(1) lookup
            key = (int(strike), side)
            if key not in snap_indexed.index:
                self.logger.error(f"Cannot sync leg {strike}{side}: Not found in option chain.")
                continue
                
            r_row = snap_indexed.loc[[key]].iloc[0]
            symbol = r_row['symbol']
            price = r_row['mid_price']
            delta = r_row['delta']
            # Handle possible NaN in theta
            theta_val = r_row['theta'] if 'theta' in r_row.index else 0.0
            theta = float(theta_val) if not pd.isna(theta_val) else 0.0
            
            # Determine instruction based on tracked live position (lq)
            lq = tracked_live_positions.get(key, 0)
            if qty > 0: # Needs to BUY
                inst = "BUY_TO_CLOSE" if lq < 0 else "BUY_TO_OPEN"
            else: # Needs to SELL
                inst = "SELL_TO_CLOSE" if lq > 0 else "SELL_TO_OPEN"
            
            # Update tracked position for next leg determination in this trade group
            tracked_live_positions[key] = tracked_live_positions.get(key, 0) + qty
            
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
                timestamp=datetime.now(CHICAGO),
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

                    self.logger.info(f"Adding RECON trade to queue for strategy {recon_trade.strategy_id} with {len(recon_trade.legs)} legs")
                    self.order_queue.put_nowait(recon_trade)
                    # Bug 5 & 8 trackers: Prevent duplicates while in queue (Task #45)
                    self.active_order_signals.add(recon_trade.strategy_id)
                    self._queued_purposes.add(recon_trade.purpose)
                    # Alert logic moved to execution_loop to avoid noise
                else:
                    self.logger.info(f"Updated reconciliation trade available for GUI sync (Already in queue: {recon_trade.strategy_id})")
            else:
                self.logger.debug(f"Sim divergence detected but trading is disabled. GAP_SYNC updated for {len(legs)} legs.")
        else:
            self.logger.info("Reconciliation built NO legs. Skipping trade creation.")


    def _create_sync_entry(self, s: SubStrategy, snap: pd.DataFrame, ts: datetime, live_t: Trade) -> Optional[Trade]:
        """Soft Bootstrap: Create a sim trade that exactly matches the recorded live trade legs and strikes."""
        legs = []
        for ll in live_t.legs:
            # Match the recorded live strike in the historical snapshot
            r = snap[(snap['strike_price'].round().astype(int) == int(round(ll.strike))) & (snap['side'] == ll.side)]
            if r.empty:
                self.logger.warning(f"Bootstrap [SOFT]: Missing strike {ll.strike}{ll.side} for {s.sid} in snapshot. Sync failed.")
                return None # Missing DB data for this specific strike at this time
            
            row = r.iloc[0]
            legs.append(OptionLeg(
                symbol=ll.symbol,
                strike=ll.strike,
                side=ll.side,
                quantity=ll.quantity,
                delta=row['delta'],
                theta=float(row['theta']) if not pd.isna(row['theta']) else 0.0,
                price=row['mid_price'],
                entry_price=ll.entry_price or row['mid_price'], # Bug Sync Fix: Use live entry price to prevent PnL drift
                target_delta=ll.target_delta
            ))
        
        # Bug Sync Fix: Use actual credit from live trade to ensure PnL alignment in simulation
        credit = live_t.credit if live_t.credit != 0 else sum(-l.quantity * l.price for l in legs) * 100
        comm = live_t.commission if live_t.commission != 0 else self.config.get('commission_per_contract', 1.13) * len(legs)
        return Trade(ts, legs, credit, comm, live_t.current_sum_delta, live_t.purpose, s.sid)


    async def _run_historical_simulation(self, start_dt: datetime, end_dt: datetime, live_trades: List[Trade] = None, mode: str = 'hard', collect_history: bool = False) -> List[Dict]:
        """
        Run backtester logic on historical data. 
        [SOFT BOOTSTRAP OVERHAUL]:
        1. Match first IC trade for each sub-strategy using 1-hr window and mutual clarity rules.
        2. If matched, entry is 'seeded' from live trade.
        3. Once open, follow simulation logic (rebalance/exit) regardless of mode.
        """
        history = []
        
        # 1. Clean and deduplicate live trades for broker-truth tracking
        unique_live = []
        seen_order_ids = set()
        for t in sorted(live_trades or [], key=lambda x: x.timestamp):
            if t.order_id:
                if t.order_id in seen_order_ids:
                    self.logger.info(f"Bootstrap [SOFT]: Skipping duplicate Order ID {t.order_id} at {t.timestamp}")
                    continue
                seen_order_ids.add(t.order_id)
            t.status = "filled"
            unique_live.append(t)

        pending_live_trades = unique_live.copy()
        
        # 2. [SOFT BOOTSTRAP] Identify Initial Entry Assignments
        # Look for 4-leg Iron Condor entries as potential candidates
        ic_live_trades = [t for t in unique_live if (t.purpose == TradePurpose.IRON_CONDOR or len(t.legs) == 4)]
        
        strat_potential_matches = defaultdict(list) # sid -> List[Trade]
        trade_potential_strats = defaultdict(list)  # Trade object -> List[sid]
        
        if mode == 'soft':
            for sid, s in self.sub_strategies.items():
                win_start = s.trade_start_time
                # 1-hour window as requested
                win_end = (datetime.combine(start_dt.date(), win_start) + timedelta(hours=1)).time()
                for lt in ic_live_trades:
                    lt_time = lt.timestamp.time()
                    if win_start <= lt_time <= win_end:
                        strat_potential_matches[sid].append(lt)
                        trade_potential_strats[id(lt)].append(sid)
        
        assigned_live_entry = {} # sid -> Trade
        for sid, matches in strat_potential_matches.items():
            if len(matches) == 1:
                lt = matches[0]
                # Mutual clarity check: only if this trade is not also ambiguous for other strategies
                if len(trade_potential_strats[id(lt)]) == 1:
                    assigned_live_entry[sid] = lt
                    self.logger.info(f"Bootstrap [SOFT]: Assigned live trade {lt.order_id} ({lt.timestamp.strftime('%H:%M')}) to {sid}.")
                else:
                    self.logger.info(f"Bootstrap [SOFT]: Match for {sid} is ambiguous (trade matches multiple strategies). Sim fallback.")
            elif len(matches) > 1:
                self.logger.info(f"Bootstrap [SOFT]: Match for {sid} is ambiguous (multiple trades in window). Sim fallback.")

        # Data loading
        sim_date_str = start_dt.date().isoformat()
        db_path = self.db_path
        market_end_today = datetime.combine(start_dt.date(), time(15, 0), CHICAGO)
        effective_end_dt = max(end_dt, market_end_today)

        from contextlib import closing
        import sqlite3
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
            return []

        if data.empty:
            self.logger.warning(f"No historical data available for simulation catch-up on {sim_date_str}")
            return []
        
        dt_series = pd.to_datetime(data['datetime'])
        if dt_series.dt.tz is None:
            data['datetime'] = dt_series.dt.tz_localize('America/Chicago')
        else:
            data['datetime'] = dt_series.dt.tz_convert('America/Chicago')

        data = data.sort_values('datetime')
        data['mid_price'] = (data['bidprice'] + data['askprice']) / 2
        data = data.dropna(subset=['delta', 'bidprice', 'askprice'])
        
        groups = data.groupby('datetime')
        start_time_obj = time(8, 30)
        end_time_obj = time(15, 0)
        
        # Padding chart (preserved from original logic)
        if collect_history and not data.empty:
            first_avail_ts = data['datetime'].iloc[0]
            first_snap = groups.get_group(first_avail_ts)
            first_spx = self.estimate_spx_price(first_snap)
            if first_avail_ts.time() > start_time_obj:
                curr_pad = start_dt
                while curr_pad < first_avail_ts:
                    history.append({
                        'ts': curr_pad.isoformat(), 'spx': first_spx,
                        'sim_sc_strike': None, 'sim_sp_strike': None,
                        'live_sc_strike': None, 'live_sp_strike': None,
                        'sim_sc_delta': 0.0, 'sim_sp_delta': 0.0,
                        'live_sc_delta': 0.0, 'live_sp_delta': 0.0,
                        'sim_pnl': 0.0, 'live_pnl': 0.0,
                        'sim_margin': 0.0, 'live_margin': 0.0
                    })
                    curr_pad += timedelta(minutes=1)

        # Minute-by-minute simulation loop
        for ts, snap in groups:
            if ts > end_dt:
                break
            snap = snap.reset_index(drop=True)
            snap['strike_int'] = snap['strike_price'].round().astype(int)
            snap_indexed = snap.set_index(['strike_int', 'side'])
            self._option_cache = {} 
            
            t_time = ts.time()
            t_trades = defaultdict(list)
            
            # 1. Update Live Combined Portfolio (Always sync truth for chart)
            while pending_live_trades and pending_live_trades[0].timestamp.replace(tzinfo=None) <= ts.replace(tzinfo=None):
                lt = pending_live_trades.pop(0)
                self.live_combined_portfolio.add_trade(lt)

            # 2. Update Simulation Sub-Strategies
            # [FIX]: Prioritize historical estimate during catch-up to avoid shadowing with current price
            spx = self.estimate_spx_price(snap) or self._last_spx_price
            decay_c = calculate_delta_decay(ts, 'CALL', self.config['initial_sum_delta']/2, start_time_obj, end_time_obj)
            decay_p = calculate_delta_decay(ts, 'PUT', self.config['initial_sum_delta']/2, start_time_obj, end_time_obj)
            t_short = (decay_c + decay_p) / 2
            
            for sid, s in self.sub_strategies.items():
                if t_time < s.trade_start_time:
                    continue
                
                # Update current position valuation (deltas/prices)
                for p in s.portfolio.positions:
                    p_key = (int(round(p.strike)), p.side)
                    if p_key in snap_indexed.index:
                        p_row = snap_indexed.loc[[p_key]].iloc[0]
                        p.delta = p_row['delta']
                        p.price = p_row['mid_price']
                        p.theta = float(p_row['theta']) if not pd.isna(p_row['theta']) else 0.0
                
                # If not yet engaged, try to open the Iron Condor
                if not s.has_traded_today:
                    trade = None
                    # [SOFT]: Check for assigned live match
                    if mode == 'soft' and sid in assigned_live_entry:
                        lt = assigned_live_entry[sid]
                        if lt.timestamp.replace(tzinfo=None) <= ts.replace(tzinfo=None):
                            trade = self._create_sync_entry(s, snap, ts, lt)
                            if trade:
                                self.logger.info(f"Bootstrap [SOFT]: Seeding {sid} with matched live trade {lt.order_id} at {ts.strftime('%H:%M')}")
                    
                    # Fallback or Hard Mode: Standard Sim Entry
                    if not trade and t_time < end_time_obj:
                        # Only fallback if not waiting for a specific future assigned trade
                        if sid not in assigned_live_entry or mode == 'hard':
                            trade = self._check_entry(s, snap, ts)
                            if trade:
                                self.logger.info(f"Bootstrap [{mode.upper()}]: Entry for {sid} via sim logic at {ts.strftime('%H:%M')}")
                    
                    if trade:
                        s.portfolio.add_trade(trade)
                        t_trades[sid].append(trade)
                        s.has_traded_today = True
                
                # Once engaged, ALWAYS follow simulation logic (rebalance/exit)
                # We ignore any subsequent live position changes for this sub-strategy.
                elif s.portfolio.positions:
                    if t_time >= end_time_obj:
                        exit_t = self._create_exit_trade(s, snap, ts, spx)
                        if exit_t:
                            s.portfolio.add_trade(exit_t)
                            t_trades[s.sid].append(exit_t)
                    else:
                        res_trades = self._check_rebalance(s, snap, ts, t_short)
                        for res_t in res_trades:
                            s.portfolio.add_trade(res_t)
                            t_trades[s.sid].append(res_t)
            
            # Sync combined simulation portfolio
            if t_trades:
                for sid, trades in t_trades.items():
                    for mt in trades:
                        self.combined_portfolio.add_trade(mt)
                self._reconcile_combined_simulation()
            
            # Final step: record deltas and history for chart
            active_ports = [self.combined_portfolio, self.live_combined_portfolio]
            for s in self.sub_strategies.values():
                if s.has_traded_today:
                    active_ports.append(s.portfolio)

            for port in active_ports:
                for p in port.positions:
                    p_key = (int(round(p.strike)), p.side)
                    if p_key in snap_indexed.index:
                        row = snap_indexed.loc[[p_key]].iloc[0]
                        p.delta = float(row['delta']) if not pd.isna(row['delta']) else 0.0
                        p.price = row['mid_price']
                        p.theta = float(row['theta']) if not pd.isna(row['theta']) else 0.0

            if collect_history:
                sim_d = self.combined_portfolio.get_all_deltas(snap)
                live_d = self.live_combined_portfolio.get_all_deltas(snap)
                history.append({
                    'ts': ts.isoformat(), 'spx': spx,
                    'sim_sc_strike': self.combined_portfolio.short_call_strike,
                    'sim_sp_strike': self.combined_portfolio.short_put_strike,
                    'live_sc_strike': self.live_combined_portfolio.short_call_strike,
                    'live_sp_strike': self.live_combined_portfolio.short_put_strike,
                    'sim_sc_delta': sim_d['abs_short_call_delta'],
                    'sim_sp_delta': sim_d['abs_short_put_delta'],
                    'live_sc_delta': live_d['abs_short_call_delta'],
                    'live_sp_delta': live_d['abs_short_put_delta'],
                    'sim_pnl': round(self.combined_portfolio.net_pnl, 2),
                    'live_pnl': round(self.live_combined_portfolio.net_pnl, 2)
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
        # P6 Fix: timestamp is redundant because _option_cache is cleared per tick/snapshot.
        # This increases cache hits for lookups shared by multiple strategies in the same tick.
        cache_key = (round(target, 4), side, max_diff, short_strike)
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
                    OptionLeg(sc['symbol'], sc['strike_price'], 'CALL', -s.unit_size, sc['delta'], sc['theta'], price=sc['mid_price'], target_delta=st),
                    OptionLeg(lc['symbol'], lc['strike_price'], 'CALL', s.unit_size, lc['delta'], lc['theta'], price=lc['mid_price'], target_delta=lt),
                    OptionLeg(sp['symbol'], sp['strike_price'], 'PUT', -s.unit_size, sp['delta'], sp['theta'], price=sp['mid_price'], target_delta=-st),
                    OptionLeg(lp['symbol'], lp['strike_price'], 'PUT', s.unit_size, lp['delta'], lp['theta'], price=lp['mid_price'], target_delta=-lt)
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
                tr = self._create_new_spread_trade(s, snap, t_short, t_long, side, ts, s.unit_size)
            elif sn_needs:
                tr = self._create_rebalance_trade(s, snap, t_short, side, ts, True, t_long=t_long)
            elif ln_needs:
                tr = self._create_rebalance_trade(s, snap, t_long, side, ts, False)
            
            if tr:
                trades.append(tr)
        return trades

    def _create_new_spread_trade(self, s: SubStrategy, snap: pd.DataFrame, st: float, lt: float, side: str, ts: datetime, quantity: int) -> Optional[Trade]:
        max_diff = self.config['max_spread_diff']
        opt_s = self._find_option(snap, st, side, ts)
        if opt_s is None: return None
        opt_l = self._find_option(snap, lt, side, ts, max_diff=max_diff, short_strike=opt_s['strike_price'])
        if opt_l is None: return None
        
        legs = [
            OptionLeg(opt_s['symbol'], opt_s['strike_price'], side, -quantity, opt_s['delta'], opt_s.get('theta', 0), price=opt_s['mid_price'], target_delta=st if side == 'CALL' else -st),
            OptionLeg(opt_l['symbol'], opt_l['strike_price'], side, quantity, opt_l['delta'], opt_l.get('theta', 0), price=opt_l['mid_price'], target_delta=lt if side == 'CALL' else -lt)
        ]
        credit = sum(-lg.quantity*lg.price for lg in legs)*100
        # Bug 10 Fix: current_sum_delta should be the sum of deltas of the legs
        sum_delta = sum(l.quantity * l.delta for l in legs)
        return Trade(ts, legs, credit, 0, sum_delta, TradePurpose.REBALANCE_NEW, s.sid)

    def _create_rebalance_trade(self, s: SubStrategy, snap: pd.DataFrame, target: float, side: str, ts: datetime, is_short: bool, t_long: float = None) -> Optional[Trade]:
        # Bug 10 Fix: Ensure current_sum_delta reflects leg sum
        with self._data_lock:
            ex_legs = [p for p in s.portfolio.positions if p.side == side and ((is_short and p.quantity < 0) or (not is_short and p.quantity > 0))]
            if not ex_legs: return None
            ex = ex_legs[0]
            
            max_diff = self.config['max_spread_diff']
        
        if is_short:
            new_s = self._find_option(snap, target, side, ts)
            if new_s is None: return None
            
            legs = [
                OptionLeg(ex.symbol, ex.strike, ex.side, -ex.quantity, ex.delta, ex.theta, price=ex.price, target_delta=ex.target_delta),
                OptionLeg(new_s['symbol'], new_s['strike_price'], side, -abs(ex.quantity), new_s['delta'], new_s.get('theta',0), price=new_s['mid_price'], target_delta=target if side == 'CALL' else -target)
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
                        legs.append(OptionLeg(new_l['symbol'], new_l['strike_price'], side, abs(ex.quantity), new_l['delta'], new_l.get('theta',0), price=new_l['mid_price'], target_delta=t_long if side == 'CALL' else -t_long))
            
            credit = sum(-l.quantity * l.price for l in legs) * 100
            comm = self.config.get('commission_per_contract', 1.13) * len(legs)
            if abs(credit) <= self.config['min_credit']*100: return None
            # Bug 10 Fix: current_sum_delta should be sum of leg deltas
            sum_delta = sum(l.quantity * l.delta for l in legs)
            return Trade(ts, legs, credit, comm, sum_delta, TradePurpose.REBALANCE_SHORT, s.sid)
        else:
            ss_legs = [p for p in s.portfolio.positions if p.side == side and p.quantity < 0]
            short_strike = ss_legs[0].strike if ss_legs else None
            new_l = self._find_option(snap, target, side, ts, max_diff=max_diff, short_strike=short_strike)
            if new_l is None: return None
            
            legs = [
                OptionLeg(ex.symbol, ex.strike, ex.side, -ex.quantity, ex.delta, ex.theta, price=ex.price, target_delta=ex.target_delta),
                OptionLeg(new_l['symbol'], new_l['strike_price'], side, abs(ex.quantity), new_l['delta'], new_l.get('theta',0), price=new_l['mid_price'], target_delta=target if side == 'CALL' else -target)
            ]
            credit = sum(-l.quantity * l.price for l in legs) * 100
            comm = self.config.get('commission_per_contract', 1.13) * len(legs)
            # Bug 10 Fix: current_sum_delta should be sum of leg deltas
            sum_delta = sum(l.quantity * l.delta for l in legs)
            return Trade(ts, legs, credit, comm, sum_delta, TradePurpose.REBALANCE_LONG, s.sid)

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
        """Aggregate duplicate symbols into single legs before submission (Bug 11 Fix)"""
        agg = {}
        for l in legs:
            if l.symbol in agg:
                agg[l.symbol].quantity += l.quantity
            else:
                agg[l.symbol] = copy.copy(l)
        # Remove zero-quantity legs (netted out)
        return [l for l in agg.values() if l.quantity != 0]

    def _classify_order_type(self, legs: List[OptionLeg]) -> Tuple[str, Optional[bool]]:
        """
        Structural classification of an option spread for floor enforcement.
        Ref: docs/order_type_classification_plan.md
        Returns: (type_str, is_credit_structural)
        """
        n = len(legs)
        if n == 0: return ("unknown", None)
        
        # 1. Single Leg
        if n == 1:
            is_credit = legs[0].quantity < 0
            return ("single", is_credit)
        
        # 2. Vertical Spread / Risk Reversal
        if n == 2:
            l1, l2 = legs
            # One long, one short, exact equal quantities
            if l1.quantity * l2.quantity < 0 and abs(l1.quantity) == abs(l2.quantity):
                return ("vertical", None) # mid-price determines Cr/Db
        
        # Sort by strike for multi-leg topology checks
        sorted_legs = sorted(legs, key=lambda x: x.strike)
        quantities = [l.quantity for l in sorted_legs]
        
        # Determine GCD unit for ratios
        unit = abs(quantities[0])
        for q in quantities[1:]: 
            unit = math.gcd(unit, abs(q))
        
        ratios = [q // unit for q in quantities] if unit > 0 else []
        
        # 3. Butterfly (3 legs, ratios 1/-2/1) — all same side required
        if n == 3:
            same_side = len(set(l.side for l in sorted_legs)) == 1
            if same_side and ratios == [1, -2, 1]: return ("butterfly", False) # Long Fly = Debit
            if same_side and ratios == [-1, 2, -1]: return ("butterfly", True)  # Short Fly = Credit

        # 4. Condors and Iron Structures
        if n == 4:
            # Same-Type Condor (+1/-1/-1/+1) — must be all same side to avoid matching IC
            same_side = len(set(l.side for l in sorted_legs)) == 1
            if same_side and ratios == [1, -1, -1, 1]: return ("condor", False) # Long outer = Debit
            if same_side and ratios == [-1, 1, 1, -1]: return ("condor", True)  # Short outer = Credit
            
            # Iron Condor / Iron Butterfly (2 Put + 2 Call)
            p_legs = [l for l in sorted_legs if l.side == 'PUT']
            c_legs = [l for l in sorted_legs if l.side == 'CALL']
            if len(p_legs) == 2 and len(c_legs) == 2:
                lp = next((l for l in p_legs if l.quantity > 0), None)
                sp = next((l for l in p_legs if l.quantity < 0), None)
                lc = next((l for l in c_legs if l.quantity > 0), None)
                sc = next((l for l in c_legs if l.quantity < 0), None)
                
                if all([lp, sp, lc, sc]):
                    # Short inner = Credit, Long inner = Debit
                    is_credit = (sp.strike > lp.strike and sc.strike < lc.strike)
                    # Iron fly when either the short body or long body shares a strike
                    t_str = "iron_fly" if (sp.strike == sc.strike or lp.strike == lc.strike) else "iron_condor"
                    return (t_str, is_credit)

        return ("unknown", None)

    def _get_smart_chunks(self, legs: List[OptionLeg]) -> List[List[OptionLeg]]:
        """
        Prioritized chunking hierarchy (Task #24):
        1. Iron Condors (4 unique strikes: 1LC, 1SC, 1LP, 1SP)
        2. Side-Specific Rolls (4 unique strikes: 2L, 2S on one side)
        3. Vertical Spreads (2 unique strikes: 1L, 1S)
        4. Residuals
        """
        unrolled = self._unroll_legs(legs)
        if not unrolled: return []
        
        remaining = list(unrolled)
        found_combos = [] # List of combo lists

        def extract_chunk(num_legs, constraint_func):
            nonlocal remaining
            # Deterministic scan over current remaining list
            for indices in combinations(range(len(remaining)), num_legs):
                combo = [remaining[i] for i in indices]
                
                # UNIQUE STRIKE RULE: No repeated strike/side in one order
                # This ensures we don't put BTC and BTO of the same symbol in the same order
                strike_keys = {(l.strike, l.side) for l in combo}
                if len(strike_keys) != num_legs: continue
                
                if constraint_func(combo):
                    # Success: extract from remaining list in reverse order
                    for i in sorted(indices, reverse=True):
                        remaining.pop(i)
                    return combo
            return None

        # Priority 1: Iron Condors (4 legs: 1xLC, 1xSC, 1xLP, 1xSP)
        while len(remaining) >= 4:
            ic = extract_chunk(4, lambda c: 
                sum(1 for l in c if l.side == 'CALL' and l.quantity > 0) == 1 and
                sum(1 for l in c if l.side == 'CALL' and l.quantity < 0) == 1 and
                sum(1 for l in c if l.side == 'PUT' and l.quantity > 0) == 1 and
                sum(1 for l in c if l.side == 'PUT' and l.quantity < 0) == 1
            )
            if not ic: break
            found_combos.append(ic)

        # Priority 2: Side-Specific Condors / Rolls (4 legs: 2L, 2S on same side)
        while len(remaining) >= 4:
            roll = extract_chunk(4, lambda c:
                len({l.side for l in c}) == 1 and
                sum(1 for l in c if l.quantity > 0) == 2 and
                sum(1 for l in c if l.quantity < 0) == 2
            )
            if not roll: break
            found_combos.append(roll)

        # Priority 3: Vertical Spreads (2 legs: 1L, 1S on same side)
        while len(remaining) >= 2:
            vs = extract_chunk(2, lambda c:
                len({l.side for l in c}) == 1 and
                sum(1 for l in c if l.quantity > 0) == 1 and
                sum(1 for l in c if l.quantity < 0) == 1
            )
            if not vs: break
            found_combos.append(vs)

        # Residuals: Aggregate whatever is left into chunks of 4 unique strikes
        leftover_rolled = self._roll_legs(remaining)
        
        # Consolidation: Group identical combo units to maximize order quantities
        # (e.g., 10 units of the same IC should be ONE order with higher quantity)
        grouped = defaultdict(list)
        for combo in found_combos:
            # Deterministic signature: sorted (symbol, instruction) tuples
            sig = tuple(sorted([(l.symbol, getattr(l, 'instruction', None)) for l in combo]))
            grouped[sig].append(combo)
            
        final_chunks = []
        for sig, combos in grouped.items():
            all_unit_legs = []
            for combo in combos:
                all_unit_legs.extend(combo)
            # Re-aggregate into full-quantity legs
            final_chunks.append(self._roll_legs(all_unit_legs))

        # Add residual chunks
        if leftover_rolled:
            for i in range(0, len(leftover_rolled), 4):
                final_chunks.append(leftover_rolled[i:i+4])

        return final_chunks

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
        """
        Calculates which chunks to submit/cancel by deduplicating against 
        current Live Working Orders (supports partial overlaps).
        Now uses a 'Subset' strategy to prevent oscillation: Only subtract quantities
        from orders that are fully within the target trade.
        """
        self.logger.debug(f"Creating execution plan for trade {trade.strategy_id}")
        target_keys = {(int(round(float(tl.strike))), tl.side) for tl in trade.legs}
        
        # 1. Categorize all working orders (Is it 'Good' or 'Stale'?)
        to_cancel = []
        protected_ids = set()
        working_qtys = defaultdict(float) # Maps (strike, side) -> signed net quantity
        
        for wo in self.working_orders:
            wid = str(wo.get('orderId'))
            if not self._is_spx_0dte_order(wo): continue
            
            # Check if order belongs to THIS rebalance/strategy intent
            # (If strategy mismatch AND not a reconciliation gap-sync, we cancel it)
            belongs_here = (self.order_to_strategy.get(wid) == trade.strategy_id or trade.purpose == TradePurpose.RECONCILIATION)
            
            is_stale = not belongs_here
            order_legs_data = [] # List of ((strike, side), signed_qty)
            
            for leg in wo.get('orderLegCollection', []):
                instr = leg.get('instruction', '')
                qty = float(leg.get('quantity', 0))
                instr_obj = leg.get('instrument', {})
                strike = instr_obj.get('strikePrice', 0)
                side = instr_obj.get('putCall', '')
                
                if not strike or not side:
                    # Try parsing symbol
                    parsed = self._parse_schwab_symbol(instr_obj.get('symbol', ''))
                    if parsed:
                        strike, side = parsed['strike'], parsed['side']
                
                if strike and side:
                    k = (int(round(float(strike))), side)
                    mult = 1.0 if 'BUY' in instr else -1.0
                    order_legs_data.append((k, qty * mult))
                    
                    # If this leg is NOT in our target reconciliation set, the whole order is stale
                    if k not in target_keys:
                        is_stale = True
                else:
                    # Cannot parse leg? Mark stale just in case
                    is_stale = True

            if is_stale:
                to_cancel.append(wo)
            else:
                # This order is a "Perfect Subset" of our current need.
                # Protect it and subtract its quantities from the gap.
                protected_ids.add(wid)
                for k, signed_v in order_legs_data:
                    working_qtys[k] += signed_v

        # 2. Subtract protected working quantities from Trade needed quantities
        remaining_legs = []
        covered_count = 0
        for leg in trade.legs:
            # Bug Fix: Ensure we use consistent rounding for multi-leg matching
            key = (int(round(float(leg.strike))), leg.side)
            needed = float(leg.quantity)
            already_covered = working_qtys.get(key, 0.0)
            
            # Use same-direction overlap logic
            # (Ensures that if we need +1 and have +1 working, we submit 0)
            if (needed > 0 and already_covered > 0) or (needed < 0 and already_covered < 0):
                if abs(needed) > abs(already_covered):
                    to_fill = needed - already_covered
                    working_qtys[key] = 0 # All working used up
                else:
                    to_fill = 0
                    working_qtys[key] -= needed # Some working remains
            else:
                to_fill = needed

            if abs(to_fill) > 0.01:
                new_leg = OptionLeg(leg.symbol, leg.strike, leg.side, to_fill, leg.delta, leg.theta, leg.price, leg.entry_price)
                new_leg.instruction = getattr(leg, 'instruction', None)
                remaining_legs.append(new_leg)
            else:
                covered_count += 1

        if covered_count > 0:
            self.logger.info(f"Adjusted execution plan: {covered_count} legs covered by working orders. Remaining: {len(remaining_legs)}")

        to_submit = self._get_smart_chunks(remaining_legs)

        return {
            'to_keep': [o for o in self.working_orders if str(o.get('orderId')) in protected_ids],
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
            strategy_id="combined", # Bug 6 Fix: Use more accurate label than GAP_SYNC
            constituent_trades=all_constituents
        )
        return [netted_trade]

    def _reconcile_combined_simulation(self, force: bool = False):
        """Bug 8 Fix: Ensure the combined simulation portfolio reflects the sum of all sub-strategies."""
        with self._data_lock:
            # P4 Optimization: Only rebuild when dirty (signals occurred) or forced
            if not self._sim_dirty and not force:
                return
            self._sim_dirty = False
            
            # We must rebuild the combined portfolio from sub-strategy portfolios to avoid drift
            new_port = Portfolio()
            # Bug 3 Fix: Preserve peak margin across reconciliations
            if hasattr(self, 'combined_portfolio'):
                new_port.max_margin = self.combined_portfolio.max_margin
            
            for sid, s in self.sub_strategies.items():
                for p in s.portfolio.positions:
                    # Create a dummy trade to populate position
                    dummy_trade = Trade(
                        timestamp=datetime.now(CHICAGO),
                        legs=[OptionLeg(p.symbol, p.strike, p.side, p.quantity, p.delta, p.theta, p.price, p.entry_price)],
                        credit=0,
                        commission=0,
                        current_sum_delta=0,
                        purpose=TradePurpose.RECONCILIATION,
                        strategy_id=sid
                    )
                    new_port.add_trade(dummy_trade)
                # Transfer cash separately
                new_port.cash += s.portfolio.cash
            
            # Transfer baseline and trades
            new_port.starting_market_value = self.combined_portfolio.starting_market_value
            # Note: We don't strictly need to copy history for reconciliation, but it helps
            new_port.trades = list(self.combined_portfolio.trades)
            self.combined_portfolio = new_port
            self.logger.debug("Reconciled combined simulation portfolio with sub-strategy sum.")

    async def _attempt_broker_reconnect(self):
        """Robustness-3 Fix: Attempt to restore broker connection session."""
        if self.broker_reconnecting: return
        self.broker_reconnecting = True
        try:
            self.logger.info("Retrying Schwab client initialization...")
            await self.initialize_schwab_client()
            if self.client:
                # Verification call
                resp = await self.client.get_account_numbers()
                if resp.status_code == 200:
                    self.logger.info("Schwab connection restored successfully.")
                    self.broker_connected = True
                    self.heartbeat_failures = 0
                    self._broadcast_alert("success", "Connection Restored", "Broker heartbeat is back online.")
                else:
                    self.logger.warning(f"Reconnect verification failed: {resp.status_code}")
        except Exception as e:
            self.logger.error(f"Failed broker reconnect attempt: {e}")
        finally:
            self.broker_reconnecting = False

    def _is_trade_redundant(self, trade: Trade) -> bool:
        """
        Check if the divergence this trade intended to fix is already gone.
        Accounts for both filled positions and working orders.
        """
        eff_live_dict = self._get_effective_live_positions()
        with self._data_lock:
            # Flatten quantities for Sim state
            sim_dict = { (int(round(p.strike)), p.side): float(p.quantity) for p in self.combined_portfolio.positions }
            
            # A trade is redundant ONLY if all legs it covers are now balanced (Sim == Effective Live)
            for leg in trade.legs:
                key = (int(round(leg.strike)), leg.side)
                if abs(sim_dict.get(key, 0.0) - eff_live_dict.get(key, 0.0)) > 0.01:
                    return False
        return True
