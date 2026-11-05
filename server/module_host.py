"""The process a module runs in. It loads the module's code, gives it a context to
register what it adds, and speaks to the server in JSON lines over stdin and stdout.

On macOS this runs inside the sandbox the server wraps around it, so nothing here is a
security boundary: it is the module's own process, and the server trusts nothing it says.
It exists to make modules short and their mistakes clear.

    def apply(ctx):
        card = ctx.card("sites", title="Sites")

        @ctx.every(300)
        def check():
            card.render([ctx.ui.status("example.com", tone="ok", detail="212 ms")])

        @ctx.tool("check_now", "Check every site now")
        def check_now(args):
            check()
            return "checked"

Everything a module registers belongs to the module and goes when it stops, so a new
version starts from nothing and nothing outlives the code that made it.
"""
import asyncio
import importlib.util
import inspect
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_ui():
    if getattr(sys, "frozen", False):
        import module_ui   # bundled with the app's own code, which has no files to load by path
        return module_ui
    # Loaded by path: putting the server's folder on sys.path would let a module
    # import the server's own code as though it were part of it.
    spec = importlib.util.spec_from_file_location("module_ui", _HERE / "module_ui.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ui_rules = _load_ui()
_out = sys.stdout
_out_lock = threading.Lock()
MIN_EVERY = 5


def send(message: dict):
    line = json.dumps(message, ensure_ascii=False, default=str)
    with _out_lock:
        _out.write(line + "\n")
        _out.flush()


class _LogWriter:
    """print() in a module goes to its log, since stdout is the protocol."""

    def __init__(self, level):
        self.level, self.buffer = level, ""

    def write(self, text):
        self.buffer += str(text)
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                send({"op": "log", "level": self.level, "text": line[:2000]})
        return len(text)

    def flush(self):
        if self.buffer.strip():
            send({"op": "log", "level": self.level, "text": self.buffer[:2000]})
        self.buffer = ""


def _call(fn, *args):
    result = fn() if _takes_nothing(fn) else fn(*args)
    if inspect.isawaitable(result):
        result = asyncio.run(_await(result))
    return result


async def _await(awaitable):
    return await awaitable


def _explain(exc: BaseException) -> str:
    """The error, and where in the module's own code it happened. The host's frames say
    nothing the module's author can act on, so they are left out."""
    here = str(Path.cwd())
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename.startswith(here)]
    where = " <- ".join(f"{Path(f.filename).relative_to(here)} line {f.lineno}, in {f.name}"
                        for f in reversed(frames[-4:]))
    return f"{type(exc).__name__}: {exc}" + (f" ({where})" if where else "")


def _takes_nothing(fn) -> bool:
    try:
        return not inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


class UI:
    """Builders for card nodes. Each returns a plain dict, so a module can also write
    the dicts itself."""

    @staticmethod
    def _node(node_type, children=None, **fields):
        node = {"type": node_type, **{k: v for k, v in fields.items() if v is not None}}
        if children is not None:
            node["children"] = list(children)
        return node

    def vstack(self, *children, spacing=None, align=None):
        return self._node("stack", _flat(children), axis="v", spacing=spacing, align=align)

    def hstack(self, *children, spacing=None, align=None):
        return self._node("stack", _flat(children), axis="h", spacing=spacing, align=align)

    def grid(self, *children, columns=2, spacing=None):
        return self._node("grid", _flat(children), columns=columns, spacing=spacing)

    def text(self, text, style=None, tone=None, lines=None):
        return self._node("text", text=str(text), style=style, tone=tone, lines=lines)

    def title(self, text, tone=None):
        return self.text(text, style="headline", tone=tone)

    def caption(self, text, tone="muted"):
        return self.text(text, style="caption", tone=tone)

    def stat(self, label, value, unit=None, detail=None, tone=None, trend=None):
        return self._node("stat", label=str(label), value=str(value), unit=unit, detail=detail,
                          tone=tone, trend=trend)

    def status(self, label, tone="ok", detail=None):
        return self._node("status", label=str(label), tone=tone, detail=detail)

    def badge(self, text, tone=None):
        return self._node("badge", text=str(text), tone=tone)

    def progress(self, value, label=None, tone=None):
        return self._node("progress", value=float(value), label=label, tone=tone)

    def chart(self, values, kind="line", label=None, tone=None, min=None, max=None):
        return self._node("chart", values=[float(v) for v in values], kind=kind, label=label,
                          tone=tone, min=min, max=max)

    def symbol(self, name, tone=None, size=None):
        return self._node("symbol", name=name, tone=tone, size=size)

    def table(self, rows, columns=None):
        return self._node("table", rows=[list(r) for r in rows], columns=columns)

    def button(self, label, action, value=None, style=None, symbol=None):
        return self._node("button", label=label, action=action, value=value, style=style, symbol=symbol)

    def link(self, label, url):
        return self._node("link", label=label, url=url)

    def field(self, name, placeholder=None, value=None):
        return self._node("field", name=name, placeholder=placeholder, value=value)

    def toggle(self, name, label=None, value=False, action=None):
        return self._node("toggle", name=name, label=label, value=bool(value), action=action)

    def divider(self):
        return {"type": "divider"}

    def spacer(self):
        return {"type": "spacer"}


