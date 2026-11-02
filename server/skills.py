"""Pluggable skills: procedures the team learns and reuses.

A skill is a folder under <data dir>/skills/<slug>/ holding SKILL.md with
a small frontmatter (name, description, avoid, author, updated) and a
markdown body of exact steps. Agents see an index of skills every turn,
read one with the skill tool when relevant, and save or update skills
themselves when they work out a procedure worth keeping. Humans can drop
skill folders in by hand too; nothing here is generated code, just
knowledge.

`description` is the trigger: it is in every agent's context on every
turn, and it is what makes a skill fire. `avoid` is the counterweight —
the cases where reaching for this skill is the wrong move — because a
skill that fires on the wrong task is worse than no skill at all, and a
description alone can only ever say yes.

Use counts live in a sidecar (.uses.json) rather than the frontmatter, so
reading a skill never touches SKILL.md's mtime and never looks like an
edit to fleet sync.
"""
import datetime
import json
import hashlib
import functools
import os
import re
import shutil
import sys
import tempfile
import threading
import time

import yaml
from jsonschema import Draft202012Validator

from apphome import ROOT, BUNDLE

SKILLS_DIR = ROOT / "skills"
MAX_SKILLS = 200
USES_PATH = SKILLS_DIR / ".uses.json"
MAX_BYTES = 128_000
_write_lock = threading.RLock()
CONTRACT_KEYS = ("version", "allowed_tools", "agents", "platforms", "inputs",
                 "max_tool_calls", "timeout_seconds")


def _serialized(fn):
    @functools.wraps(fn)
    def call(*args, **kwargs):
        with _write_lock:
            return fn(*args, **kwargs)
    return call


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:60] or "skill"


def _parse(md: str) -> dict:
    meta = {}
    body = md
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", md, re.S)
    if match:
        try:
            meta = yaml.safe_load(match[1]) or {}
        except yaml.YAMLError:
            # Older Mission Control wrote unquoted one-line descriptions with
            # colons. Keep those readable, but never guess a malformed contract.
            if any(re.search(rf"^{key}:", match[1], re.M) for key in CONTRACT_KEYS):
                raise ValueError("invalid YAML in skill contract")
            meta = dict(line.split(":", 1) for line in match[1].splitlines() if ":" in line)
            meta = {k.strip(): v.strip() for k, v in meta.items()}
        if not isinstance(meta, dict):
            raise ValueError("skill frontmatter must be a mapping")
        body = md[match.end():].lstrip("\n")
    # Accept common hyphenated frontmatter without silently dropping limits.
    for key in CONTRACT_KEYS:
        alias = key.replace("_", "-")
        if alias in meta and key not in meta:
            meta[key] = meta.pop(alias)
    for key in ("name", "description", "avoid", "author", "updated"):
        if key in meta:
            meta[key] = str(meta[key])
    return {**meta, "body": body}


def _file(slug: str):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", slug or ""):
        raise ValueError("invalid skill slug")
    path = SKILLS_DIR / slug / "SKILL.md"
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("skill files must stay inside the skills directory")
    return path


