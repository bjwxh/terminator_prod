#!/usr/bin/env python3
# tmp/sync_credentials.py
import os
import json
import boto3
from pathlib import Path

SECRET_NAME = "terminator_prod/schwab_credentials"
REGION_NAME = "us-east-2"
TARGET_DIR = Path("/home/ubuntu/.api_keys/schwab")

def sync():
    # Initialize a session using the default credentials configured on the server
    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager',
        region_name=REGION_NAME
    )

    try:
        get_secret_value_response = client.get_secret_value(
            SecretId=SECRET_NAME
        )
    except Exception as e:
        print(f"Error retrieving secret from Secrets Manager: {e}")
        return False

    if 'SecretString' in get_secret_value_response:
        secret = get_secret_value_response['SecretString']
    else:
        print("SecretString not found in response.")
        return False

    try:
        data = json.loads(secret)
    except Exception as e:
        print(f"Error parsing secret JSON: {e}")
        return False

    # Extract payloads
    sli_api = data.get("sli_api")
    sli_token = data.get("sli_token")

    if not sli_api or not sli_token:
        print("Missing sli_api or sli_token key in secret data.")
        return False

    # Create target directory
    TARGET_DIR.mkdir(parents=True, exist_ok=True)

    # Write files
    api_path = TARGET_DIR / "sli_api.json"
    token_path = TARGET_DIR / "sli_token.json"

    with open(api_path, "w") as f:
        json.dump(sli_api, f, indent=4)
    print(f"Successfully synced: {api_path}")

    with open(token_path, "w") as f:
        json.dump(sli_token, f, indent=4)
    print(f"Successfully synced: {token_path}")

    return True

if __name__ == "__main__":
    sync()
