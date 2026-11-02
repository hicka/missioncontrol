"""The Laya mail sweep: what it archives, what it never touches, and how the
owner takes a decision back. Mail and Laya are both faked; nothing here moves
real mail or loads a model."""
import asyncio

import pytest

import laya
import mailsweep
import settings


def _probs(**top):
    """A choice answer's probabilities, with the rest spread thin."""
    rest = (1 - sum(top.values())) / max(1, len(mailsweep.KINDS) - len(top))
    return {k: top.get(k, rest) for k in mailsweep.KINDS}


# Sender verdicts by address, message verdicts by subject keyword.
SENDERS = {"shop@offers.com": _probs(promotion=0.9),
           "friend@mail.com": _probs(personal=0.9),
           "news@maybe.com": _probs(promotion=0.3, personal=0.3)}


def _answer(state):
    if "recent_subjects" in state:
        probs = SENDERS.get(mailsweep.address(state["from"]), _probs(personal=0.5))
    elif "login" in state["subject"].lower():
        probs = _probs(security=0.8)
    else:
        probs = _probs(promotion=0.95)
    return {"answers": {"kind": {"type": "choice", "probabilities": probs}}}


INBOX = [  # id, sender, subject, flagged, replied
    (1, "Shop <shop@offers.com>", "50% off everything", False, False),
    (2, "Shop <shop@offers.com>", "New login to your account", False, False),
    (3, "Shop <shop@offers.com>", "Flash sale", True, False),
    (4, "Friend <friend@mail.com>", "Dinner on Friday?", False, False),
    (5, "Support <help@vendor.com>", "Re: your ticket", False, True),
    (6, "Support <help@vendor.com>", "Ticket updated", False, False),
    (7, "Kept <boss@work.com>", "Quarterly numbers", False, False),
    (8, "Spam <noise@always.com>", "Anything at all", False, False),
    (9, "Shop <shop@offers.com>", "50% off everything", False, False),
    (10, "News <news@maybe.com>", "Weekly digest", False, False),
]


@pytest.fixture
def sweep_env(monkeypatch, tmp_path):
    conf = {"apple": {"mail": True}, "laya": {"enabled": True},
            "mail_sweep": {"enabled": True, "dry_run": True, "keep_senders": ["work.com"]}}
    monkeypatch.setattr(settings, "load", lambda: conf)
    monkeypatch.setattr(mailsweep, "STATE_PATH", tmp_path / "mail_sweep.json")
    calls = {"batch": [], "moves": []}
    inbox = list(INBOX)

    async def scan(s):
        return [{"account": "iCloud", "inbox": "INBOX", "archive": "Archive",
                 "id": [m[0] for m in inbox], "sender": [m[1] for m in inbox],
                 "subject": [m[2] for m in inbox], "flagged": [m[3] for m in inbox],
                 "replied": [m[4] for m in inbox]}]

    async def batch(items, timeout=0):
        calls["batch"].append(items)
        return [_answer(i["state"]) for i in items]

    async def move(account, source, dest, ids):
        calls["moves"].append((account, source, dest, list(ids)))
        return list(ids), ""

    monkeypatch.setattr(mailsweep, "scan", scan)
    monkeypatch.setattr(mailsweep, "move", move)
    monkeypatch.setattr(laya, "batch", batch)
    state = mailsweep.load_state()
    state["always"] = ["always.com"]
    mailsweep.save_state(state)
    return conf, calls


def test_a_dry_run_reports_what_it_would_archive_and_moves_nothing(sweep_env):
    conf, calls = sweep_env
    report = asyncio.run(mailsweep.sweep())
    assert report["dry_run"] is True
    assert calls["moves"] == []
    assert report["scanned"] == 10
    assert report["protected"] == 2          # the flagged sale, the replied ticket
    assert report["kept_by_rule"] == 2       # a correspondent's other mail, the keep list
    assert report["archive"] == 3            # both "50% off" mails and the always-archive sender
    assert report["held"] == 1               # the login alert from the shop
    assert report["kept"] == 2               # the friend, and the unsure newsletter
    assert [r["sender"] for r in report["unsure_senders"]] == ["news@maybe.com"]
    assert report["held_examples"][0]["subject"] == "New login to your account"