def _flat(children):
    out = []
    for child in children:
        if isinstance(child, (list, tuple)):
            out.extend(_flat(child))
        elif child is not None:
            out.append(child)
    return out


class Card:
    def __init__(self, ctx, cid, title, subtitle, symbol, size, desktop):
        self.ctx, self.id = ctx, cid
        self.head = {"title": title, "subtitle": subtitle, "symbol": symbol, "size": size}
        self.desktop = desktop
        self.view = []
        self.tone = ""
        self.actions = {}

    def render(self, view=None, *, title=None, subtitle=None, tone=None, symbol=None):
        """Show this on the card, replacing what it showed. A list is a vertical stack."""
        if view is not None:
            self.view = view
        for key, value in (("title", title), ("subtitle", subtitle), ("symbol", symbol)):
            if value is not None:
                self.head[key] = value
        if tone is not None:
            self.tone = tone
        message = ui_rules.check_card({"id": self.id, **self.head, "tone": self.tone, "view": self.view})
        send({"op": "card", **message, "desktop": self.desktop})

    def on(self, action):
        """Run the decorated function when a button or toggle with this action is used.
        It receives the event: {"action", "value", "values"}, values being the card's fields."""
        def register(fn):
            self.actions[action] = fn
            return fn
        return register

    def remove(self):
        self.ctx._cards.pop(self.id, None)
        send({"op": "uncard", "id": self.id})


class Context:
    def __init__(self, init: dict):
        self.slug = init.get("slug", "")
        self.name = init.get("name", self.slug)
        self.config = init.get("config") or {}
        self.state = init.get("state") or {}
        self.folder = Path.cwd()
        self.data = Path(init.get("data") or (self.folder / "data"))
        self.ui = UI()
        self.stopping = threading.Event()
        self._tools, self._cards, self._timers, self._effects = {}, {}, [], []
        self._saved = json.dumps(self.state, sort_keys=True, default=str)
        self._save_lock = threading.Lock()

    # What it adds

    def tool(self, name, description="", schema=None):
        """A tool the agents can call: module(action="call", module=<slug>, tool=<name>, args={...}).
        The function receives the arguments as a dict and returns anything JSON can carry."""
        schema = schema or {"type": "object", "properties": {}}

        def register(fn):
            self._tools[name] = fn
            send({"op": "tool", "name": name, "description": description or (fn.__doc__ or "").strip(),
                  "schema": schema})
            return fn
        return register

    def card(self, cid, title="", subtitle="", symbol="", size="medium", desktop=True):
        """A card the apps draw. On the Mac it sits on the desktop unless the owner moves it off."""
        card = self._cards.get(cid) or Card(self, cid, title, subtitle, symbol, size, desktop)
        self._cards[cid] = card
        ui_rules.check_card({"id": cid, "size": size, "view": []})
        return card

    def every(self, seconds, run_now=True):
        """Run the decorated function every so many seconds, from when the module is ready."""
        seconds = max(MIN_EVERY, float(seconds))

        def register(fn):
            self._timers.append((seconds, fn, run_now))
            return fn
        return register

    def effect(self, start):
        """Run start now; whatever it returns is called when the module stops."""
        dispose = start()
        if callable(dispose):
            self._effects.append(dispose)
        return dispose

    # What it can say

    def log(self, *parts):
        send({"op": "log", "level": "info", "text": " ".join(str(p) for p in parts)[:2000]})

    def notify(self, text):
        """Tell the owner, wherever they last spoke. Limited to a few a day."""
        send({"op": "notify", "text": str(text)[:2000]})

    def ask(self, text):
        """Hand something to an agent to look into, as a job would. Limited to a few a day."""
        send({"op": "ask", "text": str(text)[:4000]})

    def save(self):
        """Keep ctx.state for the next start. It is also kept after every timer, call and action."""
        with self._save_lock:
            raw = json.dumps(self.state, sort_keys=True, default=str)
            if raw != self._saved:
                self._saved = raw
                send({"op": "state", "value": json.loads(raw)})

    # Running it

    def _start_timers(self):
        for seconds, fn, run_now in self._timers:
            threading.Thread(target=self._loop, args=(seconds, fn, run_now), daemon=True).start()

    def _loop(self, seconds, fn, run_now):
        if not run_now and self.stopping.wait(seconds):
            return
        while not self.stopping.is_set():
            self._guarded(f"timer {getattr(fn, '__name__', '')}", fn)
            if self.stopping.wait(seconds):
                return

    def _guarded(self, what, fn, *args):
        try:
            return True, _call(fn, *args)
        except Exception as exc:
            send({"op": "log", "level": "error", "text": f"{what} failed: {_explain(exc)}"[:4000]})
            return False, _explain(exc)
        finally:
            try:
                self.save()
            except Exception:
                pass

    def _dispose(self):
        self.stopping.set()
        for dispose in reversed(self._effects):
            try:
                _call(dispose)
            except Exception as exc:
                send({"op": "log", "level": "error", "text": f"stopping: {type(exc).__name__}: {exc}"})
        self._effects.clear()
        self.save()


