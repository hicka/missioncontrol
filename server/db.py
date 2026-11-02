"""SQLite persistence for threads and messages."""
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from apphome import ROOT

DB_PATH = ROOT / "missioncontrol.db"
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                thread TEXT NOT NULL,
                sender TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'chat',
                text TEXT NOT NULL,
                meta TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_thread_ts ON messages(thread, ts)")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                author TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'fact',
                text TEXT NOT NULL
            )"""
        )
        # idempotent migration for older DBs that pre-date the kind column
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(memories)").fetchall()}
        if "kind" not in cols:
            _conn.execute("ALTER TABLE memories ADD COLUMN kind TEXT NOT NULL DEFAULT 'fact'")
        if "guid" not in cols:
            _conn.execute("ALTER TABLE memories ADD COLUMN guid TEXT")
            import uuid as _uuid
            for (mid,) in _conn.execute("SELECT id FROM memories WHERE guid IS NULL").fetchall():
                _conn.execute("UPDATE memories SET guid = ? WHERE id = ?",
                              (_uuid.uuid4().hex, mid))
        if "updated" not in cols:
            _conn.execute("ALTER TABLE memories ADD COLUMN updated REAL NOT NULL DEFAULT 0")
            _conn.execute("UPDATE memories SET updated=ts WHERE updated=0")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_tombstones (
                guid TEXT PRIMARY KEY,
                ts REAL NOT NULL
            )"""
        )
        _conn.commit()
    return _conn


def _ensure_playbooks():
    _db().execute(
        """CREATE TABLE IF NOT EXISTS playbooks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            author TEXT NOT NULL,
            site TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            UNIQUE(site, title)
        )"""
    )
    _db().commit()


def save_playbook(author: str, site: str, title: str, content: str) -> int:
    site = site.lower().strip().removeprefix("www.")
    with _lock:
        _ensure_playbooks()
        cur = _db().execute(
            """INSERT INTO playbooks (ts, author, site, title, content) VALUES (?,?,?,?,?)
               ON CONFLICT(site, title) DO UPDATE SET
                 ts=excluded.ts, author=excluded.author, content=excluded.content""",
            (time.time(), author, site, title, content),
        )
        _db().commit()
        return cur.lastrowid


def find_playbooks(domain: str) -> list[dict]:
    domain = (domain or "").lower().removeprefix("www.")
    if not domain:
        return []
    with _lock:
        _ensure_playbooks()
        rows = _db().execute(
            "SELECT site, title, content, author FROM playbooks "
            "WHERE ? LIKE '%' || site ORDER BY ts DESC",
            (domain,),
        ).fetchall()
    return [{"site": r[0], "title": r[1], "content": r[2], "author": r[3]} for r in rows]


def list_playbooks() -> list[dict]:
    with _lock:
        _ensure_playbooks()
        rows = _db().execute(
            "SELECT site, title, author, ts FROM playbooks ORDER BY site, title").fetchall()
    return [{"site": r[0], "title": r[1], "author": r[2], "ts": r[3]} for r in rows]


def delete_playbook(site: str, title: str) -> bool:
    with _lock:
        _ensure_playbooks()
        cur = _db().execute("DELETE FROM playbooks WHERE site = ? AND title = ?", (site, title))
        _db().commit()
        return cur.rowcount > 0


