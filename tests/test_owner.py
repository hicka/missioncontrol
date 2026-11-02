"""The owner can name themselves; it persists and derives initials."""


def test_set_user_derives_initials_and_persists(tmp_path, monkeypatch):
    import orchestrator
    monkeypatch.setattr(orchestrator, "LOCAL_PATH", tmp_path / "agents.local.yaml")
    o = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
    o.user = {"name": "You", "initials": "U"}

    assert o.set_user("Fathuhulla Ahmed") == {"name": "Fathuhulla Ahmed", "initials": "FA"}
    assert o.set_user("madi", "MD")["initials"] == "MD"
    assert o.set_user("") == {"name": "You", "initials": "Y"}   # empty falls back, never blank

    # the override is written to the local file and survives a reload
    import yaml
    saved = yaml.safe_load((tmp_path / "agents.local.yaml").read_text())
    assert saved["user"] == {"name": "You", "initials": "Y"}


def test_local_user_override_wins_in_load_config(tmp_path, monkeypatch):
    import orchestrator
    monkeypatch.setattr(orchestrator, "CONFIG_PATH", tmp_path / "agents.yaml")
    monkeypatch.setattr(orchestrator, "LOCAL_PATH", tmp_path / "agents.local.yaml")
    (tmp_path / "agents.yaml").write_text(
        "user:\n  name: Alex\n  initials: A\nagents:\n  - id: cto\n    name: CTO\n    prompt: x\n")
    (tmp_path / "agents.local.yaml").write_text("user:\n  name: Sam\n  initials: H\n")
    cfg = orchestrator.load_config()
    assert cfg["user"] == {"name": "Sam", "initials": "H"}   # local beats the seeded default
