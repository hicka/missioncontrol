"""The team gets better every day, and can show it.

Two things teach an agent team anything durable: the owner saying it got
something wrong, and a tool failing the same way over and over. Both were
already in the database, and both were being thrown away. A correction lived
only as long as the conversation window, so "you're reading the timestamp
wrong, UTC is five hours behind the Maldives" had to be said again three weeks
later. And `cd ~/Documents/acmeshop` failed fifteen times across three agents
over a month, because nothing connected the fifteenth failure to the first.

This module connects them:

1. Every turn leaves a record: what woke it, how it ended, how many tools it
   called and how many failed, what it cost. A run that finished, was not
   corrected and did not flail is a clean run. The share of clean runs per day
   is the number this module exists to move, and the one it is judged by.
2. Every owner message is read for a correction or a standing rule, cheaply and
   without a model call. A correction marks the turn it answers as not clean.
3. Corrections and repeated failures are distilled into LESSONS: one-line
   operating rules, scoped to one agent or the whole team, shown in the
   agent's system prompt from its next turn.
4. A lesson is a hypothesis, not a fact. It starts on TRIAL and is adopted
   only if the thing it was written to prevent stops happening: the failure
   stops recurring, the owner stops making the same correction. A lesson that
   does not help is retired on its own, and a second attempt that also fails
   goes to the owner instead of round again.
5. A regression guard watches the clean-run rate. If it drops sharply, lessons
   introduced in the last two days are suspended, so a bad lesson costs at
   most a day.

What it cannot do matters as much:

- A lesson is text in a prompt. It cannot grant a tool, lift a limit, approve
  a command or skip a test: the harness enforces those whatever the prompt
  says, and check_lesson() refuses lessons that try to talk an agent out of
  them anyway.
- It never edits code. Changes to Mission Control stay with the improve tool
  and the owner's explicit approval.
- It spends little: a handful of small model calls a day, inside the team
  budget and under its own daily cap.
- It is in selfimprove.PROTECTED, so an agent cannot rewrite the rules it
  learns under.
"""
import asyncio
import datetime
import json
import logging
import re
import time
import uuid

import db
import harness
import settings as cfg

logger = logging.getLogger("missioncontrol.evolve")

TRIAL_DAYS = 5            # a lesson has to survive this long before it is adopted
TRIAL_MAX_DAYS = 21       # an agent that is rarely busy still gets a verdict
MIN_RUNS_TO_ADOPT = 4     # an idle agent proves nothing either way
MAX_LESSONS = 10          # per agent, and again for the team as a whole
LESSON_CHARS = 280
PROMPT_CHARS = 2000       # what the lessons may cost an agent's prompt, every turn
CLUSTER_MIN = 3           # one failure is bad luck, three is a pattern
CLUSTER_DAYS = 14
DAILY_CALLS = 12          # model calls this module may make per local day
SETTLE = 180              # let a correction's conversation finish before learning from it
TICK = 300
REGRESSION_DROP = 0.20
REGRESSION_MIN_RUNS = 12

STATUSES = ("trial", "active", "retired", "suspended", "proposed")
LIVE = ("trial", "active")
HELD = ("suspended", "proposed")   # not in any prompt, waiting on the owner
# Outcomes a turn can end in. "budget" and "cancelled" (a server shutdown) are
# not the agent's doing, so they stay out of the clean-run rate entirely. Nor does
# "carried_on": the stretch that finishes the work is the one that says how it went.
UNCLEAN_OUTCOMES = ("failed", "checkpoint", "stopped")
NEUTRAL_OUTCOMES = ("budget", "cancelled", "running", "carried_on")
SERIOUS_FLAGS = ("unverified_claim", "waiting_without_watch", "repeat_refused", "flailing")


# ---------- storage ----------

_ready_for = None


def _ensure():
    """Tables for this module, created once per database connection (tests
    swap the connection out from under every module)."""
    global _ready_for
    c = db._db()
    if _ready_for is c:
        return c
    c.execute("""CREATE TABLE IF NOT EXISTS turns (
        id TEXT PRIMARY KEY, ts REAL NOT NULL, ended REAL, agent TEXT NOT NULL,
        thread TEXT NOT NULL, trigger TEXT NOT NULL, mission TEXT,
        outcome TEXT NOT NULL DEFAULT 'running', rounds INTEGER NOT NULL DEFAULT 0,
        tools INTEGER NOT NULL DEFAULT 0, tool_errors INTEGER NOT NULL DEFAULT 0,
        tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
        flags TEXT NOT NULL DEFAULT '[]', corrected INTEGER NOT NULL DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS feedback (
        id TEXT PRIMARY KEY, ts REAL NOT NULL, agent TEXT NOT NULL,
        thread TEXT NOT NULL, message INTEGER, kind TEXT NOT NULL,
        score REAL NOT NULL, text TEXT NOT NULL, turn TEXT,
        state TEXT NOT NULL DEFAULT 'new', note TEXT NOT NULL DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS lessons (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, updated REAL NOT NULL,
        agent TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL,
        status TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0, signature TEXT,
        evidence TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '',
        recurrences INTEGER NOT NULL DEFAULT 0, decided REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS evolution_runs (
        id TEXT PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,
        report TEXT NOT NULL DEFAULT '{}')""")
    # What a failed turn died of, so the same crash on every turn reads as
    # one bug to fix rather than a scatter of bad runs.
    if "error" not in {r[1] for r in c.execute("PRAGMA table_info(turns)")}:
        c.execute("ALTER TABLE turns ADD COLUMN error TEXT NOT NULL DEFAULT ''")
    c.execute("CREATE INDEX IF NOT EXISTS turn_ts ON turns(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS turn_agent ON turns(agent, ts)")
    c.execute("CREATE INDEX IF NOT EXISTS feedback_ts ON feedback(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS lesson_agent ON lessons(agent, status)")
    c.execute("CREATE INDEX IF NOT EXISTS evolution_run_ts ON evolution_runs(kind, ts)")
    c.commit()
    _ready_for = c
    return c


def _rows(cursor) -> list[dict]:
    keys = [col[0] for col in cursor.description]
    out = [dict(zip(keys, row)) for row in cursor.fetchall()]
    for row in out:
        for key in ("flags", "evidence", "report"):
            if key in row and isinstance(row[key], str):
                try:
                    row[key] = json.loads(row[key])
                except ValueError:
                    row[key] = {} if key != "flags" else []
    return out


REVIEW_AT = re.compile(r"([01]?\d|2[0-3]):[0-5]\d")


def settings() -> dict:
    s = cfg.load().get("evolution") or {}
    at = str(s.get("review_at") or "03:30")
    if not REVIEW_AT.fullmatch(at):
        at = "03:30"
    return {"enabled": bool(s.get("enabled", True)), "review_at": at}


def enabled() -> bool:
    try:
        return settings()["enabled"]
    except Exception:
        return True


# ---------- turns ----------

def turn_start(agent: str, thread: str, trigger: str, mission: str | None = None) -> str:
    tid = uuid.uuid4().hex
    with db._lock:
        c = _ensure()
        c.execute("INSERT INTO turns (id, ts, agent, thread, trigger, mission) VALUES (?,?,?,?,?,?)",
                  (tid, time.time(), agent, thread, trigger or "", mission))
        c.commit()
    return tid


def turn_finish(tid: str, outcome: str, rounds: int = 0, tools: int = 0, tool_errors: int = 0,
                tokens_in: int = 0, tokens_out: int = 0, flags=(), error: str = ""):
    if not tid:
        return
    with db._lock:
        c = _ensure()
        c.execute("""UPDATE turns SET ended=?, outcome=?, rounds=?, tools=?, tool_errors=?,
                     tokens_in=?, tokens_out=?, flags=?, error=? WHERE id=?""",
                  (time.time(), outcome, rounds, tools, tool_errors, tokens_in, tokens_out,
                   json.dumps(sorted(set(flags))), (error or "")[:400], tid))
        c.commit()


