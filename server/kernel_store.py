"""What the Super-agent kernel must not lose when the process dies.

The loops are disposable; everything that makes them one continuous agent is
here. A Trigger is one immutable event, a Thread is the stream it belongs to, a
Subscription says which Worker follows a Thread, and a Session is one context:
the Router, a Worker, or the Watchdog. A Session's transcript and inbox are rows,
so a restart loads them and carries on rather than starting from a summary.
"""
import json
import time
import uuid

import db

_ready_for = None


def _ensure():
    """Create tables once per connection: tests swap the connection under us."""
    global _ready_for
    c = db._db()
    if _ready_for is c:
        return c
    c.execute("""CREATE TABLE IF NOT EXISTS threads (
        id TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '{}',
        created REAL NOT NULL, updated REAL NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS triggers (
        id TEXT PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL, thread TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT '', from_owner INTEGER NOT NULL DEFAULT 0,
        admission TEXT NOT NULL, text TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}',
        disposition TEXT NOT NULL DEFAULT 'new', detail TEXT NOT NULL DEFAULT '',
        dedupe TEXT UNIQUE)""")
    c.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
        thread TEXT NOT NULL, session TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'own',
        ts REAL NOT NULL, PRIMARY KEY (thread, session))""")
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, profile TEXT NOT NULL,
        mission TEXT, conversation TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '', brief TEXT NOT NULL DEFAULT '',
        strength TEXT NOT NULL DEFAULT 'explicit', speculative INTEGER NOT NULL DEFAULT 0,
        direct INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '',
        digest TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL, updated REAL NOT NULL, progress REAL NOT NULL,
        recoveries INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
        meta TEXT NOT NULL DEFAULT '{}')""")
    c.execute("""CREATE TABLE IF NOT EXISTS session_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, ts REAL NOT NULL,
        role TEXT NOT NULL, content TEXT NOT NULL, live INTEGER NOT NULL DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS session_inbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, ts REAL NOT NULL,
        kind TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '', text TEXT NOT NULL,
        meta TEXT NOT NULL DEFAULT '{}', wakes INTEGER NOT NULL DEFAULT 1,
        priority INTEGER NOT NULL DEFAULT 0, taken REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, session TEXT NOT NULL, tool TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, started REAL NOT NULL,
        finished REAL, result TEXT NOT NULL DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS reports (
        id TEXT PRIMARY KEY, ts REAL NOT NULL, session TEXT NOT NULL, mission TEXT,
        status TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
        question TEXT NOT NULL DEFAULT '', files TEXT NOT NULL DEFAULT '[]')""")
    c.execute("""CREATE TABLE IF NOT EXISTS policies (
        id TEXT PRIMARY KEY, ts REAL NOT NULL, text TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, author TEXT NOT NULL DEFAULT 'owner')""")
    c.execute("""CREATE TABLE IF NOT EXISTS outbox (
        id TEXT PRIMARY KEY, ts REAL NOT NULL, channel TEXT NOT NULL, chat TEXT NOT NULL,
        text TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL,
        error TEXT NOT NULL DEFAULT '', sent REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS findings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, session TEXT NOT NULL,
        verdict TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT '')""")
    # Which session has a repository's one shared checkout. Everyone else who needs
    # that repository at the same time works in a copy of their own.
    c.execute("""CREATE TABLE IF NOT EXISTS checkouts (
        repo TEXT PRIMARY KEY, session TEXT NOT NULL, ts REAL NOT NULL)""")
    # Additive, like everything here: a database from before these columns keeps working.
    if "question" not in {row[1] for row in c.execute("PRAGMA table_info(sessions)")}:
        c.execute("ALTER TABLE sessions ADD COLUMN question TEXT NOT NULL DEFAULT ''")
    if "stopped" not in {row[1] for row in c.execute("PRAGMA table_info(threads)")}:
        c.execute("ALTER TABLE threads ADD COLUMN stopped REAL")
    if "gave_up" not in {row[1] for row in c.execute("PRAGMA table_info(outbox)")}:
        c.execute("ALTER TABLE outbox ADD COLUMN gave_up REAL")
    if "strength" not in {row[1] for row in c.execute("PRAGMA table_info(reports)")}:
        c.execute("ALTER TABLE reports ADD COLUMN strength TEXT NOT NULL DEFAULT ''")
    c.execute("CREATE INDEX IF NOT EXISTS trigger_thread ON triggers(thread, ts)")
    c.execute("CREATE INDEX IF NOT EXISTS trigger_ts ON triggers(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS sub_session ON subscriptions(session)")
    c.execute("CREATE INDEX IF NOT EXISTS session_status ON sessions(status)")
    c.execute("CREATE INDEX IF NOT EXISTS log_session ON session_log(session, live, id)")
    c.execute("CREATE INDEX IF NOT EXISTS inbox_session ON session_inbox(session, taken, id)")
    c.execute("CREATE INDEX IF NOT EXISTS job_session ON jobs(session, status)")
    c.execute("CREATE INDEX IF NOT EXISTS report_session ON reports(session, ts)")
    c.commit()
    _ready_for = c
    return c


