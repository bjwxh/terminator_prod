# SPT v4 — Production Deployment Plan

**Status:** Planning
**Last Updated:** 2026-03-18
**Target:** GCP VM (us-central1) + Web UI accessible via Tailscale VPN
**Production Repo:** `~/Git/terminator_prod` (separate from this research/sandbox repo)

---

## 1. Overview

The production system splits the monolithic desktop app into two components:

| Component | Where | What |
|-----------|-------|------|
| **Trading Server** | GCP VM (us-central1) | Headless `LiveTradingMonitor` + FastAPI/WebSocket API |
| **Web UI** | Browser (MacBook, iPhone) | Dashboard replacing tkinter GUI, accessed via Tailscale VPN |
| **EOD Report** | MacBook (local) | Pulls session file from VM, runs existing `eod_report.py` locally |
| **Push Notifications** | ntfy.sh (cloud) | Trade fills, errors, and alerts pushed to iPhone and MacBook |

Trading logic is **unchanged** — the same `monitor.py`, `models.py`, `session_manager.py` code runs on the server.

---

## 2. Repository & Folder Structure

### 2.1 Repo Separation

The current `terminator` repo is a **research sandbox** — it contains experiments, one-off scripts, historical data analysis, legacy strategies, and in-progress work. Production code must never live there.

`terminator_prod` is a **separate Git repo** with a separate remote, containing only what is needed to run the live trading system. This isolation means:

- No accidental deployment of research/debug code
- Clean git history focused on production changes
- Separate access controls if ever needed (e.g., deploy keys on VM only have access to `terminator_prod`)
- `terminator` can continue evolving freely without risk of breaking production

```
~/Git/
├── terminator/          ← research sandbox (this repo, stays as-is)
└── terminator_prod/     ← production repo (new, separate remote)
```

### 2.2 Production Folder Structure

```
terminator_prod/
│
├── server/                        # Runs on GCP VM
│   ├── main.py                    # Entry point: FastAPI + monitor in shared asyncio loop
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py              # All REST endpoint handlers
│   │   └── ws.py                  # WebSocket broadcaster (500ms push to clients)
│   ├── core/                      # Trading logic — ported from terminator/live/spt_v4
│   │   ├── __init__.py
│   │   ├── monitor.py             # LiveTradingMonitor (unchanged logic)
│   │   ├── models.py              # OptionLeg, Trade, Portfolio, SubStrategy
│   │   ├── config.py              # All config parameters
│   │   ├── session_manager.py     # Session save/restore
│   │   └── utils.py               # Delta decay model
│   ├── notifications.py           # send_email() + send_push() (ntfy.sh) helpers
│   └── static/                    # Web UI — served directly by FastAPI
│       ├── index.html
│       ├── app.js                 # WebSocket client + all UI logic
│       └── style.css              # Dark theme, mobile responsive
│
├── eod/                           # Runs on MacBook
│   ├── eod_report.py              # Ported from terminator/live/spt_v4/eod_report.py
│   └── run_eod.sh                 # scp session from VM → run eod_report.py locally
│
├── deploy/                        # Infrastructure / ops
│   ├── spt_v4.service             # systemd unit file (copy to /etc/systemd/system/ on VM)
│   ├── setup_vm.sh                # One-time VM provisioning script
│   └── update.sh                  # Rolling update: git pull + systemctl restart
│
├── config.example.py              # Config template with placeholders — safe to commit
├── .gitignore                     # Excludes: *.json tokens, session_state*, *.log, __pycache__
├── requirements.txt
└── README.md                      # Setup instructions, daily ops runbook
```

### 2.3 What Is NOT in `terminator_prod`

