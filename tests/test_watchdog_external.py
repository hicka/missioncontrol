"""The outside watchdog: restart what stopped answering, and know when to stop."""
import io
import json
import importlib.machinery
import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parent.parent / "bin" / "mc-watchdog"
loader = importlib.machinery.SourceFileLoader("mc_watchdog", str(path))
spec = importlib.util.spec_from_loader("mc_watchdog", loader)
mc = importlib.util.module_from_spec(spec)
loader.exec_module(mc)

DOWN = "no answer"


def test_a_passing_blip_does_not_restart_anything():
    d = mc.Decider()
    assert d.observe(DOWN, True, 1000) == "wait"
    assert d.observe(DOWN, True, 1060) == "wait"
    assert d.observe(None, True, 1120) == "ok"
    assert d.fails == 0


def test_three_failures_in_a_row_restart_it():
    d = mc.Decider()
    assert [d.observe(DOWN, True, 1000 + i) for i in range(3)] == ["wait", "wait", "restart"]


def test_a_slow_boot_is_given_time():
    d = mc.Decider()
    for i in range(3):
        d.observe(DOWN, True, 1000 + i)
    assert all(d.observe(DOWN, True, 1010 + i) == "wait" for i in range(5))
    assert d.observe(DOWN, True, 1400) == "restart"


def test_a_service_the_owner_stopped_stays_stopped():
    d = mc.Decider()
    assert all(d.observe(DOWN, False, 1000 + i) == "ok" for i in range(10))


def test_it_gives_up_once_rather_than_looping():
    d = mc.Decider()
    now, verdicts = 1000, []
    for _ in range(25):      # fifty minutes: inside the hour the limit covers
        verdicts.append(d.observe(DOWN, True, now))
        now += 120
    assert verdicts.count("restart") == mc.MAX_RESTARTS_PER_HOUR
    assert verdicts.count("give_up") == 1


def test_recovering_clears_the_gave_up_state():
    d = mc.Decider()
    d.gave_up = True
    d.observe(None, True, 1000)
    assert not d.gave_up


def test_a_router_that_died_counts_as_down(monkeypatch):
    import io
    import json

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mc.urllib.request, "urlopen",
                        lambda url, timeout: Reply(json.dumps({"ok": True, "router_alive": False}).encode()))
    assert "Router" in mc.probe("http://x")
    monkeypatch.setattr(mc.urllib.request, "urlopen",
                        lambda url, timeout: Reply(json.dumps({"ok": True, "router_alive": True}).encode()))
    assert mc.probe("http://x") is None


def _serving(monkeypatch, answer):
    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(url, timeout=None):
        if isinstance(answer, Exception):
            raise answer
        return Reply(json.dumps(answer).encode())
    monkeypatch.setattr(mc.urllib.request, "urlopen", urlopen)


def test_once_says_all_is_well(monkeypatch, capsys):
    _serving(monkeypatch, {"ok": True, "router_alive": True, "problems": []})
    assert mc.status("http://x") == mc.OK
    assert "ok" in capsys.readouterr().out


def test_once_says_a_restart_may_help_when_it_does_not_answer(monkeypatch):
    _serving(monkeypatch, OSError("refused"))
    assert mc.status("http://x") == mc.RESTART_MAY_HELP


def test_once_says_a_restart_may_help_when_the_router_is_down(monkeypatch):
    _serving(monkeypatch, {"ok": True, "router_alive": False})
    assert mc.status("http://x") == mc.RESTART_MAY_HELP


def test_once_says_it_needs_a_person_for_what_a_restart_would_not_fix(monkeypatch, capsys):
    _serving(monkeypatch, {"ok": True, "router_alive": True, "problems": ["Webhook tunnel: exited"]})
    assert mc.status("http://x") == mc.NEEDS_A_PERSON
    assert "Webhook tunnel" in capsys.readouterr().out


def test_when_restarts_stop_helping_the_owner_is_told_without_the_app(monkeypatch, tmp_path):
    (tmp_path / "devices.json").write_text(json.dumps([{"token": "ExponentPushToken[x]"}]))
    sent, shown = [], []

    class Reply:
        def close(self):
            pass

    def urlopen(request, timeout=None):
        sent.append(json.loads(request.data))
        return Reply()
    monkeypatch.setattr(mc.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(mc.subprocess, "run", lambda args, **kw: shown.append(args))
    mc.tell_the_owner("no answer. 3 restarts did not help", str(tmp_path))
    assert sent[0][0]["to"] == "ExponentPushToken[x]" and "did not help" in sent[0][0]["body"]
    if mc.sys.platform == "darwin":
        assert shown and shown[0][0] == "osascript"


def test_a_phone_that_cannot_be_reached_does_not_stop_the_watchdog(monkeypatch, tmp_path):
    monkeypatch.setattr(mc.subprocess, "run", lambda args, **kw: None)
    mc.tell_the_owner("down", str(tmp_path))        # no devices.json at all