def _new(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _rows(cursor) -> list[dict]:
    keys = [col[0] for col in cursor.description]
    out = [dict(zip(keys, row)) for row in cursor.fetchall()]
    for row in out:
        for key in ("meta", "address", "files"):
            if key in row and isinstance(row[key], str):
                row[key] = json.loads(row[key] or ("[]" if key == "files" else "{}"))
    return out


def _one(cursor) -> dict | None:
    rows = _rows(cursor)
    return rows[0] if rows else None


def _dumps(value) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


# ---------- threads ----------

def thread_upsert(tid: str, source: str, kind: str = "topic", title: str = "",
                  address: dict | None = None) -> dict:
    """A conversation is a chat the owner holds with the agent as a whole, so
    nothing subscribes to it and every message is read fresh. A topic is one
    piece of the outside world (a PR, an email chain, a scheduled job): whoever
    is working on it follows it directly."""
    now = time.time()
    with db._lock:
        c = _ensure()
        c.execute("""INSERT INTO threads (id, source, kind, title, address, created, updated)
                     VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,
                     title=CASE WHEN excluded.title='' THEN threads.title ELSE excluded.title END,
                     address=CASE WHEN excluded.address='{}' THEN threads.address ELSE excluded.address END""",
                  (tid, source, kind, title[:200], _dumps(address), now, now))
        c.commit()
        return _one(c.execute("SELECT * FROM threads WHERE id=?", (tid,)))


def thread_get(tid: str) -> dict | None:
    with db._lock:
        return _one(_ensure().execute("SELECT * FROM threads WHERE id=?", (tid,)))


def threads(limit: int = 60) -> list[dict]:
    with db._lock:
        return _rows(_ensure().execute("SELECT * FROM threads ORDER BY updated DESC LIMIT ?", (limit,)))


def conversations(limit: int = 40) -> list[dict]:
    """The chats the owner holds with the agent, the one they used last first."""
    with db._lock:
        return _rows(_ensure().execute(
            "SELECT * FROM threads WHERE kind='conversation' ORDER BY updated DESC LIMIT ?", (limit,)))


def thread_stop(tid: str, stopped: bool = True):
    """The owner stopped the work on this thread, so the next event on it is not a
    reason to start the same work again."""
    with db._lock:
        c = _ensure()
        c.execute("UPDATE threads SET stopped=? WHERE id=?", (time.time() if stopped else None, tid))
        c.commit()


def thread_stopped(tid: str) -> float | None:
    with db._lock:
        row = _ensure().execute("SELECT stopped FROM threads WHERE id=?", (tid,)).fetchone()
    return row[0] if row else None


# ---------- triggers ----------

def trigger_add(source: str, thread: str, text: str, actor: str = "", from_owner: bool = False,
                admission: str = "candidate", meta: dict | None = None,
                dedupe: str | None = None) -> tuple[dict, bool]:
    """Returns the trigger and whether it is new. An event delivered twice (a
    webhook retry, a poll that overlaps one) is the same event, and acting on it
    twice is how one request becomes two pieces of work."""
    with db._lock:
        c = _ensure()
        if dedupe:
            seen = _one(c.execute("SELECT * FROM triggers WHERE dedupe=?", (dedupe,)))
            if seen:
                return seen, False
        tid = _new("tr")
        c.execute("INSERT INTO triggers (id, ts, source, thread, actor, from_owner, admission, text, meta, dedupe) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (tid, time.time(), source, thread, actor, int(from_owner), admission, text,
                   _dumps(meta), dedupe))
        c.commit()
        return _one(c.execute("SELECT * FROM triggers WHERE id=?", (tid,))), True


def trigger_get(tid: str) -> dict | None:
    with db._lock:
        return _one(_ensure().execute("SELECT * FROM triggers WHERE id=?", (tid,)))


def trigger_dispose(tid: str, disposition: str, detail: str = ""):
    with db._lock:
        c = _ensure()
        c.execute("UPDATE triggers SET disposition=?, detail=? WHERE id=?", (disposition, detail[:300], tid))
        c.commit()


