"""Push notifications to the owner's phones with nothing in between.

A phone registers its push token with this server; when an agent says
something while the owner is away from every app window, the server posts
to whoever delivers to that phone: Expo's push API for the cross-platform
app, Apple's own for the native iPhone app. Per-thread cooldown so a
chatty agent produces one buzz, not ten. Tokens Expo reports dead are
dropped.
"""
import json
import logging
import time

from apphome import ROOT

logger = logging.getLogger("push")
DEVICES = ROOT / "devices.json"
EXPO = "https://exp.host/--/api/v2/push/send"
COOLDOWN = 90
_last: dict[str, float] = {}


def devices() -> list[dict]:
    try:
        return json.loads(DEVICES.read_text())
    except (OSError, ValueError):
        return []


def _save(rows: list[dict]):
    DEVICES.write_text(json.dumps(rows, indent=1))


def register(token: str, name: str = "phone", kind: str = "expo", sandbox: bool = False) -> list[dict]:
    """kind: "expo" for the cross-platform app, which Expo's service delivers to, or "apns" for
    the native iPhone app, which Apple delivers to directly. sandbox: a development build,
    whose tokens only Apple's sandbox knows."""
    token = token.strip()
    rows = [d for d in devices() if d.get("token") != token]
    row = {"token": token, "name": name.strip() or "phone", "added": time.time()}
    if kind == "apns":
        row.update(kind="apns", sandbox=bool(sandbox))
    rows.append(row)
    _save(rows)
    return rows


def remove(token: str) -> list[dict]:
    rows = [d for d in devices() if d.get("token") != token]
    _save(rows)
    return rows


def due(thread: str, now: float | None = None) -> bool:
    """One notification per thread per cooldown window."""
    now = now or time.time()
    if now - _last.get(thread, 0.0) < COOLDOWN:
        return False
    _last[thread] = now
    return True


# ---------- Apple's push service, for the native iPhone app ----------

APNS = "https://api.push.apple.com"
APNS_SANDBOX = "https://api.sandbox.push.apple.com"
# Apple wants a provider token no older than an hour, and not a new one for every push.
_TOKEN_LIFE = 45 * 60
_provider: dict = {}
_said_unset = False


def apns_settings() -> dict | None:
    """The owner's push key for their own app: settings.yaml `push.apns` with `key_file` (the
    .p8 from their Apple developer account), `key_id`, `team_id` and `topic` (the app's bundle
    id). None until all four are there and the key can be read."""
    import settings as cfg
    conf = ((cfg.load().get("push") or {}).get("apns") or {})
    if not all(conf.get(k) for k in ("key_file", "key_id", "team_id", "topic")):
        return None
    from pathlib import Path
    path = Path(str(conf["key_file"])).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    try:
        return {**conf, "key": path.read_text()}
    except OSError:
        return None


def _b64(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def provider_token(conf: dict, now: float | None = None) -> str:
    """The signed token that says these pushes come from the owner's developer account."""
    now = now or time.time()
    cached = _provider.get(conf["key_id"])
    if cached and now - cached[0] < _TOKEN_LIFE:
        return cached[1]
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    head = _b64(json.dumps({"alg": "ES256", "kid": conf["key_id"]}, separators=(",", ":")).encode())
    body = _b64(json.dumps({"iss": conf["team_id"], "iat": int(now)}, separators=(",", ":")).encode())
    key = serialization.load_pem_private_key(conf["key"].encode(), password=None)
    r, s_ = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    token = f"{head}.{body}." + _b64(r.to_bytes(32, "big") + s_.to_bytes(32, "big"))
    _provider[conf["key_id"]] = (now, token)
    return token


async def _apns(rows: list[dict], thread: str, title: str, body: str, data: dict | None, transport=None) -> bool:
    global _said_unset
    conf = apns_settings()
    if not conf:
        if not _said_unset:
            _said_unset = True
            logger.warning("an iPhone is registered for push, but push.apns is not set up in settings.yaml")
        return False
    import httpx
    payload = {"aps": {"alert": {"title": title, "body": body[:300]}, "sound": "default", "thread-id": thread},
               "thread": thread, **(data or {})}
    headers = {"authorization": f"bearer {provider_token(conf)}", "apns-topic": conf["topic"],
               "apns-push-type": "alert", "apns-priority": "10"}
    ok = False
    try:
        async with httpx.AsyncClient(http2=transport is None, timeout=15.0, transport=transport) as client:
            for d in rows:
                host = APNS_SANDBOX if d.get("sandbox") else APNS
                r = await client.post(f"{host}/3/device/{d['token']}", json=payload, headers=headers)
                if r.status_code == 200:
                    ok = True
                    continue
                try:
                    reason = r.json().get("reason", "")
                except ValueError:
                    reason = ""
                # The app was removed, or this token was never Apple's: stop sending to it.
                if r.status_code == 410 or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
                    remove(d["token"])
                else:
                    logger.warning("push to %s: %s %s", d.get("name"), r.status_code, reason)
    except Exception:
        logger.exception("push to Apple failed")
    return ok


async def notify(thread: str, title: str, body: str, data: dict | None = None,
                 force: bool = False) -> bool:
    everyone = devices()
    if not everyone or (not force and not due(thread)):
        return False
    native = [d for d in everyone if d.get("kind") == "apns"]
    rows = [d for d in everyone if d.get("kind") != "apns"]
    reached = await _apns(native, thread, title, body, data) if native else False
    if not rows:
        return reached
    import httpx
    messages = [{"to": d["token"], "title": title, "body": body[:300], "sound": "default",
                 "data": {"thread": thread, **(data or {})}} for d in rows]
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(EXPO, json=messages, headers={"Accept": "application/json"})
            results = r.json().get("data") or []
    except Exception:
        logger.exception("push failed")
        return reached
    ok = reached
    for d, item in zip(rows, results):
        if item.get("status") == "ok":
            ok = True
        elif (item.get("details") or {}).get("error") == "DeviceNotRegistered":
            remove(d["token"])
        else:
            logger.warning("push to %s: %s", d.get("name"), item.get("message"))
    return ok
