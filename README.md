# 🤖 Terminator Production Trading Server

**Terminator** is a high-frequency, production-grade 0DTE options trading system. It integrates Schwab's API for live execution with a real-time web dashboard for strategy monitoring and manual trade oversight.

---

## 🚀 Key Features

### 📊 Real-Time Web Dashboard
- **Modern Responsive UI**: Dynamic grid layout that adjusts for any screen size (desktop to mobile-narrow).
- **Dual Portfolio Monitoring**: Parallel tracking of **Simulated** vs. **Live** portfolios with key metrics (Margin, Net PnL, Delta, Theta, Fees).
- **Sticky Greeks Architecture**: A decoupled sync system ensuring live Greeks (Delta/Theta) remain stable and accurate by separating 5s position refreshes from 30s market snapshots.
- **Foldable Controls**: One-click folding/unfolding of portfolio windows to clear clutter during complex sessions.

### 🛡️ Production Safety & Confirmation
- **Interactive Trade Modal**: High-visibility confirmation window for all live strategy executions.
- **Smart Countdown Timer**: 10-second auto-send countdown with visual urgency alerts (Orange at 6s, Red at 3s).
- **Pause & Override**: Immediate "Pause Timer" and "Dismiss Trade" controls to prevent unwanted executions.
- **Multi-Order Support**: Seamlessly handles strategies split across multiple broker orders (e.g., 6-leg splits) with aggregated pricing summaries.

### 🛠 Core Infrastructure
- **Unified Logic**: Shared monitor for catch-up simulations and live execution.
- **Schwab API Integration**: Native support for Schwab's asynchronous client with automatic heartbeat monitoring and VPN/DNS resilience.
- **Persistent Sessions**: Automated session state saving/loading via `SessionManager`.

---

## 🎨 Interface Showcase

<table border="0">
  <tr>
    <td width="50%"><img src="images/ui/dashboard.png" alt="Dashboard Tab"></td>
    <td width="50%"><img src="images/ui/substrategies.png" alt="Sub-Strategies Tab"></td>
  </tr>
  <tr>
    <td width="50%"><img src="images/ui/sessionstats.png" alt="Session Stats Tab"></td>
    <td width="50%"><img src="images/ui/systemlogs.png" alt="System Logs Tab"></td>
  </tr>
</table>

---

## 📂 Project Structure

```
terminator_prod/
│
├── server/                        # Production Server logic
│   ├── main.py                    # Entry point: FastAPI + Monitor Loop
│   ├── api/
│   │   ├── routes.py              # REST REST endpoint handlers
│   │   └── ws.py                  # Real-time WebSocket broadcaster
│   ├── core/                      # Trading Engine
│   │   ├── monitor.py             # LiveTradingMonitor (The Brain)
│   │   ├── models.py              # Portfolio & Option data classes
│   │   ├── config.py              # Configuration & Risk params
│   │   └── utils.py               # Spreading & Pricing logic
│   └── static/                    # Frontend Dashboard
│       ├── index.html             # UI Structure
│       ├── style.css              # Premium Dark Mode Theme
│       └── app.js                 # UI Logic & Modal Handling
│
├── deploy/                        # Infrastructure
│   └── terminator.service         # systemd unit for GCE
│
└── data/                          # Runtime logs, DBs, and session files
```

---

## 🚦 Getting Started

### 1. Local Development
1. **Initialize Environment**:
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Setup Credentials**:
   Ensure Schwab API keys are in `~/.api_keys/schwab/schwab_api.json`.
3. **Launch Terminal**:
   ```bash
   # Run the server locally on port 9000
   python3 server/main.py
   ```

### 2. Production Deployment (Failover-Ready)
The system uses a **GitHub Pull-based** deployment model. Code is automatically synchronized upon VM startup or service restart.

1.  **Push Updates**: Commit and push your local changes to GitHub:
    ```bash
    git add .
    git commit -m "Your update message"
    git push origin main
    ```
2.  **Automated Sync**: The VMs (`production-server` or `production-server-sc`) pull the latest code automatically every morning at startup (08:15 AM Chicago) via the `fetch_secrets.sh` pre-start script.
3.  **Manual Refresh**: If the VM is already running and you need to deploy an immediate fix:
    ```bash
    ./deploy/sync_keys.sh
    ```
    *This script syncs your latest Schwab tokens and restarts the service, which triggers an immediate `git pull` on the VM.*


---

## 🔒 Security & Connectivity
- **Tailscale Only**: The dashboard is only accessible via the Tailscale VPN.
- **No Secrets**: Configuration uses `config.py` which is ignored by Git. Always use `config.example.py` as a template.
- **Health Checks**: A lightweight endpoint on `:8081` provides zero-latency heartbeats for external monitoring tools.


---
<!-- STATS_START -->

## 📈 Performance Statistics

![Performance Curve](eod/assets/performance_curve.png)

| Period | Days | Total PnL | Trades/Day | Fees | Net PnL | Win % | Profit Factor | Sharpe | Max DD | Live OverTrade % | Live-Sim Net PnL Diff |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Past 5D | 5 | $-1,975.00 | 14.2 | $221.48 | **$-2,196.48** | 80.0% | 0.55 | -2.73 | $-4,933.62 | -21.1% | $-1,590.38 |
| Past 22D | 22 | $-1,717.50 | 14.5 | $1,083.67 | **$-2,801.17** | 63.6% | 0.71 | -1.51 | $-4,933.62 | -17.3% | $-3,912.47 |
| YTD | 65 | $3,275.00 | 20.2 | $2,273.56 | **$1,001.44** | 58.5% | 1.07 | 0.30 | $-4,933.62 | -13.9% | $-6,119.74 |
| All Time | 65 | $3,275.00 | 20.2 | $2,273.56 | **$1,001.44** | 58.5% | 1.07 | 0.30 | $-4,933.62 | -13.9% | $-6,119.74 |


*Updated: 2026-04-28 16:00:07*

<!-- STATS_END -->