def _trigger_route(c, tid: str, sid: str):
    """One event can reach more than one Worker (the author and a reviewer), so what
    it went to accumulates rather than the last one wiping out the first."""
    c.execute("UPDATE triggers SET detail=CASE WHEN disposition IN ('routed','passed') AND detail!='' "
              "THEN substr(detail||', '||?, 1, 300) ELSE ? END, disposition='routed' WHERE id=?", (sid, sid, tid))


def trigger_route(tid: str, sid: str):
    with db._lock:
        c = _ensure()
        _trigger_route(c, tid, sid)
        c.commit()


def triggers(limit: int = 80, thread: str | None = None, disposition: str | None = None) -> list[dict]:
    where, args = [], []
    if thread:
        where.append("thread=?"), args.append(thread)
    if disposition:
        where.append("disposition=?"), args.append(disposition)
    sql = "SELECT * FROM triggers" + (" WHERE " + " AND ".join(where) if where else "")
    with db._lock:
        return _rows(_ensure().execute(sql + " ORDER BY ts DESC, rowid DESC LIMIT ?", (*args, limit)))


def owner_words_since(ts: float, limit: int = 60) -> list[dict]:
    """What the owner has said in their own conversations since a moment."""
    with db._lock:
        return _rows(_ensure().execute(
            "SELECT t.* FROM triggers t JOIN threads h ON h.id=t.thread "
            "WHERE t.from_owner=1 AND h.kind='conversation' AND t.ts>? ORDER BY t.ts LIMIT ?", (ts, limit)))


def owner_words_to(session: str, limit: int = 4) -> list[str]:
    """What the owner said that reached this session, newest first."""
    with db._lock:
        rows = _ensure().execute(
            "SELECT text FROM triggers WHERE from_owner=1 AND instr(detail, ?)>0 ORDER BY ts DESC LIMIT ?",
            (session, limit)).fetchall()
    return [r[0] for r in rows]


def trigger_search(query: str, limit: int = 12) -> list[dict]:
    terms = [t for t in (query or "").split() if t][:6]
    if not terms:
        return []
    where = " AND ".join("text LIKE ?" for _ in terms)
    with db._lock:
        return _rows(_ensure().execute(
            f"SELECT * FROM triggers WHERE {where} ORDER BY ts DESC LIMIT ?",
            (*[f"%{t}%" for t in terms], limit)))


def trigger_counts(since: float) -> dict:
    with db._lock:
        return dict(_ensure().execute(
            "SELECT disposition, COUNT(*) FROM triggers WHERE ts>=? GROUP BY disposition", (since,)).fetchall())


# ---------- subscriptions ----------

def subscribe(thread: str, session: str, mode: str = "own"):
    with db._lock:
        c = _ensure()
        c.execute("INSERT OR REPLACE INTO subscriptions VALUES (?,?,?,?)", (thread, session, mode, time.time()))
        c.commit()


def unsubscribe(thread: str, session: str) -> bool:
    with db._lock:
        c = _ensure()
        cur = c.execute("DELETE FROM subscriptions WHERE thread=? AND session=?", (thread, session))
        c.commit()
        return bool(cur.rowcount)


def subscribers(thread: str) -> list[dict]:
    """Sessions following a thread that can still receive anything."""
    with db._lock:
        return _rows(_ensure().execute(
            "SELECT s.*, x.mode AS sub_mode FROM subscriptions x JOIN sessions s ON s.id=x.session "
            "WHERE x.thread=? AND s.status != 'ended' ORDER BY x.ts", (thread,)))


def subscriptions_of(session: str) -> list[dict]:
    with db._lock:
        return _rows(_ensure().execute("SELECT * FROM subscriptions WHERE session=? ORDER BY ts", (session,)))


def threads_that_wake(session: str) -> list[str]:
    """What it follows that can still bring it news: a GitHub thread, or one it chose to
    follow. A thread it only came from, like another worker's report, has no more to say."""
    return [s["thread"] for s in subscriptions_of(session) if s["thread"].startswith("github:") or s["mode"] == "context"]


# ---------- sessions ----------

def session_create(kind: str, profile: str, *, sid: str | None = None, mission: str | None = None,
                   conversation: str = "", title: str = "", brief: str = "", strength: str = "explicit",
                   speculative: bool = False, direct: bool = False, status: str = "queued",
                   meta: dict | None = None) -> dict:
    sid, now = sid or _new("w"), time.time()
    with db._lock:
        c = _ensure()
        c.execute("INSERT OR IGNORE INTO sessions (id, kind, profile, mission, conversation, status, title, brief, "
                  "strength, speculative, direct, created, updated, progress, meta) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (sid, kind, profile, mission, conversation, status, title[:160], brief, strength,
                   int(speculative), int(direct), now, now, now, _dumps(meta)))
        c.commit()
        return _one(c.execute("SELECT * FROM sessions WHERE id=?", (sid,)))


