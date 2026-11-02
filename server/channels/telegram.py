"""Telegram via the Bot API, long polling. Fully cross-platform.

Setup: create a bot with @BotFather, put the token in .env as
TELEGRAM_BOT_TOKEN, and add your numeric user id or @username to
allowed_senders. No webhook or public URL needed.
"""
import asyncio
import runtime

import httpx

import settings as cfg
from channels.base import Channel

API = "https://api.telegram.org"


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, conf, on_message):
        super().__init__(conf, on_message)
        self._task: asyncio.Task | None = None
        self._offset = 0

    def _token(self) -> str | None:
        return cfg.env_get("TELEGRAM_BOT_TOKEN")

    def sender_allowed(self, sender) -> bool:
        allowed = {str(a).lstrip("@").lower() for a in self.conf.get("allowed_senders") or []}
        uid, username = sender
        return str(uid) in allowed or (username or "").lower() in allowed

    async def start(self):
        token = self._token()
        if not token:
            self.detail = "no TELEGRAM_BOT_TOKEN in .env"
            self.log.warning("telegram: %s", self.detail)
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{API}/bot{token}/getMe")
                me = r.json()
                if not me.get("ok"):
                    self.detail = f"getMe failed: {me.get('description', r.status_code)}"
                    return
                self.detail = f"connected as @{me['result'].get('username', '?')}"
        except Exception as exc:
            self.detail = f"unreachable: {exc}"
            return
        self.connected = True
        self._task = runtime.spawn(self._poll_loop(), "channel_telegram", "Telegram polling")
        self.log.info("telegram %s", self.detail)

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        self.connected = False

    async def _poll_loop(self):
        token = self._token()
        async with httpx.AsyncClient(timeout=60.0) as client:
            while True:
                try:
                    r = await client.get(f"{API}/bot{token}/getUpdates",
                                         params={"timeout": 50, "offset": self._offset,
                                                 "allowed_updates": '["message"]'})
                    data = r.json()
                    if not data.get("ok"):
                        # a bad token or a second poller: back off instead of spinning
                        self.log.warning("telegram: %s", data.get("description") or r.status_code)
                        await asyncio.sleep(30)
                        continue
                    for upd in data.get("result") or []:
                        self._offset = upd["update_id"] + 1
                        msg = upd.get("message") or {}
                        text = (msg.get("text") or msg.get("caption") or "").strip()
                        media = []
                        if msg.get("photo"):
                            media.append({"kind": "image", "mime": "image/jpeg",
                                          "ref": msg["photo"][-1]["file_id"],
                                          "name": "photo.jpg"})
                        voice = msg.get("voice") or msg.get("audio")
                        if voice:
                            media.append({"kind": "audio",
                                          "mime": voice.get("mime_type") or "audio/ogg",
                                          "ref": voice["file_id"], "name": "voice.ogg"})
                        frm = msg.get("from") or {}
                        chat_id = str((msg.get("chat") or {}).get("id") or "")
                        if (not text and not media) or not chat_id:
                            continue
                        if not self.sender_allowed((frm.get("id"), frm.get("username"))):
                            self.log.info("telegram: ignored unlisted sender %s", frm.get("id"))
                            continue
                        await self.on_message(self.name, chat_id,
                                              str(frm.get("id") or ""), text, media)
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    self.log.warning("telegram poll error: %s", exc)
                    await asyncio.sleep(10)

    async def fetch_media(self, item: dict) -> bytes | None:
        token = self._token()
        if not token:
            return None
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.get(f"{API}/bot{token}/getFile",
                                     params={"file_id": item["ref"]})
                path = (r.json().get("result") or {}).get("file_path")
                if not path:
                    return None
                r = await client.get(f"{API}/file/bot{token}/{path}")
                if r.status_code == 200 and len(r.content) < 15_000_000:
                    return r.content
        except Exception as exc:
            self.log.warning("telegram media fetch failed: %s", exc)
        return None

    async def send_file(self, chat_id: str, data: bytes, mime: str, name: str) -> bool:
        token = self._token()
        if not token:
            return False
        if mime.startswith("image/"):
            method, field = "sendPhoto", "photo"
        else:
            method, field = "sendDocument", "document"
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(f"{API}/bot{token}/{method}",
                                      data={"chat_id": chat_id},
                                      files={field: (name, data, mime)})
                return bool(r.json().get("ok"))
        except Exception as exc:
            self.log.warning("file send error: %s", exc)
            return False

    async def send_voice(self, chat_id: str, data: bytes, mime: str) -> bool:
        token = self._token()
        if not token:
            return False
        ext = "m4a" if "mp4" in mime else "mp3"
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(f"{API}/bot{token}/sendAudio",
                                      data={"chat_id": chat_id},
                                      files={"audio": (f"voice.{ext}", data, mime)})
                return bool(r.json().get("ok"))
        except Exception as exc:
            self.log.warning("voice send error: %s", exc)
            return False

    async def send(self, chat_id: str, text: str) -> bool:
        token = self._token()
        if not token:
            return False
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(f"{API}/bot{token}/sendMessage",
                                      json={"chat_id": chat_id, "text": text})
                return bool(r.json().get("ok"))
        except Exception as exc:
            self.log.warning("telegram send error: %s", exc)
            return False
