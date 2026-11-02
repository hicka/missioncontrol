"""The gate in front of the reflection call: what it lets through, and why.

The old gate was two proxies for "was this a procedure?" — a twelve-call floor
and a half-hour cooldown — both there to ration an expensive model call. These
cover the replacement: a calibrated read of the trail, every turn, with the old
heuristic still standing whenever the decider cannot be reached.
"""
import asyncio
import json

import pytest

import decide
import runtime_store as store
import skills


def _fresh():
    skills.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    for d in list(skills.SKILLS_DIR.iterdir()):
        if d.is_dir():
            for f in d.iterdir():
                f.unlink()
            d.rmdir()
    if skills.USES_PATH.exists():
        skills.USES_PATH.unlink()


def _orch():
    import orchestrator
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    return o


def _gate(monkeypatch, **probabilities):
    """Answer the gate's questions with fixed probabilities, no network."""
    async def fake_nouls(state, questions):
        assert set(questions) == {"reusable", "correction"}
        return {"reusable": 0.0, "correction": 0.0, **probabilities}
    monkeypatch.setattr(decide, "nouls", fake_nouls)


def _reflection(monkeypatch, payload):
    calls = []

    async def fake_quick(system, user, **kw):
        calls.append(user)
        return payload if isinstance(payload, str) else json.dumps(payload)

    monkeypatch.setattr("llm.quick", fake_quick)
    return calls


SKILL = {"action": "new", "name": "Rotate the ASC key",
         "description": "when the App Store Connect key is revoked",
         "avoid": "not for uploading a build; that is eas submit",
         "content": "1. get the p8\n2. base64 it\n3. security add-generic-password -U\n"
                    "4. verify with a GET /v1/apps call before trusting it"}


def test_a_short_trail_can_still_hold_a_procedure(monkeypatch):
    """The whole point. Four calls is under the old twelve-call floor, so this
    learning used to be dropped without ever being looked at."""
    _fresh()
    o = _orch()
    aid = list(o.agents)[0]
    _gate(monkeypatch, reusable=0.93)
    calls = _reflection(monkeypatch, SKILL)
    asyncio.run(o._maybe_learn(aid, [f"terminal: step {i}" for i in range(4)], []))
    assert len(calls) == 1
    assert [sk["name"] for sk in skills.list_skills()] == ["Rotate the ASC key"]


def test_a_long_flailing_trail_does_not_buy_a_reflection(monkeypatch):
    """Forty calls used to be an automatic reflection; most came back 'none'."""
    _fresh()
    o = _orch()
    aid = list(o.agents)[0]
    _gate(monkeypatch, reusable=0.2, correction=0.05)
    calls = _reflection(monkeypatch, SKILL)
    asyncio.run(o._maybe_learn(aid, [f"terminal: step {i}" for i in range(40)], []))
    assert not calls and not skills.list_skills()


def test_a_correction_opens_the_gate_when_nothing_is_new(monkeypatch):
    """Revising a wrong skill is the cheap, self-correcting half of reflection;
    gating only on novelty would suppress it."""
    _fresh()
    o = _orch()
    aid = list(o.agents)[0]
    skills.save_skill("a", "ASC API", "call the ASC API", "mint a jwt\nthen call it")
    _gate(monkeypatch, reusable=0.1, correction=0.88)
    calls = _reflection(monkeypatch, {"action": "revise", "name": "ASC API",
                                      "content": "the jwt must be ES256, not RS256"})
    asyncio.run(o._maybe_learn(aid, [f"terminal: step {i}" for i in range(6)], []))
    assert len(calls) == 1
    assert "ES256" in skills.read_skill("ASC API")


def test_no_cooldown_when_the_gate_is_answering(monkeypatch):
    """Two good turns in a row are two learnings, not one and a dropped one."""
    _fresh()
    o = _orch()
    aid = list(o.agents)[0]
    _gate(monkeypatch, reusable=0.9)
    calls = _reflection(monkeypatch, {"action": "none"})
    trail = [f"terminal: step {i}" for i in range(20)]
    asyncio.run(o._maybe_learn(aid, trail, []))
    asyncio.run(o._maybe_learn(aid, trail, []))
    assert len(calls) == 2


