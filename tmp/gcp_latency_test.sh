#!/bin/bash
# tmp/gcp_latency_test.sh
set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <gcp-zone>"
    exit 1
fi

ZONE="$1"
echo "=== Latency Test on GCP Zone: $ZONE ==="

# 1. Create VM
echo "🚀 Creating VM instance..."
gcloud compute instances create latency-test-vm \
    --project=terminator-478221 \
    --zone="$ZONE" \
    --machine-type=e2-medium \
    --scopes=cloud-platform \
    --image-family=debian-12 \
    --image-project=debian-cloud

# Set cleanup trap
cleanup() {
    echo "⚠️ Trapped exit. Deleting VM latency-test-vm in $ZONE..."
    gcloud compute instances delete latency-test-vm --zone="$ZONE" --quiet --project=terminator-478221 || true
    echo "Cleanup complete."
}
trap cleanup EXIT

# 2. Wait for sshd to be online, then provision
echo "⏳ Waiting for SSH service to boot up..."
MAX_ATTEMPTS=20
ATTEMPT=1
while [ $ATTEMPT -le $MAX_ATTEMPTS ]; do
    if gcloud compute ssh latency-test-vm --project=terminator-478221 --zone="$ZONE" --command="echo ready" >/dev/null 2>&1; then
        echo "SSH is ready!"
        break
    fi
    echo "SSH not ready yet, waiting 5s (Attempt $ATTEMPT/$MAX_ATTEMPTS)..."
    sleep 5
    ATTEMPT=$((ATTEMPT + 1))
done

if [ $ATTEMPT -gt $MAX_ATTEMPTS ]; then
    echo "❌ SSH connection timed out!"
    exit 1
fi

echo "📦 Installing base packages on VM..."
gcloud compute ssh latency-test-vm --project=terminator-478221 --zone="$ZONE" --command="sudo apt update && sudo apt install -y python3-pip python3-venv git curl"

echo "🐍 Setting up Python Virtual Environment..."
gcloud compute ssh latency-test-vm --project=terminator-478221 --zone="$ZONE" --command="python3 -m venv ~/venv && ~/venv/bin/pip install --upgrade pip && ~/venv/bin/pip install schwab-py"

# 3. Copy credentials and test runner
echo "🔑 Copying credentials..."
gcloud compute ssh latency-test-vm --project=terminator-478221 --zone="$ZONE" --command="mkdir -p ~/.api_keys/schwab"
gcloud compute scp /Users/fw/.api_keys/schwab/sli_api.json /Users/fw/.api_keys/schwab/sli_token.json latency-test-vm:~/.api_keys/schwab/ --project=terminator-478221 --zone="$ZONE"

echo "🏃 Copying test script..."
gcloud compute scp /Users/fw/Git/terminator_prod/tmp/latency_test_runner.py latency-test-vm:~/ --project=terminator-478221 --zone="$ZONE"

# 4. Run test
echo "🏃 Running latency tests..."
gcloud compute ssh latency-test-vm --project=terminator-478221 --zone="$ZONE" --command="~/venv/bin/python3 ~/latency_test_runner.py"

echo "✅ Test completed successfully in GCP zone $ZONE."
# The trap cleanup will run now
