"""The heartbeat keeps its cadence across restarts instead of pulsing on boot."""
import time

from test_kernel import isolated  # noqa: F401


def test_last_pulse_survives_a_restart(tmp_path, monkeypatch):
    import cron
    monkeypatch.setattr(cron, "_last_pulse_file", lambda: tmp_path / "heartbeat.last")
    assert cron.load_last_pulse() == 0.0
    cron.save_last_pulse(1_700_000_000.4)
    assert cron.load_last_pulse() == 1_700_000_000.0


def test_pulse_is_due_only_after_the_interval():
    import cron
    now = time.time()
    assert cron.pulse_due(0.0, 30, now)                       # never pulsed: due
    assert not cron.pulse_due(now - 10 * 60, 30, now)         # 10 min ago: not yet
    assert cron.pulse_due(now - 31 * 60, 30, now)             # 31 min ago: due


def test_settings_floor_and_defaults(monkeypatch):
    import cron
    import settings as cfg
    monkeypatch.setattr(cfg, "load", lambda: {"heartbeat": {"enabled": True, "every_minutes": 2}})
    h = cron.heartbeat_settings()
    assert h["enabled"] is True and h["every_minutes"] == 5   # never tighter than 5 minutes


def test_a_quiet_stretch_spaces_pulses_out_and_anything_happening_brings_them_back():
    import cron
    settings = {"every_minutes": 60, "quiet_stretch": 4}
    now = 1_000_000.0
    hour = 3600
    quiet, busy = (lambda since: False), (lambda since: True)
    assert not cron.pulse_now(now - 0.5 * hour, settings, busy, now)        # not due at all
    assert cron.pulse_now(now - 1.1 * hour, settings, busy, now)            # due, and something happened
    assert not cron.pulse_now(now - 1.1 * hour, settings, quiet, now)       # due, but nothing to find
    assert not cron.pulse_now(now - 3.9 * hour, settings, quiet, now)
    assert cron.pulse_now(now - 4.1 * hour, settings, quiet, now)           # quiet for long enough to look anyway
    assert cron.pulse_now(now - 1.1 * hour, {**settings, "quiet_stretch": 1}, quiet, now)   # switched off
    assert cron.pulse_now(0.0, settings, quiet, now)                        # never pulsed


def test_what_counts_as_something_having_happened(isolated):
    import kernel_store as store
    start = time.time() - 10
    assert not store.anything_since(start)
    store.trigger_add("heartbeat", "heartbeat:cto", "pulse", "", False, "explicit")
    ignored, _ = store.trigger_add("github", "github:a/b#1", "CI passed", "x", False, "candidate")
    store.trigger_dispose(ignored["id"], "ignored")
    assert not store.anything_since(start)          # an earlier pulse and an ignored event are not news
    store.trigger_add("gui", "dm:cto", "hello", "owner", True, "explicit")
    assert store.anything_since(start)
    assert not store.anything_since(time.time() + 1)
    row = store.session_create("worker", "cto", title="t", status="waiting")
    assert store.anything_since(time.time() + 1)    # work under way is always worth a look
    store.session_set(row["id"], status="ended")
    assert not store.anything_since(time.time() + 1)


def test_a_task_waiting_on_the_owner_does_not_keep_the_pulse_at_full_rate(isolated):
    import kernel_store as store
    row = store.session_create("worker", "cto", title="t", status="blocked")
    assert not store.anything_since(time.time() + 1)
    store.session_set(row["id"], status="working")
    assert store.anything_since(time.time() + 1)


def test_zero_switches_the_quiet_stretch_off(monkeypatch):
    import cron
    import settings as cfg
    monkeypatch.setattr(cfg, "load", lambda: {"heartbeat": {"enabled": True, "every_minutes": 60, "quiet_stretch": 0}})
    assert cron.heartbeat_settings()["quiet_stretch"] == 1
    monkeypatch.setattr(cfg, "load", lambda: {"heartbeat": {"enabled": True, "every_minutes": 60}})
    assert cron.heartbeat_settings()["quiet_stretch"] == cron.QUIET_STRETCH