| Excluded | Reason |
|----------|--------|
| Research scripts (`research/`, `eod/backup/`, `tmp/`) | Sandbox only |
| Option chain DB (`data/option_chain.db`) | Not needed on VM; lives on MacBook |
| Downloader (`eod/downloader/`) | Not needed on VM |
| Test suite from terminator | Kept in sandbox; prod has smoke tests only |
| Session archive files (`session_state_MMDD.json`) | Runtime artifacts, gitignored |
| Credentials (`sli_api.json`, `sli_token.json`, `fw_trd_key.json`) | Never in git |
| `v4_eod_history.csv` | Runtime artifact, gitignored; lives on MacBook |

### 2.4 Secrets Management

No secrets ever touch git. On the VM, credentials live at:

```
~/.api_keys/
├── sli_api.json           # Schwab OAuth client credentials
├── sli_token.json         # Schwab OAuth token (auto-refreshed)
└── gmail/
    └── fw_trd_key.json    # Gmail SMTP OAuth for email alerts
```

`config.py` references these paths. The `config.example.py` in the repo shows the paths as placeholders.

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────┐
│                  GCP VM  (us-central1)              │
│                                                     │
│   ┌──────────────────────────────────────────────┐  │
│   │           spt_v4_server.service  (systemd)   │  │
│   │                                              │  │
│   │   FastAPI app (port 8080)                    │  │
│   │   ├── /api/*       REST endpoints            │  │
│   │   ├── /ws          WebSocket (real-time)     │  │
│   │   └── LiveTradingMonitor  (asyncio)          │  │
│   │         same monitor.py / models.py          │  │
│   └──────────────────────────────────────────────┘  │
│                                                     │
│   Tailscale: listens on tailnet interface only      │
│   No public IP exposure                             │
└─────────────────────────────────────────────────────┘
         │  Tailscale VPN (encrypted)
         │
   ┌─────┴──────────────────────────────────┐
   │  Clients (browser on tailnet)          │
   │  ├── MacBook  →  http://vm:8080        │
   │  └── iPhone   →  http://vm:8080        │
   └────────────────────────────────────────┘

   MacBook (EOD, local):
   ├── cron: pull session_state.json from VM via scp
   └── run eod_report.py --session <pulled file>
```

---

## 4. GCP VM Specification & Reliability

### 4.1 VM Configuration

| Setting | Value | Reason |
|---------|-------|--------|
| Region | `us-central1` (Iowa) | Geographically closest GCP region to Schwab's trading infrastructure in Chicago (~5ms RTT vs ~20-40ms from a home MacBook) |
| Zone | `us-central1-a` | Mature, stable zone; avoid newer zones (-f) which have less operational history |
| Machine type | `n2-standard-2` (2 vCPU, 8 GB RAM) | See §4.2 |
| OS | Ubuntu 22.04 LTS | Long-term support until 2027; well-supported by GCP, Python, systemd |
| Boot disk | 50 GB SSD Persistent Disk (pd-ssd) | Fast disk I/O for session state writes on every trade; SSD avoids latency spikes from standard HDD |
| External IP | None | No public internet exposure; all access via Tailscale |
| Preemptible / Spot | **No** | Spot VMs can be evicted mid-trade with 30s notice — never acceptable for a live trading system |
| Firewall | Default-deny all ingress | Tailscale handles secure tunneled access; no GCP firewall rules opened |

### 4.2 Why `n2-standard-2` and Not `e2`

The `e2` series (e.g., `e2-standard-2`) uses shared-core, burstable CPU allocation. Under sustained load or during burst periods on the shared host, CPU can be throttled — introducing unpredictable latency spikes in the monitoring loop, order execution, and broker API calls.

The `n2-standard-2` uses dedicated vCPUs with consistent, non-burstable performance. For a trading system where the 30-second monitoring cadence and order submission latency directly affect fill prices, CPU consistency matters more than raw cost savings. The price difference is minimal (~$50/month vs ~$35/month).

| | `e2-standard-2` | `n2-standard-2` (chosen) |
|-|-----------------|--------------------------|
| vCPU | 2 (shared, burstable) | 2 (dedicated) |
| RAM | 8 GB | 8 GB |
| CPU consistency | Variable (can throttle) | Consistent |
| Approx cost | ~$35/month | ~$50/month |
| Suitable for trading | No | **Yes** |

### 4.3 Resource Requirements

The trading server is primarily **I/O bound**, not CPU bound. The dominant operations are:

- Async HTTP calls to Schwab API (option chain fetch, order submit) — network I/O
- JSON serialization/deserialization of session state — disk I/O
- In-memory Greek calculations across ~10 legs — negligible CPU

Observed memory profile from the current local system: the monitor process uses ~200-400 MB RAM during a trading day. 8 GB RAM provides 20x headroom — sufficient for the OS, Python runtime, FastAPI, and the monitor with room to spare.

### 4.4 GCP SLA & Uptime

GCP Compute Engine single-instance SLA: **99.5% monthly uptime** (≈3.6 hours downtime/month in the worst case). In practice, well-maintained VMs in mature zones run for months without interruption.

For a trading system that only operates during market hours (08:30–15:00 ET, Mon–Fri), the effective exposure window is ~32.5 hours/week. Planned GCP maintenance events are announced in advance and can be controlled via maintenance policies.

**Mitigation for VM outages:**
- systemd `Restart=on-failure` handles process crashes within seconds
- If the VM itself goes down, the `session_state.json` on the SSD persists across reboots; trading resumes automatically when the VM restarts
- The existing broker reconciliation loop syncs live positions on startup, handling any fills that occurred during downtime
- For extended VM outages during market hours: fall back to the local MacBook setup (still maintained and runnable)

### 4.5 No Option Chain DB on VM

The VM does **not** run the option chain downloader or maintain a local SQLite DB. All live pricing data comes directly from the Schwab API (`get_option_chain()` called every 30 seconds). On mid-day restart, the historical catch-up simulation is skipped (a warning is logged); live trading resumes immediately from `session_state.json`. The option chain DB lives on the MacBook for EOD reporting only.

---

## 5. Server Architecture

### 5.1 Process Model

```
main_server.py
└── asyncio event loop
    ├── LiveTradingMonitor  (existing monitor.py, unchanged)
    │   ├── _monitoring_loop()          30s cadence
    │   ├── _broker_sync_loop()          5s cadence
    │   ├── _reconciliation_loop()      30s cadence
    │   └── _broker_heartbeat_loop()    5s cadence
    └── FastAPI app (uvicorn, async)
        ├── REST endpoints
        └── WebSocket broadcaster
```

The monitor and the FastAPI app share the same asyncio event loop — no threads, no IPC overhead. The WebSocket broadcaster pushes state snapshots to connected clients every 500ms (same cadence as the tkinter refresh).

### 5.2 API Design

#### REST Endpoints

| Method | Path | Action |
|--------|------|--------|
| `GET` | `/api/status` | Full system status snapshot (JSON) |
| `POST` | `/api/monitor/start` | Start the trading monitor |
| `POST` | `/api/monitor/stop` | Stop the trading monitor (graceful) |
| `POST` | `/api/trading/enable` | Enable live order execution |
| `POST` | `/api/trading/disable` | Disable live order execution |
| `GET` | `/api/portfolio` | Combined portfolio positions |
| `GET` | `/api/strategies` | All sub-strategies with legs and PnL |
| `GET` | `/api/orders/working` | Currently working broker orders |
| `GET` | `/api/trades` | Full trade history for session |
| `POST` | `/api/orders/manual` | Submit manual order (same as tkinter order window) |
| `POST` | `/api/orders/{order_id}/cancel` | Cancel a working order |
| `GET` | `/api/session` | Download current session_state.json (for EOD) |

#### WebSocket `/ws`

Pushes a JSON state diff every 500ms:
```json
{
  "ts": "2026-03-18T09:30:05.123Z",
  "status": "Running",
  "broker_connected": true,
  "trading_enabled": true,
  "live_pnl": 412.50,
  "sim_pnl": 387.20,
  "positions": [...],
  "strategies": [...],
  "working_orders": [...],
  "heartbeat_failures": 0
}
```

Clients reconnect automatically on disconnect (exponential backoff, cap 30s).

### 5.3 Key Files

```
live/spt_v4/
├── server/
│   ├── main_server.py       # Entry point: FastAPI + monitor in shared event loop
│   ├── api_routes.py        # All REST route handlers
│   ├── ws_broadcaster.py    # WebSocket state broadcaster
│   └── static/             # Web UI (HTML/CSS/JS, served by FastAPI)
│       ├── index.html
│       ├── app.js
│       └── style.css
```

Existing files (`monitor.py`, `models.py`, `config.py`, `session_manager.py`, `eod_report.py`) are **not modified**.

---

## 6. Web UI

### 5.1 Tech Stack

| Layer | Choice | Reason |
|-------|--------|--------|
| Served by | FastAPI `StaticFiles` | Single process, no separate web server |
| Frontend | Vanilla JS + WebSocket | No build step, works on iPhone Safari, zero dependencies |
| Styling | CSS (dark theme matching current tkinter theme) | Lightweight, mobile responsive |

No React/Node — keeps the stack simple and deployable without a build pipeline.

### 5.2 UI Sections (mirrors tkinter tabs)

| Section | Content |
|---------|---------|
| **Header** | Status pill, broker connection dot, live/sim PnL, start/stop button, trading enable toggle |
| **Portfolio** | Combined position table (strike, side, qty, delta, theta, price), total margin, net PnL |
| **Strategies** | Accordion per sub-strategy: legs, entry time, credits, rebalance count |
| **Working Orders** | Table of pending orders with cancel button per order |
| **Trade History** | Scrollable log of all executed trades with purpose, legs, credit |
| **Manual Order** | Form matching existing order_window.py: symbol picker, leg builder, limit price, confirm |

### 5.3 Mobile (iPhone)

- Responsive layout: single column on narrow screens
- Collapsible sections
- Large tap targets for order confirm/cancel buttons

---

## 7. Schwab OAuth on VM

The initial OAuth flow requires a browser. The workflow:

1. **Locally (MacBook):** Run `python -c "import schwab; schwab.auth.client_from_login_flow(...)"` to authorize and generate `sli_token.json`
2. **Upload to VM:** `scp sli_token.json user@vm-tailscale-ip:~/spt_v4/`
3. **VM auto-refresh:** schwab-py handles token refresh transparently on every API call
4. **Token expiry:** Schwab tokens typically last 7 days. If the refresh fails (network blip, Schwab outage), the monitor triggers its existing error alert (email + sound) and pauses trading. Re-authorize locally and re-upload.

> **Note:** `sli_api.json` (OAuth client credentials) also needs to be on the VM. Store it at `~/.api_keys/` matching the path in `config.py`.

---

## 8. EOD Report Flow

```
[EOD trigger — MacBook cron at 15:15 ET]
  │
  ├── scp vm-tailscale-ip:~/spt_v4/session_state.json /tmp/vm_session_$(date +%Y%m%d).json
  │
  └── python eod_report.py --session /tmp/vm_session_$(date +%Y%m%d).json
        │
        ├── Runs simulation against local option_chain.db
        ├── Reads live results from pulled session file
        ├── Generates PDF report
        └── Emails to frankwang.alert@gmail.com
```

The `--session` flag is already supported by `eod_report.py`. The MacBook keeps its existing downloader and DB unchanged.

**MacBook cron entry** (add to `crontab -e`):
```cron
15 15 * * 1-5 /Users/fw/Git/terminator/live/autorun/run_eod_report.sh
```

**`run_eod_report.sh`** (new):
```bash
#!/bin/bash
set -e
SESSION_DATE=$(date +%Y%m%d)
VM_HOST="spt-vm"  # Tailscale hostname or IP
scp $VM_HOST:~/spt_v4/session_state.json /tmp/vm_session_$SESSION_DATE.json
cd /Users/fw/Git/terminator
python live/spt_v4/eod_report.py --session /tmp/vm_session_$SESSION_DATE.json
```

---

## 9. Push Notifications

**Service:** [ntfy.sh](https://ntfy.sh) (hosted, free tier — no self-hosting complexity needed)

**How it works:**
- VM posts an HTTP request to `https://ntfy.sh/<your-secret-topic>` (topic acts as a secret — use a long random string)
- iPhone: install the **ntfy** app → subscribe to your topic → gets APNs push notifications
- MacBook: ntfy app (macOS) or Safari web push on `ntfy.sh` → subscribe to same topic

**Events that trigger a push:**

| Event | Current behavior | With push |
|-------|-----------------|-----------|
| Trade filled | Email + sound | Email + push to iPhone/MacBook |
| Broker heartbeat failure | Email + sound | Email + push |
| Schwab token expiry | Email | Email + push |
| Monitor error / crash | Email | Email + push |
| Monitor started/stopped | None | Push only |

**Integration point in `monitor.py`:** A thin `send_push()` helper wraps an async `httpx.post()` to ntfy. It's called wherever the existing `send_email_alert()` is called, plus on start/stop events. No third-party SDK needed — just an HTTP POST.

**Config additions to `config.py`:**
```python
ntfy_topic: str = ""            # e.g. "spt-v4-abc123xyz" — keep secret
ntfy_enabled: bool = True
```

**Security note:** The ntfy topic name is the only secret. Use a 20+ character random string. Traffic from VM → ntfy.sh goes over public internet (HTTPS). This is acceptable since no credentials or order details are in the push body — only event summaries like "Trade filled: IC +$2.15".

---

## 10. Security (Network & Credentials)

| Layer | Mechanism |
|-------|-----------|
| Network access | Tailscale VPN only — no GCP firewall rules needed, no public IP |
| Web UI auth | Tailscale handles identity — only tailnet devices can reach the VM |
| API credentials | `sli_api.json`, `sli_token.json` stored in `~/.api_keys/` on VM, not in git |
| Email credentials | `~/.api_keys/gmail/fw_trd_key.json` uploaded to VM separately |
| SSH access | Tailscale SSH (no open port 22 on public internet) |

---

## 11. VM Setup & Deployment

### 9.1 One-Time VM Setup

```bash
# On VM
sudo apt update && sudo apt install -y python3.11 python3-pip python3-venv git

# Install Tailscale
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Clone repo
git clone https://github.com/fw/terminator.git ~/terminator
cd ~/terminator

# Python env
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install fastapi uvicorn

# Credentials (copy from local)
mkdir -p ~/.api_keys/gmail
scp local-machine:~/.api_keys/gmail/fw_trd_key.json ~/.api_keys/gmail/
scp local-machine:~/path/to/sli_api.json ~/.api_keys/
scp local-machine:~/path/to/sli_token.json ~/terminator/live/spt_v4/
```

### 9.2 Systemd Service

**`/etc/systemd/system/spt_v4.service`:**
```ini
[Unit]
Description=SPT v4 Trading Server
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=fw
WorkingDirectory=/home/fw/terminator/live/spt_v4
ExecStart=/home/fw/terminator/.venv/bin/python server/main_server.py
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable spt_v4
sudo systemctl start spt_v4
```

**Restart behavior:** `Restart=on-failure` with 30s delay. On restart, `session_manager.py` restores session from `session_state.json` and trading resumes. Catch-up simulation is skipped (no DB on VM) — live trading proceeds immediately from restored state.

### 9.3 Deployment Updates

```bash
# On VM
cd ~/terminator
git pull
sudo systemctl restart spt_v4
```

---

## 12. Reliability & Monitoring

| Concern | Mitigation |
|---------|-----------|
| Process crash | systemd `Restart=on-failure`, 30s delay |
| Mid-day restart | Session restored from `session_state.json`; live trading resumes; broker reconciliation syncs positions |
| Session corruption | **Atomic writes**: `session_manager.py` writes to `.tmp` then renames to avoid corruption on crash |
| Schwab token expiry | Email + push alert fires; re-authorize on MacBook, upload new token |
| Network blip | Existing `run_broker_heartbeat()` handles reconnection; existing failure counter + alert |
| GCP VM reboot | systemd `WantedBy=multi-user.target` auto-starts on boot |
| Log loss | **Persistence**: Daily cron archives journald logs to `~/logs/archive/` for auditing |
| Disk space | Session JSON + logs only (~MB/day); no DB on VM |
| Latency | us-central1 → Schwab Chicago: ~5ms vs MacBook ~20-40ms (significant improvement) |

---

## 13. Implementation Phases

### Phase 0 — Repo Bootstrap (half day)
- [ ] `git init ~/Git/terminator_prod` and create remote on GitHub (private)
- [ ] Copy `core/` files from `terminator/live/spt_v4/` (`monitor.py`, `models.py`, `config.py`, `session_manager.py`, `utils.py`)
- [ ] Create `requirements.txt`, `.gitignore`, `config.example.py`, `README.md`
- [ ] Verify no secrets or research artifacts are included

### Phase 1 — Server Wrapper (1-2 days)
- [ ] Create `server/main.py`: run `LiveTradingMonitor` headless in asyncio, expose basic `/api/status` endpoint
- [ ] Verify monitor runs correctly without tkinter (remove GUI dependencies from startup path)
- [ ] Test on local machine first

### Phase 2 — Full API Layer (2-3 days)
- [ ] Implement all REST endpoints in `server/api_routes.py`
- [ ] Implement WebSocket broadcaster in `server/ws_broadcaster.py`
- [ ] Expose session download endpoint (`GET /api/session`)
- [ ] Add `send_push()` helper (`httpx` POST to ntfy.sh) and wire into all alert sites in `monitor.py`
- [ ] Disable `pygame` sound in config; verify email + push covers all alert cases

### Phase 3 — Web UI (3-4 days)
- [ ] Build `static/index.html` + `app.js` with WebSocket client
- [ ] Implement all sections: portfolio, strategies, working orders, trade history
- [ ] Implement manual order form
- [ ] Mobile responsive CSS

### Phase 4 — VM Setup & EOD (1 day)
- [ ] Provision GCP VM, install Tailscale, clone repo
- [ ] Upload credentials, configure systemd service
- [ ] Write and test `run_eod_report.sh` on MacBook
- [ ] Set up MacBook cron for EOD pull

### Phase 5 — Paper Trading on VM (1 week)
- [ ] Run with `trading_enabled = False` for 1 full week
- [ ] Verify Web UI matches tkinter output exactly
- [ ] Verify EOD report runs correctly from pulled session file
- [ ] Verify restart/recovery behavior

### Phase 6 — Live Trading on VM
- [ ] Switch to `trading_enabled = True`
- [ ] Monitor closely for 1 week
- [ ] Decommission local tkinter setup

---

## 14. Open Questions / Decisions

| Question | Decision |
|----------|---------|
| Config edits | SSH into VM, edit `config.py`, `sudo systemctl restart spt_v4` |
| Sound alerts | Disabled on VM (no audio device); replaced by email + ntfy push |
| Push notifications | ntfy.sh hosted (free tier); iOS ntfy app + macOS ntfy app / Safari web push |
| Runtime config changes | None — all config via file + restart |
| Manual order UI | Full free-form leg builder (same capability as tkinter order_window.py) |
| Log aggregation | journald on VM; daily archive to `~/logs/archive/`; `journalctl -u spt_v4 -f` for live tailing |
| Option chain DB on VM | Not needed — live trading uses Schwab API only; catch-up sim skipped on restart |