def session_start(profile: str, opening: str, *, mission: str | None = None, conversation: str = "",
                  title: str = "", brief: str = "", strength: str = "explicit", speculative: bool = False,
                  direct: bool = False, meta: dict | None = None, opening_meta: dict | None = None,
                  follow: list[str] | tuple = (), routed: list[str] | tuple = ()) -> dict:
    """A Worker comes into being whole, in one commit: its row, what it was asked,
    the threads it follows and the events it now owns. A process dying half way
    through used to leave a Worker with authority and no task, and an event that still
    looked unhandled, so the same request then started a second Worker. An event on a
    followed thread could also wake it before its task was there to read."""
    sid, now = _new("w"), time.time()
    with db._lock:
        c = _ensure()
        c.execute("INSERT INTO sessions (id, kind, profile, mission, conversation, status, title, brief, "
                  "strength, speculative, direct, created, updated, progress, meta) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (sid, "worker", profile, mission, conversation, "queued", title[:160], brief, strength,
                   int(speculative), int(direct), now, now, now, _dumps(meta)))
        c.execute("INSERT INTO session_inbox (session, ts, kind, ref, text, meta, wakes, priority) "
                  "VALUES (?,?,?,?,?,?,?,?)", (sid, now, "brief", "", opening, _dumps(opening_meta), 1, 0))
        for thread in follow:
            c.execute("INSERT INTO threads (id, source, kind, title, address, created, updated) VALUES (?,?,?,?,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET updated=excluded.updated",
                      (thread, thread.split(":", 1)[0], "topic", "", "{}", now, now))
            c.execute("INSERT OR REPLACE INTO subscriptions VALUES (?,?,?,?)", (thread, sid, "own", now))
        for tid in routed:
            _trigger_route(c, tid, sid)
        c.commit()
        return _one(c.execute("SELECT * FROM sessions WHERE id=?", (sid,)))


def session_get(sid: str) -> dict | None:
    with db._lock:
        return _one(_ensure().execute("SELECT * FROM sessions WHERE id=?", (sid,)))


def session_set(sid: str, **fields) -> dict | None:
    fields = {**fields, "updated": time.time()}
    if "meta" in fields:
        fields["meta"] = _dumps(fields["meta"])
    columns = ", ".join(f"{key}=?" for key in fields)
    with db._lock:
        c = _ensure()
        c.execute(f"UPDATE sessions SET {columns} WHERE id=?", (*fields.values(), sid))
        c.commit()
        return _one(c.execute("SELECT * FROM sessions WHERE id=?", (sid,)))


def session_advance(sid: str, **fields) -> dict | None:
    """session_set for anything that is not the owner reopening a task. A row read a
    moment ago can be out of date, and what was stopped has to stay stopped."""
    fields = {**fields, "updated": time.time()}
    if "meta" in fields:
        fields["meta"] = _dumps(fields["meta"])
    columns = ", ".join(f"{key}=?" for key in fields)
    with db._lock:
        c = _ensure()
        c.execute(f"UPDATE sessions SET {columns} WHERE id=? AND status!='ended'", (*fields.values(), sid))
        c.commit()
        return _one(c.execute("SELECT * FROM sessions WHERE id=?", (sid,)))


def session_stand_down(sid: str) -> dict | None:
    """Authority lasts as long as the task, so a finished one gives it up, unless the owner
    has just asked for something more that it has yet to read. Decided and written together,
    on what is true now and not on a row read a moment ago."""
    with db._lock:
        c = _ensure()
        row = _one(c.execute("SELECT * FROM sessions WHERE id=?", (sid,)))
        if not row or row["status"] == "ended" or row["strength"] not in ("explicit", "standing"):
            return row
        if c.execute("SELECT 1 FROM session_inbox WHERE session=? AND taken IS NULL AND wakes=1 AND priority>0 LIMIT 1",
                     (sid,)).fetchone():
            return row
        meta = row["meta"] or {}
        if row["strength"] == "explicit":
            meta = {**meta, "was_asked": True}
        c.execute("UPDATE sessions SET strength='inferred', meta=?, updated=? WHERE id=?",
                  (_dumps(meta), time.time(), sid))
        c.commit()
        return _one(c.execute("SELECT * FROM sessions WHERE id=?", (sid,)))


