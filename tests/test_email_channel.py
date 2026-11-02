import email as email_mod

from channels.email import EmailChannel, body_text


def _mime(raw: str):
    return email_mod.message_from_string(raw)


def test_body_text_plain_and_quote_trim():
    msg = _mime("""From: a@b.c
Subject: hi
Content-Type: text/plain

Please pay invoice 42.
Thanks!
On Mon, Sep 1, 2026 at 9:00 AM Someone <x@y.z> wrote:
> old quoted stuff
""")
    text = body_text(msg)
    assert "invoice 42" in text
    assert "quoted" not in text


def test_sender_allowlist_case_insensitive():
    ch = EmailChannel({"allowed_senders": ["Boss@Example.com"]}, None)
    assert ch.sender_allowed("boss@example.com")
    assert not ch.sender_allowed("evil@example.com")
