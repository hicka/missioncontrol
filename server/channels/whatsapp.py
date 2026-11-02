"""WhatsApp via the official Business Cloud API.

Setup: create a Meta developer app with the WhatsApp product, put its
permanent access token in .env as WHATSAPP_ACCESS_TOKEN, and fill
phone_number_id + verify_token in settings. Inbound arrives on the
public listener at /whatsapp-webhook (served through the app's tunnel);
Meta's dashboard needs that public URL. Quick tunnels rotate on every
restart, so for WhatsApp use a stable URL (a named cloudflared tunnel on
your own domain) and paste it into the Meta webhook config once.
"""
import httpx

import settings as cfg
from channels.base import Channel

GRAPH = "https://graph.facebook.com/v21.0"
WEBHOOK_PATH = "/whatsapp-webhook"


class WhatsAppChannel(Channel):
    name = "whatsapp"

    def _token(self) -> str | None:
        return cfg.env_get("WHATSAPP_ACCESS_TOKEN")

    async def start(self):
        token = self._token()
        pnid = self.conf.get("phone_number_id") or ""
        if not token or not pnid:
            self.detail = "needs WHATSAPP_ACCESS_TOKEN in .env and phone_number_id"
            self.log.warning("whatsapp: %s", self.detail)
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{GRAPH}/{pnid}",
                                     headers={"Authorization": f"Bearer {token}"})
                if r.status_code != 200:
                    self.detail = f"Graph API HTTP {r.status_code}"
                    return
        except Exception as exc:
            self.detail = f"unreachable: {exc}"
            return
        self.connected = True
        self.detail = "connected; webhook served at /whatsapp-webhook"
        self.log.info("whatsapp connected (phone_number_id %s)", pnid)

    def verify(self, mode: str, token: str, challenge: str) -> str | None:
        """Meta webhook GET verification handshake."""
        if mode == "subscribe" and token == (self.conf.get("verify_token") or ""):
            return challenge
        return None

    def parse_inbound(self, payload: dict) -> list[dict]:
        """[{text, sender, chat_id}] from a webhook delivery."""
        out = []
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                for msg in value.get("messages") or []:
                    sender = msg.get("from") or ""
                    if not sender:
                        continue
                    if msg.get("id"):
                        import db
                        if not db.seen_once("whatsapp", msg["id"]):
                            continue
                    mtype = msg.get("type")
                    text, media = "", []
                    if mtype == "text":
                        text = ((msg.get("text") or {}).get("body") or "").strip()
                    elif mtype == "image":
                        img = msg.get("image") or {}
                        text = (img.get("caption") or "").strip()
                        if img.get("id"):
                            media.append({"kind": "image",
                                          "mime": img.get("mime_type") or "image/jpeg",
                                          "ref": img["id"], "name": "photo.jpg"})
                    elif mtype == "audio":
                        aud = msg.get("audio") or {}
                        if aud.get("id"):
                            media.append({"kind": "audio",
                                          "mime": aud.get("mime_type") or "audio/ogg",
                                          "ref": aud["id"], "name": "voice.ogg"})
                    if text or media:
                        out.append({"text": text, "sender": sender,
                                    "chat_id": sender, "media": media})
        return out

    async def fetch_media(self, item: dict) -> bytes | None:
        token = self._token()
        if not token:
            return None
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.get(f"{GRAPH}/{item['ref']}",
                                     headers={"Authorization": f"Bearer {token}"})
                url = r.json().get("url")
                if not url:
                    return None
                r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
                if r.status_code == 200 and len(r.content) < 15_000_000:
                    return r.content
        except Exception as exc:
            self.log.warning("whatsapp media fetch failed: %s", exc)
        return None

    async def send(self, chat_id: str, text: str) -> bool:
        token = self._token()
        pnid = self.conf.get("phone_number_id") or ""
        if not token or not pnid:
            return False
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    f"{GRAPH}/{pnid}/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"messaging_product": "whatsapp", "to": chat_id,
                          "type": "text", "text": {"body": text}})
                if r.status_code == 200:
                    return True
                self.log.warning("whatsapp send HTTP %s: %s", r.status_code, r.text[:200])
        except Exception as exc:
            self.log.warning("whatsapp send error: %s", exc)
        return False
