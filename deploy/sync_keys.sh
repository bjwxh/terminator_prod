#!/bin/bash
# Sync Schwab keys/tokens from local to VM and restart terminator service
# Includes: VM auto-start, retries, and failure alerts via ntfy.sh

PROJECT="terminator-478221"
ZONE="us-central1-a"
VM_NAME="production-server"

# Configuration
GCLOUD="/Users/fw/google-cloud-sdk/bin/gcloud"
export PATH="/Users/fw/google-cloud-sdk/bin:$PATH"

LOCAL_API_JSON="$HOME/.api_keys/schwab/sli_api.json"
LOCAL_TOKEN_JSON="$HOME/.api_keys/schwab/sli_token.json"
VM_TARGET_DIR="/home/fw/.api_keys"

# NTFY configuration for alerts - REPLACE TOPIC IF NEEDED
NTFY_TOPIC="terminator-prod-api-key-copy-failure" # Add your ntfy.sh topic here for mobile alerts

send_alert() {
    local msg="$1"
    echo "ALERT: $msg"
    if [ -n "$NTFY_TOPIC" ]; then
        curl -d "$msg" "ntfy.sh/$NTFY_TOPIC" > /dev/null 2>&1
    fi
}

echo "[$(date)] Starting key sync process..."

# 1. Check VM status and start if offline
STATUS=$($GCLOUD compute instances describe "$VM_NAME" --project="$PROJECT" --zone="$ZONE" --format="get(status)" 2>/dev/null)
echo "Current VM status: $STATUS"

if [ "$STATUS" != "RUNNING" ]; then
    echo "VM is $STATUS. Attempting to start..."
    $GCLOUD compute instances start "$VM_NAME" --project="$PROJECT" --zone="$ZONE" --quiet
    if [ $? -ne 0 ]; then
        send_alert "FAILED to start VM $VM_NAME. Manual intervention required!"
        exit 1
    fi
    echo "Wait 30s for VM to boot up..."
    sleep 30
fi

# 2. Upload keys with retries
MAX_ATTEMPTS=3
ATTEMPT=1
SUCCESS=0

while [ $ATTEMPT -le $MAX_ATTEMPTS ]; do
    echo "Attempt $ATTEMPT: Uploading keys..."
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
    send_alert "FAILED to sync Schwab keys to $VM_NAME after $MAX_ATTEMPTS attempts."
    exit 1
fi

# 3. Restart service on VM
ATTEMPT=1
SUCCESS=0
while [ $ATTEMPT -le $MAX_ATTEMPTS ]; do
    echo "Attempt $ATTEMPT: Restarting service..."
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

echo "[$(date)] Sync process complete and service is running."
