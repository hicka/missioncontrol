"""Seamless fleet: a join code carries key+url+node, and joining is symmetric."""


def test_join_code_round_trips(monkeypatch):
    import sync
    import settings as cfg
    store = {"fleet": {"node_name": "studio"}}
    monkeypatch.setattr(cfg, "load", lambda: store)
    monkeypatch.setattr(cfg, "save", lambda s: store.update(s))
    monkeypatch.setattr(cfg, "env_get", lambda n: None)
    keys = {}
    monkeypatch.setattr(cfg, "env_set", lambda n, v: keys.__setitem__(n, v))

    code = sync.make_join_code("http://studio.tailnet.ts.net:8902")
    assert keys["SYNC_KEY"]                                  # minted on first use
    d = sync.read_join_code(code)
    assert d["node"] == "studio" and d["url"].endswith(":8902") and d["key"] == keys["SYNC_KEY"]
    assert sync.read_join_code("not-a-code") is None


def test_add_peer_is_live_and_dedupes(monkeypatch):
    import sync
    import settings as cfg
    store = {"fleet": {"node_name": "laptop", "peers": []}}
    monkeypatch.setattr(cfg, "load", lambda: dict(store))
    monkeypatch.setattr(cfg, "save", lambda s: store.update(s))
    sync.add_peer("Studio", "http://studio:8902/")
    sync.add_peer("studio", "http://studio-new:8902")       # same node, updated url
    peers = store["fleet"]["peers"]
    assert len(peers) == 1 and peers[0] == {"name": "studio", "url": "http://studio-new:8902"}


def test_remote_roster_addresses_agents_as_agent_at_node():
    import sync
    sync.peer_state.clear()
    sync.peer_state["laptop"] = {"ok": True, "agents": [{"id": "webdev", "name": "Web Dev"}]}
    sync.peer_state["down"] = {"ok": False, "agents": [{"id": "x"}]}
    roster = sync.remote_agents()
    assert {r["id"] for r in roster} == {"webdev@laptop"}    # only reachable peers
    sync.peer_state.clear()
