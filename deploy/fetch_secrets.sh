#!/bin/bash
# fetch_secrets.sh — Fetches Schwab API keys and tokens from GCP Secret Manager
# This runs on the VM before the terminator service starts.

PROJECT="terminator-478221"
TARGET_DIR="/home/fw/.api_keys/schwab"
mkdir -p "$TARGET_DIR"

# Get Access Token from Metadata Server
echo "Fetching access token from Metadata Server..."
RESPONSE=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token")
ACCESS_TOKEN=$(echo "$RESPONSE" | jq -r .access_token)

if [ -z "$ACCESS_TOKEN" ] || [ "$ACCESS_TOKEN" == "null" ]; then
    echo "ERROR: Failed to get access token from Metadata Server: $RESPONSE"
    exit 1
fi

# Fetch Secrets via REST API
fetch_secret() {
    local secret_id="$1"
    local output_file="$2"
    echo "Fetching $secret_id from Secret Manager..."
    
    # Secret Manager API returns base64 encoded payload in a JSON response
    PAYLOAD=$(curl -s -X GET \
        -H "Authorization: Bearer $ACCESS_TOKEN" \
        "https://secretmanager.googleapis.com/v1/projects/$PROJECT/secrets/$secret_id/versions/latest:access")
        
    echo "$PAYLOAD" | jq -r .payload.data | base64 -d > "$output_file"
    
    # Check if file exists and is not empty
    if [ $? -eq 0 ] && [ -s "$output_file" ]; then
        echo "Successfully fetched $output_file"
    else
        echo "ERROR: Failed to fetch $secret_id. Response: $PAYLOAD"
    fi
}

fetch_secret "schwab-api-keys" "$TARGET_DIR/sli_api.json"
fetch_secret "schwab-token" "$TARGET_DIR/sli_token.json"

chmod 600 "$TARGET_DIR/sli_api.json" "$TARGET_DIR/sli_token.json"
echo "Secret synchronization complete."
