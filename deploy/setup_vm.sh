#!/bin/bash
# setup_vm.sh — Provision production environment on fresh VM

set -e

REPO_DIR="/home/fw/terminator_prod"
VENV_DIR="$REPO_DIR/.venv"

echo "Setting up Terminator on VM..."

# 1. Base Utils
sudo apt update && sudo apt install -y python3-venv git curl htop

# 2. Install Tailscale (Official Script)
curl -fsSL https://tailscale.com/install.sh | sh

# 2. Virtual Env
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# 3. Pull code (already pushed via MacBook scp/git)
cd "$REPO_DIR"

# 4. Install Dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 5. Setup Service
sudo cp "$REPO_DIR/deploy/terminator.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable terminator.service
sudo systemctl start terminator.service

echo "Terminator Service started."
# Tailscale is usually setup via `tailscale up` manually once
# Verify status
systemctl status terminator.service --no-pager
