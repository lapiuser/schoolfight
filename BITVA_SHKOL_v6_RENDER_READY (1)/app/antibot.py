from __future__ import annotations

import httpx

from .config import TURNSTILE_ENABLED, TURNSTILE_SECRET_KEY

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


async def verify_turnstile(token: str | None, remote_ip: str | None = None, expected_action: str | None = None) -> bool:
    if not TURNSTILE_ENABLED:
        return True
    if not token:
        return False
    payload = {"secret": TURNSTILE_SECRET_KEY, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(TURNSTILE_VERIFY_URL, data=payload)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return False
    if not data.get("success"):
        return False
    if expected_action and data.get("action") and data.get("action") != expected_action:
        return False
    return True
