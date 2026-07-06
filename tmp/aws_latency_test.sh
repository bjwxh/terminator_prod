#!/bin/bash
# tmp/aws_latency_test.sh
set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <aws-region> [subnet-id] [instance-type]"
    exit 1
fi

REGION="$1"
SUBNET_ID="$2"
INSTANCE_TYPE="${3:-t3.medium}"

KEY_NAME="latency-test-key-$REGION"
SG_NAME="latency-test-sg-$REGION"
PEM_FILE="tmp/latency-test-key-$REGION.pem"

echo "=== Latency Test on AWS Region: $REGION ==="
if [ -n "$SUBNET_ID" ]; then
    echo "Using Subnet: $SUBNET_ID"
fi
echo "Using Instance Type: $INSTANCE_TYPE"

# 1. Get latest Ubuntu 22.04 AMI
echo "🔍 Finding latest Ubuntu 22.04 AMI..."
AMI_ID=$(aws ec2 describe-images \
    --region "$REGION" \
    --owners 099720109477 \
    --filters "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*" "Name=state,Values=available" \
    --query "sort_by(Images, &CreationDate)[-1].ImageId" \
    --output text)
echo "Found AMI: $AMI_ID"

# 2. Create SSH Key Pair
echo "🔑 Creating SSH Key Pair: $KEY_NAME..."
mkdir -p tmp
rm -f "$PEM_FILE"
aws ec2 delete-key-pair --region "$REGION" --key-name "$KEY_NAME" > /dev/null 2>&1 || true
aws ec2 create-key-pair \
    --region "$REGION" \
    --key-name "$KEY_NAME" \
    --query "KeyMaterial" \
    --output text > "$PEM_FILE"
chmod 400 "$PEM_FILE"

# 3. Create Security Group in default VPC
echo "🛡️ Setting up Security Group..."
VPC_ID=$(aws ec2 describe-vpcs \
    --region "$REGION" \
    --filters "Name=is-default,Values=true" \
    --query "Vpcs[0].VpcId" \
    --output text)
echo "Default VPC: $VPC_ID"

SG_ID=$(aws ec2 describe-security-groups \
    --region "$REGION" \
    --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
    --query "SecurityGroups[0].GroupId" \
    --output text 2>/dev/null || true)

if [ -z "$SG_ID" ] || [ "$SG_ID" = "None" ]; then
    SG_ID=$(aws ec2 create-security-group \
        --region "$REGION" \
        --group-name "$SG_NAME" \
        --description "Temporary Latency Test SG" \
        --vpc-id "$VPC_ID" \
        --query "GroupId" \
        --output text)
    echo "Security Group Created: $SG_ID"
else
    echo "Using existing Security Group: $SG_ID"
fi

# Authorize SSH ingress
aws ec2 authorize-security-group-ingress \
    --region "$REGION" \
    --group-id "$SG_ID" \
    --protocol tcp \
    --port 22 \
    --cidr 0.0.0.0/0 > /dev/null 2>&1 || true
echo "Authorized Port 22 SSH ingress"

# 4. Launch EC2 instance
echo "🚀 Launching $INSTANCE_TYPE EC2 instance..."
if [ -n "$SUBNET_ID" ]; then
    INSTANCE_ID=$(aws ec2 run-instances \
        --region "$REGION" \
        --image-id "$AMI_ID" \
        --instance-type "$INSTANCE_TYPE" \
        --key-name "$KEY_NAME" \
        --security-group-ids "$SG_ID" \
        --subnet-id "$SUBNET_ID" \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Project,Value=LatencyTest},{Key=Name,Value=latency-test-vm}]" \
        --query "Instances[0].InstanceId" \
        --output text)
else
    INSTANCE_ID=$(aws ec2 run-instances \
        --region "$REGION" \
        --image-id "$AMI_ID" \
        --instance-type "$INSTANCE_TYPE" \
        --key-name "$KEY_NAME" \
        --security-group-ids "$SG_ID" \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Project,Value=LatencyTest},{Key=Name,Value=latency-test-vm}]" \
        --query "Instances[0].InstanceId" \
        --output text)
fi
echo "Instance ID: $INSTANCE_ID"

