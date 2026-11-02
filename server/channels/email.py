"""Email as a channel: IMAP in, SMTP out, standard library only.

Setup: an address the team owns (a dedicated account or alias), its IMAP
and SMTP hosts, and an app password in .env as EMAIL_PASSWORD. Unseen
mail from allowed senders routes to the channel's agent; the agent's
replies go back by SMTP on the same subject thread.
"""
import asyncio
import runtime
import email as email_mod
import email.header
import email.mime.text
import imaplib
import re
import smtplib

import settings as cfg
from channels.base import Channel


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out = ""
    for text, enc in parts:
        out += text.decode(enc or "utf-8", "replace") if isinstance(text, bytes) else text
    return out


def body_text(msg) -> str:
    """Plain-text body, quoted reply tails trimmed."""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    text = payload.decode(part.get_content_charset() or "utf-8", "replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode(msg.get_content_charset() or "utf-8", "replace")
    lines = []
    for line in text.splitlines():
        if line.startswith(">") or re.match(r"^On .{10,80} wrote:$", line.strip()):
            break
        lines.append(line)
    return "\n".join(lines).strip()[:6000]


class EmailChannel(Channel):
    name = "email"

    def __init__(self, conf, on_message):
        super().__init__(conf, on_message)
        self._task: asyncio.Task | None = None
        self._threads: dict[str, dict] = {}  # sender -> {subject, message_id}

    def _password(self):
        return cfg.env_get("EMAIL_PASSWORD")

    def sender_allowed(self, sender: str) -> bool:
        allowed = {str(a).lower() for a in self.conf.get("allowed_senders") or []}
        return sender.lower() in allowed

    async def start(self):
        if not (self.conf.get("imap_host") and self.conf.get("address")
                and self._password()):
            self.detail = "needs imap_host, address, and EMAIL_PASSWORD in .env"
            self.log.warning("email: %s", self.detail)
            return
        try:
            ok = await asyncio.to_thread(self._check_login)
        except Exception as exc:
            self.detail = f"IMAP login failed: {str(exc)[:120]}"
            return
        if not ok:
            self.detail = "IMAP login failed"
            return
        self.connected = True
        self.detail = f"watching {self.conf['address']}"
        self._task = runtime.spawn(self._poll_loop(), "channel_email", "Email polling")
        self.log.info("email channel %s", self.detail)

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        self.connected = False

    def _check_login(self) -> bool:
        with imaplib.IMAP4_SSL(self.conf["imap_host"]) as imap:
            imap.login(self.conf["address"], self._password())
        return True

    def _fetch_unseen(self) -> list[dict]:
        out = []
        with imaplib.IMAP4_SSL(self.conf["imap_host"]) as imap:
            imap.login(self.conf["address"], self._password())
            imap.select(self.conf.get("folder") or "INBOX")
            _, data = imap.search(None, "UNSEEN")
            for num in (data[0].split() if data and data[0] else [])[:10]:
                _, msg_data = imap.fetch(num, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue
                msg = email_mod.message_from_bytes(msg_data[0][1])
                sender = email_mod.utils.parseaddr(msg.get("From", ""))[1]
                out.append({"num": num, "sender": sender,
                            "subject": _decode(msg.get("Subject")),
                            "message_id": msg.get("Message-ID", ""),
                            "text": body_text(msg)})
        return out

    def _mark_seen(self, num) -> None:
        """Only once a message has been handed to the agent: a crash in
        between must not lose mail."""
        with imaplib.IMAP4_SSL(self.conf["imap_host"]) as imap:
            imap.login(self.conf["address"], self._password())
            imap.select(self.conf.get("folder") or "INBOX")
            imap.store(num, "+FLAGS", "\\Seen")

    async def _poll_loop(self):
        poll = max(30, int(self.conf.get("poll_seconds") or 60))
        while True:
            try:
                for m in await asyncio.to_thread(self._fetch_unseen):
                    if m["text"] and self.sender_allowed(m["sender"]):
                        self._threads[m["sender"]] = {"subject": m["subject"],
                                                      "message_id": m["message_id"]}
                        text = (f"[email] subject: {m['subject']}\n{m['text']}"
                                if m["subject"] else m["text"])
                        await self.on_message(self.name, m["sender"], m["sender"], text)
                    elif m["text"]:
                        self.log.info("email: ignored unlisted sender %s", m["sender"])
                    await asyncio.to_thread(self._mark_seen, m["num"])
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.log.warning("email poll error: %s", exc)
            await asyncio.sleep(poll)

    def _smtp_send(self, to_addr: str, subject: str, text: str, in_reply_to: str):
        msg = email.mime.text.MIMEText(text)
        msg["From"] = self.conf["address"]
        msg["To"] = to_addr
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        host = self.conf.get("smtp_host") or self.conf["imap_host"].replace("imap", "smtp")
        try:
            with smtplib.SMTP_SSL(host, 465, timeout=30) as smtp:
                smtp.login(self.conf["address"], self._password())
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError):
            with smtplib.SMTP(host, 587, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(self.conf["address"], self._password())
                smtp.send_message(msg)

    async def send(self, chat_id: str, text: str) -> bool:
        thread = self._threads.get(chat_id) or {}
        subject = thread.get("subject") or "Message from your team"
        if subject and not subject.lower().startswith("re:"):
            subject = "Re: " + subject
        try:
            await asyncio.to_thread(self._smtp_send, chat_id, subject, text,
                                    thread.get("message_id") or "")
            return True
        except Exception as exc:
            self.log.warning("email send failed: %s", exc)
            return False
