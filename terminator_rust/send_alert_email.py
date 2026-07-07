#!/Users/fw/anaconda3/envs/terminator/bin/python
import os
import sys
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging

# Add project root to sys.path
dir_path = os.path.dirname(os.path.realpath(__file__))
# If running locally, root is parent. On server, root is /home/fw/terminator_prod
root_dir = "/home/fw/terminator_prod" if os.path.exists("/home/fw/terminator_prod/server") else os.path.abspath(os.path.join(dir_path, ".."))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from server.core.config import CONFIG

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("send_alert_email")

def main():
    strat_id = sys.argv[1] if len(sys.argv) > 1 else "Unknown Strategy"
    
    config_path = CONFIG.get('email_config_path', os.path.join(root_dir, 'server', 'email_config.json'))
    
    if not os.path.exists(config_path):
        logger.error(f"Email config not found at {config_path}")
        return

    try:
        with open(config_path, 'r') as f:
            email_config = json.load(f)
        
        # Ensure v5a structure if needed
        if 'from_email' not in email_config and 'sender_email' in email_config:
            email_config['from_email'] = email_config['sender_email']
            email_config['password'] = email_config['sender_password']
            email_config['smtp_server'] = 'smtp.gmail.com'
            email_config['smtp_port'] = 587

        recipients = CONFIG.get('email_recipients', ['frankwang.alert@gmail.com'])
        if not recipients:
            logger.warning("No email recipients configured, skipping alert")
            return

        msg = MIMEMultipart()
        msg['From'] = email_config['from_email']
        msg['To'] = ", ".join(recipients)
        msg['Subject'] = f"Terminator Rust Alert: New Trade Proposal ({strat_id})"
        
        body = f"A new trade proposal requires your attention in the Terminator Rust app.\nStrategy: {strat_id}\n\nPlease check the web interface to review and execute."
        msg.attach(MIMEText(body, 'plain'))
        
        with smtplib.SMTP(email_config['smtp_server'], email_config['smtp_port']) as server:
            server.starttls()
            server.login(email_config['from_email'], email_config['password'])
            server.send_message(msg)
        
        logger.info(f"Trade alert email sent to {len(recipients)} recipients")
        
    except Exception as e:
        logger.error(f"Failed to send trade alert email: {e}")

if __name__ == "__main__":
    main()
