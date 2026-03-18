#!/bin/bash
# run_eod.sh — Runs locally on MacBook
# Pulls daily session from VM and runs eod_report.py

set -e

# --- CONFIG ---
VM_HOST="production-server" # Tailscale hostname or IP
LOCAL_GIT_DIR="/Users/fw/Git/terminator_prod"
REMOTE_SESSION_DIR="/home/fw/terminator_prod/server"
DATE_STR=$(date +%Y%m%d)
TEMP_SESSION="/tmp/vm_session_$DATE_STR.json"

echo "--- Terminator EOD Report Flow ---"
echo "Date: $DATE_STR"

# 1. Pull session state from VM
echo "Step 1: Pulling session_state.json from $VM_HOST..."
if scp "$VM_HOST:$REMOTE_SESSION_DIR/session_state.json" "$TEMP_SESSION"; then
    echo "  Success: Pulled to $TEMP_SESSION"
else
    echo "  ERROR: Could not pull session from VM. Ensure VM is up and Tailscale is active."
    exit 1
fi

# 2. Run local EOD report script
echo "Step 2: Generating EOD Report..."
cd "$LOCAL_GIT_DIR"
source .venv/bin/activate || echo "  Warning: .venv not found, using system python"

python eod/eod_report.py --session "$TEMP_SESSION"

echo "Step 3: Cleanup..."
# Keep it in /tmp for today, automatic cleanup by OS
echo "Done. Report should be in your inbox if configured."
