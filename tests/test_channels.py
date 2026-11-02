from channels.imessage import IMessageChannel
from channels.whatsapp import WhatsAppChannel


def _im():
    return IMessageChannel({"enabled": True, "allowed_senders": ["+15551234567"]},
                           None)


def test_imessage_parse_text():
    ch = _im()
    got = ch.parse_inbound({"type": "new-message", "data": {
        "guid": "g1", "text": "hello", "isFromMe": False,
        "handle": {"address": "+15551234567"},
        "chats": [{"guid": "iMessage;-;+15551234567"}]}})
    assert got["text"] == "hello" and got["chat_id"] == "iMessage;-;+15551234567"
    # duplicate guid dropped
    assert ch.parse_inbound({"type": "new-message", "data": {
        "guid": "g1", "text": "hello", "isFromMe": False,
        "handle": {"address": "+15551234567"},
        "chats": [{"guid": "c"}]}}) is None


def test_imessage_parse_media_only():
    ch = _im()
    got = ch.parse_inbound({"type": "new-message", "data": {
        "guid": "g2", "text": "", "isFromMe": False,
        "attachments": [{"guid": "a1", "mimeType": "image/jpeg"}],
        "handle": {"address": "+15551234567"},
        "chats": [{"guid": "c"}]}})
    assert got["media"][0]["kind"] == "image"


def test_imessage_drops_own_and_tapbacks():
    ch = _im()
    assert ch.parse_inbound({"type": "new-message", "data": {
        "guid": "g3", "text": "x", "isFromMe": True}}) is None
    assert ch.parse_inbound({"type": "new-message", "data": {
        "guid": "g4", "text": "x", "isFromMe": False,
        "associatedMessageType": 2000}}) is None


def test_sender_allowlist():
    ch = _im()
    assert ch.sender_allowed("+15551234567")
    assert not ch.sender_allowed("+19998887777")


def test_whatsapp_parse_kinds():
    ch = WhatsAppChannel({"enabled": True}, None)
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"type": "text", "from": "15551234567", "text": {"body": "hi"}},
        {"type": "image", "from": "15551234567",
         "image": {"id": "m1", "caption": "look"}},
        {"type": "audio", "from": "15551234567", "audio": {"id": "m2"}},
    ]}}]}]}
    got = ch.parse_inbound(payload)
    assert got[0]["text"] == "hi"
    assert got[1]["media"][0]["kind"] == "image" and got[1]["text"] == "look"
    assert got[2]["media"][0]["kind"] == "audio"
