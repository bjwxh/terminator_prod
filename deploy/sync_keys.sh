#!/bin/bash
# Sync Schwab keys/tokens from local to VM and restart terminator service
# Updated: Failover-aware (handles Primary and Backup VMs)

PROJECT="terminator-478221"
PRIMARY_VM="production-server"
PRIMARY_ZONE="us-central1-a"
BACKUP_VM="production-server-backup"
BACKUP_ZONE="us-central1-c"

# Configuration
GCLOUD="/Users/fw/google-cloud-sdk/bin/gcloud"
export PATH="/Users/fw/google-cloud-sdk/bin:$PATH"

LOCAL_API_JSON="$HOME/.api_keys/schwab/sli_api.json"
LOCAL_TOKEN_JSON="$HOME/.api_keys/schwab/sli_token.json"
VM_TARGET_DIR="/home/fw/.api_keys"

# NTFY configuration for alerts
NTFY_TOPIC="terminator-prod-api-key-copy-failure"

send_alert() {
    local msg="$1"
    echo "ALERT: $msg"
    if [ -n "$NTFY_TOPIC" ]; then
        curl -d "$msg" "ntfy.sh/$NTFY_TOPIC" > /dev/null 2>&1
    fi
}

echo "[$(date)] Starting key sync process..."

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
    send_alert "FAILED to find an active VM. Neither $PRIMARY_VM nor $BACKUP_VM is running."
    exit 1
fi

# 2. Upload keys with retries
MAX_ATTEMPTS=3
ATTEMPT=1
SUCCESS=0

while [ $ATTEMPT -le $MAX_ATTEMPTS ]; do
    echo "Attempt $ATTEMPT: Uploading keys to $VM_NAME..."
    $GCLOUD compute scp "$LOCAL_API_JSON" "$LOCAL_TOKEN_JSON" "$VM_NAME:$VM_TARGET_DIR/" \
        --project="$PROJECT" --zone="$ZONE" --quiet
    
    if [ $? -eq 0 ]; then
        echo "Keys uploaded successfully."
        SUCCESS=1
        break
    else
        echo "Upload failed. Waiting 30s before retry..."
        sleep 30
        ((ATTEMPT++))
    fi
done

if [ $SUCCESS -eq 0 ]; then
    send_alert "FAILED to sync Schwab keys to $VM_NAME ($ZONE)."
    exit 1
fi

# 3. Restart service on VM
ATTEMPT=1
SUCCESS=0
while [ $ATTEMPT -le $MAX_ATTEMPTS ]; do
    echo "Attempt $ATTEMPT: Restarting service on $VM_NAME..."
    $GCLOUD compute ssh "$VM_NAME" \
        --project="$PROJECT" --zone="$ZONE" \
        --command="sudo systemctl restart terminator terminator-downloader && sudo systemctl status terminator terminator-downloader | grep Active" \
        --quiet
    
    if [ $? -eq 0 ]; then
        echo "Service restarted successfully."
        SUCCESS=1
        break
    else
        echo "Service restart failed. Retrying in 10s..."
        sleep 10
        ((ATTEMPT++))
    fi
done

if [ $SUCCESS -eq 0 ]; then
    send_alert "FAILED to restart terminator service on $VM_NAME after sync."
    exit 1
fi

echo "[$(date)] Sync process complete on $VM_NAME ($ZONE)."
