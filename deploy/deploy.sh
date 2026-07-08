#!/bin/bash
# deploy/deploy.sh
# Automates compiling the low-latency Rust binary on a build server in us-east-2
# and deploying it to the production trading EC2.

set -e

REGION="us-east-2"
INSTANCE_NAME="terminator-build-server"
SG_NAME="terminator-build-sg"
KEY_NAME="terminator-key"
KEY_PATH="tmp/terminator-key.pem"

if [ -z "$1" ]; then
    echo "Usage: $0 <destination-live-ip>"
    exit 1
fi
DEST_IP="$1"

# Ensure local SSH key has correct permissions
chmod 400 "$KEY_PATH"

echo "=== 🔍 Checking build server state ==="
# Find existing build server instance ID
INSTANCE_ID=$(aws ec2 describe-instances \
    --region "$REGION" \
    --filters "Name=tag:Name,Values=$INSTANCE_NAME" "Name=instance-state-name,Values=running,stopped" \
    --query "Reservations[0].Instances[0].InstanceId" \
    --output text 2>/dev/null || true)

if [ -z "$INSTANCE_ID" ] || [ "$INSTANCE_ID" = "None" ]; then
    echo "🏗️ Build server not found. Creating a new one..."

    # Find Ubuntu 22.04 LTS AMI
    AMI_ID=$(aws ec2 describe-images \
        --region "$REGION" \
        --owners 099720109477 \
        --filters "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*" "Name=state,Values=available" \
        --query "sort_by(Images, &CreationDate)[-1].ImageId" \
        --output text)
    echo "Found Ubuntu 22.04 AMI: $AMI_ID"

    # Find Default VPC
    VPC_ID=$(aws ec2 describe-vpcs \
        --region "$REGION" \
        --filters "Name=is-default,Values=true" \
        --query "Vpcs[0].VpcId" \
        --output text)
    echo "Default VPC: $VPC_ID"

    # Find/Create Security Group
    SG_ID=$(aws ec2 describe-security-groups \
        --region "$REGION" \
        --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
        --query "SecurityGroups[0].GroupId" \
        --output text 2>/dev/null || true)

    if [ -z "$SG_ID" ] || [ "$SG_ID" = "None" ]; then
        SG_ID=$(aws ec2 create-security-group \
            --region "$REGION" \
            --group-name "$SG_NAME" \
            --description "Terminator build server security group" \
            --vpc-id "$VPC_ID" \
            --query "GroupId" \
            --output text)
        echo "Created Security Group: $SG_ID"
        aws ec2 authorize-security-group-ingress \
            --region "$REGION" \
            --group-id "$SG_ID" \
            --protocol tcp \
            --port 22 \
            --cidr 0.0.0.0/0 >/dev/null 2>&1 || true
    else
        echo "Using existing Security Group: $SG_ID"
    fi

    # Launch Instance
    echo "🚀 Launching t3.xlarge build EC2 instance..."
    INSTANCE_ID=$(aws ec2 run-instances \
        --region "$REGION" \
        --image-id "$AMI_ID" \
        --instance-type t3.xlarge \
        --key-name "$KEY_NAME" \
        --security-group-ids "$SG_ID" \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$INSTANCE_NAME}]" \
        --query "Instances[0].InstanceId" \
        --output text)
    echo "Launched Instance ID: $INSTANCE_ID"

    # Wait until running
    echo "⏳ Waiting for instance to start..."
    aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"

    # Fetch public IP
    BUILD_IP=$(aws ec2 describe-instances \
        --region "$REGION" \
        --instance-ids "$INSTANCE_ID" \
        --query "Reservations[0].Instances[0].PublicIpAddress" \
        --output text)
    echo "Build Server IP: $BUILD_IP"

    # Wait for SSH to respond and provision build environment
    echo "⏳ Waiting for SSH service to initialize..."
    sleep 30

    echo "⚙️ Provisioning build environment (dependencies and Rust)..."
    ssh -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -i "$KEY_PATH" ubuntu@"$BUILD_IP" \
        "sudo apt-get update && sudo apt-get install -y build-essential libssl-dev pkg-config libsqlite3-dev && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"
