import os

# Copy this file to config.py on the server and fill in the blanks
CONFIG = {
    # === Strategy Parameters ===
    'init_wing_delta': 0.16,
    'initial_sum_delta': 0.35,
    'rebalance_threshold': 0.075,
    'long_leg_rebalance_delta_threshold': 0.13,
    'min_credit': 0.05,
    'min_long_delta': 0.025,
    'max_spread_diff': 50,

    # === Timing ===
    'start_time': '08:30:00',
    'end_time': '15:00:00',
    'check_interval_minutes': 0.5,
    'portfolio_start_time': '09:00:00',
    'portfolio_end_time': '11:00:00',
    'portfolio_interval_minutes': 60,
    'heartbeat_interval_seconds': 5,

    # === Live Trading Specific ===
    'trading_enabled': False,
    'commission_per_contract': 1.13,
    'order_offset': 0.1,
    'order_auto_execute_timeout': 20,
    'trade_batching_window_seconds': 0,

    # === Persistence ===
    'session_file_path': 'session_state.json',
    'db_path': '/dev/null',

    # === Email Alerts ===
    'email_alerts_enabled': True,
    'email_recipients': ['your-email@example.com'],
    'email_config_path': os.path.expanduser('~/.api_keys/gmail/fw_trd_key.json'),
    
    # === Push Notifications (ntfy.sh) ===
    'ntfy_enabled': True,
    'ntfy_topic': 'your-secret-ntfy-topic',

    # === Schwab API ===
    'account_id': 'YOUR_ACCOUNT_ID',
    'credentials_file': os.path.expanduser('~/.api_keys/sli_api.json'),
    'token_file': 'sli_token.json',
}