def test_identical_subjects_and_known_senders_are_only_asked_about_once(sweep_env):
    conf, calls = sweep_env
    first = asyncio.run(mailsweep.sweep())
    # shop, friend, news: the correspondent, the kept and the always sender are never asked
    assert first["asked_senders"] == 3
    # "50% off everything" twice is one question; the login alert is the other
    assert first["asked_messages"] == 2
    calls["batch"].clear()
    second = asyncio.run(mailsweep.sweep())
    assert calls["batch"] == []
    assert (second["asked_senders"], second["asked_messages"]) == (0, 0)
    assert second["archive"] == first["archive"]


def test_a_live_sweep_moves_only_the_clutter_and_remembers_it(sweep_env):
    conf, calls = sweep_env
    conf["mail_sweep"]["dry_run"] = False
    report = asyncio.run(mailsweep.sweep())
    assert report["archived"] == 3
    assert calls["moves"] == [("iCloud", "INBOX", "Archive", [1, 8, 9])]
    ledger = mailsweep.load_state()["archived"]
    assert {r["subject"] for r in ledger} == {"50% off everything", "Anything at all"}


def test_without_laya_nothing_is_archived_on_a_guess(sweep_env, monkeypatch):
    conf, calls = sweep_env
    conf["mail_sweep"]["dry_run"] = False

    async def down(items, timeout=0):
        return None

    monkeypatch.setattr(laya, "batch", down)
    report = asyncio.run(mailsweep.sweep())
    assert report["error"] == "Laya is not running"
    # only the sender the owner said to always archive goes
    assert calls["moves"] == [("iCloud", "INBOX", "Archive", [8])]
    assert report["pending"] == 5


def test_mail_off_means_no_sweep(sweep_env):
    conf, calls = sweep_env
    conf["apple"]["mail"] = False
    report = asyncio.run(mailsweep.sweep())
    assert "not connected" in report["error"]
    assert report["scanned"] == 0


def test_keep_with_restore_moves_the_senders_archived_mail_back(sweep_env, monkeypatch):
    conf, calls = sweep_env
    conf["mail_sweep"]["dry_run"] = False
    asyncio.run(mailsweep.sweep())
    calls["moves"].clear()

    async def osa(script, lang="AppleScript", timeout=0):
        import json
        return json.dumps({"archive": "Archive", "inbox": "INBOX", "id": [101, 102, 103],
                           "sender": ["Shop <shop@offers.com>", "Shop <shop@offers.com>", "Other <o@x.com>"],
                           "subject": ["50% off everything", "Older offer", "50% off everything"]})

    monkeypatch.setattr(mailsweep.apple, "osa", osa)
    got = asyncio.run(mailsweep.keep_sender("offers.com", restore=True))
    assert got["ok"] and got["restored"] == 1
    # only what the sweep archived comes back, not mail the owner filed themselves
    assert calls["moves"] == [("iCloud", "Archive", "INBOX", [101])]
    state = mailsweep.load_state()
    assert "offers.com" in state["keep"]
    assert all("offers.com" not in r["from"] for r in state["archived"])
    report = asyncio.run(mailsweep.sweep())
    assert report["archive"] == 1            # only the always-archive sender now


def test_always_and_keep_replace_each_other(sweep_env):
    asyncio.run(mailsweep.always_archive("Friend@Mail.com"))
    assert "friend@mail.com" in mailsweep.load_state()["always"]
    asyncio.run(mailsweep.keep_sender("friend@mail.com"))
    state = mailsweep.load_state()
    assert "friend@mail.com" in state["keep"] and "friend@mail.com" not in state["always"]


def test_sender_rules_match_addresses_and_domains_exactly():
    assert mailsweep.address("Shop <Shop@Offers.com>") == "shop@offers.com"
    assert mailsweep.address("plain@host.org") == "plain@host.org"
    assert mailsweep.matches("a@mail.offers.com", ["offers.com"])
    assert mailsweep.matches("a@offers.com", ["@offers.com"])
    assert mailsweep.matches("a@offers.com", ["a@offers.com"])
    assert not mailsweep.matches("aa@offers.com", ["a@offers.com"])
    assert not mailsweep.matches("a@notoffers.com", ["offers.com"])