def sessions(statuses: tuple | None = None, kind: str | None = None, limit: int = 100,
             profile: str | None = None) -> list[dict]:
    where, args = [], []
    if statuses:
        where.append("status IN (" + ",".join("?" * len(statuses)) + ")"), args.extend(statuses)
    if kind:
        where.append("kind=?"), args.append(kind)
    if profile:
        where.append("profile=?"), args.append(profile)
    sql = "SELECT * FROM sessions" + (" WHERE " + " AND ".join(where) if where else "")
    with db._lock:
        return _rows(_ensure().execute(sql + " ORDER BY updated DESC LIMIT ?", (*args, limit)))


def direct_line(profile: str) -> dict | None:
    """A profile's one direct line, however busy it is and whatever state it is in."""
    with db._lock:
        return _one(_ensure().execute(
            "SELECT * FROM sessions WHERE kind='worker' AND profile=? AND direct=1 AND status!='ended' "
            "ORDER BY created DESC LIMIT 1", (profile,)))


def session_for_mission(mission: str) -> dict | None:
    with db._lock:
        return _one(_ensure().execute(
            "SELECT * FROM sessions WHERE mission=? ORDER BY created DESC LIMIT 1", (mission,)))


# ---------- transcript ----------

def log_append(session: str, role: str, content) -> int:
    with db._lock:
        c = _ensure()
        cur = c.execute("INSERT INTO session_log (session, ts, role, content) VALUES (?,?,?,?)",
                        (session, time.time(), role, json.dumps(content, ensure_ascii=False, default=str)))
        c.commit()
        return cur.lastrowid


def log_live(session: str) -> list[dict]:
    with db._lock:
        rows = _ensure().execute("SELECT id, role, content FROM session_log WHERE session=? AND live=1 ORDER BY id",
                                 (session,)).fetchall()
    return [{"id": r[0], "role": r[1], "content": json.loads(r[2])} for r in rows]


def log_tail_role(session: str) -> str | None:
    """Who spoke last. A session whose last word is not its own has something unanswered."""
    with db._lock:
        row = _ensure().execute("SELECT role FROM session_log WHERE session=? AND live=1 ORDER BY id DESC LIMIT 1",
                                (session,)).fetchone()
    return row[0] if row else None


_FINAL_REPORTS = ("ready", "blocked", "failed", "dropped", "handback")


def mid_turn(session: str) -> bool:
    """Whether the model still owes something: the last word is not its own, or it
    called a tool and the process died before the tool answered."""
    with db._lock:
        rows = _ensure().execute("SELECT role, content FROM session_log WHERE session=? AND live=1 "
                                 "ORDER BY id DESC LIMIT 2", (session,)).fetchall()
    if not rows:
        return False
    role, content = rows[0][0], json.loads(rows[0][1])
    if role != "user":
        return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
    # A transcript from before turns were closed off can end on the result of a final
    # report. That turn is over, and resuming it would redo finished work.
    only_results = isinstance(content, list) and content and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    if only_results and len(rows) > 1:
        before = json.loads(rows[1][1])
        return not (isinstance(before, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "report"
            and (b.get("input") or {}).get("status") in _FINAL_REPORTS for b in before))
    return True


def log_all(session: str, limit: int = 400) -> list[dict]:
    with db._lock:
        rows = _ensure().execute("SELECT id, ts, role, content, live FROM session_log WHERE session=? "
                                 "ORDER BY id DESC LIMIT ?", (session, limit)).fetchall()
    return [{"id": r[0], "ts": r[1], "role": r[2], "content": json.loads(r[3]), "live": bool(r[4])}
            for r in reversed(rows)]


def log_compact(session: str, upto_id: int, digest: str):
    """Spill everything up to a row out of the window. The rows stay for recall;
    the digest that stands in for them lives on the session and goes in front of
    whatever is still live."""
    with db._lock:
        c = _ensure()
        c.execute("UPDATE session_log SET live=0 WHERE session=? AND id<=? AND live=1", (session, upto_id))
        c.execute("UPDATE sessions SET digest=?, updated=? WHERE id=?", (digest, time.time(), session))
        c.commit()


def log_set_aside(session: str) -> int:
    """Take what has not been answered out of the window, for when a provider refuses
    to read it. The rows stay for recall, and for finding out what was wrong with them."""
    with db._lock:
        c = _ensure()
        row = c.execute("SELECT MAX(id) FROM session_log WHERE session=? AND live=1 AND role='assistant'",
                        (session,)).fetchone()
        cur = c.execute("UPDATE session_log SET live=0 WHERE session=? AND live=1 AND id>?", (session, row[0] or 0))
        c.commit()
        return cur.rowcount


