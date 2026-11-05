"""Modules: code an agent writes that adds tools, cards and timers while the app runs."""
import asyncio
import json
import socket
import sys
import threading
import time
import types

import pytest


COUNTER = '''
def apply(ctx):
    ui = ctx.ui
    card = ctx.card("main", title=ctx.config.get("title", "Counter"), symbol="number")

    def draw():
        n = ctx.state.get("count", 0)
        card.render([ui.stat("Count", n), ui.chart([n, n + 1], kind="bar"), ui.button("Add one", "add")],
                    tone="ok" if n < 3 else "warn")

    @card.on("add")
    def add(event):
        ctx.state["count"] = ctx.state.get("count", 0) + int(event.get("value") or 1)
        draw()

    @ctx.tool("count", "The current count", {"type": "object", "properties": {"plus": {"type": "integer"}}})
    def count(args):
        ctx.state["count"] = ctx.state.get("count", 0) + args.get("plus", 0)
        draw()
        return {"count": ctx.state["count"]}

    @ctx.every(3600)
    def tick():
        draw()
'''


class FakeRouter:
    def __init__(self):
        self.told = []

    async def notify_owner(self, text, meta=None):
        self.told.append(text)
        return {"sent": True, "pushed": False}


class FakeOrch:
    def __init__(self):
        self.agents = {"cto": {"name": "CTO"}}
        self.lead = "cto"
        self.router = FakeRouter()
        self.asked = []
        self.events = []

    async def _ask_owner(self, key, line, short, meta=None):
        self.asked.append((key, line))

    async def external_event(self, aid, source, text, **kwargs):
        self.events.append((aid, source, text, kwargs))


def _ctx(strength="explicit"):
    return types.SimpleNamespace(row={"strength": strength})


@pytest.fixture
def mods(monkeypatch):
    import modules
    fake = FakeOrch()
    sent = []

    async def emit(event):
        sent.append(event)

    monkeypatch.setattr(modules, "orch", fake)
    monkeypatch.setattr(modules, "emit", emit)
    monkeypatch.setattr(modules, "SETTLE", 0.4)
    modules.FOLDER.mkdir(parents=True, exist_ok=True)
    modules.fake, modules.sent = fake, sent
    yield modules
    modules._running.clear()
    modules._retry_at.clear()


def _run(coro):
    """One event loop per scenario, and its modules stop on the loop that started them."""
    async def scenario():
        import modules
        try:
            return await coro
        finally:
            await modules.stop_all()
    return asyncio.run(scenario())


async def _create(modules, slug, code=COUNTER, strength="explicit", **fields):
    out = await modules.tool(None, _ctx(strength), "cto", {
        "action": "create", "module": slug, "name": fields.pop("name", slug.title()),
        "files": {"main.py": code}, **fields})
    if not modules.sandboxed() and strength == "explicit" and "waits for the owner" in out:
        # No sandbox here (CI on Linux), so every version is the owner's to approve.
        await modules.approve(slug)
        out = await modules._outcome(slug)
    return out