# ---------- the agent tools ----------

def _orch():
    import orchestrator
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    return o


def test_decide_tool_reports_the_choice_with_its_probabilities(monkeypatch):
    monkeypatch.setattr(laya, "enabled", lambda: True)

    async def one(state, questions, timeout=0):
        return {"answers": {"kind": {"type": "choice", "choice": "bug",
                                     "probabilities": {"bug": 0.81, "feature": 0.19}},
                            "urgent": {"type": "noul", "noul": 0.27}}}

    monkeypatch.setattr(laya, "system_one", one)
    out = asyncio.run(_orch()._exec_decide({
        "state": {"title": "Crash on save"},
        "questions": {"kind": {"type": "choice", "instructions": "What is it?",
                               "criteria": {"bug": "broken", "feature": "new"}},
                      "urgent": {"type": "noul", "instructions": "It blocks a release."}}}))
    assert out == "kind: bug (bug 0.81, feature 0.19); urgent: P(true) 0.27"


def test_decide_tool_answers_many_items_in_order(monkeypatch):
    monkeypatch.setattr(laya, "enabled", lambda: True)

    async def many(items, timeout=0):
        return [{"answers": {"spam": {"type": "noul", "noul": 0.9 if "win" in i["state"] else 0.1}}}
                for i in items]

    monkeypatch.setattr(laya, "batch", many)
    out = asyncio.run(_orch()._exec_decide({
        "items": ["you win a prize", "meeting at 3"],
        "questions": {"spam": {"type": "noul", "instructions": "This is spam."}}}))
    assert out.splitlines() == ["[0] spam: P(true) 0.90", "[1] spam: P(true) 0.10"]


def test_decide_tool_refuses_malformed_questions_and_says_when_laya_is_off(monkeypatch):
    o = _orch()
    bad = asyncio.run(o._exec_decide({"state": "x", "questions": {"q": {"type": "essay"}}}))
    assert "needs type" in bad
    monkeypatch.setattr(laya, "enabled", lambda: False)
    off = asyncio.run(o._exec_decide({"state": "x", "questions": {
        "q": {"type": "noul", "instructions": "It is late."}}}))
    assert "turned off" in off


def test_mail_cleanup_status_before_any_sweep(sweep_env):
    out = asyncio.run(_orch()._exec_mail_cleanup("cto", {"action": "status"}))
    assert "DRY RUN" in out
    assert "no sweep has run yet" in out
    assert "always archive: always.com" in out


def test_tool_definitions_exist_for_both_tools():
    import orchestrator
    names = {d["name"] for d in orchestrator.TOOL_DEFS}
    assert {"decide", "mail_cleanup"} <= names


def test_the_agent_that_starts_a_sweep_is_woken_with_its_report(sweep_env, monkeypatch):
    o = _orch()
    woken = []

    async def external_event(aid, source, text, mission=None):
        woken.append((aid, source, text))

    monkeypatch.setattr(o, "external_event", external_event)

    async def go():
        out = await o._exec_mail_cleanup("cto", {"action": "run"})
        assert "woken with its report" in out
        assert "already running" in mailsweep.start()["error"]
        await mailsweep._task
    asyncio.run(go())
    assert woken and woken[0][:2] == ("cto", "mail_sweep")
    assert "would archive 3" in woken[0][2] and "held 1 as important" in woken[0][2]


def test_progress_is_saved_while_laya_is_still_answering(sweep_env, monkeypatch):
    monkeypatch.setattr(mailsweep, "SAVE_EVERY", 1)
    saves = []
    real = mailsweep.save_state
    monkeypatch.setattr(mailsweep, "save_state", lambda state: (saves.append(len(state["senders"])), real(state)))
    asyncio.run(mailsweep.sweep())
    # a save after each sender answered, before the sweep's own final save
    assert saves[:3] == [1, 2, 3]
