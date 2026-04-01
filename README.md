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

### 2. Production Deployment (Both VMs)
Deployment is now unified across both the Primary and Backup VMs. Use the `full_deploy.sh` script which handles Git commits, multi-VM synchronization, and service restarts:

```bash
# Unified Git Commit, Sync, and Restart
./deploy/full_deploy.sh "Your commit message here"
```

The script will:
1.  **Commit** any uncommitted local changes to Git.
2.  **Archive** the project for deployment (excluding logs/DBs).
3.  **Detect** which VMs are running (`production-server` and `production-server-backup`).
4.  **Upload** code to all active VMs and **Restart** the services.

---

## 🔒 Security & Connectivity
- **Tailscale Only**: The dashboard is only accessible via the Tailscale VPN.
- **No Secrets**: Configuration uses `config.py` which is ignored by Git. Always use `config.example.py` as a template.
- **Health Checks**: A lightweight endpoint on `:8081` provides zero-latency heartbeats for external monitoring tools.
