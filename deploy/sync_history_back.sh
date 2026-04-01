#!/bin/bash
# Sync history CSV back from the active VM to local Mac at EOD
# This should run after 15:15 PM and before 15:30 PM (when VM shuts down)

PROJECT="terminator-478221"
PRIMARY_VM="production-server"
PRIMARY_ZONE="us-central1-a"
BACKUP_VM="production-server-backup"
BACKUP_ZONE="us-central1-c"

# Configuration
GCLOUD="/Users/fw/google-cloud-sdk/bin/gcloud"
export PATH="/Users/fw/google-cloud-sdk/bin:$PATH"

LOCAL_EOD_DIR="/Users/fw/Git/terminator_prod/eod"
VM_EOD_DIR="/home/fw/terminator_prod/eod"
HISTORY_FILE="terminator_eod_history.csv"

# NTFY configuration for alerts
NTFY_TOPIC="terminator-history-sync-failure"

send_alert() {
    local msg="$1"
    echo "ALERT: $msg"
    if [ -n "$NTFY_TOPIC" ]; then
        curl -d "$msg" "ntfy.sh/$NTFY_TOPIC" > /dev/null 2>&1
    fi
}

echo "[$(date)] Starting history sync-back process..."

# 1. Detect active VM
echo "Detecting active VM..."
VM_NAME=""
ZONE=""

# Check Primary first
STATUS=$($GCLOUD compute instances describe "$PRIMARY_VM" --project="$PROJECT" --zone="$PRIMARY_ZONE" --format="get(status)" 2>/dev/null)
if [ "$STATUS" == "RUNNING" ]; then
    VM_NAME="$PRIMARY_VM"
    ZONE="$PRIMARY_ZONE"
    echo "Primary VM ($VM_NAME) is active."
else
    # Check Backup
    STATUS=$($GCLOUD compute instances describe "$BACKUP_VM" --project="$PROJECT" --zone="$BACKUP_ZONE" --format="get(status)" 2>/dev/null)
    if [ "$STATUS" == "RUNNING" ]; then
        VM_NAME="$BACKUP_VM"
        ZONE="$BACKUP_ZONE"
        echo "Backup VM ($VM_NAME) is active."
    fi
fi

if [ -z "$VM_NAME" ]; then
    echo "No active VM found. Skipping history sync back."
    exit 0
fi

# 2. Pull history CSV from VM
echo "Pulling $HISTORY_FILE from $VM_NAME..."
$GCLOUD compute scp "$VM_NAME:$VM_EOD_DIR/$HISTORY_FILE" "$LOCAL_EOD_DIR/" \
    --project="$PROJECT" --zone="$ZONE" --quiet

if [ $? -eq 0 ]; then
    echo "History CSV pulled successfully from $VM_NAME."
    # Optional: Backup the local file just in case
    cp "$LOCAL_EOD_DIR/$HISTORY_FILE" "$LOCAL_EOD_DIR/$HISTORY_FILE.bak.$(date +%Y%m%d)"
else
    send_alert "FAILED to pull $HISTORY_FILE from $VM_NAME ($ZONE) at EOD."
    exit 1
fi

echo "[$(date)] History sync-back complete."
