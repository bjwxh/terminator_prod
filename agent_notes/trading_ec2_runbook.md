# Schwab Trading EC2 Startup & API Credentials Sync Runbook

This runbook describes the step-by-step process to start the production trading EC2 instance, sync Schwab API credentials if expired, and launch/verify the Rust trading backend.

---

## 1. Start the Trading EC2 Instance

1. **Check Status / Describe Instances:**
   Check if the trading instance is running in `us-east-2`:
   ```bash
   aws ec2 describe-instances --region us-east-2 --query "Reservations[*].Instances[*].{InstanceId:InstanceId,State:State.Name,Name:Tags[?Key=='Name'].Value|[0],PublicIp:PublicIpAddress}" --output table
   ```
   * Primary Instance Name: `terminator-trading-srv`
   * Instance ID: `i-0686a130b842a5015`

2. **Start the Instance:**
   ```bash
   aws ec2 start-instances --region us-east-2 --instance-ids i-0686a130b842a5015
   aws ec2 wait instance-running --region us-east-2 --instance-ids i-0686a130b842a5015
   ```

3. **Retrieve the Instance Public and Tailscale IPs:**
   Describe the running instance to fetch its public IP:
   ```bash
   aws ec2 describe-instances --region us-east-2 --instance-ids i-0686a130b842a5015 --query "Reservations[0].Instances[0].PublicIpAddress" --output text
   ```
   Retrieve its Tailscale IP (needed for Web UI):
   ```bash
   ssh -o StrictHostKeyChecking=no -i tmp/terminator-key.pem ubuntu@<PUBLIC_IP> "tailscale ip -4"
   ```

---

## 2. Refresh & Sync Schwab API Credentials (If Expired)

If the Rust backend logs show `401 Unauthorized` or token expired messages, or if the server was offline for several days, Schwab API keys and OAuth tokens must be refreshed and synced.

1. **Auto-refresh Local Token:**
   Run the local account test script using the `terminator` conda environment on the local Mac. This automatically performs a synchronous Schwab OAuth refresh and writes the fresh token to the local path `~/.api_keys/schwab/sli_token.json`:
   ```bash
   conda run -n terminator python tmp/test_get_account.py
   ```

2. **Update AWS Secrets Manager:**
   Construct a combined secret payload JSON file and upload it to AWS Secrets Manager (`terminator_prod/schwab_credentials`):
   ```python
   # Run a quick script to generate tmp/secret_payload.json:
   import json
   from pathlib import Path
   creds = json.loads(Path("~/.api_keys/schwab/sli_api.json").expanduser().read_text())
   token = json.loads(Path("~/.api_keys/schwab/sli_token.json").expanduser().read_text())
   Path("tmp/secret_payload.json").write_text(json.dumps({"sli_api": creds, "sli_token": token}))
   ```
   Upload payload:
   ```bash
   aws secretsmanager put-secret-value --secret-id terminator_prod/schwab_credentials --region us-east-2 --secret-string file://tmp/secret_payload.json
   rm tmp/secret_payload.json
   ```

3. **Trigger Remote Sync on the EC2 Instance:**
   SSH into the instance and run the sync credentials script to download the fresh keys from AWS Secrets Manager into the instance's `~/.api_keys/` folder:
   ```bash
   ssh -o StrictHostKeyChecking=no -i tmp/terminator-key.pem ubuntu@<PUBLIC_IP> "python3 /opt/terminator/sync_credentials.py"
   ```

---

## 3. Restart and Verify the Rust App

1. **Restart Systemd Services:**
   Restart both the Rust trading backend and the Python option data downloader:
   ```bash
   ssh -o StrictHostKeyChecking=no -i tmp/terminator-key.pem ubuntu@<PUBLIC_IP> "sudo systemctl restart terminator-backend.service terminator-downloader.service"
   ```

2. **Verify Logs:**
   Ensure the app successfully establishes the WebSocket streamer connection and parses the options chain:
   ```bash
   ssh -o StrictHostKeyChecking=no -i tmp/terminator-key.pem ubuntu@<PUBLIC_IP> "sudo journalctl -u terminator-backend.service -n 50 --no-pager"
   ```
   *Look for logs indicating: "Option chain mapping parsed successfully" and "WebSocket connection handshake successful!"*

3. **Access Web UI:**
   The Web UI runs on port `8081` and is accessible via Tailscale:
   `http://<TAILSCALE_IP>:8081`