def log_strip_thinking(session: str) -> int:
    """Thinking blocks are bound to the provider that wrote them. Rewrites the live
    transcript without any, and says how many rows that changed."""
    with db._lock:
        c = _ensure()
        rows = c.execute("SELECT id, content FROM session_log WHERE session=? AND live=1 AND role='assistant'",
                         (session,)).fetchall()
        changed = 0
        for rid, raw in rows:
            content = json.loads(raw)
            if not isinstance(content, list):
                continue
            kept = [b for b in content if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))]
            if len(kept) != len(content):
                c.execute("UPDATE session_log SET content=? WHERE id=?",
                          (json.dumps(kept or [{"type": "text", "text": "(no text)"}], ensure_ascii=False), rid))
                changed += 1
        c.commit()
        return changed


def log_last_activity(session: str) -> float:
    with db._lock:
        row = _ensure().execute("SELECT MAX(ts) FROM session_log WHERE session=?", (session,)).fetchone()
    return (row[0] or 0.0) if row else 0.0


def log_prune(before: float) -> int:
    """Old transcripts of finished sessions, which the summaries and reports outlive."""
    with db._lock:
        c = _ensure()
        cur = c.execute("DELETE FROM session_log WHERE ts<? AND session IN "
                        "(SELECT id FROM sessions WHERE status='ended')", (before,))
        c.commit()
        return cur.rowcount


# ---------- inbox ----------

def inbox_put(session: str, kind: str, text: str, ref: str = "", meta: dict | None = None,
              wakes: bool = True, priority: int = 0, grants: bool = False) -> int:
    """grants: the owner is speaking to this Worker, which gives it their authority. The
    two land in one commit. With a moment between them a Worker finishing up took that
    authority away again and then read their words with none, so "merge and deploy" was held."""
    with db._lock:
        c = _ensure()
        cur = c.execute("INSERT INTO session_inbox (session, ts, kind, ref, text, meta, wakes, priority) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (session, time.time(), kind, ref, text, _dumps(meta), int(wakes), int(priority)))
        if grants:
            c.execute("UPDATE sessions SET strength='explicit', speculative=0, updated=? WHERE id=? AND kind='worker'",
                      (time.time(), session))
        c.commit()
        return cur.lastrowid


def inbox_pending(session: str, limit: int = 200) -> list[dict]:
    with db._lock:
        return _rows(_ensure().execute(
            "SELECT * FROM session_inbox WHERE session=? AND taken IS NULL ORDER BY id LIMIT ?", (session, limit)))


def inbox_batch(session: str, limit: int) -> list[dict]:
    """What the next read takes when it cannot take everything: a person first, then
    what asks for a decision, and notes only with what room is left. Back in arrival
    order, so a conversation still reads the way it happened."""
    with db._lock:
        rows = _rows(_ensure().execute(
            "SELECT * FROM session_inbox WHERE session=? AND taken IS NULL "
            "ORDER BY priority DESC, wakes DESC, id LIMIT ?", (session, limit)))
    return sorted(rows, key=lambda r: r["id"])


def inbox_hold(session: str, but_the_owners: bool = False):
    """What is unread stays to be read, but stops asking to be."""
    with db._lock:
        c = _ensure()
        c.execute("UPDATE session_inbox SET wakes=0 WHERE session=? AND taken IS NULL"
                  + (" AND priority=0" if but_the_owners else ""), (session,))
        c.commit()


def inbox_withdraw(session: str, ids: list[int]):
    """Take items out without reading them, for a session that will never read them."""
    with db._lock:
        c = _ensure()
        c.executemany("UPDATE session_inbox SET taken=? WHERE id=? AND session=?",
                      [(time.time(), i, session) for i in ids])
        c.commit()


def inbox_should_wake(session: str) -> dict:
    """Whether anything pending deserves a turn, and whether a person is waiting on it."""
    with db._lock:
        row = _ensure().execute(
            "SELECT COUNT(*), COALESCE(MAX(priority),0), MIN(ts) FROM session_inbox "
            "WHERE session=? AND taken IS NULL AND wakes=1", (session,)).fetchone()
    return {"count": row[0], "priority": row[1], "oldest": row[2]}


def inbox_take(session: str, ids: list[int], role: str, content) -> int:
    """Move items into the transcript in one commit, so a crash can neither drop
    a message nor deliver it twice."""
    with db._lock:
        c = _ensure()
        now = time.time()
        c.executemany("UPDATE session_inbox SET taken=? WHERE id=?", [(now, i) for i in ids])
        cur = c.execute("INSERT INTO session_log (session, ts, role, content) VALUES (?,?,?,?)",
                        (session, now, role, json.dumps(content, ensure_ascii=False, default=str)))
        c.commit()
        return cur.lastrowid


# ---------- background jobs ----------

JOB_RESULT_KEPT = 64_000


def job_add(session: str, tool: str, label: str) -> dict:
    jid = _new("j")
    with db._lock:
        c = _ensure()
        c.execute("INSERT INTO jobs (id, session, tool, label, status, started) VALUES (?,?,?,?,?,?)",
                  (jid, session, tool, label[:200], "running", time.time()))
        c.commit()
        return _one(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)))


