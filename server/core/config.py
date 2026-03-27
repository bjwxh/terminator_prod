# live/terminator/config.py

import os
from datetime import time

CONFIG = {
    # === Strategy Parameters ===
    'init_wing_delta': 0.16,           # How far OTM the long legs are
    'initial_sum_delta': 0.35,         # Target delta for short legs (both sides combined)
    'rebalance_threshold': 0.075,      # Rebalance when short delta drifts this much
    'long_leg_rebalance_delta_threshold': 0.13,  # Rebalance long leg threshold
    'min_credit': 0.05,                # Minimum credit to accept for a trade
    'min_long_delta': 0.025,           # Minimum delta for long legs
    'max_spread_diff': 50,             # Maximum width between short and long strikes

    # === Timing ===
    'start_time': '08:30:00',          # Market open
    'end_time': '15:00:00',            # Market close / exit time
    'check_interval_minutes': 5 / 60,  # Strategy/Pricing check every 5 seconds (Enhancement)
    'portfolio_start_time': '09:00:00', # First sub-strategy start time
    'portfolio_end_time': '11:00:00',   # Last sub-strategy start time
    'portfolio_interval_minutes': 60,   # Time between sub-strategies (can be 1-30)
    'heartbeat_interval_seconds': 5,     # Broker connection check frequency

    # === Live Trading Specific ===
    'trading_enabled': False,          # SAFETY FIRST: Disable by default in prod config
    'commission_per_contract': 1.13,
    'order_offset': 0.1,              # Price offset from mid (configurable)
    'order_auto_execute_timeout': 20,  # Seconds before auto-execute
    'trade_batching_window_seconds': 0, # Delay before reconciliation sync. 0 = instant.

    # === Persistence ===
    'session_file_path': 'session_state.json', # Local to working dir
    'db_path': 'server/market_data.db', # Path to SQLite DB for EOD/reconciliation
    'bootstrap_mode': 'soft', # 'soft' = sync sim entry with live IC; 'hard' = pure logic entry

    # === Email Alerts (configurable) ===
    'email_alerts_enabled': True,
    'email_recipients': ['frankwang.alert@gmail.com'],
    'email_config_path': os.path.expanduser('~/.api_keys/gmail/fw_trd_key.json'),
    
    # === Push Notifications (ntfy.sh) ===
    'ntfy_enabled': True,
    'ntfy_topic': '', # SET THIS IN LOCAL config.py (don't commit secrets)

    # === Schwab API ===
    'account_id': '22229895', # SLI account
    'credentials_file': os.path.expanduser('~/.api_keys/sli_api.json'),
    'token_file': os.path.expanduser('~/.api_keys/sli_token.json'), # Absolute path for VM
}
