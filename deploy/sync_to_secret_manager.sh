#!/bin/bash
# sync_to_secret_manager.sh — Uploads local Schwab keys to GCP Secret Manager.
# Use this on your Mac whenever you update your API keys or refresh tokens.

PROJECT="terminator-478221"
LOCAL_API_JSON="$HOME/.api_keys/schwab/sli_api.json"
LOCAL_TOKEN_JSON="$HOME/.api_keys/schwab/sli_token.json"
LOCAL_GMAIL_JSON="$HOME/.api_keys/gmail/fw_trd_key.json"

# Check if secrets exist, if not, create them
ensure_secret() {
    local secret_id="$1"
    gcloud secrets describe "$secret_id" --project="$PROJECT" > /dev/null 2>&1
    if [ $? -ne 0 ]; then
        echo "Creating secret $secret_id..."
        gcloud secrets create "$secret_id" --project="$PROJECT" --replication-policy="automatic"
    fi
}

echo "[$(date)] Starting Secret Manager Sync..."

ensure_secret "schwab-api-keys"
ensure_secret "schwab-token"
ensure_secret "gmail-smtp-keys"

# Upload API JSON
if [ -f "$LOCAL_API_JSON" ]; then
    echo "Updating schwab-api-keys..."
    gcloud secrets versions add schwab-api-keys --project="$PROJECT" --data-file="$LOCAL_API_JSON"
else
    echo "ERROR: Local file $LOCAL_API_JSON not found."
fi

# Upload Token JSON
if [ -f "$LOCAL_TOKEN_JSON" ]; then
    echo "Updating schwab-token..."
    gcloud secrets versions add schwab-token --project="$PROJECT" --data-file="$LOCAL_TOKEN_JSON"
else
    echo "ERROR: Local file $LOCAL_TOKEN_JSON not found."
fi

# Upload Gmail JSON
if [ -f "$LOCAL_GMAIL_JSON" ]; then
    echo "Updating gmail-smtp-keys..."
    gcloud secrets versions add gmail-smtp-keys --project="$PROJECT" --data-file="$LOCAL_GMAIL_JSON"
else
    echo "WARNING: Local file $LOCAL_GMAIL_JSON not found. Skipping Gmail sync."
fi

echo "Done. Secrets updated in GCP."
