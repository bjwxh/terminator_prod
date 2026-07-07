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
echo "Step 1: Pulling session state from $VM_HOST API..."
if curl -s "http://$VM_HOST:8000/api/session" > "$TEMP_SESSION"; then
    echo "  Success: Fetched to $TEMP_SESSION"
else
    echo "  ERROR: Could not fetch session from VM. Ensure VM and API are running."
    exit 1
fi

# 2. Run local EOD report script
echo "Step 2: Generating EOD Report..."
cd "$LOCAL_GIT_DIR"

PYTHON_BIN="/Users/fw/anaconda3/envs/terminator/bin/python"
$PYTHON_BIN eod/eod_report.py --session "$TEMP_SESSION"

echo "Step 3: Cleanup..."
# Keep it in /tmp for today, automatic cleanup by OS
echo "Done. Report should be in your inbox if configured."
