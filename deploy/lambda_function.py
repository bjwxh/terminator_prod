import boto3
import os
import logging
from is_market_open import is_market_open_today

logger = logging.getLogger()
logger.setLevel(logging.INFO)

INSTANCE_ID = "i-0686a130b842a5015"
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")

def publish_error(sns_client, msg):
    if SNS_TOPIC_ARN:
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject="Terminator AWS Startup Failure Alert",
            Message=msg
        )

def lambda_handler(event, context):
    ec2 = boto3.client("ec2")
    sns = boto3.client("sns")
    
    try:
        # 1. NYSE Holiday/Calendar Check (inside try block for alerting)
        if not is_market_open_today():
            logger.info("Today is a weekend or NYSE holiday. Skipping startup.")
            return {"status": "SKIPPED", "reason": "Market is closed today"}
            
        logger.info(f"Attempting to start EC2 instance {INSTANCE_ID}...")
        ec2.start_instances(InstanceIds=[INSTANCE_ID])
        
        # 2. Wait for EC2 to transition to running status
        logger.info("Waiting for instance to reach running state...")
        waiter = ec2.get_waiter('instance_running')
        waiter.wait(
            InstanceIds=[INSTANCE_ID],
            WaiterConfig={'Delay': 15, 'MaxAttempts': 8}  # Max 2 minutes
        )
        logger.info("Instance is now running.")
        return {"status": "SUCCESS"}
        
    except Exception as e:
        error_msg = f"CRITICAL: Failed startup sequence for instance {INSTANCE_ID}. Error: {str(e)}"
        logger.error(error_msg)
        publish_error(sns, error_msg)
        return {"status": "FAILED", "error": str(e)}
