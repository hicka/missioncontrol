"""Channel manager: settings, lifecycle, and outbound routing.

settings.yaml:

channels:
  imessage: {enabled, server_url, allowed_senders, default_agent}
  telegram: {enabled, allowed_senders, default_agent}
  whatsapp: {enabled, phone_number_id, verify_token, allowed_senders, default_agent}

A legacy top-level `imessage:` section (pre-channels layout) is migrated
into channels.imessage on first load.
"""
import logging

import settings as cfg
from channels.email import EmailChannel
from channels.imessage import IMessageChannel
from channels.telegram import TelegramChannel
from channels.whatsapp import WhatsAppChannel

logger = logging.getLogger("missioncontrol.channels")

CHANNEL_TYPES = {
    "imessage": IMessageChannel,
    "telegram": TelegramChannel,
    "whatsapp": WhatsAppChannel,
    "email": EmailChannel,
}

DEFAULTS = {
    "imessage": {"enabled": False, "server_url": "http://localhost:1234",
                 "allowed_senders": [], "default_agent": ""},
    "telegram": {"enabled": False, "allowed_senders": [], "default_agent": ""},
    "whatsapp": {"enabled": False, "phone_number_id": "", "verify_token": "",
                 "allowed_senders": [], "default_agent": ""},
    "email": {"enabled": False, "imap_host": "", "smtp_host": "", "address": "",
              "folder": "INBOX", "poll_seconds": 60,
              "allowed_senders": [], "default_agent": ""},
}

SECRET_ENV = {
    "imessage": "BLUEBUBBLES_PASSWORD",
    "telegram": "TELEGRAM_BOT_TOKEN",
    "whatsapp": "WHATSAPP_ACCESS_TOKEN",
    "email": "EMAIL_PASSWORD",
}


def channel_settings() -> dict:
    s = cfg.load()
    channels = s.get("channels") or {}
    legacy = s.get("imessage")
    if legacy and "imessage" not in channels:
        channels["imessage"] = {k: legacy[k] for k in
                                ("enabled", "server_url", "allowed_senders", "default_agent")
                                if k in legacy}
        s["channels"] = channels
        s.pop("imessage", None)
        cfg.save(s)
        logger.info("migrated legacy imessage settings into channels section")
    return {name: {**DEFAULTS[name], **(channels.get(name) or {})} for name in DEFAULTS}


def save_channel(name: str, values: dict):
    s = cfg.load()
    channels = s.get("channels") or {}
    cur = {**DEFAULTS.get(name, {}), **(channels.get(name) or {})}
    cur.update(values)
    channels[name] = cur
    s["channels"] = channels
    s.pop("imessage", None)  # never resurrect the legacy section
    cfg.save(s)


class Manager:
    def __init__(self):
        self.channels: dict[str, object] = {}
        self.on_message = None  # async fn(channel, chat_id, sender, text)

    async def start(self):
        conf = channel_settings()
        for name, cls in CHANNEL_TYPES.items():
            ch = cls(conf[name], self.on_message)
            self.channels[name] = ch
            if conf[name].get("enabled"):
                try:
                    await ch.start()
                except Exception:
                    logger.exception("channel %s failed to start", name)

    async def stop(self):
        for ch in self.channels.values():
            try:
                await ch.stop()
            except Exception:
                pass
        self.channels = {}

    async def start_one(self, name):
        await self.stop_one(name)
        conf = channel_settings()[name]
        ch = CHANNEL_TYPES[name](conf, self.on_message)
        self.channels[name] = ch
        await ch.start()
        if not ch.connected:
            raise RuntimeError(ch.detail or f"{name} did not connect")

    async def stop_one(self, name):
        ch = self.channels.pop(name, None)
        if ch:
            await ch.stop()

    async def restart(self):
        await self.stop()
        await self.start()

    def get(self, name: str):
        return self.channels.get(name)

    async def send(self, channel: str, chat_id: str, text: str) -> bool:
        ch = self.channels.get(channel)
        if not ch:
            return False
        return await ch.send(chat_id, text)

    def statuses(self) -> dict:
        conf = channel_settings()
        out = {}
        for name in CHANNEL_TYPES:
            ch = self.channels.get(name)
            st = ch.status() if ch else {"enabled": False, "connected": False, "detail": ""}
            st["enabled"] = bool(conf[name].get("enabled"))
            st["secret_set"] = bool(cfg.env_get(SECRET_ENV[name]))
            out[name] = st
        return out


manager = Manager()