def test_an_unreachable_decider_falls_back_to_the_old_heuristic(monkeypatch):
    """None means 'decide this yourself', never 'no'."""
    _fresh()
    o = _orch()
    aid = list(o.agents)[0]

    async def unreachable(state, questions):
        return None

    monkeypatch.setattr(decide, "nouls", unreachable)
    calls = _reflection(monkeypatch, {"action": "none"})
    asyncio.run(o._maybe_learn(aid, [f"terminal: step {i}" for i in range(4)], []))
    assert not calls                     # under the twelve-call floor
    asyncio.run(o._maybe_learn(aid, [f"terminal: step {i}" for i in range(20)], []))
    assert len(calls) == 1               # over it
    asyncio.run(o._maybe_learn(aid, [f"terminal: step {i}" for i in range(20)], []))
    assert len(calls) == 1               # and the cooldown still holds


def test_the_gate_records_what_it_said_and_what_it_bought(monkeypatch):
    """A threshold is only tunable against labelled history."""
    _fresh()
    o = _orch()
    aid = list(o.agents)[0]
    _gate(monkeypatch, reusable=0.91)
    _reflection(monkeypatch, SKILL)
    asyncio.run(o._maybe_learn(aid, [f"terminal: step {i}" for i in range(5)], []))
    row = store.decisions(kind="learn_gate")[0]
    assert row["acted"] == 1
    assert row["detail"]["reusable"] == 0.91
    assert row["detail"]["threshold"] == o.LEARN_P
    assert row["outcome"].startswith("learned:")


def test_a_gate_that_opened_on_nothing_is_labelled_as_such(monkeypatch):
    """The reflection itself scores the gate: high probability, no skill, so the
    threshold was too low for this shape of trail."""
    _fresh()
    o = _orch()
    aid = list(o.agents)[0]
    _gate(monkeypatch, reusable=0.8)
    _reflection(monkeypatch, {"action": "none"})
    asyncio.run(o._maybe_learn(aid, [f"terminal: step {i}" for i in range(5)], []))
    row = store.decisions(kind="learn_gate")[0]
    assert row["acted"] == 1 and row["outcome"] == "none"


def test_the_toggle_still_wins(monkeypatch):
    _fresh()
    o = _orch()
    aid = list(o.agents)[0]
    monkeypatch.setattr(o, "_autolearn_on", lambda: False)
    _gate(monkeypatch, reusable=0.99)
    calls = _reflection(monkeypatch, SKILL)
    asyncio.run(o._maybe_learn(aid, [f"terminal: step {i}" for i in range(20)], []))
    assert not calls


# ---------- the decider itself ----------

def test_state_is_redacted_before_it_leaves_the_machine(monkeypatch):
    import secret_mask
    secret_mask.protect("hunter2-the-password")
    sent = {}

    async def fake_post(payload):
        sent.update(payload)
        return {"answers": {"q": {"type": "noul", "noul": 0.5}}}

    monkeypatch.setattr(decide, "configured", lambda: True)
    monkeypatch.setattr(decide, "_post", fake_post)
    got = asyncio.run(decide.noul(
        {"trail": ["login: typed hunter2-the-password into #pw"]}, "did it work?"))
    assert got == 0.5
    assert "hunter2-the-password" not in json.dumps(sent)
    assert "[private input]" in json.dumps(sent)


def test_an_unconfigured_decider_answers_none():
    assert asyncio.run(decide.noul("anything", "is it so?")) is None


def test_a_refused_request_is_none_not_false(monkeypatch):
    """A 401 must not read as 'no': that would silently close every gate."""
    class Response:
        status_code, text = 401, "bad key"

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): return Response()

    monkeypatch.setattr(decide, "configured", lambda: True)
    monkeypatch.setattr(decide, "_key", lambda: "k")
    monkeypatch.setattr(decide.httpx, "AsyncClient", lambda **kw: Client())
    assert asyncio.run(decide.noul("state", "question?")) is None


def test_a_missing_answer_does_not_become_a_zero(monkeypatch):
    async def fake_post(payload):
        return {"answers": {"reusable": {"type": "noul", "noul": 0.6}}}

    monkeypatch.setattr(decide, "configured", lambda: True)
    monkeypatch.setattr(decide, "_post", fake_post)
    got = asyncio.run(decide.nouls("s", {"reusable": {"instructions": "a"},
                                         "correction": {"instructions": "b"}}))
    assert got == {"reusable": 0.6}      # absent, not 0.0
