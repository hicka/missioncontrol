"""Link to a Mission Control Cloud account: push notifications and plan info.

The cloud account powers the metered model plan and lets this server
notify the owner's phones (via the mobile app) when something needs
them while they are away. Configured from Settings; the account token
lives in .env as MC_CLOUD_TOKEN.
"""
import logging
import time

import httpx

import settings as cfg

logger = logging.getLogger("missioncontrol.cloud")

_last_push: dict[str, float] = {}
PUSH_COOLDOWN = 90.0


def cloud_url() -> str:
    return ((cfg.load().get("cloud") or {}).get("url") or "").rstrip("/")


def token() -> str | None:
    return cfg.env_get("MC_CLOUD_TOKEN")


def connected() -> bool:
    return bool(cloud_url() and token())


async def me() -> dict | None:
    if not connected():
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{cloud_url()}/api/me",
                                 headers={"Authorization": f"Bearer {token()}"})
            if r.status_code == 200:
                return r.json().get("user")
    except Exception as exc:
        logger.warning("cloud /me failed: %s", exc)
    return None


async def login(url: str, email: str, password: str) -> dict:
    url = url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(f"{url}/api/login",
                                  json={"email": email, "password": password})
            if r.status_code != 200:
                msg = None
                if r.headers.get("content-type", "").startswith("application/json"):
                    msg = r.json().get("message")
                return {"ok": False, "error": msg or f"HTTP {r.status_code}"}
            data = r.json()
            cfg.env_set("MC_CLOUD_TOKEN", data["token"])
            cfg.update("cloud", {"url": url})
            return {"ok": True, "user": data.get("user")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:150]}


def disconnect():
    cfg.env_set("MC_CLOUD_TOKEN", "")
    cfg.update("cloud", {"url": ""})


async def notify(key: str, title: str, body: str):
    """Push to the owner's phones, rate-limited per key so bursts of
    activity do not become notification spam."""
    if not connected():
        return
    now = time.time()
    if now - _last_push.get(key, 0) < PUSH_COOLDOWN:
        return
    _last_push[key] = now
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(f"{cloud_url()}/api/push",
                              headers={"Authorization": f"Bearer {token()}"},
                              json={"title": title[:120], "body": body[:500]})
    except Exception as exc:
        logger.warning("cloud push failed: %s", exc)
