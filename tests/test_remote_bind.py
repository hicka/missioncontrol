"""Where the app listens, and the paged thread endpoint the UI reads."""
import asyncio


def _main():
    import main
    return main


def test_bind_host_is_loopback_until_the_owner_exposes_it(monkeypatch):
    m = _main()
    monkeypatch.delenv("MISSIONCONTROL_HOST", raising=False)
    monkeypatch.setattr(m.cfg, "load", lambda: {})
    assert m.bind_host() == "127.0.0.1"
    monkeypatch.setattr(m.cfg, "load", lambda: {"server": {"expose": False}})
    assert m.bind_host() == "127.0.0.1"
    monkeypatch.setattr(m.cfg, "load", lambda: {"server": {"expose": True}})
    assert m.bind_host() == "0.0.0.0"


def test_bind_host_stays_local_when_settings_cannot_be_read(monkeypatch):
    m = _main()
    monkeypatch.delenv("MISSIONCONTROL_HOST", raising=False)

    def boom():
        raise OSError("settings.yaml is unreadable")

    monkeypatch.setattr(m.cfg, "load", boom)
    assert m.bind_host() == "127.0.0.1"


def test_env_overrides_the_setting(monkeypatch):
    m = _main()
    monkeypatch.setenv("MISSIONCONTROL_HOST", "10.0.0.4")
    monkeypatch.setattr(m.cfg, "load", lambda: {"server": {"expose": True}})
    assert m.bind_host() == "10.0.0.4"


def test_desktop_and_the_service_agree_on_the_host():
    """The bundle and the source entry point must not drift apart again."""
    import inspect
    import desktop
    assert "bind_host()" in inspect.getsource(desktop.run_server)
    src = open(_main().__file__).read()
    assert 'if __name__ == "__main__":' in src and "host=bind_host()" in src


def test_thread_endpoint_pages_and_reports_more():
    import db
    m = _main()
    for i in range(12):
        db.add_message("dm:api", "api", f"api line {i}")
    first = asyncio.run(m.thread("dm:api", limit=5))
    assert len(first["messages"]) == 5
    assert first["has_more"] and first["at_latest"]

    oldest = first["messages"][0]
    older = asyncio.run(m.thread("dm:api", before_ts=oldest["ts"], before_id=oldest["id"], limit=5))
    assert older["messages"][-1]["id"] < oldest["id"]
    assert not older["at_latest"]

    whole = asyncio.run(m.thread("dm:api", limit=300))
    assert not whole["has_more"]


def test_thread_endpoint_lands_on_a_searched_message():
    import db
    m = _main()
    target = db.add_message("dm:jump", "jump", "the needle")
    for i in range(8):
        db.add_message("dm:jump", "jump", f"hay {i}")
    page = asyncio.run(m.thread("dm:jump", at_id=target["id"], limit=5))
    assert page["messages"][-1]["id"] == target["id"]   # the hit anchors the page
    assert not page["at_latest"]


def test_search_endpoint_ignores_an_empty_query():
    import db
    m = _main()
    db.add_message("dm:api", "api", "a distinctive kingfisher sighting")
    assert asyncio.run(m.search_history(q="  "))["results"] == []
    hits = asyncio.run(m.search_history(q="kingfisher"))["results"]
    assert any("kingfisher" in r["text"] for r in hits)
