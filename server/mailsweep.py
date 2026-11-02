"""Mail cleanup: archive what the owner never needed to see, decided by Laya.

Every message in the Mail inbox is judged by the self-hosted Laya model in two
steps, both from sender and subject alone:

1. The sender, once: shown a few of their subjects, what kind of mail do they
   send? A sender whose mail is mostly promotions, social notifications,
   automated notices or receipts is a bulk sender.
2. Each message from a bulk sender: what kind of email is this one? A bank
   that sends offers also sends login alerts, so a message that looks like a
   security notice, something to act on, or a personal note stays put.

Mail that passes both is moved to the account's Archive mailbox, where it stays
searchable and can be moved back. No language model is involved at any point,
so a sweep costs nothing but local CPU, however big the inbox. Laya answers a
plain yes/no about email poorly but sorts it into kinds well, which is why the
questions are choices.

What a sweep never touches: flagged mail, mail the owner replied to, senders
the owner corresponds with (they replied to a real share of that sender's
mail), and senders on the keep list. A sender the owner has said is always
clutter skips both questions. Every verdict is remembered, so later sweeps only
ask about senders and subjects that are genuinely new.

The first sweeps run dry: they report what they would archive and move
nothing, until the owner turns dry_run off.
"""
import asyncio
import json
import logging
import re
import time
from collections import Counter, defaultdict

import apple
import laya
import settings as cfg
from apphome import ROOT

log = logging.getLogger("missioncontrol.mailsweep")

STATE_PATH = ROOT / "mail_sweep.json"
KINDS = {"promotion": "marketing, sales, offers, discounts, newsletters",
         "social": "social network notifications and updates",
         "automated": "automated system notices: backups, deploys, reports, maintenance",
         "receipt": "receipts, payouts, order and shipping confirmations",
         "security": "security alerts, login notices, verification codes",
         "action": "bills, payment problems, account issues or requests that need action",
         "personal": "written by a person to the owner, or a reply in a conversation"}
BULK = ("promotion", "social", "automated", "receipt")
OWNER = ("security", "action", "personal")
SENDER_QUESTION = "What kind of mail does this sender send?"
MESSAGE_QUESTION = "What kind of email is this?"
# A sender the owner replies to is someone they correspond with, but one reply
# to five thousand backup notices is not.
CORRESPONDENT_SHARE = 0.05
SAMPLE_SUBJECTS = 6
BATCH = 200              # questions per request to Laya
SAVE_EVERY = 1000        # answers between saves during a sweep
MOVE_CHUNK = 250         # moves per osascript call
MAX_PAIRS = 60_000       # remembered message verdicts
MAX_LEDGER = 20_000      # archived messages remembered for restoring
MAX_RUNS = 30

_lock = asyncio.Lock()
_task = None


def settings() -> dict:
    s = cfg.load().get("mail_sweep") or {}
    return {"enabled": bool(s.get("enabled", False)),
            "dry_run": bool(s.get("dry_run", True)),
            "every_minutes": max(15, int(s.get("every_minutes") or 60)),
            "bulk_at": float(s.get("bulk_at") or 0.7),
            "keep_at": float(s.get("keep_at") or 0.35),
            "recheck_days": int(s.get("recheck_days") or 30),
            "max_judged": int(s.get("max_judged") or 3000),
            "max_moves": int(s.get("max_moves") or 5000),
            "accounts": list(s.get("accounts") or []),
            "inbox": s.get("inbox") or "INBOX",
            "archive": s.get("archive") or "Archive",
            "keep_senders": [str(x).lower() for x in (s.get("keep_senders") or [])]}


# ---------- state ----------

def load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        state = {}
    for key, empty in (("senders", {}), ("pairs", {}), ("archived", []), ("runs", []),
                       ("keep", []), ("always", [])):
        state.setdefault(key, empty)
    return state


def save_state(state: dict):
    if len(state["pairs"]) > MAX_PAIRS:
        state["pairs"] = dict(list(state["pairs"].items())[-MAX_PAIRS:])
    state["archived"] = state["archived"][-MAX_LEDGER:]
    state["runs"] = state["runs"][-MAX_RUNS:]
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False))
    tmp.replace(STATE_PATH)


def address(sender: str) -> str:
    m = re.search(r"<([^<>@\s]+@[^<>\s]+)>", sender or "")
    return (m.group(1) if m else (sender or "")).strip().lower()


