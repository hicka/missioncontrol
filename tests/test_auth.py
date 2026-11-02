import importlib


def _main():
    import main
    return main


def test_local_requests_trusted():
    m = _main()
    assert m._request_authorized("127.0.0.1", "", "")
    assert m._request_authorized("::1", "", "")


def test_remote_denied_without_token_config(monkeypatch):
    m = _main()
    monkeypatch.setattr(m, "_api_token", lambda: None)
    assert not m._request_authorized("192.168.1.50", "Bearer anything", "")


def test_remote_bearer_and_param(monkeypatch):
    m = _main()
    monkeypatch.setattr(m, "_api_token", lambda: "sekret-token")
    assert m._request_authorized("192.168.1.50", "Bearer sekret-token", "")
    assert m._request_authorized("100.64.0.7", "", "sekret-token")
    assert not m._request_authorized("192.168.1.50", "Bearer wrong", "")
    assert not m._request_authorized("192.168.1.50", "", "")