def job_finish(jid: str, status: str, result: str = "") -> dict | None:
    with db._lock:
        c = _ensure()
        c.execute("UPDATE jobs SET status=?, finished=?, result=? WHERE id=? AND status='running'",
                  (status, time.time(), result[:JOB_RESULT_KEPT], jid))
        c.commit()
        return _one(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)))


def job_get(jid: str) -> dict | None:
    with db._lock:
        return _one(_ensure().execute("SELECT * FROM jobs WHERE id=?", (jid,)))


def jobs_of(session: str, status: str | None = None, limit: int = 40) -> list[dict]:
    sql = "SELECT * FROM jobs WHERE session=?" + (" AND status=?" if status else "")
    args = (session, status) if status else (session,)
    with db._lock:
        return _rows(_ensure().execute(sql + " ORDER BY started DESC LIMIT ?", (*args, limit)))


# ---------- reports ----------

def report_add(session: str, mission: str | None, status: str, summary: str,
               question: str = "", files: list | None = None) -> dict:
    rid = _new("rp")
    with db._lock:
        c = _ensure()
        c.execute("INSERT INTO reports (id, ts, session, mission, status, summary, question, files) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  (rid, time.time(), session, mission, status, summary, question,
                   json.dumps(files or [], ensure_ascii=False)))
        c.commit()
        return _one(c.execute("SELECT * FROM reports WHERE id=?", (rid,)))


def report_deliver(session: str, mission: str | None, status: str, summary: str, question: str = "",
                   files: list | None = None, to: str = "", kind: str = "report", wakes: bool = True,
                   strength: str = "") -> dict:
    """A report and its place in the reader's inbox, in one commit. They used to be two
    steps with a slow one between them, and a shutdown landing there lost the report:
    the Worker read as finished and nobody was ever told."""
    rid, now = _new("rp"), time.time()
    with db._lock:
        c = _ensure()
        c.execute("INSERT INTO reports (id, ts, session, mission, status, summary, question, files, strength) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (rid, now, session, mission, status, summary, question,
                   json.dumps(files or [], ensure_ascii=False), strength))
        if to:
            c.execute("INSERT INTO session_inbox (session, ts, kind, ref, text, meta, wakes, priority) "
                      "VALUES (?,?,?,?,?,?,?,?)", (to, now, kind, rid, "", "{}", int(wakes), 0))
        c.commit()
        return _one(c.execute("SELECT * FROM reports WHERE id=?", (rid,)))


def report_get(rid: str) -> dict | None:
    with db._lock:
        return _one(_ensure().execute("SELECT * FROM reports WHERE id=?", (rid,)))


def reports_of(session: str, limit: int = 20) -> list[dict]:
    with db._lock:
        return _rows(_ensure().execute("SELECT * FROM reports WHERE session=? ORDER BY ts DESC LIMIT ?",
                                       (session, limit)))


def report_files_allowed(path: str, since: float = 0.0) -> bool:
    """A reply may attach only what a Worker actually reported."""
    with db._lock:
        rows = _ensure().execute("SELECT files FROM reports WHERE ts>=? ORDER BY ts DESC LIMIT 500",
                                 (since,)).fetchall()
    return any(path in json.loads(r[0] or "[]") for r in rows)


# ---------- semantic policies ----------

def policy_add(text: str, author: str = "owner") -> dict:
    pid = _new("p")
    with db._lock:
        c = _ensure()
        c.execute("INSERT INTO policies VALUES (?,?,?,?,?)", (pid, time.time(), text.strip(), 1, author))
        c.commit()
        return _one(c.execute("SELECT * FROM policies WHERE id=?", (pid,)))


def policy_set(pid: str, enabled: bool) -> bool:
    with db._lock:
        c = _ensure()
        cur = c.execute("UPDATE policies SET enabled=? WHERE id=?", (int(enabled), pid))
        c.commit()
        return bool(cur.rowcount)