def last_turn(agent: str, thread: str | None = None, within: float = 7200) -> dict | None:
    """The newest finished turn of this agent, optionally in one thread."""
    since = time.time() - within
    with db._lock:
        c = _ensure()
        if thread:
            rows = _rows(c.execute(
                "SELECT * FROM turns WHERE agent=? AND thread=? AND ended IS NOT NULL AND ts>? "
                "ORDER BY ts DESC LIMIT 1", (agent, thread, since)))
        else:
            rows = _rows(c.execute(
                "SELECT * FROM turns WHERE agent=? AND ended IS NOT NULL AND ts>? "
                "ORDER BY ts DESC LIMIT 1", (agent, since)))
    return rows[0] if rows else None


def counted(turn: dict) -> bool:
    """Whether a turn says anything about how the team works. A heartbeat that
    checked its list and had nothing to do is not a run, and counting it would
    make every quiet night look like a perfect one."""
    # The owner correcting it part way says how it went, on a stretch that otherwise would not.
    if turn.get("outcome") in NEUTRAL_OUTCOMES and not (turn["outcome"] == "carried_on" and turn.get("corrected")):
        return False
    # A Router turn nobody asked for is routing, however many tool calls it made.
    # Its dispatches say nothing about how well anyone did the work.
    if turn.get("trigger") == "router":
        return False
    return turn.get("trigger") == "user" or int(turn.get("tools") or 0) > 0


def clean(turn: dict) -> bool:
    if turn.get("outcome") in UNCLEAN_OUTCOMES or turn.get("corrected"):
        return False
    if any(f in SERIOUS_FLAGS for f in (turn.get("flags") or [])):
        return False
    tools, errors = int(turn.get("tools") or 0), int(turn.get("tool_errors") or 0)
    # A grep that finds nothing exits 1, and that is the harness's "failed"
    # too, so one error is not flailing. A quarter of the calls failing is.
    return errors * 4 <= max(tools, 1) or errors <= 1


# ---------- owner feedback ----------

# Calibrated by hand against 470 of the owner's own messages, labelled one by
# one: 55 corrections, 19 standing rules and a long tail of ordinary requests.
# The score is a sum of cues so that one weak signal ("why", "but", "still")
# never carries a message on its own; two of them, or one strong one ("the
# wrong", "I already told you", "correction"), do. On that set it reads
# corrections and rules at 0.93 precision and 0.81 recall, and most of what it
# misses is exasperation with nothing to learn from ("my god"). Precision
# matters more than recall here: a missed correction is a lesson not learned,
# but a false one asks the distiller to invent a lesson, and it is told to
# answer "none" when there is nothing to fix, which it can only do for free.
_CUES = [
    # a verdict on what the agent did
    (r"\bthe wrong\b|\bwrong (place|domain|branch|repo|file|one|thing|number|id|account|date|time|"
     r"version|key|button|profile|tab|page|link|url|path|folder|channel|person|agent|timezone|way)\b",
     2.5, "correction"),
    (r"\b(is|are|was|were|it's|its|that's|thats|you're|you are|youre)\s+(\w+\s+){0,3}(wrong|incorrect)\b",
     2.0, "correction"),
    (r"\b(diagnosis|assumption|reading|math|estimate|answer|summary|report|timestamp|time|date)\s+"
     r"(is|was)\s+(wrong|off|incorrect)\b", 2.5, "correction"),
    (r"\bhallucinat\w*|\bmade (that|it) up\b|\bthat's not (true|right|correct|what i)\b", 2.5, "correction"),
    (r"\bnot what i (asked|said|meant|wanted)\b|\bwhat i mean(t)? is\b", 2.5, "correction"),
    (r"\bcorrection\b", 2.5, "correction"),
    # the same thing, asked for twice
    (r"\b(i (already )?(said|told you|asked you|instructed|explained)|don't make me repeat|"
     r"i have to remind you|i keep (telling|asking)|how many times|i instructed before)\b", 2.5, "correction"),
    (r"\bexplicitly (asking|asked|told|said|instructed)\b", 2.5, "correction"),
    (r"\byou (forgot|missed|ignored|skipped|never|keep|kept|still)\b", 1.5, "correction"),
    (r"\byou (don't|do not|didn't|did not|aren't|are not|haven't|have not|won't|wouldn't)\b", 2.0, "correction"),
    (r"\bwhy (did|didn't|did not|are|were|do|don't|would|have|haven't|is|isn't)\s+you\b", 2.0, "correction"),
    (r"\bwhy (is|was|are|were|has|hasn't|wasn't|isn't)\b", 1.5, "correction"),
    (r"\bwithout (merge|merging|asking|telling|testing|approval|checking|review|reviewing)\b", 1.0, "correction"),
    (r"\bstill (not|isn't|is not|hasn't|has not|open|unmerged|broken|failing|pending|wrong|no)\b",
     1.25, "correction"),
    (r"\b(is|are|was|has|have|it|its|it's)\s*(not|n't)\s+(\w+\s+){0,2}(merged|deployed|submitted|done|"
     r"fixed|working|running|reported|logged out|created|sent|there|live|up)\b", 1.5, "correction"),
    (r"^(it's|its|it is|that's|thats|this is|that is)\s+not\b", 1.0, "correction"),
    (r"\bi (verified|checked|confirmed|can see|see that|just checked)\b", 0.75, "correction"),
    (r"\bstop\s+(\w+ing|on|that|it|this|the)\b|^\s*stop[.!]*\s*$", 2.0, "correction"),
    (r"\bbut you\b", 1.25, "correction"),
    (r"\bbut (the|it|that|this)\b", 0.75, "correction"),
    (r"\b(again|anyway)\s*[.!?]*$", 1.0, "correction"),
    (r"\byou did (that|this|it)\b", 0.75, "correction"),
    (r"\b(should|could) (have|'ve)\b|\bshould've\b|\bsupposed to\b|\bmeant to\b", 1.25, "correction"),
    (r"\byou should\b", 2.0, "correction"),
    (r"\bit should (be|have|say|show|use|go|read|look)\b", 1.25, "correction"),
    (r"\bshould (be|go) (in|on|against|from|into|under)\b", 2.0, "correction"),
    (r"\b(it's|its|it is|use|should be|needs to be|is)\s+[\w.@/:-]+,?\s+(not|no)\s+(on\s+)?[\w.@/:-]+",
     2.0, "correction"),
    (r"\b\w+,?\s+not\s+(this|that|hand|the|a|an|on|in|from|via|with|using)\b", 1.25, "correction"),
    (r"\b(needs|has|ought) to (be|go) (in|on|against|from|into|under|to)\b", 2.0, "correction"),
    (r"\b(is|isn't|is not) the (problem|provlem|prob\w*|issue|point)\b", 2.0, "correction"),
    (r"\b(listen|pay attention)\b.*\b(what i|to me)\b", 2.0, "correction"),
    (r"\btoo (fast|slow|early|late|soon|long|much|many|often|quick|quickly)\b", 1.25, "correction"),
    (r"\byou(?:'re| are| re)\s+\w+ing\b", 0.75, "correction"),
    (r"\b(already|is already) (configured|done|set ?up|created|merged|deployed|submitted|there|running|"
     r"fixed|closed|approved)\b", 1.0, "correction"),
    (r"\bmy god\b|\bseriously\b|\bcome on\b|\bugh\b|\bwtf\b|!!+|\?\?+", 1.0, "correction"),
    (r"\bdon't (touch|do|send|use|include|put|pull|ever|change|run|merge|deploy|spam|post|reply|create|"
     r"open|involve|make)\b", 1.0, "correction"),
    (r"\b(don't|do not) want\b", 1.0, "correction"),
    (r"\b(we|i) (are|am|'re|'m) not\b", 1.0, "correction"),
    (r"\bwe (need|should|must|have) to\b|\bwe should\b", 0.75, "correction"),
    (r"\bno,?\s+(actually|that|it's not|you|this|not|don't|stop|wait)\b", 1.25, "correction"),
    # standing guidance, and the facts about this machine it rests on
    (r"\b(always|never|from now on|going forward|in (the )?future|next time|whenever|every ?time|"
     r"each time|after each|by default|as a rule)\b", 2.0, "rule"),
    (r"\b(standing rule|make (this|it) a rule|remember (to|that)|please remember|don't forget to)\b",
     2.5, "rule"),
    (r"\b(all|any) (future|new) (pr's|prs?|pull requests?|issues?|code|changes|tasks|work|builds?|"
     r"releases?)\b", 1.5, "rule"),
    (r"\b(is|are) (\w+'?s )?(responsibility|job)\b|\bresponsib(le|ility) for\b", 2.0, "rule"),
    (r"\b(should|must) (always|never)\b", 1.0, "rule"),
    (r"\bmake sure (to|that|all|every|you|the)\b", 1.5, "rule"),
    (r"\b(all|every|each) (open |new |future )?(pr's|prs?|pull requests?|issues?)\b", 1.0, "rule"),
    (r"\bwhen(ever)? i (ask|tell|say)\b", 2.0, "rule"),
    (r"\ball agents (can|should|must|need)\b", 2.0, "rule"),
    (r"\b(to be|should be|must be|are to be) (routed|handled|done|sent|assigned|owned)\b", 2.0, "rule"),
    (r"\buse (nvm|node|python|php|herd|pnpm|yarn|bun|uv|pip)\b", 2.0, "rule"),
    (r"\b(i'm|im|i am|we're|we are) (using|running)\b", 2.0, "rule"),
    (r"\b(is|are) managed (by|with|through)\b", 2.0, "rule"),
]
_CUES_RX = [(re.compile(p, re.I), w, k) for p, w, k in _CUES]
# "No, it's fine" answers a question; it does not correct anything.
_DAMPERS = [re.compile(p, re.I) for p in (
    r"^\s*(no|nope|nah)[\s,.!]*(it's|its|that's|thats)?\s*(ok(ay)?|fine|good|all good|alright|"
    r"no worries|no problem|need)\b",
    r"^\s*(no|nope|nah)\s*[?.!]*\s*$",
    r"\bnothing (you did )?(was )?wrong\b|\bnot your fault\b|\bno action needed\b|\bnever ?mind\b",
)]
_PRAISE = re.compile(r"\b(thanks|thank you|great|good job|nice|perfect|well done|good review|"
                     r"looks good|is good|awesome|excellent|love it|good to go)\b", re.I)