def _handle(ctx: Context, message: dict):
    op = message.get("op")
    if op == "call":
        fn = ctx._tools.get(message.get("tool"))
        if fn is None:
            send({"op": "result", "id": message.get("id"), "ok": False,
                  "error": f"this module has no tool {message.get('tool')!r}"})
            return
        ok, value = ctx._guarded(f"tool {message.get('tool')}", fn, message.get("args") or {})
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            value = str(value)
        send({"op": "result", "id": message.get("id"), "ok": ok, **({"value": value} if ok else {"error": value})})
    elif op == "action":
        card = ctx._cards.get(message.get("card"))
        fn = card and card.actions.get(message.get("action"))
        if fn is None:
            send({"op": "log", "level": "warn",
                  "text": f"no handler for action {message.get('action')!r} on card {message.get('card')!r}"})
            return
        ctx._guarded(f"action {message.get('action')}", fn,
                     {"action": message.get("action"), "value": message.get("value"),
                      "values": message.get("values") or {}})


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    folder, entry = Path(argv[0]).resolve(), argv[1] if len(argv) > 1 else "main.py"
    os.chdir(folder)
    sys.path.insert(0, str(folder))
    sys.stdout = _LogWriter("info")
    sys.stderr = _LogWriter("error")
    line = sys.stdin.readline()
    init = json.loads(line) if line.strip() else {}
    ctx = Context(init)
    try:
        spec = importlib.util.spec_from_file_location("module_main", folder / entry)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {entry}")
        code = importlib.util.module_from_spec(spec)
        sys.modules["module_main"] = code
        spec.loader.exec_module(code)
        apply = getattr(code, "apply", None)
        if not callable(apply):
            raise RuntimeError(f"{entry} has no apply(ctx) function")
        _call(apply, ctx)
    except BaseException as exc:
        send({"op": "failed", "error": _explain(exc)[:4000]})
        return 1
    send({"op": "ready", "tools": sorted(ctx._tools), "cards": sorted(ctx._cards)})
    ctx._start_timers()
    work = []
    for raw in sys.stdin:
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if message.get("op") == "stop":
            break
        thread = threading.Thread(target=_handle, args=(ctx, message), daemon=True)
        thread.start()
        work = [t for t in work if t.is_alive()] + [thread]
    ctx._dispose()
    deadline = time.monotonic() + 3
    for thread in work:
        thread.join(max(0.0, deadline - time.monotonic()))
    send({"op": "stopped"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
