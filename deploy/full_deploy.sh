#!/bin/bash
# full_deploy.sh — Unified Git Commit & Multi-VM Deployment

set -e

PROJECT="terminator-478221"
PRIMARY_VM="production-server"
PRIMARY_ZONE="us-central1-a"
BACKUP_VM="production-server-backup"
BACKUP_ZONE="us-central1-c"

REPO_DIR="/home/fw/terminator_prod"
LOCAL_REPO_PATH=$(pwd)

# 1. Version Control Check
if [[ -n $(git status -s) ]]; then
    echo "📦 Detected uncommitted changes. Committing now..."
    git add .
    if [ -z "$1" ]; then
        msg="Deployment: $(date '+%Y-%m-%d %H:%M:%S')"
    else
        msg="$1"
    fi
    git commit -m "$msg"
    echo "✅ Changes committed: $msg"
else
    echo "✅ Git repository is clean. No new commit needed."
fi

# 2. Package current directory (excluding logs and secrets)
echo "🏗️  Archiving current project state..."
tar --exclude='./logs/*' --exclude='./data/*.db' --exclude='./.venv' \
    --exclude='.git' --exclude='**/__pycache__' \
    -czf /tmp/terminator_deploy.tar.gz .

# 3. Deploy to available VMs
VMS=("$PRIMARY_VM:$PRIMARY_ZONE" "$BACKUP_VM:$BACKUP_ZONE")

for entry in "${VMS[@]}"; do
    VM="${entry%%:*}"
    ZONE="${entry#*:}"
    
    echo "--- CHECKING VM: $VM ($ZONE) ---"
    
    STATUS=$(gcloud compute instances describe "$VM" --project="$PROJECT" --zone="$ZONE" --format="get(status)" 2>/dev/null)
    
    if [ "$STATUS" == "RUNNING" ]; then
        echo "🚀 Syncing code to $VM..."
        gcloud compute scp /tmp/terminator_deploy.tar.gz "$VM:~/terminator_update.tar.gz" \
            --project="$PROJECT" --zone="$ZONE" --quiet
        
        echo "🔄 Extracting and restarting services on $VM..."
        gcloud compute ssh "$VM" --project="$PROJECT" --zone="$ZONE" --quiet --command "
            sudo tar -xzf ~/terminator_update.tar.gz -C $REPO_DIR && \
            sudo systemctl restart terminator terminator-downloader && \
            sudo systemctl status terminator terminator-downloader | grep Active
        "
        echo "✅ Deployment successful on $VM."
    else
        echo "⚠️  VM $VM is $STATUS. Skipping code sync/restart."
        echo "   Note: This VM will have old code if it starts up tomorrow before the next deploy."
    fi
done

rm /tmp/terminator_deploy.tar.gz
echo "🎉 Full deployment cycle complete."