def _ensure_fts():
    """Full-text index over team memory so retrieval scales to any size."""
    c = _db()
    c.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            text, content='memories', content_rowid='id')"""
    )
    c.execute(
        """CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
             INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
           END"""
    )
    c.execute(
        """CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
             INSERT INTO memories_fts(memories_fts, rowid, text)
             VALUES ('delete', old.id, old.text);
           END"""
    )
    c.execute(
        """CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF text ON memories BEGIN
             INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
             INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
           END"""
    )
    # count(*) on a content-linked FTS table reads through to the source
    # table, so it cannot tell whether the index is populated; track the
    # build explicitly instead.
    c.execute("CREATE TABLE IF NOT EXISTS fts_state (version INTEGER)")
    if not c.execute("SELECT version FROM fts_state").fetchone():
        c.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
        c.execute("INSERT INTO fts_state (version) VALUES (1)")
    c.commit()


_STOPWORDS = frozenset(
    "the and for with that this from have has was were are you your our their "
    "they them then than what when where which while will would could should "
    "about into over under after before been being both does done just like "
    "more most much must need only same some such very want well were what "
    "your please thanks hello".split())


def _fts_query(text: str, max_terms: int = 14) -> str:
    words = re.findall(r"[^\W_]+(?:[./#-][^\W_]+)*", text.casefold(), re.UNICODE)
    seen, terms = set(), []
    for w in reversed(words):  # favor the newest part of the context
        w = w.strip("./-#")
        if len(w) < 2 or w in _STOPWORDS or w in seen:
            continue
        seen.add(w)
        terms.append('"' + w.replace('"', "") + '"')
        if len(terms) >= max_terms:
            break
    return " OR ".join(terms)


def search_memories(query_text: str, limit: int = 10) -> list[dict]:
    """BM25 retrieval over the ENTIRE memory store, any age."""
    q = _fts_query(query_text)
    if not q:
        return []
    with _lock:
        _ensure_fts()
        try:
            rows = _db().execute(
                """SELECT m.id, m.ts, m.author, m.kind, m.text
                   FROM memories_fts f JOIN memories m ON m.id = f.rowid
                   WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT ?""",
                (q, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [{"id": r[0], "ts": r[1], "author": r[2], "kind": r[3], "text": r[4]}
            for r in rows]


def add_memory(author: str, text: str, kind: str = "fact",
               guid: str | None = None, ts: float | None = None) -> int:
    import uuid as _uuid
    text = text.strip()
    if not text or len(text) > 16000:
        raise ValueError("memory text must contain 1–16000 characters")
    with _lock:
        _ensure_fts()
        if guid is None:
            prior = _db().execute("SELECT id FROM memories WHERE text=? AND kind=?", (text, kind)).fetchone()
            if prior:
                return prior[0]
        now = ts if ts is not None else time.time()
        cur = _db().execute(
            "INSERT INTO memories (ts, author, kind, text, guid, updated) VALUES (?,?,?,?,?,?)",
            (now, author, kind, text, guid or _uuid.uuid4().hex, now),
        )
        _db().commit()
        return cur.lastrowid


def delete_memory(mid: int) -> bool:
    with _lock:
        _ensure_fts()
        row = _db().execute("SELECT guid FROM memories WHERE id = ?", (mid,)).fetchone()
        cur = _db().execute("DELETE FROM memories WHERE id = ?", (mid,))
        if row and row[0]:
            _db().execute(
                "INSERT OR REPLACE INTO memory_tombstones (guid, ts) VALUES (?, ?)",
                (row[0], time.time()))
        _db().commit()
        return cur.rowcount > 0


def update_memory(mid: int, text: str, kind: str | None = None) -> bool:
    text = text.strip()
    if not text or len(text) > 16000:
        raise ValueError("memory text must contain 1–16000 characters")
    with _lock:
        _ensure_fts()
        if kind:
            cur = _db().execute("UPDATE memories SET text = ?, kind = ?, updated = ? WHERE id = ?",
                                (text, kind, time.time(), mid))
        else:
            cur = _db().execute("UPDATE memories SET text = ?, updated = ? WHERE id = ?", (text, time.time(), mid))
        _db().commit()
        return cur.rowcount > 0


def list_memories(limit: int = 80) -> list[dict]:
    with _lock:
        rows = _db().execute(
            "SELECT id, ts, author, kind, text FROM memories ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"id": r[0], "ts": r[1], "author": r[2], "kind": r[3], "text": r[4]}
            for r in reversed(rows)]


def list_memories_by_kind(kind: str, limit: int = 500) -> list[dict]:
    """The newest memories of one kind, oldest first. Standing rules are read
    this way so that any number of newer facts cannot push one out of view."""
    with _lock:
        rows = _db().execute(
            "SELECT id, ts, author, kind, text FROM memories WHERE kind = ? ORDER BY id DESC LIMIT ?",
            (kind, limit),
        ).fetchall()
    return [{"id": r[0], "ts": r[1], "author": r[2], "kind": r[3], "text": r[4]}
            for r in reversed(rows)]


def _row_to_msg(row) -> dict:
    return {
        "id": row[0],
        "ts": row[1],
        "thread": row[2],
        "sender": row[3],
        "kind": row[4],
        "text": row[5],
        "meta": json.loads(row[6]),
    }


COLS = "id, ts, thread, sender, kind, text, meta"


def recent_activity(limit: int = 40) -> list[dict]:
    with _lock:
        rows = _db().execute(f"SELECT {COLS} FROM messages WHERE kind='a2a' "
                             "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_msg(r) for r in rows]


def knowledge_counts() -> dict:
    with _lock:
        return {"memories": _db().execute("SELECT count(*) FROM memories").fetchone()[0]}


def add_message(thread: str, sender: str, text: str, kind: str = "chat", meta: dict | None = None) -> dict:
    ts = time.time()
    with _lock:
        _ensure_msg_fts()
        cur = _db().execute(
            "INSERT INTO messages (ts, thread, sender, kind, text, meta) VALUES (?,?,?,?,?,?)",
            (ts, thread, sender, kind, text, json.dumps(meta or {})),
        )
        _db().commit()
        mid = cur.lastrowid
    return {"id": mid, "ts": ts, "thread": thread, "sender": sender, "kind": kind,
            "text": text, "meta": meta or {}}


def thread_view(thread: str, agent_id: str | None = None, limit: int = 300,
                before: tuple[float, int] | None = None) -> list[dict]:
    """Messages for a thread, newest last. For dm:<agent>, also merge a2a traffic
    involving the agent. `before` is the (ts, id) of the oldest message already
    held, so the caller can page further back; ts alone would skip messages that
    share a timestamp."""
    cursor, args = "", []
    if before:
        cursor = " AND (ts < ? OR (ts = ? AND id < ?))"
        args = [before[0], before[0], before[1]]
    with _lock:
        if agent_id:
            rows = _db().execute(
                f"""SELECT {COLS} FROM messages
                    WHERE (thread = ? OR (kind = 'a2a'
                           AND (sender = ? OR json_extract(meta,'$.to') = ?))){cursor}
                    ORDER BY ts DESC, id DESC LIMIT ?""",
                (thread, agent_id, agent_id, *args, limit),
            ).fetchall()
        else:
            rows = _db().execute(
                f"""SELECT {COLS} FROM messages WHERE thread = ?{cursor}
                    ORDER BY ts DESC, id DESC LIMIT ?""",
                (thread, *args, limit),
            ).fetchall()
    return [_row_to_msg(r) for r in reversed(rows)]


def agent_context(agent_id: str, limit: int = 60) -> list[dict]:
    """Everything this agent has seen or said, across DM, group, and a2a."""
    with _lock:
        rows = _db().execute(
            f"""SELECT {COLS} FROM messages
                WHERE (thread = ? AND kind != 'tool')
                   OR thread LIKE 'group:%'
                   OR (kind = 'a2a' AND (sender = ? OR json_extract(meta,'$.to') = ?))
                ORDER BY ts DESC LIMIT ?""",
            (f"dm:{agent_id}", agent_id, agent_id, limit),
        ).fetchall()
    return [_row_to_msg(r) for r in reversed(rows)]


def get_message(mid: int) -> dict | None:
    with _lock:
        row = _db().execute(f"SELECT {COLS} FROM messages WHERE id = ?", (mid,)).fetchone()
    return _row_to_msg(row) if row else None


def last_message(thread: str, agent_id: str | None = None) -> dict | None:
    msgs = thread_view(thread, agent_id, limit=1)
    return msgs[-1] if msgs else None


_msg_fts_ready = False


def _ensure_msg_fts():
    """Full-text index over chat history. Unlike the memory index this sits on
    the hot path of every message, so the DDL runs once per process, not once
    per insert."""
    global _msg_fts_ready
    if _msg_fts_ready:
        return
    c = _db()
    c.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            text, content='messages', content_rowid='id')"""
    )
    c.execute(
        """CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
             INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
           END"""
    )
    c.execute(
        """CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
             INSERT INTO messages_fts(messages_fts, rowid, text)
             VALUES ('delete', old.id, old.text);
           END"""
    )
    c.execute("CREATE TABLE IF NOT EXISTS msg_fts_state (version INTEGER)")
    if not c.execute("SELECT version FROM msg_fts_state").fetchone():
        # backfill every message written before this index existed
        c.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
        c.execute("INSERT INTO msg_fts_state (version) VALUES (1)")
    c.commit()
    _msg_fts_ready = True


