import os
import json
import httpx
from datetime import datetime, UTC
from dotenv import load_dotenv
from core.logger import logger

load_dotenv()

SUPPORT_JSON_PATH = os.getenv("SUPPORT_JSON_PATH", "support_tickets.json")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
TARGET_EMAIL = os.getenv("ADMIN_EMAIL")

def process_support_ticket(data: dict):
    save_to_json(data)
    if RESEND_API_KEY:
        try:
            send_email_via_resend(data)
        except Exception as e:
            logger.error(f"Failed to dispatch support ticket email: {e}")

def save_to_json(data: dict):
    file_path = SUPPORT_JSON_PATH
    if not os.path.isabs(file_path):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        file_path = os.path.join(base_dir, file_path)

    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        **data
    }

    records = []
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    records = json.loads(content)
        except Exception as e:
            logger.error(f"Error parsing existing JSON support records: {e}")

    records.append(entry)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error writing to support JSON file: {e}")

def send_email_via_resend(data: dict):
    import html
    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    }

    escaped_name = html.escape(data.get('name', ''))
    escaped_email = html.escape(data.get('email', ''))
    escaped_subject = html.escape(data.get('subject', ''))
    escaped_message = html.escape(data.get('message', '')).replace('\n', '<br>').replace('\\n', '<br>')

    html_content = f"""
    <h2>New Support Ticket Received</h2>
    <p><strong>Name:</strong> {escaped_name}</p>
    <p><strong>Email:</strong> {escaped_email}</p>
    <p><strong>Subject:</strong> {escaped_subject}</p>
    <p><strong>Message:</strong></p>
    <blockquote style="background: #f4f4f5; border-left: 4px solid #ef4444; padding: 12px 16px; margin: 12px 0; font-family: sans-serif;">
        {escaped_message}
    </blockquote>
    <p style="color: #9ca3af; font-size: 11px; margin-top: 24px; border-top: 1px solid #e5e7eb; pt: 8px;">
        Generated automatically by the FlexURL Support Ticketing system.
    </p>
    """

    payload = {
        "from": "FlexURL Support <support@resend.dev>",
        "to": [TARGET_EMAIL],
        "subject": f"FlexURL Support - {escaped_subject}",
        "html": html_content
    }

    resp = httpx.post(url, headers=headers, json=payload, timeout=10)
    resp.raise_for_status()