def matches(addr: str, rules) -> bool:
    """An exact address, or a domain written as example.com or @example.com."""
    domain = addr.rpartition("@")[2]
    for rule in rules:
        rule = str(rule).strip().lower()
        if not rule:
            continue
        bare = rule.lstrip("@")
        if rule == addr or ("@" not in bare and (bare == domain or domain.endswith("." + bare))):
            return True
    return False


def _pair(sender: str, subject: str) -> str:
    return f"{address(sender)}|{' '.join((subject or '').split())[:200]}"


# ---------- Mail ----------

_SCAN = """
const app = Application("Mail");
const want = %(accounts)s;
const lower = (s) => String(s || "").toLowerCase();
const out = [];
app.accounts().forEach((acct) => {
  let name = "";
  try { name = acct.name(); if (!acct.enabled()) return; } catch (e) { return; }
  if (want.length && !want.includes(name)) return;
  const names = acct.mailboxes.name();
  const inbox = names.find((n) => lower(n) === lower(%(inbox)s));
  const archive = names.find((n) => lower(n) === lower(%(archive)s));
  if (!inbox) { out.push({account: name, error: "no inbox mailbox"}); return; }
  const msgs = acct.mailboxes.byName(inbox).messages;
  out.push({account: name, inbox: inbox, archive: archive || null,
            id: msgs.id(), sender: msgs.sender(), subject: msgs.subject(),
            flagged: msgs.flaggedStatus(), replied: msgs.wasRepliedTo()});
});
JSON.stringify(out);
"""

_MOVE = """
const app = Application("Mail");
const acct = app.accounts.byName(%(account)s);
const from = acct.mailboxes.byName(%(source)s);
const to = acct.mailboxes.byName(%(dest)s);
const moved = [];
for (const id of %(ids)s) {
  try { app.move(from.messages.byId(id), {to: to}); moved.push(id); } catch (e) {
    try { from.messages.byId(id).mailbox = to; moved.push(id); } catch (e2) {}
  }
}
JSON.stringify(moved);
"""


async def scan(s: dict) -> list[dict]:
    """Every inbox, read in bulk: a whole property per Apple event, so 25,000
    messages take a second or two rather than minutes."""
    script = _SCAN % {"accounts": json.dumps(s["accounts"]), "inbox": json.dumps(s["inbox"]),
                      "archive": json.dumps(s["archive"])}
    return json.loads(await apple.osa(script, "JavaScript", timeout=300) or "[]")


async def move(account: str, source: str, dest: str, ids: list) -> tuple[list, str]:
    """Move messages by id. Returns the ids that moved, and why it stopped early
    if it did: what already moved is still reported, so the ledger has it."""
    moved = []
    for i in range(0, len(ids), MOVE_CHUNK):
        chunk = ids[i:i + MOVE_CHUNK]
        script = _MOVE % {"account": json.dumps(account), "source": json.dumps(source),
                          "dest": json.dumps(dest), "ids": json.dumps(chunk)}
        try:
            moved += json.loads(await apple.osa(script, "JavaScript", timeout=600) or "[]")
        except apple.AppleError as exc:
            return moved, str(exc)
    return moved, ""


# ---------- judging ----------

async def classify(states: list, question: str) -> tuple[list[dict | None], str]:
    """Laya's probability per kind for each state, in order. Stops at the first
    batch Laya cannot answer; what is missing comes back as None, with why."""
    out: list[dict | None] = []
    for i in range(0, len(states), BATCH):
        chunk = states[i:i + BATCH]
        results = await laya.batch([{"state": st, "questions": {"kind": {
            "type": "choice", "instructions": question, "criteria": KINDS}}} for st in chunk])
        if results is None:
            out += [None] * (len(states) - len(out))
            return out, "Laya is not running" if laya.enabled() else "Laya is turned off"
        for res in results:
            kind = ((res or {}).get("answers") or {}).get("kind") or {}
            out.append(kind.get("probabilities") if isinstance(kind.get("probabilities"), dict) else None)
    return out, ""


async def _ask_in_chunks(state: dict, keys: list, states: list, question: str, store) -> tuple[int, str]:
    """Ask Laya in chunks and save after each one, so an app restart in the
    middle of a long first sweep keeps every answer already paid for in time.
    Returns how many were answered, and why it stopped early if it did."""
    answered = 0
    for i in range(0, len(keys), SAVE_EVERY):
        got, problem = await classify(states[i:i + SAVE_EVERY], question)
        for key, probs in zip(keys[i:i + SAVE_EVERY], got):
            if probs:
                store(key, probs)
                answered += 1
        save_state(state)
        if problem:
            return answered, problem
    return answered, ""


