"""Agents can hand files back to the owner: message_user(file=...) copies
the file into uploads/ and posts it as a chat attachment."""
import asyncio


def _orch():
    import orchestrator
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    return o


def test_a_file_handed_over_reaches_the_chat_as_an_attachment(tmp_path):
    import db
    o = _orch()
    aid = list(o.agents)[0]
    src = tmp_path / "desktop shot.png"
    src.write_bytes(b"\x89PNG fake image bytes")
    asyncio.run(o.deliver(f"dm:{aid}", "here is the screenshot", sender=aid, files=[str(src)]))
    last = db.last_message(f"dm:{aid}")
    assert last["text"] == "here is the screenshot"
    atts = last["meta"]["attachments"]
    assert len(atts) == 1 and atts[0]["kind"] == "image"
    assert atts[0]["url"].startswith("/uploads/")
    # the copy in uploads/ is what the chat serves, independent of the source
    from pathlib import Path
    assert Path(atts[0]["path"]).read_bytes() == src.read_bytes()


def test_a_missing_file_does_not_lose_the_message():
    import db
    o = _orch()
    aid = list(o.agents)[0]
    asyncio.run(o.deliver(f"dm:{aid}", "oops, no file", sender=aid, files=["/nope/never/was.png"]))
    last = db.last_message(f"dm:{aid}")
    assert last["text"] == "oops, no file" and not (last["meta"] or {}).get("attachments")
