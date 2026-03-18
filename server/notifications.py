import os
import json
import logging
import smtplib
import httpx
import asyncio
import traceback
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

async def send_push(topic: str, message: str, title: str = "Terminator Alert", priority: str = "default"):
    """Send push notification via ntfy.sh"""
    if not topic:
        return
    
    try:
        url = f"https://ntfy.sh/{topic}"
        headers = {
            "Title": title,
            "Priority": priority,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, content=message, headers=headers)
            if resp.status_code == 200:
                logger.info(f"Push notification sent to ntfy.sh topic: {topic}")
            else:
                logger.error(f"Failed to send push notification: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Error sending push notification: {e}")

def send_email_alert(config: Dict, subject: str, body: str):
    """Send email alert via SMTP (blocking, should be run in executor)"""
    if not config.get('email_alerts_enabled', True):
        return
        
    config_path = config.get('email_config_path')
    if not config_path or not os.path.exists(config_path):
        logger.warning(f"Email config file not found at {config_path} - email alerts disabled")
        return
    
    try:
        with open(config_path, 'r') as f:
            email_creds = json.load(f)
        
        # Normalize credentials
        from_email = email_creds.get('from_email') or email_creds.get('sender_email')
        password = email_creds.get('password') or email_creds.get('sender_password')
        smtp_server = email_creds.get('smtp_server', 'smtp.gmail.com')
        smtp_port = email_creds.get('smtp_port', 587)
        
        recipients = config.get('email_recipients', [])
        if not recipients:
            logger.warning("No email recipients configured, skipping alert")
            return

        msg = MIMEMultipart()
        msg['From'] = from_email
        msg['To'] = ", ".join(recipients)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(from_email, password)
            server.send_message(msg)
        
        logger.info(f"Email alert sent to {len(recipients)} recipients")
        
    except Exception as e:
        logger.error(f"Failed to send email alert: {e}")

async def notify_all(config: Dict, message: str, title: str = "Terminator Alert", email_body: Optional[str] = None):
    """Convenience helper to send both push and email"""
    # 1. Push (Async)
    if config.get('ntfy_enabled', True):
        await send_push(config.get('ntfy_topic'), message, title=title)
    
    # 2. Email (Blocking -> run in thread)
    if config.get('email_alerts_enabled', True):
        loop = asyncio.get_event_loop()
        subject = f"{title}: {message[:50]}"
        body = email_body or message
        await loop.run_in_executor(None, send_email_alert, config, subject, body)
