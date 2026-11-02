"""The heartbeat keeps its cadence across restarts instead of pulsing on boot."""
import time


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
