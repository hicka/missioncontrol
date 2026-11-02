"""The three added features: image generation, stop, daily brief."""
import asyncio
import datetime


def _orch():
    import orchestrator
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    return o




def test_generate_image_reports_failure(monkeypatch):
    import imagegen
    o = _orch()
    aid = list(o.agents)[0]
    monkeypatch.setattr(imagegen, "generate", lambda p, a="1:1": _fail())
    out = asyncio.run(o._exec_tool(aid, "~", "generate_image", {"prompt": "x"}))
    assert out.startswith("image generation failed")
    assert asyncio.run(o._exec_tool(aid, "~", "generate_image",
                                    {"prompt": "  "})) == "empty prompt"


async def _fail():
    return None, "no MINIMAX_API_KEY set"




def test_brief_is_due_once_a_day_after_the_set_time():
    import cron
    at = "08:00"
    before = datetime.datetime(2026, 9, 4, 7, 30)
    after = datetime.datetime(2026, 9, 4, 8, 15)
    assert not cron._brief_due(before, "", at)                 # before the time
    assert cron._brief_due(after, "", at)                      # at/after, not yet sent
    assert not cron._brief_due(after, "2026-09-04", at)        # already sent today
    assert cron._brief_due(datetime.datetime(2026, 9, 5, 8, 1), "2026-09-04", at)  # next day


def test_brief_settings_defaults(monkeypatch):
    import cron
    import settings as cfg
    monkeypatch.setattr(cfg, "load", lambda: {})
    b = cron.brief_settings()
    assert b == {"enabled": False, "agent": "", "at": "08:00"}


def test_generate_image_hands_back_a_file_to_report(monkeypatch):
    import imagegen
    o = _orch()
    aid = list(o.agents)[0]

    async def fake_gen(prompt, aspect="1:1"):
        assert prompt and aspect in imagegen.ASPECTS
        return ({"name": "image-abc.jpg", "path": "/x/image-abc.jpg",
                 "url": "/uploads/image-abc.jpg", "mime": "image/jpeg",
                 "kind": "image", "size": 10, "desc": prompt[:200]}, None)

    monkeypatch.setattr(imagegen, "generate", fake_gen)
    out = asyncio.run(o._exec_tool(aid, "~", "generate_image",
                                   {"prompt": "a teal sales chart", "aspect": "16:9"}))
    assert "/x/image-abc.jpg" in out and "report" in out


def test_stopping_a_profile_with_nothing_running_says_so():
    o = _orch()
    assert asyncio.run(o.stop_agent(list(o.agents)[1])) is False
