---
name: trading-ec2-runbook
description: Instructions for starting the Schwab trading EC2 instance, checking/refreshing Schwab API/OAuth credentials, and restarting the Rust trading backend.
---

# Trading EC2 Startup & Schwab API Sync

When asked to perform any of the following tasks:
- Start the trading EC2 instance (`terminator-trading-srv`).
- Sync, refresh, or resolve Schwab API credentials or OAuth tokens (`sli_token.json` or `sli_api.json`).
- Restart or verify the Rust trading backend (`terminator-backend.service`).
- Run the daily morning startup workflow.

You MUST read and follow the step-by-step instructions in the runbook located at:
[trading_ec2_runbook.md](file:///Users/fw/Git/terminator_prod/agent_notes/trading_ec2_runbook.md)