def _read(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("skill is too large (128 KB maximum)")
    return path.read_text(encoding="utf-8")


def _atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".skill-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def validate_contract(meta: dict) -> dict:
    contract = {k: meta[k] for k in CONTRACT_KEYS if k in meta}
    for key in ("allowed_tools", "agents", "platforms"):
        if key in contract:
            value = contract[key]
            if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
                raise ValueError(f"{key} must be a list of names")
    for key, default, upper in (("max_tool_calls", 20, 200), ("timeout_seconds", 600, 3600)):
        value = contract.get(key, default)
        if type(value) is not int or not 1 <= value <= upper:
            raise ValueError(f"{key} must be an integer from 1 to {upper}")
        contract[key] = value
    schema = contract.get("inputs", {"type": "object"})
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("inputs must be a JSON Schema with type: object")
    visited = set()
    def local_refs(value):
        if isinstance(value, (dict, list)):
            if id(value) in visited:
                raise ValueError("recursive YAML structures are not supported")
            visited.add(id(value))
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("$ref", "$dynamicRef") and (not isinstance(item, str) or not item.startswith("#")):
                    raise ValueError("skill schemas support local references only")
                local_refs(item)
        elif isinstance(value, list):
            for item in value:
                local_refs(item)
    local_refs(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ValueError(f"invalid inputs schema: {str(exc).splitlines()[0]}") from exc
    contract["inputs"] = schema
    contract["version"] = str(contract.get("version", "1"))
    return contract


def manifest(name: str, agent: str | None = None, inputs: dict | None = None) -> dict:
    slug = slug_for(name)
    if not slug:
        raise ValueError(f"no skill named '{name}'")
    raw = _read(_file(slug))
    meta = _parse(raw)
    contract = validate_contract(meta)
    if "agents" in contract and agent is not None and agent not in contract["agents"]:
        raise ValueError(f"skill '{slug}' is not available to agent '{agent}'")
    platform = "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux"
    if "platforms" in contract and platform not in contract["platforms"]:
        raise ValueError(f"skill '{slug}' is unavailable on {platform}")
    if inputs is not None:
        error = next(Draft202012Validator(contract["inputs"]).iter_errors(inputs), None)
        if error:
            path = ".".join(map(str, error.absolute_path)) or "inputs"
            raise ValueError(f"{path}: {error.message}")
    return {**meta, **contract, "slug": slug,
            "revision": hashlib.sha256(raw.encode()).hexdigest()[:16]}


def list_skills() -> list[dict]:
    if not SKILLS_DIR.exists():
        return []
    uses = _load_uses()
    out = []
    for d in sorted(SKILLS_DIR.iterdir()):
        f = d / "SKILL.md"
        if not d.is_dir() or d.is_symlink() or f.is_symlink() or not f.exists():
            continue
        error = None
        try:
            meta = _parse(_read(f))
            contract = validate_contract(meta)
        except (ValueError, OSError, UnicodeError) as exc:
            meta, contract, error = {}, {}, str(exc)
        stat = uses.get(d.name) or {}
        out.append({"slug": d.name, "name": meta.get("name", d.name),
                    "description": meta.get("description", ""),
                    "avoid": meta.get("avoid", ""),
                    "author": meta.get("author", ""),
                    "updated": meta.get("updated", ""),
                    "uses": int(stat.get("count") or 0),
                    "last_used": stat.get("last") or 0,
                    "contract": contract, "valid": error is None, "error": error})
    return out


# Every skill's trigger line is read by every agent on every turn, so the
# catalogue is a fixed budget however many skills exist. Spending it in
# alphabetical order of the directory name is spending it at random: past the
# cap a skill becomes invisible for being called "zzz-", and below it the agent
# reads forty triggers for a task that matches two.
_STOP = frozenset((
    "the a an and or of to for in on at by with from into this that it is are was"
    " were be been being do does did done how when what which who why not no if"
    " then than as but so we you i they them our your my me us can will just"
).split())


def _terms(text: str) -> set:
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in _STOP}


def rank(index: list[dict], query: str, limit: int) -> list[dict]:
    """The skills most worth showing this turn, most relevant first.

    Scored on the trigger (name and description), because that is what says when
    a skill fires; `avoid` is displayed but not matched, or a skill would surface
    on exactly the task it says it is wrong for. Rare words count for more than
    common ones, so a catalogue where every entry says "deploy" is not decided by
    that word. Ties, and an empty query, fall back to what has actually been used
    and how recently — which beats the directory listing either way.
    """
    if limit <= 0 or not index:
        return []
    wanted = _terms(query)
    document_count = {}
    for sk in index:
        for term in _terms(f"{sk.get('name', '')} {sk.get('description', '')}"):
            document_count[term] = document_count.get(term, 0) + 1
    newest = max((sk.get("last_used") or 0) for sk in index) or 1

    def score(sk):
        trigger = _terms(f"{sk.get('name', '')} {sk.get('description', '')}")
        hit = sum(1.0 / (1 + document_count.get(term, 0)) for term in wanted & trigger)
        # A skill in active use is part of the current working set; a skill
        # nothing has ever read is the one that should lose the slot.
        familiarity = min(int(sk.get("uses") or 0), 20) / 200.0
        recency = 0.05 * ((sk.get("last_used") or 0) / newest)
        return (hit, familiarity + recency, sk.get("slug") or "")

    return sorted(index, key=score, reverse=True)[:limit]


def _load_uses() -> dict:
    try:
        value = json.loads(USES_PATH.read_text())
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def record_use(slug: str):
    """Count a read. Stale, never-read skills are the ones worth pruning, and
    a skill that fires constantly is the one worth keeping sharp."""
    if not slug:
        return
    with _write_lock:
        uses = _load_uses()
        row = uses.setdefault(slug, {"count": 0, "last": 0})
        row["count"] = int(row.get("count") or 0) + 1
        row["last"] = time.time()
        _atomic(USES_PATH, json.dumps(uses))


def slug_for(name: str) -> str | None:
    """The slug a name resolves to, or None if no such skill."""
    slug = _slug(name)
    if _file(slug).exists():
        return slug
    for s in list_skills():
        if s["name"].lower() == (name or "").lower():
            return s["slug"]
    return None


def read_skill(name: str) -> str | None:
    slug = slug_for(name)
    return _read(_file(slug)) if slug else None


def _front(name: str, description: str, avoid: str, author: str) -> str:
    today = datetime.date.today().isoformat()
    meta = {"name": _oneline(name), "description": _oneline(description)}
    if avoid:
        meta["avoid"] = _oneline(avoid)
    meta.update(author=author, updated=today)
    return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n\n"


def _oneline(s: str) -> str:
    """Frontmatter is one key per line, so a value that wraps would silently
    truncate the skill's trigger."""
    return " ".join((s or "").split())


@_serialized
def save_skill(author: str, name: str, description: str, content: str,
               avoid: str = "", mode: str = "replace") -> str:
    """mode='replace' writes the skill as given. mode='revise' keeps the body
    that is already there and appends `content` as a dated revision, so an
    agent can correct one detail without having to reproduce the whole
    procedure from memory — which in practice means it never corrects it."""
    slug = _slug(name)
    f = _file(slug)
    existing = _parse(_read(f)) if f.exists() else None
    if mode not in ("replace", "revise"):
        raise ValueError("mode must be replace or revise")
    if not name.strip() or not content.strip():
        raise ValueError("name and content are required")
    if existing is None and len(list_skills()) >= MAX_SKILLS:
        raise ValueError("skill limit reached; prune stale skills first")

    body = content.strip()
    if mode == "revise":
        if existing is None:
            raise ValueError(f"no skill named '{name}' to revise")
        today = datetime.date.today().isoformat()
        body = (existing["body"].rstrip() +
                f"\n\n## Revised {today} ({author})\n{body}")
        # an unstated field on a revision keeps what the skill already said
        description = description or existing.get("description", "")
        avoid = avoid or existing.get("avoid", "")

    header = _parse(_front(name, description, avoid, author))
    header.pop("body")
    # Learning can refine instructions, but cannot discard an owner's contract.
    header.update({k: existing[k] for k in CONTRACT_KEYS if existing and k in existing})
    raw = "---\n" + yaml.safe_dump(header, sort_keys=False, allow_unicode=True) + "---\n\n" + body + "\n"
    if len(raw.encode()) > MAX_BYTES:
        raise ValueError("skill is too large (128 KB maximum)")
    with _write_lock:
        _atomic(f, raw)
    return slug


@_serialized
def write_skill(slug: str, content: str) -> bool:
    """Replace a skill's SKILL.md wholesale (the editor path)."""
    f = _file(slug)
    if not f.exists():
        return False
    if len(content.encode()) > MAX_BYTES:
        raise ValueError("skill is too large (128 KB maximum)")
    validate_contract(_parse(content))
    with _write_lock:
        _atomic(f, content)
    return True


@_serialized
def delete_skill(name: str) -> bool:
    slug = _slug(name)
    f = _file(slug)
    d = f.parent
    if not f.exists():
        return False
    with _write_lock:
        shutil.rmtree(d)
    return True


# ---------- fleet sync ----------

def export_skills(since: float) -> list[dict]:
    """Skills whose file changed since `since`, full content included."""
    out = []
    if not SKILLS_DIR.exists():
        return out
    for d in SKILLS_DIR.iterdir():
        f = d / "SKILL.md"
        if not d.is_dir() or d.is_symlink() or f.is_symlink() or not f.exists():
            continue
        mtime = f.stat().st_mtime
        if mtime <= since:
            continue
        out.append({"slug": d.name, "mtime": mtime, "content": _read(f)})
    return out


@_serialized
def merge_skills(rows: list[dict]) -> int:
    """Last-write-wins by file mtime; the newer copy replaces the older."""
    applied = 0
    for sk in rows:
        slug = _slug(sk.get("slug") or "")
        if not slug or not sk.get("content"):
            continue
        f = _file(slug)
        if f.exists() and f.stat().st_mtime >= sk["mtime"]:
            continue
        if len(sk["content"].encode()) > MAX_BYTES:
            continue
        try:
            validate_contract(_parse(sk["content"]))
        except ValueError:
            continue
        _atomic(f, sk["content"])
        os.utime(f, (sk["mtime"], sk["mtime"]))
        applied += 1
    return applied


def ensure_starters():
    """Seed bundled, editable procedures once without replacing learned skills."""
    source = BUNDLE / "bundled_skills"
    if not source.is_dir():
        return
    for d in source.iterdir():
        src = d / "SKILL.md"
        if src.is_file():
            dest = _file(d.name)
            if not dest.exists():
                _atomic(dest, src.read_text(encoding="utf-8"))
