# Terminator Rust Application

This directory contains the core Rust-based SPX option trading backend for the Terminator trading system.

## System Architecture Overview

The system is deployed on AWS in the `us-east-2` (Ohio) region to minimize pricing stream latency.

```
                  ┌───────────────────────┐
                  │ AWS Secrets Manager   │
                  │ (Credentials & Token) │
                  └───────────┬───────────┘
                              │ Daily 8:00 AM Sync
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Live Trading EC2 Instance                    │
│                                                                 │
│  ┌──────────────────────────┐     ┌──────────────────────────┐  │
│  │    Terminator Backend    │     │  Option Data Downloader  │  │
│  │       (Rust App)         │     │     (Python Script)      │  │
│  │   [8:25 AM - 3:15 PM]    │     │   [8:25 AM - 3:15 PM]    │  │
│  └────────────┬─────────────┘     └────────────┬─────────────┘  │
│               │                                │                │
│               ▼                                ▼                │
│       Schwab Trading API               Schwab MD Feed           │
└───────────────────────┬────────────────────────┬────────────────┘
                        │                        │
                        └───────────┬────────────┘
                                    ▼
                          Schwab Brokerage API
```

### Components

1. **Rust Trading Backend (`terminator_rust`)**: 
   * Handles real-time option pricing subscription, BSM greeks calculations, grid strategy management, and order placement/cancellation.
   * Exposes a Web UI on port `8081` for monitoring portfolio status.
2. **Python Option Data Downloader (`server/downloader/downloader.py`)**:
   * Pulls option chain data periodically during market hours and inserts them into a local SQLite database (`market_data.db`).

---

## Production Deployment Specifications (AWS `us-east-2`)

### 1. Credentials Synchronization
Schwab API keys and tokens are stored in AWS Secrets Manager under `terminator_prod/schwab_credentials` and synced locally daily:
* **Script**: `/opt/terminator/sync_credentials.py`
* **Local Paths**: 
  * `~/.api_keys/schwab/sli_api.json`
  * `~/.api_keys/schwab/sli_token.json`
* **Trigger**: Scheduled by the `schwab-sync.timer` systemd timer at **8:00 AM on Weekdays** (Chicago time).

### 2. Market-Hours Scheduler
Trading processes run only when the NYSE market is open:
* **Market Checker**: `/opt/terminator/is_market_open.py` evaluates if the current day is an active trading day (filtering weekends and US federal/NYSE holidays).
* **Start Timer (`terminator-start.timer`)**: Fires daily at **8:25 AM Chicago time** (America/Chicago). Starts the trading services only if the market is open.
* **Stop Timer (`terminator-stop.timer`)**: Fires daily at **3:15 PM Chicago time** (America/Chicago). Stops the services.

### 3. VPN & Security
The live trading server runs Tailscale VPN. Public access to port `8081` (Web UI) is blocked, and access is only permitted securely over your Tailscale tailnet.
