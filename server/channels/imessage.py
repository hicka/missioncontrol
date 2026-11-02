"""iMessage via a BlueBubbles server.

The BlueBubbles app (macOS) hosts iMessage and posts new-message webhooks
to this app's own port (/imessage-webhook, handled in main.py, which
forwards payloads here). Works from Windows too by pointing server_url at
a BlueBubbles server running on a Mac on the network.
"""
import asyncio
import uuid
from urllib.parse import quote

import httpx

import settings as cfg
from channels.base import Channel

WEBHOOK_PATH = "/imessage-webhook"


class IMessageChannel(Channel):
    name = "imessage"

    def __init__(self, conf, on_message):
        super().__init__(conf, on_message)
        self._seen_guids: dict[str, bool] = {}

    def _base_and_pw(self):
        pw = cfg.env_get("BLUEBUBBLES_PASSWORD")
        return (self.conf.get("server_url") or "http://localhost:1234").rstrip("/"), pw

    async def start(self):
        base, pw = self._base_and_pw()
        if not pw:
            self.detail = "no BLUEBUBBLES_PASSWORD in .env"
            self.log.warning("iMessage: %s", self.detail)
            return
        url = f"http://127.0.0.1:{cfg.port('app')}{WEBHOOK_PATH}?password={quote(pw, safe='')}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{base}/api/v1/webhook", params={"password": pw})
                existing = r.json().get("data") or []
                if not any(w.get("url") == url for w in existing):
                    r = await client.post(f"{base}/api/v1/webhook", params={"password": pw},
                                          json={"url": url, "events": ["new-message"]})
                    if not (200 <= r.status_code < 300):
                        self.detail = f"webhook registration HTTP {r.status_code}"
                        return
            self.connected = True
            self.detail = "webhook registered"
            self.log.info("iMessage webhook active on %s", base)
        except Exception as exc:
            self.detail = f"BlueBubbles unreachable: {exc}"
            self.log.warning("iMessage inactive: %s", exc)

    def parse_inbound(self, payload: dict) -> dict | None:
        """{guid, text, sender, chat_id} from a new-message webhook, or None."""
        if payload.get("type") not in (None, "new-message", "message", "updated-message"):
            return None
        rec = payload.get("data") or {}
        if rec.get("isFromMe") or rec.get("fromMe"):
            return None
        if isinstance(rec.get("associatedMessageType"), int) and rec["associatedMessageType"] != 0:
            return None  # tapback
        guid = rec.get("guid")
        if not guid:
            return None
        import db
        if not db.seen_once("imessage", guid):
            return None
        text = (rec.get("text") or "").strip()
        media = []
        for att in (rec.get("attachments") or [])[:3]:
            mime = (att.get("mimeType") or "").lower()
            if att.get("guid") and (mime.startswith("image/") or mime.startswith("audio/")):
                media.append({"kind": "image" if mime.startswith("image/") else "audio",
                              "mime": mime, "ref": att["guid"],
                              "name": att.get("transferName") or "attachment"})
        if not text and not media:
            return None
        handle = rec.get("handle") or {}
        sender = handle.get("address") or ""
        chats = rec.get("chats") or []
        chat_id = chats[0].get("guid") if chats else (f"any;-;{sender}" if sender else None)
        if not chat_id:
            return None
        return {"guid": guid, "text": text, "sender": sender, "chat_id": chat_id,
                "media": media}

    def password(self) -> str | None:
        return cfg.env_get("BLUEBUBBLES_PASSWORD")

    async def fetch_media(self, item: dict) -> bytes | None:
        base, pw = self._base_and_pw()
        if not pw:
            return None
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.get(
                    f"{base}/api/v1/attachment/{item['ref']}/download",
                    params={"password": pw})
                if r.status_code == 200 and len(r.content) < 15_000_000:
                    return r.content
        except Exception as exc:
            self.log.warning("attachment fetch failed: %s", exc)
        return None

    async def send_file(self, chat_id: str, data: bytes, mime: str, name: str) -> bool:
        base, pw = self._base_and_pw()
        if not pw:
            return False
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(
                    f"{base}/api/v1/message/attachment", params={"password": pw},
                    data={"chatGuid": chat_id, "name": name,
                          "method": "apple-script",
                          "tempGuid": f"missioncontrol-{uuid.uuid4()}"},
                    files={"attachment": (name, data, mime)})
                return 200 <= r.status_code < 300
        except httpx.TimeoutException:
            return True  # BlueBubbles often delivers despite the slow response
        except Exception as exc:
            self.log.warning("file send error: %s", exc)
            return False

    async def send_voice(self, chat_id: str, data: bytes, mime: str) -> bool:
        base, pw = self._base_and_pw()
        if not pw:
            return False
        ext = "m4a" if "mp4" in mime else "mp3"
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(
                    f"{base}/api/v1/message/attachment", params={"password": pw},
                    data={"chatGuid": chat_id, "name": f"voice.{ext}",
                          "method": "apple-script",
                          "tempGuid": f"missioncontrol-{uuid.uuid4()}"},
                    files={"attachment": (f"voice.{ext}", data, mime)})
                return 200 <= r.status_code < 300
        except httpx.TimeoutException:
            return True  # BlueBubbles often delivers despite the slow response
        except Exception as exc:
            self.log.warning("voice send error: %s", exc)
            return False

    async def send(self, chat_id: str, text: str) -> bool:
        base, pw = self._base_and_pw()
        if not pw:
            return False
        body = {"chatGuid": chat_id, "message": text, "method": "apple-script",
                "tempGuid": f"missioncontrol-{uuid.uuid4()}"}
        async with httpx.AsyncClient(timeout=120.0) as client:
            for attempt in range(2):
                try:
                    r = await client.post(f"{base}/api/v1/message/text",
                                          params={"password": pw}, json=body)
                    if 200 <= r.status_code < 300:
                        return True
                    self.log.warning("send HTTP %s: %s", r.status_code, r.text[:200])
                except httpx.TimeoutException:
                    # BlueBubbles often delivers despite a slow response;
                    # retrying here double-sends. Treat as probably-delivered.
                    self.log.warning("send response timed out (likely delivered anyway)")
                    return True
                except Exception as exc:
                    self.log.warning("send error: %s", exc)
                if attempt == 0:
                    await asyncio.sleep(2)
                    body["tempGuid"] = f"missioncontrol-{uuid.uuid4()}"
        return False