async def _until(check, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        await asyncio.sleep(0.05)
    return False


# ---------- what a card may say ----------

def test_card_views_are_typed_and_strict():
    import module_ui as ui
    view = ui.check_view([{"type": "text", "text": "hi"}, {"type": "progress", "value": 4}])
    assert view["type"] == "stack" and view["children"][1]["value"] == 1.0
    with pytest.raises(ui.CardError, match="unknown type"):
        ui.check_view({"type": "iframe"})
    with pytest.raises(ui.CardError, match="no field 'colour'"):
        ui.check_view({"type": "text", "text": "x", "colour": "red"})
    with pytest.raises(ui.CardError, match="tone"):
        ui.check_view({"type": "status", "label": "x", "tone": "purple"})
    with pytest.raises(ui.CardError, match="http"):
        ui.check_view({"type": "link", "label": "x", "url": "file:///etc/passwd"})
    with pytest.raises(ui.CardError, match="needs 'action'"):
        ui.check_view({"type": "button", "label": "Go"})
    with pytest.raises(ui.CardError, match="at most"):
        ui.check_view([{"type": "divider"}] * (ui.MAX_NODES + 1))
    with pytest.raises(ui.CardError, match="card id"):
        ui.check_card({"id": "has space"})
    card = ui.check_card({"id": "a", "title": "T", "tone": "bad", "view": [{"type": "stat", "label": "n", "value": 3}]})
    assert card["view"]["children"][0]["value"] == "3" and card["size"] == "medium"


def test_manifest_and_files_are_checked_before_anything_is_written():
    import modules
    assert modules.check_manifest({"name": "X"})["permissions"] == {"network": False, "keys": [], "read": []}
    with pytest.raises(modules.ModuleError, match="unknown permissions"):
        modules.check_manifest({"name": "X", "permissions": {"shell": True}})
    with pytest.raises(modules.ModuleError, match="names from the .env"):
        modules.check_manifest({"name": "X", "permissions": {"keys": ["lower"]}})
    with pytest.raises(modules.ModuleError, match="data folder"):
        modules.check_manifest({"name": "X", "permissions": {"read": [str(modules.ROOT / ".env")]}})
    with pytest.raises(modules.ModuleError, match="config does not match"):
        modules.check_manifest({"name": "X", "config": {"n": "a"},
                                "config_schema": {"type": "object", "properties": {"n": {"type": "integer"}}}})
    for bad, why in (({"../x.py": ""}, "inside the module"), ({"data/x.py": ""}, "data/"),
                     ({"module.json": "{}"}, "data/"), ({"run.sh": ""}, "text files"),
                     ({"main.py": "def apply(ctx:\n"}, "line 1")):
        with pytest.raises(modules.ModuleError, match=why):
            modules._check_files(bad)


def test_the_profile_closes_everything_and_opens_only_what_the_manifest_says():
    import modules
    man = modules.check_manifest({"name": "X"})
    profile = modules.sandbox_profile("demo-x", man)
    base = str(modules.folder("demo-x").resolve())
    assert "(deny network*)" in profile and "(deny appleevent-send)" in profile
    assert f'(subpath "{base}/data")' in profile.split("(deny file-write*)")[1]
    assert '"/Users"' in profile.split("(allow file-read-data")[0]
    opened = modules.sandbox_profile("demo-x", modules.check_manifest({"name": "X", "permissions": {"network": True}}))
    assert "(deny network*)" not in opened


def test_inferred_work_cannot_touch_modules_and_standing_work_waits():
    import modules
    import triggers
    import workers
    assert "module" in workers.INFERRED_DENIED and "module" in triggers.STANDING_SOURCES


def test_inferred_and_standing(mods):
    out = _run(mods.tool(None, _ctx("inferred"), "cto", {"action": "list"}))
    assert out.startswith("refused")
    out = _run(_create(mods, "by-a-job", strength="standing"))
    assert "waits for the owner's approval" in out and "did not ask" in out
    row = mods.get("by-a-job")
    assert row["status"] == "pending" and row["approved"] == ""
    assert "by-a-job" not in mods._running
    assert mods.fake.asked and "Inbox" in mods.fake.asked[-1][1]
    assert [i["id"] for i in mods.pending()] == ["by-a-job"]
    assert _run(mods.tool(None, _ctx("standing"), "cto", {"action": "remove", "module": "by-a-job"})).startswith("refused")


# ---------- running one ----------

def test_a_module_adds_a_tool_and_a_card_and_answers_its_buttons(mods):
    async def scenario():
        out = await _create(mods, "counter", config={"title": "Clicks"})
        assert "is running" in out, out
        assert "Tools: count" in out and 'main "Clicks"' in out and "desktop" in out
        assert await _until(lambda: ("counter", "main") in mods._cards)
        card = mods._cards[("counter", "main")]
        assert card["view"]["children"][0]["value"] == "0" and card["desktop"] is True
        assert json.loads(await mods.tool(None, _ctx(), "cto", {"action": "call", "module": "counter", "tool": "count",
                                                                   "args": {"plus": 2}})) == {"count": 2}
        bad = await mods.tool(None, _ctx(), "cto", {"action": "call", "module": "counter", "tool": "count",
                                                    "args": {"plus": "x"}})
        assert bad.startswith("error: invalid plus")
        assert await mods.act("counter", "main", "add", 1)
        assert await _until(lambda: mods._cards[("counter", "main")]["tone"] == "warn")
        assert await _until(lambda: mods.get("counter")["state"].get("count") == 3)
        assert any(e.get("type") == "module_card" for e in mods.sent)
        # The owner's choice of where a card goes outlasts the module drawing it again.
        await mods.set_desktop("counter", "main", False)
        await mods.call("counter", "count", {})
        assert mods._cards[("counter", "main")]["desktop"] is False
        assert "count" in mods.index_block() and "counter" in mods.index_block()
        # Stopping takes its tools, and leaves its card up marked as stopped.
        await mods.tool(None, _ctx(), "cto", {"action": "stop", "module": "counter"})
        assert mods.get("counter")["status"] == "stopped"
        assert mods.cards("counter")[0]["module_status"] == "stopped"
        assert "not running" in await mods.tool(None, _ctx(), "cto", {"action": "call", "module": "counter",
                                                                     "tool": "count"})
        # State is kept across a restart.
        out = await mods.tool(None, _ctx(), "cto", {"action": "start", "module": "counter"})
        assert "is running" in out, out
        assert json.loads(await mods.tool(None, _ctx(), "cto", {"action": "call", "module": "counter",
                                                                   "tool": "count"}))["count"] == 3
        await mods.tool(None, _ctx(), "cto", {"action": "remove", "module": "counter"})
        assert mods.get("counter") is None and not mods.folder("counter").exists() and not mods.cards("counter")
    _run(scenario())


def test_a_new_version_replaces_the_old_and_takes_down_cards_it_no_longer_draws(mods, monkeypatch):
    monkeypatch.setattr(mods, "SWEEP_AFTER", 0.2)
    two = COUNTER.replace('    @ctx.every(3600)', '    ctx.card("extra", title="Extra").render([ui.text("x")])\n\n    @ctx.every(3600)')

    async def scenario():
        assert "is running" in await _create(mods, "versions", code=two)
        assert await _until(lambda: ("versions", "extra") in mods._cards)
        first = mods.get("versions")["approved"]
        out = await mods.tool(None, _ctx(), "cto", {"action": "update", "module": "versions",
                                                    "files": {"main.py": COUNTER, "notes.md": "v2"}})
        if not mods.sandboxed():
            await mods.approve("versions")
            out = await mods._outcome("versions")
        assert "is running" in out, out
        assert mods.get("versions")["approved"] != first
        await asyncio.sleep(0.3)
        await mods._tick()
        assert ("versions", "extra") not in mods._cards and ("versions", "main") in mods._cards
        assert (mods.folder("versions") / "notes.md").read_text() == "v2"
    _run(scenario())


def test_code_changed_on_disk_waits_for_the_owner(mods):
    async def scenario():
        assert "is running" in await _create(mods, "edited")
        (mods.folder("edited") / "main.py").write_text(COUNTER + "\n# changed by hand\n")
        await mods._scan()
        row = mods.get("edited")
        assert row["status"] == "pending" and "outside the module tool" in row["reason"]
        assert "edited" not in mods._running
        before = len(mods.fake.asked)
        await mods._scan()
        assert len(mods.fake.asked) == before   # told once per version
        await mods.approve("edited")
        assert await _until(lambda: mods.get("edited")["status"] == "running")
        # A folder that appears by itself is a module nobody approved.
        dropped = mods.folder("dropped-in")
        dropped.mkdir()
        (dropped / "main.py").write_text(COUNTER)
        (dropped / "module.json").write_text(json.dumps({"name": "Dropped"}))
        await mods._scan()
        assert mods.get("dropped-in")["status"] == "pending"
        assert "dropped-in" not in mods._running
    _run(scenario())


def test_a_module_that_fails_to_load_says_why_and_is_not_retried(mods):
    broken = "def apply(ctx):\n    raise RuntimeError('no config for ' + ctx.name)\n"

    async def scenario():
        out = await _create(mods, "broken", code=broken)
        assert "FAILED" in out and "RuntimeError: no config for Broken (main.py line 2, in apply)" in out, out
        assert "broken" not in mods._retry_at
        out = await _create(mods, "no-apply", code="x = 1\n")
        assert "has no apply(ctx)" in out
    _run(scenario())


def test_an_apply_that_never_returns_is_stopped(mods, monkeypatch):
    monkeypatch.setattr(mods, "APPLY_LIMIT", 0.5)
    monkeypatch.setattr(mods, "START_WAIT", 0.3)

    async def scenario():
        out = await _create(mods, "stuck", code="import time\n\ndef apply(ctx):\n    time.sleep(60)\n")
        assert "has not said it is ready" in out, out
        await asyncio.sleep(0.6)
        await mods._tick()
        row = mods.get("stuck")
        assert row["status"] == "failed" and "did not return" in row["error"] and "stuck" not in mods._running
    _run(scenario())


def test_a_crash_comes_back_after_a_wait(mods):
    crash = ("import os\n\ndef apply(ctx):\n    @ctx.tool('die')\n    def die(args):\n        os._exit(3)\n")

    async def scenario():
        assert "is running" in await _create(mods, "crasher", code=crash)
        with pytest.raises(mods.ModuleError, match="exited"):
            await mods.call("crasher", "die", {})
        assert await _until(lambda: mods.get("crasher")["status"] == "failed")
        assert "starting again in 5 s" in mods.get("crasher")["error"]
        mods._retry_at["crasher"] = 0
        await mods._tick()
        assert await _until(lambda: mods.get("crasher")["status"] == "running")
    _run(scenario())


def test_notices_and_requests_are_rationed(mods):
    talker = ("def apply(ctx):\n    @ctx.tool('say')\n    def say(args):\n        ctx.notify(args.get('text', 'hi'))\n"
              "        ctx.ask('look at ' + args.get('text', 'hi'))\n        return 'ok'\n")

    async def scenario():
        assert "is running" in await _create(mods, "talker", code=talker)
        await mods.call("talker", "say", {"text": "one"})
        await mods.call("talker", "say", {"text": "two"})
        assert await _until(lambda: mods.fake.router.told and mods.fake.events)
        await asyncio.sleep(0.2)
        assert mods.fake.router.told == ["Talker: one"]
        aid, source, text, kwargs = mods.fake.events[0]
        assert (aid, source, kwargs["thread"]) == ("cto", "module:talker", "module:talker")
        assert "look at one" in text and len(mods.fake.events) == 1
        assert any("refused" in line["text"] for line in mods.logs("talker"))
    _run(scenario())


def test_a_bad_card_is_an_error_in_the_modules_own_code(mods):
    bad = ("def apply(ctx):\n    @ctx.every(3600)\n    def draw():\n"
           "        ctx.card('c').render([{'type': 'marquee'}])\n")

    async def scenario():
        out = await _create(mods, "bad-card", code=bad)
        assert "is running" in out and "unknown type 'marquee'" in out, out
        assert "(main.py line 4, in draw)" in out and "module_host" not in out, out
        assert ("bad-card", "c") not in mods._cards
    _run(scenario())


def test_the_tool_definition_fits_the_harness():
    import orchestrator
    names = [t["name"] for t in orchestrator.TOOL_DEFS]
    assert names.count("module") == 1
    from jsonschema import Draft202012Validator
    Draft202012Validator.check_schema(orchestrator.modules_mod.TOOL_DEF["input_schema"])


# ---------- the sandbox, where there is one ----------

darwin = pytest.mark.skipif(sys.platform != "darwin", reason="the module sandbox is macOS's")


@darwin
def test_the_sandbox_holds_what_the_manifest_does_not_give(mods, tmp_path):
    import modules
    (modules.ROOT / ".env").write_text("SECRET=hunter2\n")
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]
    def serve():
        try:
            while True:
                server.accept()[0].close()
        except OSError:
            pass   # closed at the end of the test
    threading.Thread(target=serve, daemon=True).start()
    probe = f'''
import os, socket

def attempt(fn):
    try:
        fn()
        return "allowed"
    except Exception as exc:
        return "denied " + type(exc).__name__

def apply(ctx):
    @ctx.tool("probe")
    def probe(args):
        return {{
            "secret": attempt(lambda: open({str(modules.ROOT / ".env")!r}).read()),
            "home": attempt(lambda: os.listdir(os.path.expanduser("~"))),
            "own_code": attempt(lambda: open("main.py").read()),
            "write_code": attempt(lambda: open("main.py", "a").write("#")),
            "write_data": attempt(lambda: (ctx.data / "x.txt").write_text("1")),
            "network": attempt(lambda: socket.create_connection(("127.0.0.1", {port}), timeout=3).close()),
            "key": os.environ.get("SECRET", ""),
        }}
'''

    async def scenario():
        assert "is running" in await _create(mods, "probe-closed", code=probe)
        closed = await mods.call("probe-closed", "probe", {})
        assert closed["secret"].startswith("denied") and closed["home"].startswith("denied")
        assert closed["own_code"] == "allowed" and closed["write_data"] == "allowed"
        assert closed["write_code"].startswith("denied") and closed["network"].startswith("denied")
        assert closed["key"] == ""
        import settings
        mods_env = settings.env_all
        try:
            settings.env_all = lambda: {"SECRET": "hunter2"}
            assert "is running" in await _create(mods, "probe-open", code=probe,
                                                 permissions={"network": True, "keys": ["SECRET"]})
        finally:
            settings.env_all = mods_env
        opened = await mods.call("probe-open", "probe", {})
        assert opened["network"] == "allowed" and opened["key"] == "hunter2"
        assert opened["secret"].startswith("denied")
    try:
        _run(scenario())
    finally:
        server.close()


def test_agents_reach_modules_through_the_tool_dispatcher(mods):
    import orchestrator
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    aid = next(iter(o.agents))
    assert "apply(ctx)" in asyncio.run(o._exec_tool(aid, "~", "module", {"action": "guide"}))
    assert "action=guide" in asyncio.run(o._exec_tool(aid, "~", "module", {"action": "list"})) or \
        "|" in asyncio.run(o._exec_tool(aid, "~", "module", {"action": "list"}))
    bad = asyncio.run(o._exec_tool(aid, "~", "module", {"action": "create", "module": "x"}))
    assert bad.startswith("module is the module's slug")