def _share(probabilities: dict, kinds) -> float:
    return round(sum(float(probabilities.get(k) or 0) for k in kinds), 4)


def _top(probabilities: dict) -> str:
    return max(probabilities, key=lambda k: probabilities[k]) if probabilities else ""


def _summary(rows: list[dict], limit: int) -> list[dict]:
    by = defaultdict(list)
    for r in rows:
        by[address(r["from"])].append(r)
    top = sorted(by.items(), key=lambda kv: -len(kv[1]))[:limit]
    return [{"sender": addr, "count": len(items),
             "subjects": list(dict.fromkeys(r["subject"] for r in items))[:3]}
            for addr, items in top]


async def sweep(dry_run: bool | None = None) -> dict:
    """One pass over the inbox. dry_run None follows the settings."""
    async with _lock:
        return await _sweep(dry_run)


async def _sweep(dry_run: bool | None) -> dict:
    s = settings()
    dry = s["dry_run"] if dry_run is None else dry_run
    started = time.time()
    report = {"ts": started, "dry_run": dry, "scanned": 0, "protected": 0, "kept_by_rule": 0,
              "asked_senders": 0, "asked_messages": 0, "pending": 0, "kept": 0, "held": 0,
              "archive": 0, "archived": 0, "error": ""}
    if not apple.enabled("mail"):
        report["error"] = "Mail is not connected (Settings > Apple apps)"
        return report
    state = load_state()
    keep_rules = s["keep_senders"] + state["keep"]
    try:
        boxes = await scan(s)
    except apple.AppleError as exc:
        report["error"] = str(exc)
        return report
    report["scan_seconds"] = round(time.time() - started, 1)

    # Per sender: how much mail, how much of it answered, and what it is about.
    stats = defaultdict(lambda: {"n": 0, "replied": 0, "from": "", "subjects": []})
    for box in boxes:
        for sender, subject, replied in zip(box.get("sender") or [], box.get("subject") or [],
                                            box.get("replied") or []):
            st = stats[address(sender)]
            st["n"] += 1
            st["replied"] += bool(replied)
            st["from"] = st["from"] or sender or ""
            if subject and subject not in st["subjects"] and len(st["subjects"]) < SAMPLE_SUBJECTS:
                st["subjects"].append(subject[:120])

    messages = []
    for box in boxes:
        if box.get("error"):
            report["error"] = f"{box['account']}: {box['error']}"
            continue
        for mid, sender, subject, flagged, replied in zip(
                box["id"], box["sender"], box["subject"], box["flagged"], box["replied"]):
            report["scanned"] += 1
            if flagged or replied:
                report["protected"] += 1
                continue
            addr = address(sender)
            st = stats[addr]
            if matches(addr, keep_rules) or (st["replied"] and st["replied"] / st["n"] >= CORRESPONDENT_SHARE):
                report["kept_by_rule"] += 1
                continue
            messages.append({"account": box["account"], "inbox": box["inbox"], "archive": box["archive"],
                             "id": mid, "from": sender or "", "subject": subject or "", "addr": addr,
                             "always": matches(addr, state["always"])})

    budget = s["max_judged"]
    t0 = time.time()
    problem = ""

    # 1. Senders never judged, or judged long ago, the biggest first.
    stale = started - s["recheck_days"] * 86400
    counts = Counter(m["addr"] for m in messages if not m["always"]
                     and (state["senders"].get(m["addr"]) or {}).get("ts", 0) < stale)
    ask = [addr for addr, _ in counts.most_common(budget)]
    if ask:
        def sender_verdict(addr, probs):
            state["senders"][addr] = {"p": _share(probs, BULK), "kind": _top(probs), "ts": started}

        report["asked_senders"], problem = await _ask_in_chunks(
            state, ask, [{"from": stats[a]["from"], "messages_in_inbox": stats[a]["n"],
                          "recent_subjects": stats[a]["subjects"]} for a in ask],
            SENDER_QUESTION, sender_verdict)
        budget -= report["asked_senders"]

    # 2. Each message from a bulk sender, once per distinct subject.
    def bulk(m):
        verdict = state["senders"].get(m["addr"])
        return verdict and verdict["p"] >= s["bulk_at"]

    pairs = Counter(_pair(m["from"], m["subject"]) for m in messages
                    if not m["always"] and bulk(m) and _pair(m["from"], m["subject"]) not in state["pairs"])
    if pairs and not problem and budget > 0:
        first = {}
        for m in messages:
            first.setdefault(_pair(m["from"], m["subject"]), m)
        keys = [key for key, _ in pairs.most_common(budget)]

        def message_verdict(key, probs):
            state["pairs"][key] = {"k": _share(probs, OWNER), "kind": _top(probs)}

        report["asked_messages"], problem = await _ask_in_chunks(
            state, keys, [{"from": first[k]["from"], "subject": first[k]["subject"]} for k in keys],
            MESSAGE_QUESTION, message_verdict)
    report["laya_seconds"] = round(time.time() - t0, 1)
    if problem:
        report["error"] = problem

    archive, held, unsure = [], [], []
    for m in messages:
        verdict = state["senders"].get(m["addr"])
        if m["always"]:
            archive.append(m)
            continue
        if not verdict:
            report["pending"] += 1
        elif verdict["p"] < s["bulk_at"]:
            report["kept"] += 1
            if verdict["p"] >= 0.5:
                unsure.append(m)
        elif _pair(m["from"], m["subject"]) not in state["pairs"]:
            report["pending"] += 1
        elif state["pairs"][_pair(m["from"], m["subject"])]["k"] >= s["keep_at"]:
            held.append(m)
        else:
            archive.append(m)
    report["held"] = len(held)
    report["archive"] = len(archive)
    report["senders"] = _summary(archive, 25)
    report["held_examples"] = [{"from": m["addr"], "subject": m["subject"]} for m in held[:15]]
    report["unsure_senders"] = _summary(unsure, 15)

    if not dry and archive:
        t0 = time.time()
        moved_rows = []
        groups = defaultdict(list)
        for m in archive[:s["max_moves"]]:
            groups[(m["account"], m["inbox"], m["archive"])].append(m)
        for (account, inbox, dest), rows in groups.items():
            if not dest:
                report["error"] = f"{account} has no '{s['archive']}' mailbox to archive into"
                continue
            moved, failed = await move(account, inbox, dest, [m["id"] for m in rows])
            if failed:
                report["error"] = failed
            done = set(moved)
            moved_rows += [m for m in rows if m["id"] in done]
        report["archived"] = len(moved_rows)
        report["move_seconds"] = round(time.time() - t0, 1)
        state["archived"] += [{"ts": started, "account": m["account"], "from": m["from"],
                               "subject": m["subject"]} for m in moved_rows]

    report["seconds"] = round(time.time() - started, 1)
    state["last_run"] = started
    state["runs"].append(report)
    save_state(state)
    log.info("mail sweep%s: %d scanned, %d+%d asked, %d to archive, %d archived, %d held, %d pending%s",
             " (dry run)" if dry else "", report["scanned"], report["asked_senders"],
             report["asked_messages"], report["archive"], report["archived"], report["held"],
             report["pending"], f"; {report['error']}" if report["error"] else "")
    return report


