# Terminator Production Trading Server

Production-grade deployment of the Live Options Trading system.

## Project Structure

```
terminator_prod/
│
├── server/                        # Runs on GCP VM
│   ├── main.py                    # Entry point: FastAPI + monitor in shared asyncio loop
│   ├── api/
│   │   ├── routes.py              # All REST endpoint handlers
│   │   └── ws.py                  # WebSocket broadcaster
│   ├── core/                      # Trading logic
│   │   ├── monitor.py             # LiveTradingMonitor
│   │   ├── models.py              # Core data classes
│   │   ├── config.py              # Configuration
│   │   └── utils.py               # Calculation helpers
│   ├── notifications.py           # push/email helpers
│   └── static/                    # Web UI (HTML/CSS/JS)
│
├── eod/                           # Runs on MacBook
│   ├── eod_report.py              # EOD simulation and report generation
│   └── run_eod.sh                 # Automation script
│
├── deploy/                        # Infrastructure
│   └── terminator.service             # systemd unit file
│
├── config.example.py              # Template for config.py
└── requirements.txt
```

## Setup Instructions (Local)

1. Create a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Copy the example config and edit it with your settings:
   ```bash
   cp server/core/config.py server/core/config.py  # If not already there
   # or
   cp config.example.py server/core/config.py
   ```

3. Ensure credentials are in place:
   - `~/.api_keys/sli_api.json`
   - `~/.api_keys/sli_token.json`
   - `~/.api_keys/gmail/fw_trd_key.json`

## Deployment to GCP

See `deploy/` for automation and configuration details. Access is restricted via Tailscale VPN.

## Security

- No secrets or keys are committed to Git.
- Use `.gitignore` to keep runtime artifacts and credentials out of history.
- Ensure all API endpoints are only reachable via VPN.
