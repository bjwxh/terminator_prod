#!/bin/bash
# fetch_secrets.sh — Fetches Schwab API keys and tokens from GCP Secret Manager
# This runs on the VM before the terminator service starts.

PROJECT="terminator-478221"
TARGET_DIR="/home/fw/.api_keys/schwab"
mkdir -p "$TARGET_DIR"

echo "Fetching Schwab API keys from Secret Manager..."
gcloud secrets versions access latest --secret="schwab-api-keys" --project="$PROJECT" > "$TARGET_DIR/sli_api.json"
if [ $? -eq 0 ]; then
    echo "Successfully fetched sli_api.json"
else
    echo "ERROR: Failed to fetch sli_api.json from Secret Manager."
fi

echo "Fetching Schwab token from Secret Manager..."
gcloud secrets versions access latest --secret="schwab-token" --project="$PROJECT" > "$TARGET_DIR/sli_token.json"
if [ $? -eq 0 ]; then
    echo "Successfully fetched sli_token.json"
else
    echo "ERROR: Failed to fetch sli_token.json from Secret Manager."
fi

chmod 600 "$TARGET_DIR/sli_api.json" "$TARGET_DIR/sli_token.json"
echo "Secret synchronization complete."
