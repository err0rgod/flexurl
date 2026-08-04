import os
import httpx
from core.logger import logger

CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN")
CLOUDFLARE_ZONE_ID = os.getenv("CLOUDFLARE_ZONE_ID")

class CloudflareSaaSManager:
    def __init__(self):
        self.base_url = f"https://api.cloudflare.com/client/v4/zones/{CLOUDFLARE_ZONE_ID}/custom_hostnames"
        self.headers = {
            "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
            "Content-Type": "application/json"
        }

    def register_custom_domain(self, domain_name: str) -> dict:
        """
        Registers a new custom hostname in Cloudflare to provision SSL/TLS certificates.
        """
        if not CLOUDFLARE_API_TOKEN or not CLOUDFLARE_ZONE_ID:
            logger.error("Cloudflare environment variables are missing.")
            return {"status": "error", "message": "Cloudflare integration is not configured."}

        payload = {
            "hostname": domain_name,
            "ssl": {
                "method": "http",
                "type": "dv",
                "settings": {
                    "http2": "on",
                    "tls_1_3": "on"
                }
            }
        }

        try:
            with httpx.Client() as client:
                resp = client.post(self.base_url, json=payload, headers=self.headers, timeout=10)
                if resp.status_code in (200, 201):
                    data = resp.json()
                    hostname_id = data["result"]["id"]
                    logger.info(f"Registered {domain_name} in Cloudflare. Hostname ID: {hostname_id}")
                    return {
                        "status": "success",
                        "hostname_id": hostname_id
                    }
                else:
                    errors = resp.json().get("errors", [])
                    err_msg = errors[0]["message"] if errors else "Unknown error"
                    logger.error(f"Failed to register custom domain {domain_name}: {resp.text}")
                    return {"status": "error", "message": err_msg}
        except Exception as e:
            logger.error(f"Cloudflare registration request failed: {e}")
            return {"status": "error", "message": str(e)}

    def remove_custom_domain(self, hostname_id: str) -> bool:
        """
        Deletes a custom hostname from Cloudflare.
        """
        if not CLOUDFLARE_API_TOKEN or not CLOUDFLARE_ZONE_ID or not hostname_id:
            return False

        try:
            with httpx.Client() as client:
                resp = client.delete(f"{self.base_url}/{hostname_id}", headers=self.headers, timeout=10)
                if resp.status_code == 200:
                    logger.info(f"Successfully deleted Cloudflare Custom Hostname ID {hostname_id}")
                    return True
                else:
                    logger.error(f"Failed to delete Cloudflare custom domain {hostname_id}: {resp.text}")
                    return False
        except Exception as e:
            logger.error(f"Cloudflare deletion request failed: {e}")
            return False

    def get_custom_domain_status(self, hostname_id: str) -> dict:
        """
        Retrieves the status of a custom hostname from Cloudflare.
        """
        if not CLOUDFLARE_API_TOKEN or not CLOUDFLARE_ZONE_ID or not hostname_id:
            return {"status": "error", "message": "Cloudflare integration is not configured."}

        try:
            with httpx.Client() as client:
                resp = client.get(f"{self.base_url}/{hostname_id}", headers=self.headers, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    result = data.get("result", {})
                    return {
                        "status": "success",
                        "hostname_status": result.get("status"),
                        "ssl_status": result.get("ssl", {}).get("status")
                    }
                else:
                    errors = resp.json().get("errors", [])
                    err_msg = errors[0]["message"] if errors else "Unknown error"
                    logger.error(f"Failed to get custom domain status {hostname_id}: {resp.text}")
                    return {"status": "error", "message": err_msg}
        except Exception as e:
            logger.error(f"Cloudflare status request failed: {e}")
            return {"status": "error", "message": str(e)}