# Set trap to ensure cleanup on script failure
cleanup() {
    echo "⚠️ Trapped exit. Cleaning up EC2 resources in $REGION..."
    if [ -n "$INSTANCE_ID" ]; then
        echo "Stopping/Terminating EC2 Instance $INSTANCE_ID..."
        aws ec2 terminate-instances --region "$REGION" --instance-ids "$INSTANCE_ID" > /dev/null || true
        echo "Waiting for instance to terminate..."
        aws ec2 wait instance-terminated --region "$REGION" --instance-ids "$INSTANCE_ID" > /dev/null || true
    fi
    if [ -n "$SG_ID" ]; then
        echo "Deleting Security Group $SG_ID..."
        aws ec2 delete-security-group --region "$REGION" --group-id "$SG_ID" > /dev/null || true
    fi
    if [ -n "$KEY_NAME" ]; then
        echo "Deleting Key Pair $KEY_NAME..."
        aws ec2 delete-key-pair --region "$REGION" --key-name "$KEY_NAME" > /dev/null || true
    fi
    rm -f "$PEM_FILE"
    echo "Cleanup complete."
}
trap cleanup EXIT

# 5. Wait for instance to be running
echo "⏳ Waiting for instance to be running..."
aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"

# Get Public IP
IP=$(aws ec2 describe-instances \
    --region "$REGION" \
    --instance-ids "$INSTANCE_ID" \
    --query "Reservations[0].Instances[0].PublicIpAddress" \
    --output text)
echo "Instance Public IP: $IP"

# 6. Wait for SSH daemon to be online
echo "⏳ Waiting for SSH service to boot up..."
MAX_ATTEMPTS=30
ATTEMPT=1
while [ $ATTEMPT -le $MAX_ATTEMPTS ]; do
    if ssh -i "$PEM_FILE" -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -o ConnectTimeout=5 "ubuntu@$IP" "echo ready" >/dev/null; then
        echo "SSH is ready!"
        break
    fi
    echo "SSH not ready yet (will print error if any)..."
    ssh -i "$PEM_FILE" -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -o ConnectTimeout=5 "ubuntu@$IP" "echo ready" 2>&1 || true
    echo "Waiting 5s (Attempt $ATTEMPT/$MAX_ATTEMPTS)..."
    sleep 5
    ATTEMPT=$((ATTEMPT + 1))
done

if [ $ATTEMPT -gt $MAX_ATTEMPTS ]; then
    echo "❌ SSH connection timed out!"
    exit 1
fi

# 7. Provision & Run Test on EC2
echo "📦 Provisioning dependencies on EC2 instance..."
ssh -i "$PEM_FILE" -o StrictHostKeyChecking=no -o IdentitiesOnly=yes "ubuntu@$IP" "sudo apt update && sudo apt install -y python3-pip python3-venv"
ssh -i "$PEM_FILE" -o StrictHostKeyChecking=no -o IdentitiesOnly=yes "ubuntu@$IP" "python3 -m venv ~/venv && ~/venv/bin/pip install --upgrade pip && ~/venv/bin/pip install schwab-py"

echo "🔑 Transferring credentials and test script..."
ssh -i "$PEM_FILE" -o StrictHostKeyChecking=no -o IdentitiesOnly=yes "ubuntu@$IP" "mkdir -p ~/.api_keys/schwab"
scp -i "$PEM_FILE" -o StrictHostKeyChecking=no -o IdentitiesOnly=yes ~/.api_keys/schwab/sli_api.json ~/.api_keys/schwab/sli_token.json "ubuntu@$IP:~/.api_keys/schwab/"
scp -i "$PEM_FILE" -o StrictHostKeyChecking=no -o IdentitiesOnly=yes tmp/latency_test_runner.py "ubuntu@$IP:~/"

echo "🏃 Running latency tests on EC2..."
ssh -i "$PEM_FILE" -o StrictHostKeyChecking=no -o IdentitiesOnly=yes "ubuntu@$IP" "~/venv/bin/python3 ~/latency_test_runner.py"

echo "✅ Test completed successfully in $REGION."
# The trap will clean up all resources now