_CONTRACTIONS = re.compile(r"\b(dont|didnt|doesnt|isnt|wasnt|arent|werent|havent|hasnt|wont|"
                           r"wouldnt|cant|couldnt|shouldnt)\b", re.I)


def _normal(text: str) -> str:
    t = (text or "").replace("’", "'").replace("‘", "'")
    # Owners type on phones: "didnt" is "didn't" for every cue above.
    t = _CONTRACTIONS.sub(lambda m: m[0][:-1] + "'t", t)
    return " ".join(t.split())


def classify(text: str) -> tuple[str | None, float]:
    """('correction' | 'rule' | 'praise' | None, score) for one owner message."""
    t = _normal(text)
    if not t or t.startswith(("/", "[photo]", "(Sent by")):
        return None, 0.0
    scores = {"correction": 0.0, "rule": 0.0}
    for rx, weight, kind in _CUES_RX:
        if rx.search(t):
            scores[kind] += weight
    if any(rx.search(t) for rx in _DAMPERS):
        scores["correction"] -= 3.0
    # A long message is a brief or a spec, and specs are full of "should".
    if len(t) > 600:
        scores["correction"] -= 1.0
    correction, rule = scores["correction"], scores["rule"]
    if rule >= 2.0 and rule >= correction:
        return "rule", rule
    if correction >= 2.0:
        return "correction", correction
    if correction <= 0 and rule <= 0 and _PRAISE.search(t):
        return "praise", 1.0
    return None, max(correction, rule)


def note_owner_message(agent: str, thread: str, message_id: int | None, text: str,
                       ts: float | None = None, state: str = "new") -> dict | None:
    """Record a correction, rule or praise addressed to one agent. A correction
    also marks that agent's latest turn in the thread as not clean."""
    kind, score = classify(text)
    if not kind:
        return None
    if message_id is not None:
        with db._lock:
            c = _ensure()
            # One message is one piece of feedback, however it gets read: live
            # as it arrives, or by the history backfill a few seconds later.
            if c.execute("SELECT 1 FROM feedback WHERE message=? AND agent=?",
                         (message_id, agent)).fetchone():
                return None
    fid, now = uuid.uuid4().hex, ts or time.time()
    turn = None
    if kind == "correction" and state == "new":
        turn = last_turn(agent, thread) or last_turn(agent)
    with db._lock:
        c = _ensure()
        c.execute("INSERT INTO feedback (id, ts, agent, thread, message, kind, score, text, turn, state) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (fid, now, agent, thread, message_id, kind, score,
                   harness.redact((text or "")[:2000]), turn["id"] if turn else None,
                   state if kind != "praise" else "history"))
        if turn:
            c.execute("UPDATE turns SET corrected=1 WHERE id=?", (turn["id"],))
        c.commit()
    return {"id": fid, "kind": kind, "score": score, "turn": turn["id"] if turn else None}