def _search_query(text: str) -> str:
    """An FTS5 query for what a person typed into a search box: every word has
    to match, and the one they are still typing matches as a prefix."""
    words = re.findall(r"[\w./#@-]+", text.lower())
    terms = [w.replace('"', "") for w in words if w.strip("./#@-")]
    if not terms:
        return ""
    quoted = [f'"{t}"' for t in terms]
    quoted[-1] += "*"
    return " AND ".join(quoted)


def search_messages(query_text: str, limit: int = 60) -> list[dict]:
    """Full-text search across every message ever sent, any age."""
    q = _search_query(query_text)
    if not q:
        return []
    with _lock:
        _ensure_msg_fts()
        try:
            rows = _db().execute(
                f"""SELECT {", ".join("m." + c for c in COLS.split(", "))}
                    FROM messages_fts f JOIN messages m ON m.id = f.rowid
                    WHERE messages_fts MATCH ? ORDER BY m.ts DESC LIMIT ?""",
                (q, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [_row_to_msg(r) for r in rows]


# ---------- self-improvement proposals ----------

def _ensure_improvements():
    _db().execute(
        """CREATE TABLE IF NOT EXISTS improvements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            agent TEXT NOT NULL,
            title TEXT NOT NULL,
            rationale TEXT NOT NULL DEFAULT '',
            branch TEXT NOT NULL,
            worktree TEXT NOT NULL DEFAULT '',
            stat TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            note TEXT NOT NULL DEFAULT ''
        )"""
    )
    _db().commit()


def _improvement_row(r) -> dict:
    return {"id": r[0], "ts": r[1], "agent": r[2], "title": r[3], "rationale": r[4],
            "branch": r[5], "worktree": r[6], "stat": json.loads(r[7] or "{}"),
            "status": r[8], "note": r[9]}


IMP_COLS = "id, ts, agent, title, rationale, branch, worktree, stat, status, note"


def add_improvement(agent: str, title: str, rationale: str, branch: str,
                    worktree: str, stat: dict) -> int:
    with _lock:
        _ensure_improvements()
        cur = _db().execute(
            "INSERT INTO improvements (ts, agent, title, rationale, branch, worktree, stat)"
            " VALUES (?,?,?,?,?,?,?)",
            (time.time(), agent, title, rationale, branch, worktree, json.dumps(stat)),
        )
        _db().commit()
        return cur.lastrowid


def list_improvements(status: str | None = None, limit: int = 50) -> list[dict]:
    with _lock:
        _ensure_improvements()
        if status:
            rows = _db().execute(
                f"SELECT {IMP_COLS} FROM improvements WHERE status = ? "
                f"ORDER BY ts DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = _db().execute(
                f"SELECT {IMP_COLS} FROM improvements ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
    return [_improvement_row(r) for r in rows]


def get_improvement(iid: int) -> dict | None:
    with _lock:
        _ensure_improvements()
        row = _db().execute(f"SELECT {IMP_COLS} FROM improvements WHERE id = ?",
                            (iid,)).fetchone()
    return _improvement_row(row) if row else None


def set_improvement(iid: int, status: str, note: str = "") -> bool:
    with _lock:
        _ensure_improvements()
        cur = _db().execute(
            "UPDATE improvements SET status = ?, note = ? WHERE id = ?",
            (status, note, iid))
        _db().commit()
        return cur.rowcount > 0


def update_improvement(iid: int, **fields) -> bool:
    """Change any of status, note, stat, branch, worktree: a fix in progress
    fills its row in as it goes."""
    cols = {k: (json.dumps(v) if k == "stat" else v) for k, v in fields.items()
            if k in ("status", "note", "stat", "branch", "worktree")}
    if not cols:
        return False
    with _lock:
        _ensure_improvements()
        cur = _db().execute(
            f"UPDATE improvements SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
            (*cols.values(), iid))
        _db().commit()
        return cur.rowcount > 0


# ---------- cron jobs ----------

def _ensure_cron():
    _db().execute(
        """CREATE TABLE IF NOT EXISTS cron_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            agent TEXT NOT NULL,
            name TEXT NOT NULL,
            schedule TEXT NOT NULL,
            task TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_by TEXT NOT NULL,
            last_run REAL,
            next_run REAL
        )"""
    )
    cron_cols = {r[1] for r in _db().execute("PRAGMA table_info(cron_jobs)").fetchall()}
    for col, typ in (("monitor_cmd", "TEXT"), ("monitor_last", "TEXT"), ("note", "TEXT"),
                     ("runs", "INTEGER DEFAULT 0"), ("max_runs", "INTEGER DEFAULT 0")):
        if col not in cron_cols:
            _db().execute(f"ALTER TABLE cron_jobs ADD COLUMN {col} {typ}")
    _db().commit()


def _cron_row(r) -> dict:
    return {"id": r[0], "ts": r[1], "agent": r[2], "name": r[3], "schedule": r[4],
            "task": r[5], "status": r[6], "created_by": r[7],
            "last_run": r[8], "next_run": r[9],
            "monitor_cmd": r[10], "monitor_last": r[11], "note": r[12],
            "runs": r[13] or 0, "max_runs": r[14] or 0}


CRON_COLS = ("id, ts, agent, name, schedule, task, status, created_by, "
             "last_run, next_run, monitor_cmd, monitor_last, note, runs, max_runs")


def add_cron(agent: str, name: str, schedule: str, task: str, created_by: str,
             next_run: float | None, status: str = "pending",
             monitor_cmd: str | None = None, max_runs: int = 0) -> int:
    with _lock:
        _ensure_cron()
        cur = _db().execute(
            "INSERT INTO cron_jobs (ts, agent, name, schedule, task, status, "
            "created_by, next_run, monitor_cmd, max_runs) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), agent, name, schedule, task, status, created_by,
             next_run, monitor_cmd, max_runs))
        _db().commit()
        return cur.lastrowid


def list_cron(status: str | None = None) -> list[dict]:
    with _lock:
        _ensure_cron()
        if status:
            rows = _db().execute(
                f"SELECT {CRON_COLS} FROM cron_jobs WHERE status = ? ORDER BY id", (status,)).fetchall()
        else:
            rows = _db().execute(
                f"SELECT {CRON_COLS} FROM cron_jobs ORDER BY id").fetchall()
    return [_cron_row(r) for r in rows]


def get_cron(jid: int) -> dict | None:
    with _lock:
        _ensure_cron()
        r = _db().execute(f"SELECT {CRON_COLS} FROM cron_jobs WHERE id = ?", (jid,)).fetchone()
    return _cron_row(r) if r else None


def update_cron(jid: int, **fields) -> bool:
    allowed = {"status", "last_run", "next_run", "schedule", "task", "name",
               "monitor_cmd", "monitor_last", "note", "runs", "max_runs"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    with _lock:
        _ensure_cron()
        cur = _db().execute(
            f"UPDATE cron_jobs SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
            (*sets.values(), jid))
        _db().commit()
        return cur.rowcount > 0


def delete_cron(jid: int) -> bool:
    with _lock:
        _ensure_cron()
        cur = _db().execute("DELETE FROM cron_jobs WHERE id = ?", (jid,))
        _db().commit()
        return cur.rowcount > 0


def due_cron(now: float) -> list[dict]:
    with _lock:
        _ensure_cron()
        rows = _db().execute(
            f"SELECT {CRON_COLS} FROM cron_jobs WHERE status = 'active' "
            "AND next_run IS NOT NULL AND next_run <= ? ORDER BY next_run", (now,)).fetchall()
    return [_cron_row(r) for r in rows]


# ---------- watches: ephemeral, no-approval status polls ----------

def _ensure_watches():
    _db().execute(
        """CREATE TABLE IF NOT EXISTS watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            agent TEXT NOT NULL,
            name TEXT NOT NULL,
            poll TEXT NOT NULL,
            done_re TEXT,
            fail_re TEXT,
            interval INTEGER NOT NULL,
            max_checks INTEGER NOT NULL,
            checks INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            last_out TEXT,
            last_poll REAL,
            created_by TEXT NOT NULL
        )"""
    )
    cols = {r[1] for r in _db().execute("PRAGMA table_info(watches)")}
    for name in ("cwd", "mission", "goal", "session"):
        if name not in cols:
            _db().execute(f"ALTER TABLE watches ADD COLUMN {name} TEXT")
    if "notified" not in cols:
        _db().execute("ALTER TABLE watches ADD COLUMN notified INTEGER NOT NULL DEFAULT 1")
        _db().execute("UPDATE watches SET notified=0 WHERE status='active'")
    if "notice_claimed" not in cols:
        _db().execute("ALTER TABLE watches ADD COLUMN notice_claimed REAL NOT NULL DEFAULT 0")
    _db().commit()


WATCH_COLS = ("id, ts, agent, name, poll, done_re, fail_re, interval, max_checks, "
              "checks, status, last_out, last_poll, created_by, cwd, mission, notified, "
              "notice_claimed, goal, session")


def _watch_row(r) -> dict:
    return {"id": r[0], "ts": r[1], "agent": r[2], "name": r[3], "poll": r[4],
            "done_re": r[5], "fail_re": r[6], "interval": r[7], "max_checks": r[8],
            "checks": r[9], "status": r[10], "last_out": r[11], "last_poll": r[12],
            "created_by": r[13], "cwd": r[14], "mission": r[15],
            "notified": bool(r[16]), "notice_claimed": r[17], "goal": r[18], "session": r[19]}


def add_watch(agent: str, name: str, poll: str, done_re: str | None,
              fail_re: str | None, interval: int, max_checks: int,
              created_by: str, cwd: str | None = None, mission: str | None = None,
              goal: str | None = None, session: str | None = None) -> int:
    with _lock:
        _ensure_watches()
        cur = _db().execute(
            "INSERT INTO watches (ts, agent, name, poll, done_re, fail_re, interval, "
            "max_checks, created_by, cwd, mission, notified, goal, session) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
            (time.time(), agent, name, poll, done_re, fail_re, interval, max_checks,
             created_by, cwd, mission, goal, session))
        _db().commit()
        return cur.lastrowid


def list_watches(status: str | None = None) -> list[dict]:
    with _lock:
        _ensure_watches()
        if status:
            rows = _db().execute(
                f"SELECT {WATCH_COLS} FROM watches WHERE status = ? ORDER BY id DESC",
                (status,)).fetchall()
        else:
            rows = _db().execute(
                f"SELECT {WATCH_COLS} FROM watches ORDER BY id DESC").fetchall()
    return [_watch_row(r) for r in rows]


def get_watch(wid: int) -> dict | None:
    with _lock:
        _ensure_watches()
        r = _db().execute(f"SELECT {WATCH_COLS} FROM watches WHERE id = ?", (wid,)).fetchone()
    return _watch_row(r) if r else None


def active_watches() -> list[dict]:
    return list_watches("active")


def update_watch(wid: int, **fields) -> bool:
    allowed = {"status", "checks", "last_out", "last_poll", "cwd", "mission"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    with _lock:
        _ensure_watches()
        cur = _db().execute(
            f"UPDATE watches SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
            (*sets.values(), wid))
        _db().commit()
        return cur.rowcount > 0


def delete_watch(wid: int) -> bool:
    with _lock:
        _ensure_watches()
        cur = _db().execute("DELETE FROM watches WHERE id = ?", (wid,))
        _db().commit()
        return cur.rowcount > 0


def claim_watch(wid: int, checks: int, now: float) -> bool:
    with _lock:
        _ensure_watches()
        cur = _db().execute("UPDATE watches SET checks=checks+1, last_poll=? "
                            "WHERE id=? AND status='active' AND checks=?",
                            (now, wid, checks))
        _db().commit()
        return cur.rowcount > 0


def finish_watch_poll(wid: int, status: str, out: str) -> bool:
    """Cancellation wins over a poll that was already in flight."""
    with _lock:
        _ensure_watches()
        cur = _db().execute("UPDATE watches SET status=?, last_out=? WHERE id=? AND status='active'",
                            (status, out[:4000], wid))
        _db().commit()
        return cur.rowcount > 0


def pending_watch_notices() -> list[dict]:
    with _lock:
        _ensure_watches()
        return [_watch_row(r) for r in _db().execute(f"SELECT {WATCH_COLS} FROM watches "
            "WHERE notified=0 AND status IN ('done','failed','expired')")]


def claim_watch_notice(wid: int) -> bool:
    with _lock:
        _ensure_watches()
        now = time.time()
        cur = _db().execute("UPDATE watches SET notice_claimed=? WHERE id=? AND notified=0 "
                            "AND (notice_claimed=0 OR notice_claimed<?)", (now, wid, now - 60))
        _db().commit()
        return cur.rowcount > 0


def finish_watch_notice(wid: int, delivered: bool):
    with _lock:
        _db().execute("UPDATE watches SET notified=?, notice_claimed=0 WHERE id=?", (int(delivered), wid))
        _db().commit()


# ---------- fleet sync ----------

def export_memories(since: float) -> tuple[list[dict], list[dict]]:
    """(rows, tombstones) changed since `since` (with overlap handled by caller)."""
    with _lock:
        rows = _db().execute(
            "SELECT guid, ts, author, kind, text, updated FROM memories WHERE updated > ?", (since,)
        ).fetchall()
        tombs = _db().execute(
            "SELECT guid, ts FROM memory_tombstones WHERE ts > ?", (since,)).fetchall()
    return ([{"guid": r[0], "ts": r[1], "author": r[2], "kind": r[3], "text": r[4], "updated": r[5]}
             for r in rows],
            [{"guid": r[0], "ts": r[1]} for r in tombs])


def merge_memories(rows: list[dict], tombs: list[dict]) -> int:
    """Apply a peer's changes; guid-deduped, tombstones win. Returns applied count."""
    applied = 0
    with _lock:
        _ensure_fts()
        c = _db()
        dead = {t["guid"] for t in tombs}
        for t in tombs:
            c.execute("INSERT OR REPLACE INTO memory_tombstones (guid, ts) VALUES (?,?)",
                      (t["guid"], t["ts"]))
            cur = c.execute("DELETE FROM memories WHERE guid = ?", (t["guid"],))
            applied += cur.rowcount
        for m in rows:
            if not m.get("guid") or m["guid"] in dead:
                continue
            if c.execute("SELECT 1 FROM memory_tombstones WHERE guid = ?",
                         (m["guid"],)).fetchone():
                continue
            updated = m.get("updated", m["ts"])
            prior = c.execute("SELECT updated FROM memories WHERE guid=?", (m["guid"],)).fetchone()
            if prior:
                if updated <= prior[0]:
                    continue
                c.execute("UPDATE memories SET text=?, kind=?, author=?, updated=? WHERE guid=?",
                          (m["text"], m.get("kind") or "fact", m["author"], updated, m["guid"]))
            else:
                c.execute("INSERT INTO memories (ts, author, kind, text, guid, updated) VALUES (?,?,?,?,?,?)",
                          (m["ts"], m["author"], m.get("kind") or "fact", m["text"], m["guid"], updated))
            applied += 1
        c.commit()
    return applied


def export_playbooks(since: float) -> list[dict]:
    with _lock:
        _ensure_playbooks()
        rows = _db().execute(
            "SELECT ts, author, site, title, content FROM playbooks WHERE ts > ?",
            (since,)).fetchall()
    return [{"ts": r[0], "author": r[1], "site": r[2], "title": r[3], "content": r[4]}
            for r in rows]


def merge_playbooks(rows: list[dict]) -> int:
    applied = 0
    with _lock:
        _ensure_playbooks()
        c = _db()
        for b in rows:
            existing = c.execute(
                "SELECT ts FROM playbooks WHERE site = ? AND title = ?",
                (b["site"], b["title"])).fetchone()
            if existing and existing[0] >= b["ts"]:
                continue
            c.execute(
                """INSERT INTO playbooks (ts, author, site, title, content) VALUES (?,?,?,?,?)
                   ON CONFLICT(site, title) DO UPDATE SET
                     ts=excluded.ts, author=excluded.author, content=excluded.content""",
                (b["ts"], b["author"], b["site"], b["title"], b["content"]))
            applied += 1
        c.commit()
    return applied


# ---------- usage tracking ----------

def _ensure_usage():
    _db().execute(
        """CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            agent TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            in_tokens INTEGER NOT NULL,
            out_tokens INTEGER NOT NULL
        )"""
    )
    # Input the provider read back from its cache, kept apart from in_tokens because
    # it costs about a tenth as much.
    if "cache_read" not in {row[1] for row in _db().execute("PRAGMA table_info(usage)")}:
        _db().execute("ALTER TABLE usage ADD COLUMN cache_read INTEGER NOT NULL DEFAULT 0")
    _db().commit()


def add_usage(agent: str, provider: str, model: str, in_tokens: int, out_tokens: int, cache_read: int = 0):
    with _lock:
        _ensure_usage()
        _db().execute(
            "INSERT INTO usage (ts, agent, provider, model, in_tokens, out_tokens, cache_read) "
            "VALUES (?,?,?,?,?,?,?)",
            (time.time(), agent, provider, model, in_tokens, out_tokens, cache_read))
        _db().commit()


def usage_today() -> int:
    with _lock:
        _ensure_usage()
        row = _db().execute(
            "SELECT SUM(in_tokens) + SUM(out_tokens) + SUM(cache_read) / 10 FROM usage "
            "WHERE date(ts, 'unixepoch', 'localtime') = date('now', 'localtime')").fetchone()
    return int(row[0] or 0)


def usage_summary(days: int = 7) -> dict:
    since = time.time() - days * 86400
    with _lock:
        _ensure_usage()
        per_agent = _db().execute(
            "SELECT agent, SUM(in_tokens), SUM(out_tokens), COUNT(*), SUM(cache_read) FROM usage "
            "WHERE ts > ? GROUP BY agent ORDER BY SUM(in_tokens)+SUM(out_tokens) DESC",
            (since,)).fetchall()
        per_day = _db().execute(
            "SELECT date(ts, 'unixepoch', 'localtime'), SUM(in_tokens), SUM(out_tokens) "
            "FROM usage WHERE ts > ? GROUP BY 1 ORDER BY 1", (since,)).fetchall()
    return {"days": days,
            "per_agent": [{"agent": r[0] or "(internal)", "in_tokens": r[1] or 0,
                           "out_tokens": r[2] or 0, "calls": r[3], "cache_read": r[4] or 0} for r in per_agent],
            "per_day": [{"day": r[0], "in_tokens": r[1] or 0, "out_tokens": r[2] or 0}
                        for r in per_day]}


# ---------- rolling context summaries ----------

def _ensure_summaries():
    _db().execute(
        """CREATE TABLE IF NOT EXISTS summaries (
            agent TEXT PRIMARY KEY,
            ts REAL NOT NULL,
            upto_id INTEGER NOT NULL,
            text TEXT NOT NULL
        )"""
    )
    _db().commit()


def get_summary(agent: str) -> dict | None:
    with _lock:
        _ensure_summaries()
        r = _db().execute(
            "SELECT ts, upto_id, text FROM summaries WHERE agent = ?", (agent,)).fetchone()
    return {"ts": r[0], "upto_id": r[1], "text": r[2]} if r else None


def set_summary(agent: str, upto_id: int, text: str):
    with _lock:
        _ensure_summaries()
        _db().execute(
            "INSERT INTO summaries (agent, ts, upto_id, text) VALUES (?,?,?,?) "
            "ON CONFLICT(agent) DO UPDATE SET ts=excluded.ts, "
            "upto_id=excluded.upto_id, text=excluded.text",
            (agent, time.time(), upto_id, text))
        _db().commit()


def unsummarized_count(agent: str, upto_id: int) -> int:
    """Messages this agent can see that are newer than the summary."""
    with _lock:
        rows = _db().execute(
            """SELECT COUNT(*) FROM messages
               WHERE id > ? AND (
                 (thread = ? AND kind != 'tool')
                 OR thread LIKE 'group:%'
                 OR (kind = 'a2a' AND (sender = ? OR json_extract(meta,'$.to') = ?)))""",
            (upto_id, f"dm:{agent}", agent, agent)).fetchone()
    return rows[0]


def messages_between(agent: str, after_id: int, before_id: int) -> list[dict]:
    with _lock:
        rows = _db().execute(
            f"""SELECT {COLS} FROM messages
                WHERE id > ? AND id <= ? AND (
                  (thread = ? AND kind != 'tool')
                  OR thread LIKE 'group:%'
                  OR (kind = 'a2a' AND (sender = ? OR json_extract(meta,'$.to') = ?)))
                ORDER BY id""",
            (after_id, before_id, f"dm:{agent}", agent, agent)).fetchall()
    return [_row_to_msg(r) for r in rows]


# ---------- outward actions and checkpoints ----------

def _ensure_ledger():
    _db().execute(
        """CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL, agent TEXT NOT NULL, key TEXT NOT NULL,
            command TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT ''
        )""")
    _db().execute("CREATE INDEX IF NOT EXISTS actions_key ON actions(key, ts)")
    if "finished" not in {row[1] for row in _db().execute("PRAGMA table_info(actions)")}:
        _db().execute("ALTER TABLE actions ADD COLUMN finished REAL")
    _db().execute(
        """CREATE TABLE IF NOT EXISTS checkpoints (
            agent TEXT PRIMARY KEY, ts REAL NOT NULL, thread TEXT NOT NULL,
            text TEXT NOT NULL, actions TEXT NOT NULL DEFAULT '[]'
        )""")
    _db().commit()


def add_action(agent: str, key: str, command: str, outcome: str) -> int:
    with _lock:
        _ensure_ledger()
        cur = _db().execute(
            "INSERT INTO actions (ts, agent, key, command, outcome) VALUES (?,?,?,?,?)",
            (time.time(), agent, key, command, outcome[:1000]))
        _db().commit()
        return cur.lastrowid


def finish_action(action_id: int, outcome: str):
    with _lock:
        _ensure_ledger()
        _db().execute("UPDATE actions SET outcome = ?, finished = ? WHERE id = ?",
                      (outcome[:1000], time.time(), action_id))
        _db().commit()


def seen_once(channel: str, key: str, keep: float = 2 * 86400) -> bool:
    """True the first time a channel reports this message id; False on
    redelivery. Survives restarts."""
    with _lock:
        _db().execute(
            "CREATE TABLE IF NOT EXISTS seen (channel TEXT NOT NULL, key TEXT NOT NULL, "
            "ts REAL NOT NULL, PRIMARY KEY (channel, key))")
        _db().execute("DELETE FROM seen WHERE ts < ?", (time.time() - keep,))
        cur = _db().execute("INSERT OR IGNORE INTO seen (channel, key, ts) VALUES (?,?,?)",
                            (channel, key, time.time()))
        _db().commit()
        return cur.rowcount > 0


def actions_since(ts: float) -> list[dict]:
    with _lock:
        _ensure_ledger()
        rows = _db().execute("SELECT ts, agent, command, outcome, finished FROM actions WHERE ts > ? ORDER BY ts DESC",
                             (ts,)).fetchall()
    return [{"ts": r[0], "agent": r[1], "command": r[2], "outcome": r[3] or "", "finished": r[4]} for r in rows]


def recent_action(key: str, window: float, now: float | None = None) -> dict | None:
    now = now or time.time()
    with _lock:
        _ensure_ledger()
        row = _db().execute(
            "SELECT ts, agent, command, outcome FROM actions WHERE key = ? AND ts > ? "
            "ORDER BY ts DESC LIMIT 1", (key, now - window)).fetchone()
    return {"ts": row[0], "agent": row[1], "command": row[2], "outcome": row[3]} if row else None


# ---------- WhatsApp: the owner's own messages, read-only ----------

WA_COLS = ("id, wa_id, chat, chat_name, sender, sender_name, from_me, is_group, ts, kind, text, "
           "file, mime, file_name, size, note, quoted_id, quoted_text, raw")


def _ensure_wa():
    _db().execute(
        """CREATE TABLE IF NOT EXISTS wa_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wa_id TEXT NOT NULL,
            chat TEXT NOT NULL,
            chat_name TEXT NOT NULL DEFAULT '',
            sender TEXT NOT NULL DEFAULT '',
            sender_name TEXT NOT NULL DEFAULT '',
            from_me INTEGER NOT NULL DEFAULT 0,
            is_group INTEGER NOT NULL DEFAULT 0,
            ts REAL NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            file TEXT NOT NULL DEFAULT '',
            mime TEXT NOT NULL DEFAULT '',
            file_name TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            quoted_id TEXT NOT NULL DEFAULT '',
            quoted_text TEXT NOT NULL DEFAULT '',
            UNIQUE(chat, wa_id)
        )""")
    # raw: a history message's download details, until its file is fetched
    if "raw" not in {r[1] for r in _db().execute("PRAGMA table_info(wa_messages)").fetchall()}:
        _db().execute("ALTER TABLE wa_messages ADD COLUMN raw TEXT NOT NULL DEFAULT ''")
    _db().execute("CREATE INDEX IF NOT EXISTS idx_wa_chat_ts ON wa_messages(chat, ts)")
    _db().commit()


def _wa_row(r) -> dict:
    return dict(zip([c.strip() for c in WA_COLS.split(",")], r))


def wa_add(rows: list[dict]) -> list[int]:
    """Store messages; one already stored (same chat and WhatsApp id) is skipped.
    Returns the ids of the rows that were new."""
    cols = ("wa_id", "chat", "chat_name", "sender", "sender_name", "from_me", "is_group", "ts",
            "kind", "text", "file", "mime", "file_name", "size", "note", "quoted_id",
            "quoted_text", "raw")
    numeric = ("from_me", "is_group", "ts", "size")
    added = []
    with _lock:
        _ensure_wa()
        for row in rows:
            cur = _db().execute(
                f"INSERT OR IGNORE INTO wa_messages ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                tuple(row.get(c, 0 if c in numeric else "") for c in cols))
            if cur.rowcount:
                added.append(cur.lastrowid)
        _db().commit()
    return added


def wa_set_file(mid: int, file: str, size: int, note: str = "") -> bool:
    """A history message's file has been fetched (or could not be: note says why)."""
    with _lock:
        _ensure_wa()
        cur = _db().execute(
            "UPDATE wa_messages SET file = ?, size = CASE WHEN ? > 0 THEN ? ELSE size END, note = ?, "
            "raw = CASE WHEN ? != '' THEN '' ELSE raw END WHERE id = ?",
            (file, size, size, note, file, mid))
        _db().commit()
        return cur.rowcount > 0


def wa_get(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    with _lock:
        _ensure_wa()
        rows = _db().execute(
            f"SELECT {WA_COLS} FROM wa_messages WHERE id IN ({','.join('?' * len(ids))}) ORDER BY ts, id",
            tuple(ids)).fetchall()
    return [_wa_row(r) for r in rows]


def wa_chats(limit: int = 30, query: str = "") -> list[dict]:
    """Chats by latest message, with how many messages each has and the newest one."""
    like = f"%{query.strip().lower()}%"
    with _lock:
        _ensure_wa()
        rows = _db().execute(
            """SELECT chat, MAX(chat_name), is_group, COUNT(*), MAX(ts)
               FROM wa_messages
               GROUP BY chat HAVING LOWER(MAX(chat_name)) LIKE ? OR chat LIKE ?
               ORDER BY MAX(ts) DESC LIMIT ?""",
            (like, like, limit)).fetchall()
    return [{"chat": r[0], "chat_name": r[1] or "", "is_group": bool(r[2]), "count": r[3],
             "last_ts": r[4]} for r in rows]


def wa_messages(chat: str | None = None, since: float = 0, before_id: int | None = None,
                around_id: int | None = None, query: str = "", limit: int = 30) -> list[dict]:
    """Messages oldest first: a chat's latest, the ones around one message, or a search."""
    with _lock:
        _ensure_wa()
        if around_id is not None:
            anchor = _db().execute("SELECT chat, ts FROM wa_messages WHERE id = ?", (around_id,)).fetchone()
            if not anchor:
                return []
            half = max(1, limit // 2)
            older = _db().execute(
                f"SELECT {WA_COLS} FROM wa_messages WHERE chat = ? AND (ts < ? OR (ts = ? AND id <= ?)) "
                "ORDER BY ts DESC, id DESC LIMIT ?", (anchor[0], anchor[1], anchor[1], around_id, half + 1)).fetchall()
            newer = _db().execute(
                f"SELECT {WA_COLS} FROM wa_messages WHERE chat = ? AND (ts > ? OR (ts = ? AND id > ?)) "
                "ORDER BY ts, id LIMIT ?", (anchor[0], anchor[1], anchor[1], around_id, half)).fetchall()
            return [_wa_row(r) for r in reversed(older)] + [_wa_row(r) for r in newer]
        where, args = ["ts >= ?"], [since]
        if chat:
            where.append("chat = ?")
            args.append(chat)
        if before_id:
            where.append("id < ?")
            args.append(before_id)
        if query.strip():
            for word in query.lower().split()[:6]:
                where.append("(LOWER(text) LIKE ? OR LOWER(file_name) LIKE ? OR LOWER(sender_name) LIKE ? "
                             "OR LOWER(chat_name) LIKE ?)")
                args += [f"%{word}%"] * 4
        rows = _db().execute(
            f"SELECT {WA_COLS} FROM wa_messages WHERE {' AND '.join(where)} ORDER BY ts DESC, id DESC LIMIT ?",
            (*args, limit)).fetchall()
    return [_wa_row(r) for r in reversed(rows)]


def wa_counts() -> dict:
    with _lock:
        _ensure_wa()
        total, chats, files, newest = _db().execute(
            "SELECT COUNT(*), COUNT(DISTINCT chat), SUM(file != ''), MAX(ts) FROM wa_messages").fetchone()
    return {"messages": total or 0, "chats": chats or 0, "files": files or 0, "newest": newest}


def wa_prune(before: float) -> list[str]:
    """Forget messages older than `before`; returns their files for the caller to delete."""
    with _lock:
        _ensure_wa()
        files = [r[0] for r in _db().execute(
            "SELECT file FROM wa_messages WHERE ts < ? AND file != ''",
            (before,)).fetchall()]
        _db().execute("DELETE FROM wa_messages WHERE ts < ?", (before,))
        _db().commit()
    return files
