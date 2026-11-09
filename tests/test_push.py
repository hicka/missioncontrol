"""Push reaches a phone through whoever delivers to it: Expo, or Apple for the native app."""
import asyncio
import json

import httpx
import pytest

import push


@pytest.fixture
def phones(tmp_path, monkeypatch):
    monkeypatch.setattr(push, "DEVICES", tmp_path / "devices.json")
    monkeypatch.setattr(push, "_last", {})
    monkeypatch.setattr(push, "_provider", {})
    return tmp_path


def _key() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def test_the_provider_token_is_signed_for_the_owners_account_and_kept_for_a_while(phones):
    import base64
    conf = {"key": _key(), "key_id": "ABC123DEFG", "team_id": "TEAM123456", "topic": "com.example.app"}
    token = push.provider_token(conf, now=1_000_000)
    head, body, signature = token.split(".")
    pad = lambda part: base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    assert json.loads(pad(head)) == {"alg": "ES256", "kid": "ABC123DEFG"}
    assert json.loads(pad(body)) == {"iss": "TEAM123456", "iat": 1_000_000}
    assert len(pad(signature)) == 64
    assert push.provider_token(conf, now=1_000_000 + 600) == token          # not a new one for every push
    assert push.provider_token(conf, now=1_000_000 + 3000) != token         # and never an hour old


def test_a_native_phone_is_pushed_through_apple_and_a_dead_token_is_dropped(phones, monkeypatch):
    monkeypatch.setattr(push, "apns_settings", lambda: {"key": _key(), "key_id": "K", "team_id": "T", "topic": "com.example.app"})
    push.register("aa" * 32, "phone", "apns")
    push.register("bb" * 32, "old phone", "apns", sandbox=True)
    seen = []

    def handler(request):
        seen.append((str(request.url), dict(request.headers), json.loads(request.content)))
        if "bbbb" in str(request.url):
            return httpx.Response(410, json={"reason": "Unregistered"})
        return httpx.Response(200)

    ok = asyncio.run(push._apns(push.devices(), "dm:lead", "Lead", "The fix is live.", {"approval": "a1"},
                                transport=httpx.MockTransport(handler)))
    assert ok and len(seen) == 2
    url, headers, payload = seen[0]
    assert url == f"https://api.push.apple.com/3/device/{'aa' * 32}"
    assert headers["apns-topic"] == "com.example.app" and headers["authorization"].startswith("bearer ")
    assert payload["aps"]["alert"] == {"title": "Lead", "body": "The fix is live."} and payload["approval"] == "a1"
    assert seen[1][0].startswith("https://api.sandbox.push.apple.com/")      # a development build's token
    assert [d["token"] for d in push.devices()] == ["aa" * 32]               # the dead one is gone


def test_without_a_key_a_native_phone_is_not_pushed_and_the_other_kind_still_is(phones, monkeypatch):
    monkeypatch.setattr(push, "apns_settings", lambda: None)
    push.register("aa" * 32, "phone", "apns")
    push.register("ExponentPushToken[abc]", "android")
    sent = []

    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            sent.append((url, json))
            return httpx.Response(200, json={"data": [{"status": "ok"}]})
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    assert asyncio.run(push.notify("dm:lead", "Lead", "hello", force=True))
    assert len(sent) == 1 and sent[0][0] == push.EXPO and [m["to"] for m in sent[0][1]] == ["ExponentPushToken[abc]"]