def policy_remove(pid: str) -> bool:
    with db._lock:
        c = _ensure()
        cur = c.execute("DELETE FROM policies WHERE id=?", (pid,))
        c.commit()
        return bool(cur.rowcount)


def policies(enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM policies" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY ts"
    with db._lock:
        return _rows(_ensure().execute(sql))


# ---------- outbound messages that failed ----------

def outbox_add(channel: str, chat: str, text: str, error: str = "", delay: float = 5.0) -> str:
    oid = _new("o")
    now = time.time()
    with db._lock:
        c = _ensure()
        c.execute("INSERT INTO outbox (id, ts, channel, chat, text, attempts, next_try, error) VALUES (?,?,?,?,?,?,?,?)",
                  (oid, now, channel, chat, text, 1, now + delay, error[:300]))
        c.commit()
    return oid


def outbox_due(now: float, limit: int = 20) -> list[dict]:
    with db._lock:
        return _rows(_ensure().execute(
            "SELECT * FROM outbox WHERE sent IS NULL AND gave_up IS NULL AND next_try<=? ORDER BY ts LIMIT ?",
            (now, limit)))


def outbox_give_up(oid: str, error: str = ""):
    with db._lock:
        c = _ensure()
        c.execute("UPDATE outbox SET gave_up=?, error=? WHERE id=?", (time.time(), error[:300], oid))
        c.commit()


def outbox_result(oid: str, sent: bool, error: str = "", delay: float = 60.0):
    now = time.time()
    with db._lock:
        c = _ensure()
        if sent:
            c.execute("UPDATE outbox SET sent=?, error='' WHERE id=?", (now, oid))
        else:
            c.execute("UPDATE outbox SET attempts=attempts+1, next_try=?, error=? WHERE id=?",
                      (now + delay, error[:300], oid))
        c.commit()


def outbox_stuck(limit: int = 20) -> list[dict]:
    with db._lock:
        return _rows(_ensure().execute(
            "SELECT * FROM outbox WHERE sent IS NULL AND gave_up IS NULL ORDER BY ts LIMIT ?", (limit,)))


# ---------- who has a repository's shared checkout ----------

# A session in one of these may have work in progress sitting in the checkout.
HOLDS_CHECKOUT = ("queued", "working", "waiting", "blocked", "stalled")


def checkout_holder(repo: str) -> str | None:
    with db._lock:
        row = _ensure().execute("SELECT session FROM checkouts WHERE repo=?", (repo,)).fetchone()
    return row[0] if row else None


def checkout_claim(repo: str, session: str) -> bool:
    """Take the shared checkout if nobody is using it. False means someone is."""
    with db._lock:
        c = _ensure()
        row = c.execute("SELECT session FROM checkouts WHERE repo=?", (repo,)).fetchone()
        if row and row[0] != session:
            holder = c.execute("SELECT status FROM sessions WHERE id=?", (row[0],)).fetchone()
            if holder and holder[0] in HOLDS_CHECKOUT:
                return False
        c.execute("INSERT OR REPLACE INTO checkouts VALUES (?,?,?)", (repo, session, time.time()))
        c.commit()
        return True


# ---------- watchdog findings ----------

def finding_add(session: str, verdict: str, reason: str = "", action: str = "") -> dict:
    with db._lock:
        c = _ensure()
        cur = c.execute("INSERT INTO findings (ts, session, verdict, reason, action) VALUES (?,?,?,?,?)",
                        (time.time(), session, verdict, reason[:600], action[:200]))
        c.commit()
        return _one(c.execute("SELECT * FROM findings WHERE id=?", (cur.lastrowid,)))


def findings(limit: int = 40, session: str | None = None) -> list[dict]:
    sql = "SELECT * FROM findings" + (" WHERE session=?" if session else "") + " ORDER BY ts DESC LIMIT ?"
    args = (session, limit) if session else (limit,)
    with db._lock:
        return _rows(_ensure().execute(sql, args))


# ---------- restart ----------

def recover() -> dict:
    """Background work does not survive its process, so say so instead of
    leaving a session waiting on a result that will never arrive."""
    with db._lock:
        c = _ensure()
        now = time.time()
        lost = _rows(c.execute("SELECT * FROM jobs WHERE status='running'"))
        c.execute("UPDATE jobs SET status='lost', finished=?, result='The host restarted while this was running.' "
                  "WHERE status='running'", (now,))
        c.commit()
    return {"lost_jobs": lost}
