"""A source checkout following GitHub. These use real git repositories, because what
matters is what git actually does to the checkout, and that the rules hold: only ever a
fast-forward, never over someone's work, and a plain push reaches nobody on releases."""
import asyncio
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import selfimprove as si
import selfupdate


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          cwd=str(cwd), capture_output=True, text=True, check=True).stdout.strip()


def commit(cwd, name="change.txt", text="x"):
    (Path(cwd) / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-qm", f"change {name}")
    return git(cwd, "rev-parse", "HEAD")


class Install:
    """A bare origin, a checkout following it, and a second clone that publishes to it."""
    def __init__(self, root: Path):
        self.origin = root / "origin.git"
        self.checkout = root / "checkout"
        self.publisher = root / "publisher"
        git(root, "init", "-q", "--bare", "-b", "master", str(self.origin))
        git(root, "clone", "-q", str(self.origin), str(self.publisher))
        git(self.publisher, "checkout", "-q", "-b", "master")
        (self.publisher / ".gitignore").write_text("settings.yaml\n")
        git(self.publisher, "add", ".gitignore")
        commit(self.publisher, "start.txt")
        git(self.publisher, "push", "-q", "origin", "master")
        git(root, "clone", "-q", str(self.origin), str(self.checkout))

    def publish(self, tag=None):
        sha = commit(self.publisher, f"{sha_name(self.publisher)}.txt")
        git(self.publisher, "push", "-q", "origin", "master")
        if tag:
            git(self.publisher, "tag", "-a", tag, "-m", tag)
            git(self.publisher, "push", "-q", "origin", tag)
        return sha

    @property
    def head(self):
        return git(self.checkout, "rev-parse", "HEAD")


def sha_name(cwd):
    return git(cwd, "rev-parse", "--short", "HEAD")


@pytest.fixture
def install(tmp_path, monkeypatch):
    made = Install(tmp_path)
    monkeypatch.setattr(si, "REPO", made.checkout)
    monkeypatch.setattr(selfupdate, "ROOT", tmp_path)
    monkeypatch.setattr(selfupdate, "_save", lambda **values: saved.update(values))
    saved: dict = {"auto": False, "channel": "release", "skip": []}
    made.saved = saved
    monkeypatch.setattr(selfupdate, "settings", lambda: {"auto": saved.get("auto", False), "channel": saved.get("channel", "release"),
                                                         "skip": list(saved.get("skip") or [])})
    monkeypatch.setattr(selfupdate, "settings_attempt", lambda: saved.get("attempt"))
    return made


class FakeOrch:
    def __init__(self, busy=()):
        self.busy = list(busy)


def test_the_newest_release_is_the_highest_version_and_its_commit():
    listing = "\n".join([
        "aaa\trefs/tags/v1.9.0",
        "bbb\trefs/tags/v1.10.0",
        "ccc\trefs/tags/v1.10.0^{}",     # the commit an annotated tag points at
        "ddd\trefs/tags/v2.0.0-rc1",     # not a release
        "eee\trefs/tags/nightly",
    ])
    assert selfupdate.newest_release(listing) == ("v1.10.0", "ccc")
    assert selfupdate.newest_release("") is None


def test_a_plain_push_to_master_reaches_nobody_following_releases(install):
    """Self-repair pushes to master on its own. That must not land on other machines."""
    install.publish()
    found = selfupdate.look()
    assert found["state"] == "current" and found["target"] is None


def test_a_new_release_is_found_and_can_be_fast_forwarded(install):
    sha = install.publish("v1.2.0")
    found = selfupdate.look()
    assert (found["state"], found["target"], found["sha"]) == ("available", "v1.2.0", sha)


def test_the_master_channel_follows_master(install):
    sha = install.publish()
    install.saved["channel"] = "master"
    found = selfupdate.look()
    assert (found["state"], found["sha"]) == ("available", sha)


def test_an_uncommitted_change_is_never_swept_along(install):
    install.publish("v1.2.0")
    (install.checkout / "start.txt").write_text("my work")
    assert selfupdate.look()["state"] == "blocked"


def test_a_checkout_on_another_branch_stays_there(install):
    install.publish("v1.2.0")
    git(install.checkout, "checkout", "-q", "-b", "my-work")
    assert selfupdate.look()["state"] == "blocked"


def test_local_commits_mean_no_fast_forward_and_nothing_moves(install):
    install.publish("v1.2.0")
    mine = commit(install.checkout, "mine.txt")
    assert selfupdate.look()["state"] == "blocked"
    assert install.head == mine


def test_a_version_that_did_not_come_back_is_not_tried_again(install):
    sha = install.publish("v1.2.0")
    install.saved["skip"] = [sha]
    assert selfupdate.look()["state"] == "blocked"
    install.publish("v1.2.1")
    assert selfupdate.look()["target"] == "v1.2.1"


def test_already_having_the_release_is_current(install):
    install.publish("v1.2.0")
    git(install.checkout, "pull", "-q", "origin", "master")
    assert selfupdate.look()["state"] == "current"


def test_the_signed_app_is_left_to_update_itself(install, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert selfupdate.look()["state"] == "app"


def apply(install, monkeypatch, busy=(), launchd=True):
    calls = {"watchdog": None, "restart": 0}
    monkeypatch.setattr(si, "_busy", lambda orch: list(busy))
    monkeypatch.setattr(selfupdate, "_under_launchd", lambda: launchd)
    monkeypatch.setattr(si, "_arm_watchdog", lambda previous, port: calls.update(watchdog=previous))
    monkeypatch.setattr(si, "restart", lambda: calls.update(restart=calls["restart"] + 1))
    found = selfupdate.look()
    return asyncio.run(selfupdate.apply(FakeOrch(), found)), calls


def test_an_update_waits_for_running_work(install, monkeypatch):
    install.publish("v1.2.0")
    before = install.head
    note, calls = apply(install, monkeypatch, busy=["webdev"])
    assert "webdev" in note and install.head == before and calls["restart"] == 0


def test_nothing_moves_when_nothing_here_can_restart_it(install, monkeypatch):
    """Moving the code under a process that will not restart leaves the two out of step."""
    install.publish("v1.2.0")
    before = install.head
    note, calls = apply(install, monkeypatch, launchd=False)
    assert "available" in note and install.head == before and calls["restart"] == 0


def test_updating_moves_the_checkout_behind_the_watchdog(install, monkeypatch):
    sqlite3.connect(selfupdate.ROOT / "missioncontrol.db").close()
    before = install.head
    sha = install.publish("v1.2.0")
    note, calls = apply(install, monkeypatch)
    assert "v1.2.0" in note
    assert install.head == sha
    assert calls == {"watchdog": before, "restart": 1}
    assert install.saved["attempt"]["previous"] == before
    assert list(selfupdate.ROOT.glob("missioncontrol.db-before-*"))


def test_a_node_package_change_is_left_to_the_owner(install, monkeypatch):
    (install.publisher / "whatsapp").mkdir()
    (install.publisher / "whatsapp/package-lock.json").write_text("{}")
    git(install.publisher, "add", "whatsapp")
    before = install.head
    install.publish("v1.2.0")
    note, calls = apply(install, monkeypatch)
    assert "npm" in note and install.head == before and calls["restart"] == 0


def settle(install, monkeypatch):
    told = []

    async def tell(orch, text):
        told.append(text)
    monkeypatch.setattr(si, "_tell_owner", tell)
    asyncio.run(selfupdate.settle(FakeOrch()))
    return told


def test_a_rollback_is_remembered_and_the_owner_is_told(install, monkeypatch):
    install.saved["attempt"] = {"sha": "bad-sha", "previous": install.head, "name": "v1.2.0"}
    told = settle(install, monkeypatch)
    assert install.saved["skip"] == ["bad-sha"] and install.saved["attempt"] is None
    assert "v1.2.0" in told[0] and "put back" in told[0]


def test_a_good_update_is_announced_and_not_skipped(install, monkeypatch):
    install.saved["attempt"] = {"sha": install.head, "previous": "older", "name": "v1.2.0"}
    told = settle(install, monkeypatch)
    assert install.saved["skip"] == [] and told == ["Updated to v1.2.0."]


def test_only_the_latest_database_copies_are_kept(install):
    database = selfupdate.ROOT / "missioncontrol.db"
    with sqlite3.connect(database) as db:
        db.execute("create table t (x)")
        db.execute("insert into t values (1)")
    for label in ("one", "two", "three"):
        path = Path(selfupdate.backup_database(label))
        with sqlite3.connect(path) as copy:
            assert copy.execute("select x from t").fetchone() == (1,)
    assert len(list(selfupdate.ROOT.glob("missioncontrol.db-before-*"))) == selfupdate.KEEP_BACKUPS


def test_the_signed_app_copies_the_database_only_when_the_version_changes(install):
    sqlite3.connect(selfupdate.ROOT / "missioncontrol.db").close()
    copies = lambda: list(selfupdate.ROOT.glob("missioncontrol.db-before-*"))
    selfupdate.note_app_version("1.2.0")
    assert copies() == []             # a first run has nothing to protect from a migration
    selfupdate.note_app_version("1.2.0")
    assert copies() == []
    selfupdate.note_app_version("1.3.0")
    assert [path.name for path in copies()] == ["missioncontrol.db-before-1.2.0"]


def test_what_reaches_every_install_is_out_of_an_agents_reach():
    for path in ("server/selfupdate.py", "native/macos/Updates.swift", "scripts/build_macos.py", "packaging/macos/entitlements.plist"):
        assert si.protected_hits([path]) == [path], path
