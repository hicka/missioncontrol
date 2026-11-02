import datetime
import time

import pytest

import cron


GOOD = ["every 30 minutes", "every 2 hours", "daily 09:00", "weekly mon 08:30",
        "once 2030-01-01 10:00", "0 9 * * 1-5", "*/15 * * * *"]
BAD = ["every 1 minutes", "daily 25:00", "whenever", "61 9 * * *", "once 2020-01-01 10:00"]


@pytest.mark.parametrize("s", GOOD)
def test_validate_good(s):
    assert cron.validate(s)


@pytest.mark.parametrize("s", BAD)
def test_validate_bad(s):
    with pytest.raises(ValueError):
        cron.validate(s)


def test_next_run_interval():
    now = time.time()
    assert abs(cron.next_run("every 30 minutes", now) - now - 1800) < 2


def test_next_run_cron_weekday():
    d = datetime.datetime.fromtimestamp(cron.next_run("0 9 * * 1-5", time.time()))
    assert d.hour == 9 and d.minute == 0 and d.weekday() < 5


def test_next_run_daily_is_future():
    now = time.time()
    nr = cron.next_run("daily 09:00", now)
    assert nr > now
    assert datetime.datetime.fromtimestamp(nr).hour == 9


def test_once_past_returns_none():
    assert cron.next_run("once 2020-01-01 10:00", time.time()) is None


def test_heartbeat_settings_defaults():
    h = cron.heartbeat_settings()
    assert h["enabled"] is False
    assert h["every_minutes"] >= 5


def test_heartbeat_file_seeded():
    f = cron.heartbeat_file()
    assert f.exists()
    assert "Heartbeat" in f.read_text()
