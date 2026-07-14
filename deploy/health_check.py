#!/usr/bin/env python3
import json
import os
import smtplib
import subprocess
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

RECIPIENTS = ["frankwang.alert@gmail.com"]

def find_gmail_config():
    paths_to_try = [
        os.path.expanduser("~/.api_keys/gmail/fw_trd_key.json"),
        "/home/fw/.api_keys/gmail/fw_trd_key.json",
        "/home/ubuntu/.api_keys/gmail/fw_trd_key.json",
        "/opt/terminator/.api_keys/gmail/fw_trd_key.json",
    ]
    for p in paths_to_try:
        if p and os.path.exists(p):
            return p
    # Raise exception if credentials missing to ensure visible systemd service failure
    raise FileNotFoundError(f"Gmail SMTP credentials not found in any of the expected paths: {paths_to_try}")

def send_alert(subject, body):
    config_path = find_gmail_config()
    with open(config_path) as f:
        creds = json.load(f)
        
    # Normalize credentials structures
    from_email = creds.get('from_email') or creds.get('sender_email')
    password = creds.get('password') or creds.get('sender_password')
    
    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = ", ".join(RECIPIENTS)
    msg['Subject'] = f"CRITICAL: {subject}"
    msg.attach(MIMEText(body, 'plain'))
    with smtplib.SMTP('smtp.gmail.com', 587) as server:
        server.starttls()
        server.login(from_email, password)
        server.send_message(msg)

def check_health():
    # 1. Check Axum Web UI and Schwab Auth
    try:
        req = urllib.request.Request("http://localhost:8081/api/status")
        with urllib.request.urlopen(req, timeout=5) as response:
            status_data = json.loads(response.read().decode())
        
        if not status_data.get("is_running"):
            send_alert("Trading Bot Stopped", "Rust backend status report shows is_running=false.")
            return
            
        if not status_data.get("broker_connected"):
            send_alert("Schwab Connection Dead", "Rust backend is up, but broker_connected=false (likely expired credentials).")
            return
            
    except Exception as e:
        send_alert("Rust Backend Unreachable", f"Failed to query Web UI status endpoint on port 8081: {e}")
        return

    # 2. Check Downloader Systemd Service
    res = subprocess.run(["systemctl", "is-active", "terminator-downloader.service"], capture_output=True, text=True)
    if res.stdout.strip() != "active":
        send_alert("Downloader Inactive", f"terminator-downloader.service is not active: {res.stdout.strip()}")
        return

if __name__ == "__main__":
    check_health()
