"""What a module's card may say, checked the same way on both sides of the pipe.

A card is a small tree of typed nodes that the apps draw natively. It is data, never
markup: nothing a module sends is run or fetched by the app, so a card can say anything
and still do nothing but show it. The module host checks a card before it leaves the
sandbox, so a mistake reaches the module's own code as an error with its line number,
and the server checks it again on arrival, since the host is the module's process.

Every node is an object with a `type`. Fields not listed for that type are refused
rather than dropped, so a misspelt field is a visible mistake instead of a silent one.
"""

TONES = ("", "ok", "warn", "bad", "info", "muted", "accent")
SIZES = ("small", "medium", "large")

MAX_NODES = 400
MAX_DEPTH = 10
MAX_TEXT = 2000
MAX_VALUES = 240
MAX_ROWS = 60
MAX_BYTES = 96_000

_STR, _NUM, _INT, _BOOL, _ANY = "str", "num", "int", "bool", "any"

# type -> {field: kind or tuple of allowed strings}. `children` is handled on its own.
NODES = {
    "stack": {"axis": ("v", "h"), "spacing": _NUM, "align": ("leading", "center", "trailing")},
    "grid": {"columns": _INT, "spacing": _NUM},
    "text": {"text": _STR, "style": ("title", "headline", "body", "caption", "footnote", "mono"),
             "tone": TONES, "lines": _INT},
    "stat": {"label": _STR, "value": _STR, "unit": _STR, "detail": _STR, "tone": TONES,
             "trend": ("", "up", "down", "flat")},
    "status": {"label": _STR, "detail": _STR, "tone": TONES},
    "badge": {"text": _STR, "tone": TONES},
    "progress": {"value": _NUM, "label": _STR, "tone": TONES},
    "chart": {"kind": ("line", "bar"), "values": "values", "label": _STR, "tone": TONES,
              "min": _NUM, "max": _NUM},
    "symbol": {"name": _STR, "tone": TONES, "size": _NUM},
    "table": {"columns": "columns", "rows": "rows"},
    "button": {"label": _STR, "action": _STR, "value": _ANY, "symbol": _STR,
               "style": ("primary", "plain", "destructive")},
    "link": {"label": _STR, "url": _STR},
    "field": {"name": _STR, "placeholder": _STR, "value": _STR},
    "toggle": {"name": _STR, "label": _STR, "value": _BOOL, "action": _STR},
    "divider": {},
    "spacer": {},
}
CONTAINERS = ("stack", "grid")
REQUIRED = {"text": ("text",), "stat": ("label", "value"), "status": ("label",), "badge": ("text",),
            "progress": ("value",), "chart": ("values",), "symbol": ("name",), "table": ("rows",),
            "button": ("label", "action"), "link": ("label", "url"), "field": ("name",),
            "toggle": ("name",)}


class CardError(ValueError):
    pass


def _kind_ok(kind, value) -> bool:
    if isinstance(kind, tuple):
        return isinstance(value, str) and value in kind
    if kind == _STR:
        return isinstance(value, (str, int, float)) and not isinstance(value, bool)
    if kind == _NUM:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == _INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == _BOOL:
        return isinstance(value, bool)
    if kind == _ANY:
        return value is None or isinstance(value, (str, int, float, bool, list, dict))
    if kind == "values":
        return (isinstance(value, list) and len(value) <= MAX_VALUES
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value))
    if kind == "columns":
        return isinstance(value, list) and len(value) <= 8 and all(isinstance(v, str) for v in value)
    if kind == "rows":
        return (isinstance(value, list) and len(value) <= MAX_ROWS and all(
            isinstance(row, list) and len(row) <= 8 and all(
                isinstance(cell, (str, int, float)) and not isinstance(cell, bool) for cell in row)
            for row in value))
    return False


def _check(node, where: str, depth: int, count: list) -> dict:
    if not isinstance(node, dict):
        raise CardError(f"{where}: a node is an object with a type, not {type(node).__name__}")
    kind = node.get("type")
    if kind not in NODES:
        raise CardError(f"{where}: unknown type {kind!r}; known types: {', '.join(NODES)}")
    count[0] += 1
    if count[0] > MAX_NODES:
        raise CardError(f"a card holds at most {MAX_NODES} nodes")
    if depth > MAX_DEPTH:
        raise CardError(f"{where}: nested deeper than {MAX_DEPTH}")
    fields = NODES[kind]
    out = {"type": kind}
    for key, value in node.items():
        if key == "type":
            continue
        if key == "children" and kind in CONTAINERS:
            if not isinstance(value, list):
                raise CardError(f"{where}.children: a list of nodes")
            out["children"] = [_check(child, f"{where}.{kind}[{i}]", depth + 1, count)
                               for i, child in enumerate(value)]
            continue
        if key not in fields:
            allowed = ", ".join(fields) + (", children" if kind in CONTAINERS else "")
            raise CardError(f"{where}: {kind} has no field {key!r} (it takes: {allowed or 'nothing'})")
        if value is None:
            continue
        if not _kind_ok(fields[key], value):
            want = " | ".join(fields[key]) if isinstance(fields[key], tuple) else fields[key]
            raise CardError(f"{where}: {kind}.{key} must be {want}, not {value!r}"[:300])
        if isinstance(value, str) and len(value) > MAX_TEXT:
            value = value[:MAX_TEXT]
        if fields[key] == _STR and not isinstance(value, str):
            value = str(value)
        out[key] = value
    for key in REQUIRED.get(kind, ()):
        if key not in out:
            raise CardError(f"{where}: {kind} needs {key!r}")
    if kind == "link" and not out["url"].startswith(("https://", "http://")):
        raise CardError(f"{where}: a link opens only http(s) addresses")
    if kind == "grid" and not 1 <= out.get("columns", 2) <= 6:
        raise CardError(f"{where}: a grid has 1 to 6 columns")
    if kind == "progress":
        out["value"] = max(0.0, min(float(out["value"]), 1.0))
    return out


def check_view(view) -> dict:
    """The card body as the apps will draw it, or CardError saying what is wrong.
    A list is shorthand for a vertical stack."""
    import json
    if isinstance(view, list):
        view = {"type": "stack", "children": view}
    checked = _check(view, "view", 0, [0])
    if len(json.dumps(checked)) > MAX_BYTES:
        raise CardError(f"a card is at most {MAX_BYTES // 1000} KB once encoded")
    return checked


def check_card(card: dict) -> dict:
    """A whole card message: its id, header and body."""
    cid = str(card.get("id") or "").strip()
    if not cid or len(cid) > 60 or not all(c.isalnum() or c in "-_." for c in cid):
        raise CardError("a card id is 1 to 60 letters, digits, '-', '_' or '.'")
    size = card.get("size") or "medium"
    if size not in SIZES:
        raise CardError(f"size is one of {', '.join(SIZES)}")
    tone = card.get("tone") or ""
    if tone not in TONES:
        raise CardError(f"tone is one of {', '.join(t for t in TONES if t)}")
    return {"id": cid, "title": str(card.get("title") or "")[:120],
            "subtitle": str(card.get("subtitle") or "")[:200],
            "symbol": str(card.get("symbol") or "")[:80], "tone": tone, "size": size,
            "view": check_view(card.get("view") if card.get("view") is not None else [])}
