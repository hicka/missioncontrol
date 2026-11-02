"""Common shape for messaging channels.

A channel connects one external messaging surface (iMessage, Telegram,
WhatsApp) to the team: inbound user texts are handed to `on_message`,
and agent replies mirror back out through `send`. Each channel routes
its inbound traffic to its own configured default agent.
"""
import logging


class Channel:
    name = "base"

    def __init__(self, conf: dict, on_message):
        """on_message(channel_name, chat_id, sender, text) -> awaitable"""
        self.conf = conf
        self.on_message = on_message
        self.connected = False
        self.detail = ""
        self.log = logging.getLogger(f"missioncontrol.{self.name}")

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, chat_id: str, text: str) -> bool:
        raise NotImplementedError

    def sender_allowed(self, sender: str) -> bool:
        allowed = [str(a) for a in self.conf.get("allowed_senders") or []]
        return str(sender) in allowed

    def status(self) -> dict:
        return {"enabled": bool(self.conf.get("enabled")),
                "connected": self.connected, "detail": self.detail}