def feedback(limit: int = 50, state: str | None = None, since: float = 0) -> list[dict]:
    with db._lock:
        c = _ensure()
        if state:
            return _rows(c.execute("SELECT * FROM feedback WHERE state=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                                   (state, since, limit)))
        return _rows(c.execute("SELECT * FROM feedback WHERE ts>=? ORDER BY ts DESC LIMIT ?",
                               (since, limit)))


def _set_feedback(ids: list[str], state: str, note: str = ""):
    if not ids:
        return
    with db._lock:
        c = _ensure()
        c.executemany("UPDATE feedback SET state=?, note=? WHERE id=?",
                      [(state, note[:300], i) for i in ids])
        c.commit()


# ---------- lessons ----------

# A lesson that makes an agent more careful is cheap to get wrong. One that
# makes it more permissive is not, and no trial period makes that safe, so
# those are refused outright instead of measured. Some phrasings are permissive
# whatever surrounds them; others ("without asking") are refused unless the
# same clause negates them, because "never merge without asking" is exactly
# the kind of lesson that should get through.
_ALWAYS_LOOSE = [re.compile(p, re.I) for p in (
    r"\b(no need|don't need|do not need|never need|needn't) to (ask|confirm|check with|wait for|get)\b",
    r"\bauto-?approv\w*|\bself-?approv\w*|\bpre-?approv\w*|\balready (been )?approved\b",
    r"\bapproved once\b|\b(treat|consider|count|take)\b.{0,60}\bas (approved|confirmed|consent\w*|permission|a yes)\b",
    r"\bnever ask\b|\bdon't ask (the owner|for)\b|\bstop asking\b",
    r"\b(again|anyway)\b.{0,40}\b(even if|despite)\b.{0,40}\b(refused|blocked|held|denied)\b",
)]
_LOOSE_UNLESS_NEGATED = [re.compile(p, re.I) for p in (
    r"\b(ignore|bypass|disable|skip|override|circumvent|work ?around|get around|turn off|suppress)\b"
    r".{0,60}\b(approval|guard|blocklist|block list|budget|limit|rule|safety|confirmation|tests?|"
    r"checks?|review|ledger|permission|policy|hold)",
    r"\bwithout (asking|approval|confirm\w*|permission|the owner|a review|tests?|checking with)\b",
    r"\b(merge|deploy|push|release|publish|submit)\w*\b.{0,40}\bdirectly\b",
    r"\bdirectly\b.{0,30}\b(merge|deploy|push|release|publish|submit)",
    r"\bonce\b.{0,40}\b(green|passes|passed|approved|reviewed)\b.{0,40}\b(merge|deploy|push|release|publish|submit)",
    r"\b(merge|deploy|push|release|publish|submit)\w*\b.{0,40}\bonce\b.{0,40}\b(green|passes|passed)\b",
    r"--force\b|--no-verify\b|\bpush\s+-f\b|\brm\s+-\w*r\w*f|\bsudo\b|\bchmod\s+777\b",
)]
_NEGATION = re.compile(r"\b(never|not|don't|do not|must not|mustn't|no|nor|avoid)\b", re.I)
# Lessons about acting on the outside world or on permission are where a
# wrong lesson costs the most, and where an injected instruction would aim. A
# model-written one on these subjects waits for the owner instead of going on
# trial by itself.
_SENSITIVE = re.compile(r"\b(merg\w*|deploy\w*|push\w*|releas\w*|publish\w*|submit\w*|delet\w*|"
                        r"approv\w*|permission\w*|consent|authori[sz]\w*|credential\w*|password\w*|"
                        r"payment\w*|secret\w*|token\w*|force\w*|production|prod)\b", re.I)


def check_lesson(text: str) -> str | None:
    """Why this text cannot be a lesson, or None if it can."""
    t = " ".join((text or "").split())
    if len(t) < 12:
        return "too short to say anything"
    if len(t) > LESSON_CHARS:
        return f"over {LESSON_CHARS} characters; a lesson is one line"
    if harness.redact(t) != t:
        return "contains something that looks like a secret"
    loose = "would loosen a safety rule; lessons may only make agents more careful or more accurate"
    norm = _normal(t)
    if any(rx.search(norm) for rx in _ALWAYS_LOOSE):
        return loose
    for clause in re.split(r"[.;:!?]|,\s+(?:but|so|and then|then)\b", norm):
        for rx in _LOOSE_UNLESS_NEGATED:
            m = rx.search(clause)
            if m and not _NEGATION.search(clause[:m.start()]):
                return loose
    return None


def sensitive(text: str) -> bool:
    return bool(_SENSITIVE.search(_normal(text)))


_STOP = frozenset((
    "the a an and or of to for in on at by with from into this that it is are was were be been "
    "being do does did done how when what which who why not no if then than as but so we you i "
    "they them our your my me us can will just use using it's its should must always never next "
    "time before after any all one "
    # the words corrections are made of, which say nothing about what was wrong
    "wrong right again still reading read didn't didnt don't dont doesn't you're youre isn't "
    "wasn't please make sure told said asked keep stop sending correction thing things way "
    "place need needs want instead actually just also now here there"
).split())


def _terms(text: str) -> set:
    words = (w.strip(".-/_~") for w in re.split(r"[^a-z0-9.~/_'-]+", _normal(text).lower()))
    return {w for w in words if len(w) > 2 and w not in _STOP}


def _overlap(a: str, b: str) -> tuple[float, int]:
    ta, tb = _terms(a), _terms(b)
    shared = len(ta & tb)
    return shared / max(1, min(len(ta), len(tb))), shared


def _similar(a: str, b: str) -> bool:
    ratio, shared = _overlap(a, b)
    return shared >= 2 and ratio >= 0.6


def lessons(agent: str | None = None, statuses=None, limit: int = 300) -> list[dict]:
    where, args = [], []
    if agent is not None:
        where.append("(agent=? OR agent='*')")
        args.append(agent)
    if statuses:
        where.append("status IN (%s)" % ",".join("?" * len(statuses)))
        args.extend(statuses)
    sql = "SELECT * FROM lessons" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY pinned DESC, (status='active') DESC, ts DESC LIMIT ?"
    with db._lock:
        c = _ensure()
        return _rows(c.execute(sql, (*args, limit)))


def get_lesson(lid: int) -> dict | None:
    with db._lock:
        c = _ensure()
        rows = _rows(c.execute("SELECT * FROM lessons WHERE id=?", (lid,)))
    return rows[0] if rows else None


_prompt_cache: dict[str, tuple[float, str]] = {}


def _changed():
    _prompt_cache.clear()


def add_lesson(agent: str, text: str, source: str, evidence: dict | None = None,
               signature: str | None = None, status: str | None = None) -> dict:
    """Refuses (ValueError) anything check_lesson() refuses, near-duplicates of
    a live lesson in the same scope, and a full scope with nothing on trial to
    make room. The owner's own lessons skip the trial: they are instructions,
    not hypotheses."""
    text = " ".join((text or "").split())
    reason = check_lesson(text)
    if reason:
        raise ValueError(reason)
    agent = agent or "*"
    owner = source in ("owner", "owner-rule")
    if status is None:
        if source != "owner" and sensitive(text):
            status = "proposed"
        else:
            status = "active" if owner else "trial"
    if status not in STATUSES:
        raise ValueError("unknown lesson status")
    live = [l for l in lessons(None, LIVE) if l["agent"] == agent]
    # A suspended or proposed lesson is still the team's answer to its problem;
    # writing another beside it would undo the hold.
    for existing in live + [l for l in lessons(None, HELD) if l["agent"] == agent]:
        if _similar(existing["text"], text) or (signature and existing.get("signature") == signature):
            raise ValueError(f"already covered by lesson L{existing['id']}")
    if len(live) >= MAX_LESSONS:
        room = sorted((l for l in live if l["status"] == "trial" and not l["pinned"]
                       and l["source"] not in ("owner", "owner-rule")), key=lambda l: l["ts"])
        if not room:
            raise ValueError(f"this scope already holds {MAX_LESSONS} lessons; retire one first")
        update_lesson(room[0]["id"], status="retired",
                      note="retired untested to make room for a newer lesson")
    now = time.time()
    with db._lock:
        c = _ensure()
        cur = c.execute("INSERT INTO lessons (ts, updated, agent, text, source, status, pinned, signature, "
                        "evidence, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (now, now, agent, text, source, status, int(source == "owner"), signature,
                         json.dumps(evidence or {}, ensure_ascii=False)[:8000],
                         "touches an outward action or a permission, so it waits for your OK"
                         if status == "proposed" else ""))
        c.commit()
        lid = cur.lastrowid
    _changed()
    logger.info("lesson L%s (%s, %s) for %s: %s", lid, source, status, agent, text[:120])
    return get_lesson(lid)


def update_lesson(lid: int, **fields) -> dict | None:
    allowed = {k: v for k, v in fields.items()
               if k in ("status", "pinned", "text", "note", "recurrences", "decided")}
    if "status" in allowed and allowed["status"] not in STATUSES:
        raise ValueError("unknown lesson status")
    if "text" in allowed:
        allowed["text"] = " ".join(str(allowed["text"]).split())
        reason = check_lesson(allowed["text"])
        if reason:
            raise ValueError(reason)
    if "pinned" in allowed:
        allowed["pinned"] = int(bool(allowed["pinned"]))
    if not allowed:
        return get_lesson(lid)
    if allowed.get("status") == "trial":
        before = get_lesson(lid)
        if before and before["status"] != "trial":
            # Put back on trial, it starts a new trial: judged on what happens
            # from now, not retired again for what happened before.
            allowed.update(ts=time.time(), recurrences=0, decided=None)
    allowed["updated"] = time.time()
    with db._lock:
        c = _ensure()
        c.execute("UPDATE lessons SET %s WHERE id=?" % ", ".join(f"{k}=?" for k in allowed),
                  (*allowed.values(), lid))
        c.commit()
    _changed()
    return get_lesson(lid)


def for_prompt(agent: str) -> list[dict]:
    """The lessons this agent reads every turn: pinned and owner-given first,
    then adopted, then on trial; the team's alongside its own; within budget."""
    rows = lessons(agent, ("trial", "active"))
    rank = {"owner": 0, "owner-rule": 0}
    rows.sort(key=lambda l: (not l["pinned"], rank.get(l["source"], 1),
                             l["status"] != "active", -l["ts"]))
    out, used = [], 0
    for lesson in rows:
        cost = len(lesson["text"]) + 16
        if used + cost > PROMPT_CHARS:
            continue
        out.append(lesson)
        used += cost
    return out


def lessons_block(agent: str) -> str:
    """Prompt text, cached until a lesson changes (it is read on every turn)."""
    hit = _prompt_cache.get(agent)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    try:
        rows = for_prompt(agent)
    except Exception:
        logger.exception("could not load lessons for %s", agent)
        return ""
    if not rows:
        text = ""
    else:
        lines = [f"- [L{l['id']}]{' (team)' if l['agent'] == '*' else ''} {l['text']}" for l in rows]
        text = ("OPERATING LESSONS (learned from this team's own mistakes and the owner's corrections. "
                "Follow them; the owner's words in the current conversation still come first, and no "
                "lesson widens what you are allowed to do):\n" + "\n".join(lines))
    _prompt_cache[agent] = (time.time(), text)
    return text


# ---------- failure patterns ----------

_ERRORISH = re.compile(r"error|not found|no such|unknown|invalid|denied|refused|failed|fatal|cannot|"
                       r"can't|unable|timed out|traceback|exception|usage:|not a |missing|forbidden|"
                       r"unauthori[sz]ed|syntax|accepts at most|blocked|held for approval", re.I)
_NOISE = re.compile(r"\b[0-9a-f]{7,40}\b|\b\d+(\.\d+)*\b")
# The harness and the guards saying no are not the agent getting something
# wrong, and they read alike whatever was refused, so they would pool into one
# meaningless pattern. Holds are the owner's to answer, not a lesson's.
_NOT_MISTAKES = re.compile(r"^(error:\s*)?(refused: this exact|turn tool budget exhausted|skill tool budget|"
                           r"skill deadline|(still )?held for approval|blocked by policy|blocked for the bot|"
                           r"cancelled by the owner)", re.I)


def signature(tool: str, result: str, call: str = "") -> str | None:
    """What a failure was, with the parts that vary from run to run (ids,
    numbers, hashes) taken out and the parts that teach something (the path, the
    flag, the command) kept. None for failures that are not a pattern: a grep
    that matched nothing is not a mistake."""
    if _NOT_MISTAKES.match((result or "").strip()):
        return None
    lines = [l.strip() for l in (result or "").splitlines() if l.strip()]
    if tool == "terminal" and lines and re.fullmatch(r"exit \d+", lines[0]):
        lines = lines[1:]
    if not lines:
        return None
    # A command that printed what was asked and exited 1 anyway (a find with
    # one unreadable folder, an ls of two paths where one is missing) has no
    # error line to learn from, and clustering its output only finds noise.
    line = next((l for l in lines[:12] if _ERRORISH.search(l)), None)
    if not line:
        return None
    line = re.sub(r"^error:\s*", "", line, flags=re.I)
    # "timed out" says nothing about what hung. Without the command it names,
    # every timeout anywhere would count against a lesson about one of them.
    if re.search(r"timed out", line, re.I) and call:
        line += " | " + " ".join(call.split()[:3])
    line = " ".join(_NOISE.sub("#", line.lower()).split())[:160]
    return f"{tool}: {line}" if len(line) >= 6 else None


def _arguments(row: dict) -> str:
    raw = (row.get("detail") or {}).get("arguments", "")
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return str(raw)[:300]
    if isinstance(data, dict):
        pick = data.get("command") or data.get("url") or data.get("query") or data
        return (pick if isinstance(pick, str) else json.dumps(pick, ensure_ascii=False))[:300]
    return str(data)[:300]


def _failed_executions(since: float) -> list[dict]:
    import runtime_store
    with db._lock:
        runtime_store._ensure()
        return runtime_store._rows(db._db().execute(
            "SELECT * FROM executions WHERE kind='tool' AND status='failed' AND ts>=? ORDER BY ts",
            (since,)))


def _recovery(failure: dict) -> str | None:
    """What the same agent ran next that worked, if it looks like a retry of
    the same thing: the most useful half of any lesson about a failure."""
    import runtime_store
    with db._lock:
        runtime_store._ensure()
        rows = runtime_store._rows(db._db().execute(
            "SELECT * FROM executions WHERE kind='tool' AND agent=? AND name=? AND status='completed' "
            "AND ts>? AND ts<? ORDER BY ts LIMIT 6",
            (failure["agent"], failure["name"], failure["ts"], failure["ts"] + 900)))
    wanted = _terms(_arguments(failure))
    for row in rows:
        got = _terms(_arguments(row))
        if wanted and len(wanted & got) / max(1, len(wanted | got)) >= 0.2:
            return _arguments(row)
    return None


def failure_clusters(since: float, min_count: int = CLUSTER_MIN, recoveries: bool = True) -> list[dict]:
    groups: dict[str, dict] = {}
    for row in _failed_executions(since):
        sig = signature(row["name"], row.get("result") or "", _arguments(row))
        if not sig:
            continue
        g = groups.setdefault(sig, {"signature": sig, "tool": row["name"], "count": 0,
                                    "agents": {}, "first": row["ts"], "last": row["ts"], "samples": []})
        g["count"] += 1
        g["agents"][row["agent"]] = g["agents"].get(row["agent"], 0) + 1
        g["last"] = row["ts"]
        if len(g["samples"]) < 4:
            g["samples"].append(row)
    out = []
    for g in groups.values():
        if g["count"] < min_count:
            continue
        samples = []
        for row in g["samples"]:
            sample = {"agent": row["agent"], "call": _arguments(row),
                      "error": " ".join((row.get("result") or "").split())[:300]}
            if recoveries:
                fixed = _recovery(row)
                if fixed:
                    sample["then_worked"] = fixed
            samples.append(sample)
        out.append({**g, "samples": samples})
    out.sort(key=lambda g: (-g["count"], -g["last"]))
    states = signature_states()
    for g in out:
        g["state"] = _state_of(states.get(g["signature"]))
        g["lesson"] = g["state"] != "open"
    return out


MAX_ATTEMPTS = 2     # lessons tried on one failure before it is the owner's to decide


def signature_states() -> dict:
    """For each failure signature: whether a lesson is in use or held, whether
    the owner retired one, and the attempts that did not help."""
    out = {}
    for l in lessons(None, None, 2000):
        sig = l.get("signature")
        if not sig:
            continue
        st = out.setdefault(sig, {"live": False, "held": False, "owner": False, "tried": []})
        if l["status"] in LIVE:
            st["live"] = True
        elif l["status"] in HELD:
            st["held"] = True
        elif (l.get("note") or "").startswith("did not help"):
            st["tried"].append(l)
        elif (l.get("note") or "").startswith("retired by the owner"):
            st["owner"] = True
    return out


def _state_of(st: dict | None) -> str:
    """open: nothing addresses it yet; lesson: one is in use; held: one waits
    on the owner; owner: the owner retired one, so it is not relearned behind
    their back; gave-up: two lessons failed, it needs a decision or a fix."""
    if not st:
        return "open"
    if st["live"]:
        return "lesson"
    if st["held"]:
        return "held"
    if st["owner"]:
        return "owner"
    if len(st["tried"]) >= MAX_ATTEMPTS:
        return "gave-up"
    return "open"


def _recurrences(lesson: dict) -> int:
    """How often the thing a lesson was written to prevent has happened since."""
    since = lesson["ts"] + 60
    if lesson.get("signature"):
        return sum(1 for row in _failed_executions(since)
                   if (lesson["agent"] in ("*", row["agent"]))
                   and signature(row["name"], row.get("result") or "", _arguments(row)) == lesson["signature"])
    own = _terms(lesson["text"])
    source = own | _terms(" ".join((lesson.get("evidence") or {}).get("corrections") or []))
    hits = 0
    for row in feedback(200, since=since):
        if row["kind"] not in ("correction", "rule") or row["state"] == "history":
            continue
        if lesson["agent"] not in ("*", row["agent"]):
            continue
        said = _terms(row["text"])
        shared = said & source
        # The same complaint again names the same subject: at least one word of
        # the lesson itself, and a good share of what was said this time.
        if len(shared) >= 2 and said & own and len(shared) / max(1, min(len(said), len(source))) >= 0.35:
            hits += 1
    return hits


# ---------- scorecard ----------

def _day(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def scorecard(days: int = 14, now: float | None = None) -> dict:
    """Per local day: runs, clean runs, the owner's corrections, tool calls and
    failures, and tokens spent. Tool counts come from the execution log, which
    goes back further than turn records, so the trend has a past on day one."""
    import runtime_store
    now = now or time.time()
    start_day = datetime.date.fromtimestamp(now) - datetime.timedelta(days=days - 1)
    since = time.mktime(start_day.timetuple())
    buckets = {}
    for i in range(days):
        d = (start_day + datetime.timedelta(days=i)).isoformat()
        buckets[d] = {"day": d, "runs": 0, "clean": 0, "corrections": 0, "rules": 0, "praise": 0,
                      "tool_calls": 0, "tool_errors": 0, "tokens": 0}
    agents: dict[str, dict] = {}
    week = now - 7 * 86400
    with db._lock:
        c = _ensure()
        turns = _rows(c.execute("SELECT * FROM turns WHERE ts>=? AND ended IS NOT NULL", (since,)))
        notes = _rows(c.execute("SELECT ts, agent, kind FROM feedback WHERE ts>=?", (since,)))
        runtime_store._ensure()
        tools = c.execute("SELECT ts, agent, status FROM executions WHERE kind='tool' AND ts>=?",
                          (since,)).fetchall()
        db._ensure_usage()
        spend = c.execute("SELECT ts, in_tokens + out_tokens FROM usage WHERE ts>=?", (since,)).fetchall()
    for t in turns:
        b = buckets.get(_day(t["ts"]))
        if not b or not counted(t):
            continue
        b["runs"] += 1
        ok = clean(t)
        b["clean"] += ok
        if t["ts"] >= week:
            a = agents.setdefault(t["agent"], {"runs": 0, "clean": 0, "corrections": 0,
                                               "tool_calls": 0, "tool_errors": 0})
            a["runs"] += 1
            a["clean"] += ok
    for n in notes:
        b = buckets.get(_day(n["ts"]))
        key = {"correction": "corrections", "rule": "rules", "praise": "praise"}.get(n["kind"])
        if b and key:
            b[key] += 1
        if n["kind"] == "correction" and n["ts"] >= week:
            agents.setdefault(n["agent"], {"runs": 0, "clean": 0, "corrections": 0,
                                           "tool_calls": 0, "tool_errors": 0})["corrections"] += 1
    for ts, agent, status in tools:
        b = buckets.get(_day(ts))
        if b:
            b["tool_calls"] += 1
            b["tool_errors"] += status == "failed"
        if ts >= week:
            a = agents.setdefault(agent, {"runs": 0, "clean": 0, "corrections": 0,
                                          "tool_calls": 0, "tool_errors": 0})
            a["tool_calls"] += 1
            a["tool_errors"] += status == "failed"
    for ts, tokens in spend:
        b = buckets.get(_day(ts))
        if b:
            b["tokens"] += int(tokens or 0)
    rows = list(buckets.values())
    for b in rows:
        b["clean_rate"] = round(b["clean"] / b["runs"], 3) if b["runs"] else None
        b["error_rate"] = round(b["tool_errors"] / b["tool_calls"], 3) if b["tool_calls"] else None
    for a in agents.values():
        a["clean_rate"] = round(a["clean"] / a["runs"], 3) if a["runs"] else None
        a["error_rate"] = round(a["tool_errors"] / a["tool_calls"], 3) if a["tool_calls"] else None

    def window(part):
        runs = sum(b["runs"] for b in part)
        calls = sum(b["tool_calls"] for b in part)
        return {"runs": runs, "clean": sum(b["clean"] for b in part),
                "clean_rate": round(sum(b["clean"] for b in part) / runs, 3) if runs else None,
                "corrections": sum(b["corrections"] for b in part),
                "tool_calls": calls, "tool_errors": sum(b["tool_errors"] for b in part),
                "error_rate": round(sum(b["tool_errors"] for b in part) / calls, 3) if calls else None,
                "tokens": sum(b["tokens"] for b in part)}

    return {"days": rows, "agents": agents, "week": window(rows[-7:]),
            "previous_week": window(rows[-14:-7]) if days >= 14 else None}


# ---------- learning with a model ----------

# The usage table only counts calls whose provider reported usage. The cap
# cannot depend on that, so calls are also counted here as they are made.
_made = {"day": "", "n": 0}
_cool_until = 0.0
COOLDOWN = 1800           # after a failed call, wait before trying the model again


def _calls_today() -> int:
    midnight = time.mktime(datetime.date.today().timetuple())
    with db._lock:
        _ensure()
        db._ensure_usage()
        recorded = db._db().execute("SELECT COUNT(*) FROM usage WHERE agent='evolution' AND ts>=?",
                                    (midnight,)).fetchone()[0]
    return max(recorded, _made["n"] if _made["day"] == datetime.date.today().isoformat() else 0)


def _can_spend() -> str | None:
    """Why no model call may be made right now, or None."""
    import budget
    import llm
    if not llm.ready():
        return "no model is configured"
    if budget.status().get("exceeded"):
        return "the team's daily budget is spent"
    if _calls_today() >= DAILY_CALLS:
        return f"already made {DAILY_CALLS} learning calls today"
    if time.time() < _cool_until:
        return "the model failed recently; trying again later"
    return None


def _parse(raw: str) -> dict:
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


_WRITE_RULES = (
    "Write each lesson as ONE imperative line under 220 characters that names the concrete thing: "
    "the path, branch, domain, flag, timezone, tool or teammate. Say what to do, and what not to do "
    "when that is the mistake. Lessons may only make an agent more careful or more accurate: never "
    "tell it to skip, bypass or relax an approval, a confirmation, a test, a review or a limit, and "
    "never include secrets. If there is nothing durable to learn (a one-off, a misunderstanding "
    "already resolved, or exasperation with no instruction in it), return no lessons; that answer "
    "costs nothing, while a vague lesson costs every agent context on every turn forever.")


async def _ask(system: str, user: str, max_tokens: int = 900) -> dict:
    global _cool_until
    import llm
    today = datetime.date.today().isoformat()
    if _made["day"] != today:
        _made.update(day=today, n=0)
    _made["n"] += 1
    try:
        raw = await llm.quick(system, user, max_tokens=max_tokens, agent="evolution")
    except Exception:
        # An outage should cost one failed attempt per half hour, not one
        # every tick for as long as it lasts.
        _cool_until = time.time() + COOLDOWN
        raise
    return _parse(raw)


def _context_before(thread: str, message_id: int | None, limit: int = 6) -> list[str]:
    if not message_id:
        return []
    with db._lock:
        rows = db._db().execute(
            "SELECT sender, kind, text FROM messages WHERE thread=? AND id<? AND kind IN ('chat','a2a','sys') "
            "ORDER BY id DESC LIMIT ?", (thread, message_id, limit)).fetchall()
    return [f"{s}: {' '.join((t or '').split())[:300]}" for s, k, t in reversed(rows)]


def _recent_calls(agent: str, before: float, window: float = 1800, limit: int = 10) -> list[str]:
    import runtime_store
    with db._lock:
        runtime_store._ensure()
        rows = runtime_store._rows(db._db().execute(
            "SELECT * FROM executions WHERE kind='tool' AND agent=? AND ts<=? AND ts>? ORDER BY ts DESC LIMIT ?",
            (agent, before, before - window, limit)))
    return [f"{r['name']} [{r['status']}]: {_arguments(r)[:160]}" for r in reversed(rows)]


async def distill_feedback(orch, now: float | None = None) -> list[dict]:
    """Turn the owner's settled corrections and rules into lessons, one model
    call per agent with something new. Returns the lessons it added."""
    now = now or time.time()
    pending = [f for f in feedback(60, "new") if f["kind"] in ("correction", "rule")]
    if not pending:
        return []
    by_agent: dict[str, list] = {}
    for f in sorted(pending, key=lambda f: f["ts"]):
        by_agent.setdefault(f["agent"], []).append(f)
    added = []
    for agent, items in list(by_agent.items())[:3]:
        if agent not in orch.agents:
            _set_feedback([f["id"] for f in items], "skipped", "agent no longer on the team")
            continue
        # Mid-conversation, the next message often finishes the thought.
        if now - items[-1]["ts"] < SETTLE or orch.agent_state.get(agent) in ("thinking", "doing"):
            continue
        blocked = _can_spend()
        if blocked:
            logger.info("lesson distillation deferred: %s", blocked)
            return added
        a = orch.agents[agent]
        live = lessons(agent, ("trial", "active"))
        retired = [l for l in lessons(agent, ("retired",), limit=8) if l["agent"] == agent]
        blocks = []
        for f in items[-4:]:
            before = _context_before(f["thread"], f.get("message"))
            calls = _recent_calls(agent, f["ts"])
            blocks.append(
                f"OWNER ({f['kind']}): {f['text'][:900]}\n"
                + ("CONVERSATION JUST BEFORE IT:\n  " + "\n  ".join(before) + "\n" if before else "")
                + ("WHAT THE AGENT HAD JUST DONE:\n  " + "\n  ".join(calls) + "\n" if calls else ""))
        existing = "\n".join(f"L{l['id']} ({'team' if l['agent'] == '*' else 'agent'}, {l['status']}): {l['text']}"
                             for l in live) or "(none)"
        failed = "\n".join(f"L{l['id']}: {l['text']} -- retired: {l['note']}" for l in retired) or "(none)"
        try:
            data = await _ask(
                "You keep the operating lessons of a team of AI agents that work for one owner. The owner "
                "has just corrected an agent or given it a standing instruction. Decide what durable lesson, "
                "if any, would stop the same correction being needed again.\n\n" + _WRITE_RULES + "\n\n"
                "scope is 'agent' when it is about this agent's own job, 'team' when every teammate needs it "
                "(facts about the owner, their machine, their repos, how they like to be reported to). Use "
                "replaces=<lesson number> when the correction shows an existing lesson was wrong or not "
                "strong enough, and say the better version. Do not restate a lesson that already exists.\n\n"
                'Reply with ONE json object and nothing else: {"lessons": [{"scope": "agent"|"team", '
                '"text": "...", "replaces": null}]}',
                f"AGENT: {a['name']} ({agent}): {a.get('blurb') or ''}\n\n"
                f"LESSONS IT ALREADY HAS:\n{existing}\n\nLESSONS THAT WERE TRIED AND DID NOT WORK:\n{failed}\n\n"
                + "\n\n".join(blocks))
        except Exception as exc:
            logger.warning("lesson distillation for %s failed: %s", agent, exc)
            return added
        made = []
        for item in _items(data)[:3]:
            scope = "*" if item.get("scope") == "team" else agent
            source = "owner-rule" if all(f["kind"] == "rule" for f in items) else "correction"
            evidence = {"corrections": [f["text"][:400] for f in items[-4:]],
                        "feedback": [f["id"] for f in items]}
            replaced = item.get("replaces")
            try:
                replaced = int(str(replaced).lstrip("Ll")) if replaced not in (None, "", "null") else None
            except ValueError:
                replaced = None
            old = get_lesson(replaced) if replaced else None
            if old and old["agent"] in (agent, "*") and old["status"] in ("trial", "active"):
                evidence["attempt"] = int((old.get("evidence") or {}).get("attempt") or 1) + 1
                evidence["replaces"] = old["id"]
            try:
                lesson = add_lesson(scope, item["text"], source, evidence)
            except ValueError as exc:
                logger.info("lesson for %s not kept: %s", agent, exc)
                continue
            if old and evidence.get("replaces"):
                update_lesson(old["id"], status="retired",
                              note=f"rewritten as L{lesson['id']} after another correction")
            made.append(lesson)
        _set_feedback([f["id"] for f in items], "learned" if made else "skipped",
                      ", ".join(f"L{l['id']}" for l in made) or "nothing durable to learn")
        added.extend(made)
    if added:
        await _emit(orch)
    return added


async def learn_from_failures(clusters: list[dict]) -> list[dict]:
    """One model call for the worst failure patterns nothing yet addresses."""
    fresh = [c for c in clusters if c.get("state", "open") == "open"][:5]
    if not fresh:
        return []
    blocked = _can_spend()
    if blocked:
        logger.info("failure learning deferred: %s", blocked)
        return []
    states = signature_states()
    retired = {sig: st["tried"] for sig, st in states.items()}
    parts = []
    for i, c in enumerate(fresh, 1):
        tried = retired.get(c["signature"]) or []
        samples = "\n".join(
            f"  - {s['agent']} ran: {s['call']}\n    got: {s['error']}"
            + (f"\n    then this worked: {s['then_worked']}" if s.get("then_worked") else "")
            for s in c["samples"])
        parts.append(f"P{i}: {c['count']} failures by {', '.join(sorted(c['agents']))}\n{samples}"
                     + ("".join(f"\n  a lesson already tried and failed: {l['text']}" for l in tried)))
    try:
        data = await _ask(
            "You keep the operating lessons of a team of AI agents. Below are tool failures that keep "
            "recurring. For each pattern where the samples show what the agents should do instead (the "
            "right path, the right flag, the command that then worked), write one lesson. When they only "
            "show what does not work, say that plainly. Skip patterns that are noise or that depend on "
            "the outside world being down.\n\n" + _WRITE_RULES + "\n\n"
            'Reply with ONE json object and nothing else: {"lessons": [{"pattern": "P1", "text": "..."}]}',
            "\n\n".join(parts))
    except Exception as exc:
        logger.warning("failure learning failed: %s", exc)
        return []
    made = []
    for item in _items(data):
        match = re.fullmatch(r"P(\d+)", str(item.get("pattern") or "").strip(), re.I)
        if not match or not 1 <= int(match[1]) <= len(fresh):
            continue
        c = fresh[int(match[1]) - 1]
        scope = next(iter(c["agents"])) if len(c["agents"]) == 1 else "*"
        tried = retired.get(c["signature"]) or []
        evidence = {"count": c["count"], "agents": c["agents"],
                    "samples": c["samples"][:3], "attempt": len(tried) + 1}
        try:
            made.append(add_lesson(scope, item["text"], "failure", evidence, c["signature"]))
        except ValueError as exc:
            logger.info("failure lesson not kept: %s", exc)
    return made


def _items(data) -> list[dict]:
    """The lessons a model answered with, or nothing: whatever shape it chose,
    a malformed answer must not stop the review that asked for it."""
    items = data.get("lessons") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [i for i in items[:5] if isinstance(i, dict) and isinstance(i.get("text"), str) and i["text"].strip()]


# ---------- the daily review ----------

def runs(kind: str = "review", limit: int = 20) -> list[dict]:
    with db._lock:
        c = _ensure()
        return _rows(c.execute("SELECT * FROM evolution_runs WHERE kind=? ORDER BY ts DESC LIMIT ?",
                               (kind, limit)))


def _record_run(kind: str, report: dict) -> str:
    rid = uuid.uuid4().hex[:12]
    with db._lock:
        c = _ensure()
        c.execute("INSERT INTO evolution_runs VALUES (?,?,?,?)",
                  (rid, time.time(), kind, json.dumps(report, ensure_ascii=False, default=str)[:60000]))
        c.commit()
    return rid


def review_due(now: float | None = None) -> bool:
    """Due when no review has run since the most recent scheduled time. Asleep
    at 03:30, the review runs on waking; awake, it runs once a night at the
    time set, and the time does not creep earlier each day."""
    now = now or time.time()
    last = runs("review", 1)
    if not last:
        return True
    hh, mm = (int(x) for x in settings()["review_at"].split(":"))
    local = datetime.datetime.fromtimestamp(now)
    slot = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if slot > local:
        slot -= datetime.timedelta(days=1)
    return last[0]["ts"] < slot.timestamp()


def evaluate_trials(now: float | None = None) -> dict:
    """Adopt what worked, retire what did not, and hand the owner what two
    lessons in a row could not fix."""
    now = now or time.time()
    out = {"adopted": [], "retired": [], "attention": []}
    for lesson in lessons(None, ("trial", "active")):
        owners = lesson["source"] in ("owner", "owner-rule")
        hits = _recurrences(lesson)
        if hits != lesson["recurrences"]:
            update_lesson(lesson["id"], recurrences=hits)
        if lesson["status"] == "active":
            # Adopted lessons and the owner's own are never retired on their
            # own; a rule that keeps being broken is a conversation to have.
            if owners and hits and hits > int(lesson["recurrences"] or 0):
                out["attention"].append({"lesson": lesson["id"], "text": lesson["text"],
                                         "why": f"broken {hits} time(s) since it was set"})
            continue
        age = now - lesson["ts"]
        limit = 2 if lesson["signature"] else 1
        if hits >= limit:
            attempt = int((lesson.get("evidence") or {}).get("attempt") or 1)
            update_lesson(lesson["id"], status="retired", decided=now,
                          note=f"did not help: what it was meant to prevent happened {hits} more time(s)")
            out["retired"].append({"lesson": lesson["id"], "text": lesson["text"], "hits": hits})
            if attempt >= 2:
                out["attention"].append({
                    "lesson": lesson["id"], "text": lesson["text"],
                    "why": "two lessons in a row did not stop this; it needs your decision or a code fix"})
            continue
        # Only runs that did something count towards adoption: a quiet
        # heartbeat cannot show a lesson worked.
        neutral = ",".join(f"'{outcome}'" for outcome in NEUTRAL_OUTCOMES)
        real = (f"ended IS NOT NULL AND outcome NOT IN ({neutral}) "
                "AND (trigger='user' OR (tools>0 AND trigger!='router'))")
        with db._lock:
            c = _ensure()
            if lesson["agent"] == "*":
                busy = c.execute(f"SELECT COUNT(*) FROM turns WHERE ts>? AND {real}",
                                 (lesson["ts"],)).fetchone()[0]
            else:
                busy = c.execute(f"SELECT COUNT(*) FROM turns WHERE agent=? AND ts>? AND {real}",
                                 (lesson["agent"], lesson["ts"])).fetchone()[0]
        if (age >= TRIAL_DAYS * 86400 and busy >= MIN_RUNS_TO_ADOPT) or age >= TRIAL_MAX_DAYS * 86400:
            update_lesson(lesson["id"], status="active", decided=now,
                          note=f"adopted: no recurrence over {int(age // 86400)} days and {busy} runs")
            out["adopted"].append({"lesson": lesson["id"], "text": lesson["text"]})
    return out


def regression_guard(now: float | None = None) -> dict | None:
    """Suspend the newest lessons when the last day went clearly worse than
    the week before it. Returns what it did, or None when it held."""
    now = now or time.time()
    with db._lock:
        c = _ensure()
        rows = _rows(c.execute("SELECT * FROM turns WHERE ts>? AND ended IS NOT NULL", (now - 8 * 86400,)))
    recent = [t for t in rows if t["ts"] >= now - 86400 and counted(t)]
    before = [t for t in rows if t["ts"] < now - 86400 and counted(t)]
    if len(recent) < REGRESSION_MIN_RUNS or len(before) < 3 * REGRESSION_MIN_RUNS:
        return None
    rate = sum(map(clean, recent)) / len(recent)
    baseline = sum(map(clean, before)) / len(before)
    if baseline - rate < REGRESSION_DROP:
        return None
    suspended = []
    for lesson in lessons(None, ("trial",)):
        if lesson["ts"] >= now - 2 * 86400 and not lesson["pinned"] \
                and lesson["source"] not in ("owner", "owner-rule"):
            update_lesson(lesson["id"], status="suspended", decided=now,
                          note=f"suspended by the regression guard: clean runs fell from "
                               f"{baseline:.0%} to {rate:.0%}")
            suspended.append({"lesson": lesson["id"], "text": lesson["text"]})
    return {"baseline": round(baseline, 3), "rate": round(rate, 3), "suspended": suspended}


async def review(orch, reason: str = "scheduled", now: float | None = None) -> dict:
    """The nightly pass: judge the lessons on trial, guard against a bad day,
    learn from the failures that keep happening, and write it all down."""
    now = now or time.time()
    report = {"reason": reason, "ts": now}
    card = await asyncio.to_thread(scorecard, 14, now)
    report["week"], report["previous_week"] = card["week"], card["previous_week"]
    report.update(await asyncio.to_thread(evaluate_trials, now))
    guard = await asyncio.to_thread(regression_guard, now)
    report["regression"] = guard
    clusters = await asyncio.to_thread(failure_clusters, now - CLUSTER_DAYS * 86400)
    # Only patterns still happening this week are worth a lesson; the rest
    # were fixed by something else, or by the owner.
    live = [c for c in clusters if c["last"] >= now - 3 * 86400]
    learned = []
    # A night the guard fired is not a night to write new lessons on top of
    # the ones it just held back.
    if enabled() and not (guard and guard["suspended"]):
        try:
            learned = await learn_from_failures(live)
        except Exception as exc:
            logger.exception("failure learning failed during the review")
            report["learning_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    report["learned"] = [{"lesson": l["id"], "text": l["text"], "agent": l["agent"], "status": l["status"]}
                         for l in learned]
    learned_sigs = {l["signature"] for l in learned}
    report["clusters"] = [{"signature": c["signature"], "count": c["count"], "agents": c["agents"],
                           "last": c["last"],
                           "state": "lesson" if c["signature"] in learned_sigs else c["state"]}
                          for c in clusters[:8]]
    rid = await asyncio.to_thread(_record_run, "review", report)
    report["id"] = rid
    logger.info("evolution review %s: %d adopted, %d retired, %d learned, guard %s",
                rid, len(report["adopted"]), len(report["retired"]), len(learned),
                "fired" if guard else "held")
    # The owner hears about it only when there is something to decide or
    # something went wrong. A quiet night says nothing.
    lead = next(iter(orch.agents), None)
    notes = []
    if guard and guard["suspended"]:
        notes.append(f"Clean runs fell from {guard['baseline']:.0%} to {guard['rate']:.0%} in the last day, "
                     f"so I suspended {len(guard['suspended'])} new lesson(s) until you look at them.")
    for item in report["attention"]:
        notes.append(f"Lesson L{item['lesson']} ({item['text'][:120]}): {item['why']}.")
    if lead and notes:
        try:
            await orch._post(f"dm:{lead}", "evolution", "\n".join(notes) +
                             "\nDetails are on the Evolution page.", kind="sys")
        except Exception:
            logger.exception("could not post the evolution notice")
    await _emit(orch)
    return report


def backfill(days: int = 30) -> int:
    """Read the owner's past messages once, so the scorecard has a history of
    corrections from the first day. They are kept as history only: lessons
    from month-old conversations would teach things the owner has since
    changed their mind about."""
    if runs("backfill", 1):
        return 0
    since = time.time() - days * 86400
    with db._lock:
        rows = db._db().execute(
            "SELECT id, ts, thread, text FROM messages WHERE sender='user' AND kind='chat' AND ts>? "
            "AND (thread LIKE 'dm:%' OR thread LIKE 'group:%') ORDER BY id", (since,)).fetchall()
    with db._lock:
        seen = {r[0] for r in _ensure().execute("SELECT message FROM feedback WHERE message IS NOT NULL")}
    n = 0
    for mid, ts, thread, text in rows:
        if mid in seen:
            continue
        agent = thread[3:] if thread.startswith("dm:") else "*"
        if note_owner_message(agent, thread, mid, text, ts=ts, state="history"):
            n += 1
    _record_run("backfill", {"messages": len(rows), "feedback": n})
    return n


async def _emit(orch):
    try:
        await orch._emit({"type": "evolution_updated"})
    except Exception:
        pass


def snapshot() -> dict:
    """Everything the Evolution page shows, in one read."""
    card = scorecard(30)
    last = runs("review", 1)
    rows = lessons(None, None, 400)
    counts = {s: sum(1 for l in rows if l["status"] == s) for s in STATUSES}
    return {"enabled": enabled(), "settings": settings(), "scorecard": card,
            "lessons": rows, "counts": counts,
            "last_review": last[0] if last else None,
            "calls_today": _calls_today(), "daily_calls": DAILY_CALLS}


async def loop(orch):
    await asyncio.sleep(45)
    try:
        n = await asyncio.to_thread(backfill)
        if n:
            logger.info("evolution: read %d past corrections and rules into history", n)
    except Exception:
        logger.exception("evolution backfill failed")
    logger.info("evolution loop started (tick %ss)", TICK)
    while True:
        try:
            if enabled():
                await distill_feedback(orch)
                if await asyncio.to_thread(review_due):
                    await review(orch)
        except Exception:
            logger.exception("evolution tick failed")
        await asyncio.sleep(TICK)
