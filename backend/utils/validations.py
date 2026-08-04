from pydantic import AnyHttpUrl, ValidationError, TypeAdapter
import httpx
import os
import ipaddress
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()
SAFE_BROWSING = os.getenv("SAFE_BROWSING")

async def check_safe_browsing(url: str) -> bool:
    payload = {
        "uris": [url],
        "threatTypes": [
            "MALWARE",
            "SOCIAL_ENGINEERING",
            "UNWANTED_SOFTWARE",
            "MALICIOUS_BINARY"
        ]
    }

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"https://safebrowsing.googleapis.com/v5/uris:batchGet",
                params={"key": SAFE_BROWSING},
                json=payload,
                timeout=5
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("threats"):
                return False

            return True

        except Exception:
            # Fallback to safe if API is down to avoid blocking user flow
            return True

async def is_live_url(url: str) -> bool:
    async with httpx.AsyncClient() as client:
        try: 
            # We use a 2s timeout here to keep response times fast
            response = await client.head(url, timeout=2.0, follow_redirects=True)
            return response.status_code < 500
        except Exception:
            return False 

import socket

def get_resolved_ips(hostname: str) -> list[str]:
    """Resolves a hostname to a list of IP addresses (both IPv4 and IPv6)."""
    try:
        addr_info = socket.getaddrinfo(hostname, None)
        return list(set(info[4][0] for info in addr_info))
    except Exception:
        return []

def is_private_url(url: str) -> bool:
    """
    Checks if a URL points to private, loopback, or local networking spaces.
    Resolves hostname to ensure DNS mapping does not point to internal resources (SSRF protection).
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return True

        # Check if direct hostname string matches private IP or localhost
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback:
                return True
        except ValueError:
            pass

        if hostname.lower() in ("localhost", "127.0.0.1", "::1"):
            return True

        # Resolve hostname to check behind DNS aliases (OWASP A10:2021 SSRF protection)
        ips = get_resolved_ips(hostname)
        if not ips:
            # Block if it cannot be resolved (could be an invalid or strictly internal name)
            return True

        for ip_str in ips:
            try:
                ip = ipaddress.ip_address(ip_str)
                # Block private (RFC 1918), loopback, link-local, and multicast spaces
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
                    return True
            except ValueError:
                return True

        return False
    except Exception:
        return True

async def is_valid_url(url: str) -> bool:
    adapter = TypeAdapter(AnyHttpUrl)
    try:
        adapter.validate_python(url)

        # Block internal/private network access (SSRF protection)
        if is_private_url(url):
            return False

        return await is_live_url(url)
    except ValidationError:
        return False

import re

RESERVED_ALIASES = {
    "login", "signup", "dashboard", "analytics", "auth", "api", "privacy", 
    "contact", "contact-sales", "shorten", "static", "docs", "redoc", "openapi.json"
}

def is_valid_custom_alias(alias: str) -> bool:
    """
    Validates a custom alias for length, alphanumeric content, and reserved routes.
    
    Args:
        alias (str): The requested short code alias.
        
    Returns:
        bool: True if safe and valid, False otherwise.
    """
    if not alias:
        return False
    # Enforce database max length of 20 characters and min of 3
    if not (3 <= len(alias) <= 20):
        return False
    # Prevent path traversals, special chars, or slash-based routing hijacks
    if not re.match(r"^[a-zA-Z0-9_-]+$", alias):
        return False
    # Block collisions with core platform routes
    if alias.lower() in RESERVED_ALIASES:
        return False
    return True