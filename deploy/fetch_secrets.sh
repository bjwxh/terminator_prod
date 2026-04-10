#!/bin/bash
# fetch_secrets.sh — Fetches Schwab API keys and Github PAT from GCP Secret Manager
# This runs on the VM before the terminator service starts.

PROJECT="terminator-478221"
REPO_DIR="/home/fw/terminator_prod"
TARGET_DIR="/home/fw/.api_keys/schwab"
mkdir -p "$TARGET_DIR"

# 1. Get Access Token from Metadata Server
echo "Fetching access token from Metadata Server..."
RESPONSE=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token")
ACCESS_TOKEN=$(echo "$RESPONSE" | jq -r .access_token)

if [ -z "$ACCESS_TOKEN" ] || [ "$ACCESS_TOKEN" == "null" ]; then
    echo "ERROR: Failed to get access token from Metadata Server: $RESPONSE"
    exit 1
fi

# Helper function to fetch secrets via REST API
fetch_secret_to_stdout() {
    local secret_id="$1"
    curl -s -X GET \
        -H "Authorization: Bearer $ACCESS_TOKEN" \
        "https://secretmanager.googleapis.com/v1/projects/$PROJECT/secrets/$secret_id/versions/latest:access" | jq -r .payload.data | base64 -d
}

# 2. Fetch Schwab API keys & token
echo "Fetching Schwab keys from Secret Manager..."
fetch_secret_to_stdout "schwab-api-keys" > "$TARGET_DIR/sli_api.json.tmp" && mv "$TARGET_DIR/sli_api.json.tmp" "$TARGET_DIR/sli_api.json"
fetch_secret_to_stdout "schwab-token" > "$TARGET_DIR/sli_token.json.tmp" && mv "$TARGET_DIR/sli_token.json.tmp" "$TARGET_DIR/sli_token.json"
chmod 600 "$TARGET_DIR/sli_api.json" "$TARGET_DIR/sli_token.json"
echo "✅ Schwab keys synchronized."

# 3. Pull latest code from GitHub (Automated Deploy)
echo "Checking for code updates from GitHub..."
GITHUB_TOKEN=$(fetch_secret_to_stdout "github-pat")

if [ -n "$GITHUB_TOKEN" ] && [ "$GITHUB_TOKEN" != "null" ]; then
    cd "$REPO_DIR"
    # Authenticated pull using temporary URL config
    REMOTE_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/bjwxh/terminator_prod.git"
    git pull "$REMOTE_URL" main
    if [ $? -eq 0 ]; then
        echo "✅ Code update complete (or already up to date)."
    else
        echo "⚠️  WARNING: Failed to pull latest code from GitHub."
    fi
else
    echo "⚠️  WARNING: Github PAT not found. Skipping code update."
fi

echo "Secret synchronization and update process complete."