def start(dry_run: bool | None = None, on_done=None) -> dict:
    """Run a sweep in the background; a big first sweep takes a while.
    on_done(report), if given, is awaited when it finishes, so whoever asked
    hears the result without polling for it."""
    global _task
    if _lock.locked() or (_task and not _task.done()):
        return {"ok": False, "error": "a sweep is already running; check status for its report"}
    import runtime

    async def run():
        report = await sweep(dry_run)
        if on_done:
            try:
                await on_done(report)
            except Exception:
                log.exception("mail sweep: reporting the result failed")
        return report

    _task = runtime.spawn(run(), "mail_sweep", "mail sweep")
    return {"ok": True, "started": True,
            "dry_run": settings()["dry_run"] if dry_run is None else dry_run}


def describe(report: dict) -> str:
    """A sweep's report in a few lines, for an agent to relay."""
    import datetime
    when = datetime.datetime.fromtimestamp(report["ts"]).strftime("%Y-%m-%d %H:%M")
    verb = "would archive" if report["dry_run"] else "archived"
    moved = report["archive"] if report["dry_run"] else report["archived"]
    out = [f"sweep {when}{' (dry run, nothing moved)' if report['dry_run'] else ''}, "
           f"{report.get('seconds', 0)}s: {report['scanned']} scanned, {verb} {moved}, "
           f"held {report['held']} as important, kept {report['kept']}, "
           f"{report['protected']} flagged/replied, {report['kept_by_rule']} kept by rule, "
           f"{report['pending']} not judged yet (later sweeps continue)"]
    if report.get("error"):
        out.append(f"problem: {report['error']}")
    for title, key in (("archived senders", "senders"), ("unsure senders (kept)", "unsure_senders")):
        rows = report.get(key) or []
        if rows:
            out.append(f"{title}: " + "; ".join(
                f"{r['sender']} x{r['count']} ({r['subjects'][0][:50] if r['subjects'] else ''})"
                for r in rows[:12]))
    if report.get("held_examples"):
        out.append("held as important: " + "; ".join(
            f"{r['from']}: {r['subject'][:50]}" for r in report["held_examples"][:8]))
    return "\n".join(out)


