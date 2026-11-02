"""The skill harness: a negative trigger, revision, use counts, reflection."""
import asyncio
import json

import skills


def _fresh(tmp_name):
    skills.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    for d in list(skills.SKILLS_DIR.iterdir()):
        if d.is_dir():
            for f in d.iterdir():
                f.unlink()
            d.rmdir()
    if skills.USES_PATH.exists():
        skills.USES_PATH.unlink()
    return tmp_name


def test_avoid_survives_the_round_trip():
    _fresh("a")
    skills.save_skill("devops", "Deploy acmeshop", "when shipping acmeshop to prod",
                      "1. run the thing\n2. watch it", avoid="never for acme-mobile; use eas")
    sk = skills.list_skills()[0]
    assert sk["avoid"] == "never for acme-mobile; use eas"
    assert "avoid:" in skills.read_skill("Deploy acmeshop")


def test_a_wrapping_description_cannot_truncate_the_trigger():
    """Frontmatter is one key per line, so an embedded newline would silently
    cut the description in half and change what the skill fires on."""
    _fresh("b")
    skills.save_skill("a", "Multi", "first line\nsecond line that would be lost", "body here")
    assert skills.list_skills()[0]["description"] == "first line second line that would be lost"


def test_revise_keeps_the_body_and_dates_the_correction():
    _fresh("c")
    skills.save_skill("a", "Ship it", "when shipping", "1. original step\n2. another")
    skills.save_skill("b", "Ship it", "", "step 1 is wrong, do X instead",
                      mode="revise")
    text = skills.read_skill("Ship it")
    assert "1. original step" in text            # nothing was destroyed
    assert "step 1 is wrong" in text             # the correction landed
    assert "## Revised" in text
    assert skills.list_skills()[0]["description"] == "when shipping"  # kept


def test_revise_can_add_the_guard_that_was_missing():
    _fresh("d")
    skills.save_skill("a", "ASC API", "call the ASC API", "mint a jwt")
    skills.save_skill("a", "ASC API", "", "not for uploading binaries",
                      avoid="uploading a binary; use eas submit", mode="revise")
    assert skills.list_skills()[0]["avoid"] == "uploading a binary; use eas submit"


def test_revising_a_skill_that_does_not_exist_is_refused():
    _fresh("e")
    try:
        skills.save_skill("a", "Nope", "d", "c", mode="revise")
        assert False, "should have raised"
    except ValueError as exc:
        assert "no skill named" in str(exc)


def test_reads_are_counted_without_touching_the_file():
    _fresh("f")
    skills.save_skill("a", "Counted", "when counting", "body")
    slug = skills.slug_for("Counted")
    mtime = (skills.SKILLS_DIR / slug / "SKILL.md").stat().st_mtime
    skills.record_use(slug)
    skills.record_use(slug)
    assert skills.list_skills()[0]["uses"] == 2
    # fleet sync keys off mtime, so counting a read must not look like an edit
    assert (skills.SKILLS_DIR / slug / "SKILL.md").stat().st_mtime == mtime
    assert skills.export_skills(mtime) == []


def test_slug_for_resolves_by_name_and_misses_cleanly():
    _fresh("g")
    skills.save_skill("a", "Find Me", "when finding", "body")
    assert skills.slug_for("Find Me") == "find-me"
    assert skills.slug_for("find me") == "find-me"
    assert skills.slug_for("not a skill") is None


# ---------- the reflection pass ----------

def _orch():
    import orchestrator
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    return o


def test_a_short_turn_is_not_a_procedure(monkeypatch):
    _fresh("h")
    o = _orch()
    aid = list(o.agents)[0]
    called = []
    monkeypatch.setattr("llm.quick", lambda *a, **k: called.append(1))
    asyncio.run(o._maybe_learn(aid, ["terminal: ls"] * 3, []))
    assert not called and not skills.list_skills()


def test_reflection_writes_a_skill_from_a_long_turn(monkeypatch):
    _fresh("i")
    o = _orch()
    aid = list(o.agents)[0]

    async def fake_quick(system, user, **kw):
        assert "TOOL TRAIL" in user
        return json.dumps({"action": "new", "name": "Rotate the ASC key",
                           "description": "when the App Store Connect key is revoked",
                           "avoid": "not for uploading a build; that is eas submit",
                           "content": "1. get the p8\n2. base64 it\n3. security add-generic-password -U\n"
                                      "4. verify with a GET /v1/apps call before trusting it"})

    monkeypatch.setattr("llm.quick", fake_quick)
    asyncio.run(o._maybe_learn(aid, [f"terminal: step {i}" for i in range(20)], []))
    sk = skills.list_skills()
    assert len(sk) == 1
    assert sk[0]["name"] == "Rotate the ASC key"
    assert "eas submit" in sk[0]["avoid"]


