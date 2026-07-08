# Changelog 008: UI Chase Glitch and Email Alert Fixes

Date: 2026-07-07

## Overview
This changelog documents the fixes made to address user-reported issues with the order chase UI glitch, low audio alert volume, missing email alerts for trade proposals, and the integration of the EOD report script with the Rust backend.

## Details

1. **Chase Order UI Glitch (Frontend & Backend):**
   - **Frontend (`app.js`):** Updated `confirmChaseOrder` to close the modal synchronously before the `fetch` API call, eliminating the UI blocking feel. Added a temporary UI toast/banner while the chase is processing.
   - **Backend (`web.rs`):** Intercepted the `Ok(false)` return type from `chase_order` (which occurs during instant partial/full fills). Instead of emitting a 500 server error, the API now returns a 200 OK with a user-friendly message indicating the order might have already been filled or canceled.

2. **Louder Audio Alerts:**
   - **Audio Processing:** Processed `chime.mp3` and `error.mp3` with `ffmpeg` to manually double their volume, ensuring alerts are audible across all browsers without complex Web Audio API logic.

3. **Email Alerts for Trade Proposals:**
   - **Python Script (`send_alert_email.py`):** Created a new Python utility that seamlessly inherits the configuration from `server/email_config.json` used by the legacy Python app.
   - **Backend Integration (`strategy.rs`):** The Rust strategy engine now asynchronously spawns `send_alert_email.py` via `std::process::Command` whenever `pending_trade` is populated, ensuring prompt email delivery for manual interventions. Hardcoded macOS paths were replaced with dynamic paths to gracefully handle both local environments and the production Ubuntu EC2 structure.

4. **EOD Report Compatibility:**
   - **Backend Integration (`web.rs`):** Implemented a new `GET /api/session` endpoint. This endpoint constructs the exact `"live_combined_portfolio"` payload (real trades, contract counts, gross/net PnL) expected by the legacy EOD reporting logic, dynamically serializing `broker_portfolio`.
   - **Reporting Script (`run_terminator_eod.sh`):** Swapped out the brittle `scp` logic for a clean `curl` request to the Rust VM's API, decoupling the report script from physical JSON state files.

## Post-Deployment Fixes & Refinements (2026-07-08)

1. **Audio Alert Redirection:**
   - **Frontend (`app.js`):** Modified `playNotificationSound()` to play the `chime.mp3` file (via `playSound('info')`) instead of synthesizing custom Web Audio API oscillator beeps, ensuring the custom-amplified `chime.mp3` file is actually played on modal popups.

2. **Email Alert & Path Fixes:**
   - **Credentials Sync**: Synced the local Gmail credentials `fw_trd_key.json` to `/home/ubuntu/.api_keys/gmail/fw_trd_key.json` on the EC2 server (run under the `ubuntu` user).
   - **Python Path Resolution (`send_alert_email.py`):** Fixed `root_dir` resolution to correctly handle the production `/opt/terminator` directory layout and avoid `ModuleNotFoundError: No module named 'server'`. Cleaned up duplicate config paths.
   - **Rust Backend Path Priority (`strategy.rs`):** Updated the binary to look for `send_alert_email.py` in `/opt/terminator` first to avoid executing stale versions in other locations.
   - **Automated Deploy (`deploy.sh`):** Updated the deployment script to copy and move `send_alert_email.py` to `/opt/terminator/` on the server during automated deploy.
