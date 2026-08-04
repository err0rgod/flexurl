import os
import json
import httpx
from datetime import datetime, UTC
from dotenv import load_dotenv
from core.logger import core.logger

load_dotenv()

# Editable path loaded from the environment variables (with default fallback)
QUOTATIONS_JSON_PATH = os.getenv("QUOTATIONS_JSON_PATH", "quotations.json")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
TARGET_EMAIL = os.getenv("ADMIN_EMAIL")

def process_quotation(data: dict):
    """
    Process the sales quote: save to local JSON storage and trigger email alert.
    """
    # 1. Save data to JSON log
    save_to_json(data)
    
    # 2. Dispatch email via Resend
    if RESEND_API_KEY:
        send_email_via_resend(data)
    else:
        logger.warning("RESEND_API_KEY not found in .env. Skipping email notification.")

def save_to_json(data: dict):
    # Resolve path relative to project root if not absolute
    file_path = QUOTATIONS_JSON_PATH
    if not os.path.isabs(file_path):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        file_path = os.path.join(base_dir, file_path)

    # Setup the new record
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        **data
    }

    # Read existing entries
    records = []
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    records = json.loads(content)
                    if not isinstance(records, list):
                        records = [records]
        except Exception as e:
            logger.error(f"Error parsing existing JSON records, starting fresh: {e}")

    # Append and commit
    records.append(entry)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error writing to JSON storage file: {e}")
        raise e

def send_email_via_resend(data: dict):
    import html
    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    }

    escaped_biz = html.escape(data.get('business_name', ''))
    escaped_primary = html.escape(data.get('primary_contact', ''))
    escaped_alternate = html.escape(data.get('alternate_contact') or 'N/A')
    escaped_cloud = html.escape(data.get('cloud_provider') or 'Not Specified')
    escaped_demand = html.escape(data.get('demand_desc', '')).replace('\n', '<br>')

    # Format mail contents
    html_content = f"""
    <h2>New FlexURL Enterprise Dedicated Inquiry</h2>
    <p><strong>Business Name:</strong> {escaped_biz}</p>
    <p><strong>Primary Contact:</strong> {escaped_primary}</p>
    <p><strong>Alternate Contact:</strong> {escaped_alternate}</p>
    <p><strong>Preferred Infrastructure:</strong> {escaped_cloud}</p>
    <p><strong>Requirements Demand Description:</strong></p>
    <blockquote style="background: #f4f4f5; border-left: 4px solid #6366f1; padding: 12px 16px; margin: 12px 0; font-family: sans-serif;">
        {escaped_demand}
    </blockquote>
    <p style="color: #9ca3af; font-size: 11px; margin-top: 24px; border-top: 1px solid #e5e7eb; pt: 8px;">
        Generated automatically by the FlexURL lead dispatch system.
    </p>
    """

    payload = {
        "from": "FlexURL Quotes <onboarding@resend.dev>",
        "to": [TARGET_EMAIL],
        "subject": f"FlexURL Lead - {escaped_biz}",
        "html": html_content
    }

    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to post email via Resend API: {e}")
        raise e