def test_reflection_declining_to_learn_writes_nothing(monkeypatch):
    _fresh("j")
    o = _orch()
    aid = list(o.agents)[0]

    async def fake_quick(system, user, **kw):
        return '{"action": "none"}'

    monkeypatch.setattr("llm.quick", fake_quick)
    asyncio.run(o._maybe_learn(aid, [f"terminal: step {i}" for i in range(20)], []))
    assert skills.list_skills() == []


def test_a_thin_skill_is_dropped_rather_than_costing_everyone_a_line(monkeypatch):
    _fresh("k")
    o = _orch()
    aid = list(o.agents)[0]

    async def fake_quick(system, user, **kw):
        return json.dumps({"action": "new", "name": "Vague", "description": "stuff",
                           "content": "do the thing"})

    monkeypatch.setattr("llm.quick", fake_quick)
    asyncio.run(o._maybe_learn(aid, [f"terminal: step {i}" for i in range(20)], []))
    assert skills.list_skills() == []


def test_reflection_corrects_a_skill_that_misled_the_agent(monkeypatch):
    """The case this was built for: a skill fires on the wrong task, the turn
    goes badly, and the next turn should not repeat it."""
    _fresh("l")
    skills.save_skill("devops", "ASC API", "use for EAS/iOS submissions", "mint a jwt, call the api")
    o = _orch()
    aid = list(o.agents)[0]

    async def fake_quick(system, user, **kw):
        assert "SKILLS THIS AGENT READ THIS TURN: asc-api" in user
        return json.dumps({"action": "revise", "name": "ASC API",
                           "avoid": "uploading a binary; use eas submit --id <BUILD>",
                           "content": "Do not hand-roll buildUploadFiles; eas submit does it."})

    monkeypatch.setattr("llm.quick", fake_quick)
    asyncio.run(o._maybe_learn(aid, [f"terminal: attempt {i}" for i in range(30)], ["asc-api"]))
    sk = skills.list_skills()[0]
    assert sk["avoid"].startswith("uploading a binary")
    body = skills.read_skill("ASC API")
    assert "mint a jwt" in body and "Do not hand-roll" in body


def test_reflection_cannot_revise_a_skill_that_is_not_there(monkeypatch):
    _fresh("m")
    o = _orch()
    aid = list(o.agents)[0]

    async def fake_quick(system, user, **kw):
        return json.dumps({"action": "revise", "name": "Ghost", "content": "x" * 200})

    monkeypatch.setattr("llm.quick", fake_quick)
    asyncio.run(o._maybe_learn(aid, [f"terminal: step {i}" for i in range(20)], []))
    assert skills.list_skills() == []


def test_reflection_survives_a_model_that_does_not_answer_in_json(monkeypatch):
    _fresh("n")
    o = _orch()
    aid = list(o.agents)[0]

    async def fake_quick(system, user, **kw):
        return "Sure! I think you should save a skill about deploying."

    monkeypatch.setattr("llm.quick", fake_quick)
    asyncio.run(o._maybe_learn(aid, [f"terminal: step {i}" for i in range(20)], []))
    assert skills.list_skills() == []


def test_the_cooldown_stops_the_same_lesson_being_learned_all_evening(monkeypatch):
    _fresh("o")
    o = _orch()
    aid = list(o.agents)[0]
    calls = []

    async def fake_quick(system, user, **kw):
        calls.append(1)
        return '{"action": "none"}'

    monkeypatch.setattr("llm.quick", fake_quick)
    trail = [f"terminal: step {i}" for i in range(20)]
    asyncio.run(o._maybe_learn(aid, trail, []))
    asyncio.run(o._maybe_learn(aid, trail, []))
    assert len(calls) == 1


def test_the_toggle_turns_reflection_off(monkeypatch):
    _fresh("p")
    o = _orch()
    aid = list(o.agents)[0]
    calls = []
    monkeypatch.setattr(o, "_autolearn_on", lambda: False)
    monkeypatch.setattr("llm.quick", lambda *a, **k: calls.append(1))
    asyncio.run(o._maybe_learn(aid, [f"terminal: step {i}" for i in range(20)], []))
    assert not calls
