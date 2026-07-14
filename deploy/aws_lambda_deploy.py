import boto3
import json
import zipfile
import io
import time
import os

REGION = "us-east-2"
INSTANCE_ID = "i-0686a130b842a5015"
EMAIL_RECIPIENT = "frankwang.alert@gmail.com"

# Resource Names
SNS_TOPIC_NAME = "terminator-alerts"
LAMBDA_ROLE_NAME = "terminator-lambda-execution-role"
SCHEDULER_ROLE_NAME = "terminator-scheduler-execution-role"
LAMBDA_FUNC_NAME = "terminator-auto-start"
SCHEDULE_NAME = "terminator-morning-start-schedule"
ALARM_NAME = "terminator-auto-start-errors-alarm"

def deploy():
    print("=== Starting AWS Resource Deployment ===")
    
    # Initialize clients
    sns_client = boto3.client("sns", region_name=REGION)
    iam_client = boto3.client("iam")
    lambda_client = boto3.client("lambda", region_name=REGION)
    scheduler_client = boto3.client("scheduler", region_name=REGION)
    cw_client = boto3.client("cloudwatch", region_name=REGION)
    
    # 1. Create SNS Topic
    print(f"Creating SNS Topic: {SNS_TOPIC_NAME}...")
    topic_resp = sns_client.create_topic(Name=SNS_TOPIC_NAME)
    sns_topic_arn = topic_resp["TopicArn"]
    print(f"SNS Topic ARN: {sns_topic_arn}")
    
    # 2. Subscribe Email
    print(f"Subscribing {EMAIL_RECIPIENT} to SNS Topic...")
    # Check existing subscriptions first
    subs = sns_client.list_subscriptions_by_topic(TopicArn=sns_topic_arn)["Subscriptions"]
    is_subscribed = False
    for sub in subs:
        if sub["Endpoint"] == EMAIL_RECIPIENT:
            is_subscribed = True
            print("Subscription already exists.")
            break
            
    if not is_subscribed:
        sns_client.subscribe(
            TopicArn=sns_topic_arn,
            Protocol="email",
            Endpoint=EMAIL_RECIPIENT
        )
        print("Subscription created. Verification email sent.")
        
    # 3. Create IAM Role for Lambda
    print(f"Creating IAM Role: {LAMBDA_ROLE_NAME}...")
    lambda_trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Service": "lambda.amazonaws.com"
                },
                "Action": "sts:AssumeRole"
            }
        ]
    }
    
    lambda_role_arn = None
    try:
        role_resp = iam_client.create_role(
            RoleName=LAMBDA_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(lambda_trust_policy),
            Description="Execution role for Terminator Auto-Start Lambda"
        )
        lambda_role_arn = role_resp["Role"]["Arn"]
        print(f"Created IAM Role: {lambda_role_arn}")
    except iam_client.exceptions.EntityAlreadyExistsException:
        print("Role already exists. Retrieving ARN...")
        role_resp = iam_client.get_role(RoleName=LAMBDA_ROLE_NAME)
        lambda_role_arn = role_resp["Role"]["Arn"]
        
    # Attach Inline Policy to Lambda Role
    lambda_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "ec2:StartInstances",
                    "ec2:DescribeInstances"
                ],
                "Resource": "*"
            },
            {
                "Effect": "Allow",
                "Action": [
                    "sns:Publish"
                ],
                "Resource": sns_topic_arn
            },
            {
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents"
                ],
                "Resource": "*"
            }
        ]
    }
    
    print("Attaching policy to Lambda Role...")
    iam_client.put_role_policy(
        RoleName=LAMBDA_ROLE_NAME,
        PolicyName="terminator-lambda-policy",
        PolicyDocument=json.dumps(lambda_policy)
    )
    
    # 4. Create IAM Role for Scheduler
    print(f"Creating IAM Role: {SCHEDULER_ROLE_NAME}...")
    scheduler_trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Service": "scheduler.amazonaws.com"
                },
                "Action": "sts:AssumeRole"
            }
        ]
    }
    
    scheduler_role_arn = None
    try:
        role_resp = iam_client.create_role(
            RoleName=SCHEDULER_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(scheduler_trust_policy),
            Description="Execution role for Terminator Auto-Start Scheduler"
        )
        scheduler_role_arn = role_resp["Role"]["Arn"]
        print(f"Created IAM Role: {scheduler_role_arn}")
    except iam_client.exceptions.EntityAlreadyExistsException:
        print("Role already exists. Retrieving ARN...")
        role_resp = iam_client.get_role(RoleName=SCHEDULER_ROLE_NAME)
        scheduler_role_arn = role_resp["Role"]["Arn"]
        
    # 5. Zip Lambda Code
    print("Packaging Lambda deployment package...")
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
        deploy_dir = os.path.dirname(os.path.realpath(__file__))
        zip_file.write(os.path.join(deploy_dir, "lambda_function.py"), "lambda_function.py")
        zip_file.write(os.path.join(deploy_dir, "is_market_open.py"), "is_market_open.py")
    zip_buffer.seek(0)
    zip_bytes = zip_buffer.read()
    
    # 6. Create or Update Lambda Function
    print("Creating/Updating Lambda Function...")
    # Wait for IAM role propagation
    print("Waiting 15 seconds for IAM permissions to propagate...")
    time.sleep(15)
    
    lambda_arn = None
    try:
        lambda_resp = lambda_client.create_function(
            FunctionName=LAMBDA_FUNC_NAME,
            Runtime="python3.11",
            Role=lambda_role_arn,
            Handler="lambda_function.lambda_handler",
            Code={"ZipFile": zip_bytes},
            Timeout=130, # 2 min + padding
            MemorySize=128,
            Environment={
                "Variables": {
                    "SNS_TOPIC_ARN": sns_topic_arn
                }
            }
        )
        lambda_arn = lambda_resp["FunctionArn"]
        print(f"Created Lambda function: {lambda_arn}")
    except lambda_client.exceptions.ResourceConflictException:
        print("Lambda function already exists. Updating code and config...")
        # Update function code
        lambda_client.update_function_code(
            FunctionName=LAMBDA_FUNC_NAME,
            ZipFile=zip_bytes
        )
        
        # Wait for function update to complete (prevents ResourceConflictException)
        print("Waiting for Lambda update to complete...")
        for _ in range(30):
            func_info = lambda_client.get_function(FunctionName=LAMBDA_FUNC_NAME)
            status = func_info.get("Configuration", {}).get("LastUpdateStatus", "Successful")
            if status != "InProgress":
                break
            time.sleep(1)
            
        # Update configuration
        lambda_resp = lambda_client.update_function_configuration(
            FunctionName=LAMBDA_FUNC_NAME,
            Role=lambda_role_arn,
            Timeout=130,
            Environment={
                "Variables": {
                    "SNS_TOPIC_ARN": sns_topic_arn
                }
            }
        )
        lambda_arn = lambda_resp["FunctionArn"]
        print("Updated Lambda function configuration successfully.")
        
    # Allow Scheduler to invoke Lambda
    account_id = sns_topic_arn.split(":")[4]
    try:
        lambda_client.add_permission(
            FunctionName=LAMBDA_FUNC_NAME,
            StatementId="AllowSchedulerToInvoke",
            Action="lambda:InvokeFunction",
            Principal="scheduler.amazonaws.com",
            SourceArn=f"arn:aws:scheduler:{REGION}:{account_id}:schedule/*"
        )
        print("Added permission for EventBridge Scheduler to invoke Lambda.")
    except lambda_client.exceptions.ResourceConflictException:
        print("Invoke permission already exists.")
        
    # Attach Policy to Scheduler Role to invoke Lambda
    scheduler_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "lambda:InvokeFunction",
                "Resource": lambda_arn
            }
        ]
    }
    iam_client.put_role_policy(
        RoleName=SCHEDULER_ROLE_NAME,
        PolicyName="terminator-scheduler-policy",
        PolicyDocument=json.dumps(scheduler_policy)
    )

    # 7. Create/Update EventBridge Schedule
    print(f"Creating/Updating EventBridge Schedule: {SCHEDULE_NAME}...")
    schedule_payload = {
        "Name": SCHEDULE_NAME,
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "ScheduleExpression": "cron(20 8 ? * MON-FRI *)",
        "ScheduleExpressionTimezone": "America/Chicago",
        "State": "ENABLED",
        "Target": {
            "Arn": lambda_arn,
            "RoleArn": scheduler_role_arn
        }
    }
    
    try:
        scheduler_client.create_schedule(**schedule_payload)
        print("Created schedule successfully.")
    except scheduler_client.exceptions.ConflictException:
        print("Schedule already exists. Updating...")
        # Get target and update
        scheduler_client.update_schedule(**schedule_payload)
        print("Updated schedule successfully.")
        
    # 8. Create CloudWatch Metric Alarm
    print(f"Creating CloudWatch Metric Alarm: {ALARM_NAME}...")
    cw_client.put_metric_alarm(
        AlarmName=ALARM_NAME,
        AlarmDescription="Trigger alert if Lambda terminator-auto-start execution fails",
        ActionsEnabled=True,
        AlarmActions=[sns_topic_arn],
        MetricName="Errors",
        Namespace="AWS/Lambda",
        Statistic="Sum",
        Dimensions=[
            {
                "Name": "FunctionName",
                "Value": LAMBDA_FUNC_NAME
            }
        ],
        Period=300,
        EvaluationPeriods=1,
        Threshold=1.0,
        ComparisonOperator="GreaterThanOrEqualToThreshold"
    )
    print("CloudWatch Alarm created/updated successfully.")
    print("=== Resource Deployment Completed Successfully ===")

if __name__ == "__main__":
    deploy()