else
    # Instance exists, check if it needs to be started
    STATE=$(aws ec2 describe-instances \
        --region "$REGION" \
        --instance-ids "$INSTANCE_ID" \
        --query "Reservations[0].Instances[0].State.Name" \
        --output text)

    echo "Build server exists in state: $STATE"
    if [ "$STATE" = "stopped" ]; then
        echo "🚀 Starting build server..."
        aws ec2 start-instances --region "$REGION" --instance-ids "$INSTANCE_ID" >/dev/null
        aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"
    fi

    BUILD_IP=$(aws ec2 describe-instances \
        --region "$REGION" \
        --instance-ids "$INSTANCE_ID" \
        --query "Reservations[0].Instances[0].PublicIpAddress" \
        --output text)
    echo "Build Server IP: $BUILD_IP"

    echo "⏳ Checking SSH connection..."
    until ssh -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -o ConnectTimeout=5 -i "$KEY_PATH" ubuntu@"$BUILD_IP" "echo SSH OK" >/dev/null 2>&1; do
        echo "Waiting for SSH to become ready..."
        sleep 5
    done

    # Check if Rust is installed, if not, provision
    if ! ssh -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -i "$KEY_PATH" ubuntu@"$BUILD_IP" "test -f /home/ubuntu/.cargo/bin/cargo" >/dev/null 2>&1; then
        echo "⚙️ Provisioning build environment (dependencies and Rust)..."
        ssh -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -i "$KEY_PATH" ubuntu@"$BUILD_IP" \
            "sudo apt-get update && sudo apt-get install -y build-essential libssl-dev pkg-config libsqlite3-dev && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"
    fi
fi

echo "=== 📂 Syncing source code to build server ==="
# Ensure build folder exists
ssh -o IdentitiesOnly=yes -i "$KEY_PATH" ubuntu@"$BUILD_IP" "mkdir -p ~/terminator_rust"

# Sync files (excluding target/ and logs/)
rsync -avz -e "ssh -i $KEY_PATH -o StrictHostKeyChecking=no -o IdentitiesOnly=yes" \
    --exclude "target/" \
    --exclude "logs/" \
    --exclude ".git/" \
    terminator_rust/ ubuntu@"$BUILD_IP":/home/ubuntu/terminator_rust/

echo "=== 🛠️ Building optimized release binary ==="
ssh -o IdentitiesOnly=yes -i "$KEY_PATH" ubuntu@"$BUILD_IP" "cd ~/terminator_rust && ~/.cargo/bin/cargo build --release"

echo "=== 📥 Retrieving compiled binary ==="
mkdir -p target/release
scp -o IdentitiesOnly=yes -i "$KEY_PATH" ubuntu@"$BUILD_IP":/home/ubuntu/terminator_rust/target/release/terminator_rust target/release/terminator_rust_aws

echo "=== 🛑 Stopping build server (minimizing costs) ==="
aws ec2 stop-instances --region "$REGION" --instance-ids "$INSTANCE_ID" >/dev/null
echo "Build server stopped."

echo "=== 🚀 Deploying binary and assets to Target Trading Server: $DEST_IP ==="
# Copy binary and alert email script to temp directory on production server
scp -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -i "$KEY_PATH" target/release/terminator_rust_aws ubuntu@"$DEST_IP":/tmp/terminator_rust
scp -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -i "$KEY_PATH" terminator_rust/send_alert_email.py ubuntu@"$DEST_IP":/tmp/send_alert_email.py

# Sync static assets to temp directory on production server
rsync -avz -e "ssh -i $KEY_PATH -o StrictHostKeyChecking=no -o IdentitiesOnly=yes" \
    terminator_rust/static/ ubuntu@"$DEST_IP":/tmp/static/

# Move binary, script, and static folder to opt/terminator and restart trading systemd service
ssh -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -i "$KEY_PATH" ubuntu@"$DEST_IP" \
    "sudo mv /tmp/terminator_rust /opt/terminator/terminator_rust && sudo chmod +x /opt/terminator/terminator_rust && sudo mv /tmp/send_alert_email.py /opt/terminator/send_alert_email.py && sudo chmod +x /opt/terminator/send_alert_email.py && sudo rm -rf /opt/terminator/static && sudo mv /tmp/static /opt/terminator/static && sudo systemctl restart terminator-backend.service && echo 'Deployment completed successfully!'"