# ---------- owner overrides ----------

async def keep_sender(sender: str, restore: bool = False) -> dict:
    """Never archive this sender (an address or a domain) again; with restore,
    move what the sweep archived from them back to the inbox."""
    rule = sender.strip().lower()
    if not rule:
        return {"ok": False, "error": "sender is empty"}
    async with _lock:
        state = load_state()
        if rule not in state["keep"]:
            state["keep"].append(rule)
        state["always"] = [r for r in state["always"] if r != rule]
        restored = await _restore(state, rule) if restore else 0
        save_state(state)
    return {"ok": True, "keep": state["keep"], "restored": restored}


async def always_archive(sender: str) -> dict:
    """Archive this sender's mail without asking (an address or a domain)."""
    rule = sender.strip().lower()
    if not rule:
        return {"ok": False, "error": "sender is empty"}
    async with _lock:
        state = load_state()
        if rule not in state["always"]:
            state["always"].append(rule)
        state["keep"] = [r for r in state["keep"] if r != rule]
        save_state(state)
    return {"ok": True, "always": state["always"]}


_RESTORE_SCAN = """
const app = Application("Mail");
const acct = app.accounts.byName(%(account)s);
const names = acct.mailboxes.name();
const lower = (s) => String(s || "").toLowerCase();
const archive = names.find((n) => lower(n) === lower(%(archive)s));
const inbox = names.find((n) => lower(n) === lower(%(inbox)s));
const msgs = archive ? acct.mailboxes.byName(archive).messages : null;
JSON.stringify(msgs && inbox ? {archive: archive, inbox: inbox, id: msgs.id(),
                                sender: msgs.sender(), subject: msgs.subject()} : {});
"""


async def _restore(state: dict, rule: str) -> int:
    """Move back what the sweep archived from senders matching rule: the
    ledger's messages, found in the Archive by sender and subject."""
    s = settings()
    ours = [r for r in state["archived"] if matches(address(r["from"]), [rule])]
    by_account = defaultdict(set)
    for r in ours:
        by_account[r["account"]].add((r["from"], r["subject"]))
    restored, complete = 0, True
    for account, wanted in by_account.items():
        box = json.loads(await apple.osa(
            _RESTORE_SCAN % {"account": json.dumps(account), "archive": json.dumps(s["archive"]),
                             "inbox": json.dumps(s["inbox"])}, "JavaScript", timeout=300) or "{}")
        if not box.get("id"):
            complete = False
            continue
        ids = [mid for mid, sender, subject in zip(box["id"], box["sender"], box["subject"])
               if (sender, subject) in wanted]
        moved, failed = await move(account, box["archive"], box["inbox"], ids)
        restored += len(moved)
        if failed:
            complete = False
            log.warning("restoring %s stopped early: %s", rule, failed)
    if ours and complete:
        gone = {id(r) for r in ours}
        state["archived"] = [r for r in state["archived"] if id(r) not in gone]
    return restored


def status() -> dict:
    state = load_state()
    runs = state["runs"]
    return {"settings": settings(), "laya": laya.enabled(), "running": _lock.locked(),
            "last_run": state.get("last_run"), "last": runs[-1] if runs else None,
            "senders_judged": len(state["senders"]), "messages_judged": len(state["pairs"]),
            "archived_total": len(state["archived"]), "keep": state["keep"],
            "always": state["always"]}


async def loop():
    """Sweep every every_minutes while the service is on. Never calls an LLM."""
    await asyncio.sleep(60)
    while True:
        try:
            s = settings()
            last = load_state().get("last_run") or 0
            if s["enabled"] and not _lock.locked() and time.time() - last >= s["every_minutes"] * 60:
                await sweep()
        except Exception:
            log.exception("mail sweep failed")
        await asyncio.sleep(60)
