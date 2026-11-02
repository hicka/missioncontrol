"""Durable mission, skill and tool execution records in the application's DB."""
import json
import time
import uuid

import db


def _ensure():
    c = db._db()
    c.execute("""CREATE TABLE IF NOT EXISTS missions (
        id TEXT PRIMARY KEY, ts REAL NOT NULL, updated REAL NOT NULL,
        agent TEXT NOT NULL, thread TEXT NOT NULL, objective TEXT NOT NULL,
        status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS mission_steps (
        id TEXT PRIMARY KEY, mission TEXT NOT NULL, agent TEXT NOT NULL,
        status TEXT NOT NULL, updated REAL NOT NULL, result TEXT NOT NULL DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS executions (
        id TEXT PRIMARY KEY, ts REAL NOT NULL, updated REAL NOT NULL,
        kind TEXT NOT NULL, agent TEXT NOT NULL, name TEXT NOT NULL,
        status TEXT NOT NULL, mission TEXT, skill_run TEXT,
        detail TEXT NOT NULL DEFAULT '{}', result TEXT NOT NULL DEFAULT '')""")
    # A gate's threshold is only tunable against its own history: what it said,
    # what it let through, and what that turned out to be worth.
    c.execute("""CREATE TABLE IF NOT EXISTS decisions (
        id TEXT PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,
        agent TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
        acted INTEGER NOT NULL, outcome TEXT NOT NULL DEFAULT '')""")
    # A refusal that leaves the agent with nowhere to go just moves the work to
    # the owner's own hands. The command it wanted is kept verbatim so approving
    # runs exactly what was judged, never a paraphrase of it.
    c.execute("""CREATE TABLE IF NOT EXISTS approvals (
        id TEXT PRIMARY KEY, ts REAL NOT NULL, updated REAL NOT NULL,
        agent TEXT NOT NULL, command TEXT NOT NULL, cwd TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
        result TEXT NOT NULL DEFAULT '')""")
    c.execute("CREATE INDEX IF NOT EXISTS execution_ts ON executions(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS step_mission ON mission_steps(mission)")
    c.execute("CREATE INDEX IF NOT EXISTS decision_ts ON decisions(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS approval_status ON approvals(status)")
    # Held MCP tool calls share the Inbox with held shell commands; `kind` keeps
    # an approved tool call from ever being handed to a shell, and the reverse.
    columns = {row[1] for row in c.execute("PRAGMA table_info(approvals)")}
    if "kind" not in columns:
        c.execute("ALTER TABLE approvals ADD COLUMN kind TEXT NOT NULL DEFAULT 'command'")
    # Which session is waiting on the answer, so it goes back to that one.
    if "session" not in columns:
        c.execute("ALTER TABLE approvals ADD COLUMN session TEXT")
    c.commit()


def _rows(cursor):
    keys = [col[0] for col in cursor.description]
    out = [dict(zip(keys, row)) for row in cursor.fetchall()]
    for row in out:
        if "detail" in row:
            row["detail"] = json.loads(row["detail"])
    return out


def mission_create(agent: str, thread: str, objective: str) -> dict:
    mid, now = uuid.uuid4().hex[:12], time.time()
    with db._lock:
        _ensure()
        db._db().execute("INSERT INTO missions VALUES (?,?,?,?,?,?,?,?)",
                         (mid, now, now, agent, thread, objective, "queued", ""))
        db._db().commit()
    return mission_get(mid)


def mission_get(mid: str) -> dict | None:
    with db._lock:
        _ensure()
        rows = _rows(db._db().execute("SELECT * FROM missions WHERE id=?", (mid,)))
    return rows[0] if rows else None


def missions(limit=40) -> list[dict]:
    with db._lock:
        _ensure()
        return _rows(db._db().execute("SELECT * FROM missions ORDER BY updated DESC LIMIT ?",
                                      (max(1, min(limit, 200)),)))


def mission_set(mid: str, status: str, result: str | None = None) -> dict | None:
    with db._lock:
        _ensure()
        c = db._db()
        if result is None:
            c.execute("UPDATE missions SET status=?, updated=? WHERE id=?", (status, time.time(), mid))
        else:
            c.execute("UPDATE missions SET status=?, updated=?, result=? WHERE id=?",
                      (status, time.time(), result[:4000], mid))
        c.commit()
        return (_rows(c.execute("SELECT * FROM missions WHERE id=?", (mid,))) or [None])[0]


def mission_assign(mid: str, agent: str):
    with db._lock:
        _ensure()
        c = db._db()
        c.execute("UPDATE missions SET agent=?, updated=? WHERE id=?", (agent, time.time(), mid))
        c.commit()


def step_create(mid: str, agent: str) -> str:
    sid = uuid.uuid4().hex
    with db._lock:
        _ensure()
        c = db._db()
        c.execute("INSERT INTO mission_steps VALUES (?,?,?,?,?,?)",
                  (sid, mid, agent, "queued", time.time(), ""))
        c.execute("UPDATE missions SET status='running', updated=? WHERE id=?", (time.time(), mid))
        c.commit()
    return sid


def step_finish(sid: str, status: str, result: str = "") -> dict | None:
    with db._lock:
        _ensure()
        c = db._db()
        row = c.execute("SELECT mission FROM mission_steps WHERE id=?", (sid,)).fetchone()
        if not row:
            return None
        mid = row[0]
        c.execute("UPDATE mission_steps SET status=?, updated=?, result=? WHERE id=?",
                  (status, time.time(), result[:4000], sid))
        states = {r[0] for r in c.execute("SELECT status FROM mission_steps WHERE mission=?", (mid,))}
        state = next((s for s in ("running", "queued", "waiting", "failed", "cancelled")
                      if s in states), "completed")
        c.execute("UPDATE missions SET status=?, updated=?, result=CASE WHEN ?='' THEN result ELSE ? END WHERE id=?",
                  (state, time.time(), result, result[:4000], mid))
        c.commit()
        return _rows(c.execute("SELECT * FROM missions WHERE id=?", (mid,)))[0]


def mission_resume(mid: str):
    with db._lock:
        _ensure()
        # Old interrupted/checkpoint steps are superseded by the explicit resume.
        db._db().execute("UPDATE mission_steps SET status='completed' WHERE mission=? AND status IN ('waiting','failed','cancelled')", (mid,))
        db._db().commit()


def execution_start(kind: str, agent: str, name: str, detail: dict | None = None,
                    mission: str | None = None, skill_run: str | None = None) -> dict:
    eid, now = uuid.uuid4().hex, time.time()
    with db._lock:
        _ensure()
        c = db._db()
        c.execute("INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (eid, now, now, kind, agent, name, "running", mission, skill_run,
                   json.dumps(detail or {}, ensure_ascii=False), ""))
        c.commit()
        return _rows(c.execute("SELECT * FROM executions WHERE id=?", (eid,)))[0]


def execution_finish(eid: str, status: str, result: str) -> dict:
    with db._lock:
        _ensure()
        c = db._db()
        c.execute("UPDATE executions SET status=?, updated=?, result=? WHERE id=?",
                  (status, time.time(), result[:8000], eid))
        c.commit()
        return _rows(c.execute("SELECT * FROM executions WHERE id=?", (eid,)))[0]


def executions(limit=80, kind: str | None = None, include_running=False) -> list[dict]:
    with db._lock:
        _ensure()
        where, args = (" WHERE kind=?", [kind]) if kind else ("", [])
        recent = _rows(db._db().execute("SELECT * FROM executions" + where +
                                        " ORDER BY ts DESC LIMIT ?", [*args, max(1, min(limit, 200))]))
        if not include_running:
            return recent
        # Long-running tools must remain visible after newer history rolls over.
        active_where = where + (" AND" if where else " WHERE") + " status='running'"
        running = _rows(db._db().execute("SELECT * FROM executions" + active_where, args))
        return sorted({row["id"]: row for row in [*recent, *running]}.values(),
                      key=lambda row: row["ts"], reverse=True)


def recover():
    """Interrupted actions are reviewable; never silently replay outward tools."""
    with db._lock:
        _ensure()
        c = db._db()
        now = time.time()
        c.execute("UPDATE executions SET status='interrupted', updated=?, result='Interrupted by server restart.' WHERE status='running'", (now,))
        c.execute("UPDATE mission_steps SET status='waiting', updated=? WHERE status IN ('running','queued')", (now,))
        c.execute("UPDATE missions SET status='waiting', updated=?, result='Interrupted by server restart. Resume when ready.' WHERE status IN ('running','queued')", (now,))
        c.commit()


def decision_record(kind: str, agent: str, detail: dict, acted: bool) -> str:
    """Log what a gate was told and whether it opened. Returns the record id."""
    did = uuid.uuid4().hex
    with db._lock:
        _ensure()
        c = db._db()
        c.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?)",
                  (did, time.time(), kind, agent,
                   json.dumps(detail or {}, ensure_ascii=False), int(acted), ""))
        c.commit()
    return did


def decision_outcome(did: str, outcome: str):
    """Label a decision once its cost is known: this is the calibration signal."""
    if not did:
        return
    with db._lock:
        _ensure()
        c = db._db()
        c.execute("UPDATE decisions SET outcome=? WHERE id=?", (outcome[:200], did))
        c.commit()


def decisions(limit=100, kind: str | None = None) -> list[dict]:
    with db._lock:
        _ensure()
        c = db._db()
        if kind:
            return _rows(c.execute(
                "SELECT * FROM decisions WHERE kind=? ORDER BY ts DESC LIMIT ?", (kind, limit)))
        return _rows(c.execute("SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,)))


def approval_create(agent: str, command: str, cwd: str, reason: str, kind: str = "command") -> dict:
    aid, now = uuid.uuid4().hex[:12], time.time()
    import session as sess
    current = sess.CURRENT.get()
    with db._lock:
        _ensure()
        c = db._db()
        c.execute("INSERT INTO approvals (id, ts, updated, agent, command, cwd, reason, status, result, kind, session) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (aid, now, now, agent, command, cwd, reason, "pending", "", kind, current.id if current else None))
        c.commit()
        return _rows(c.execute("SELECT * FROM approvals WHERE id=?", (aid,)))[0]


def approval_get(aid: str) -> dict | None:
    with db._lock:
        _ensure()
        rows = _rows(db._db().execute("SELECT * FROM approvals WHERE id=?", (aid,)))
    return rows[0] if rows else None


def approvals(status: str | None = None, limit=50) -> list[dict]:
    with db._lock:
        _ensure()
        c = db._db()
        if status:
            return _rows(c.execute("SELECT * FROM approvals WHERE status=? ORDER BY ts DESC LIMIT ?",
                                   (status, limit)))
        return _rows(c.execute("SELECT * FROM approvals ORDER BY ts DESC LIMIT ?", (limit,)))


def approval_claim(aid: str, status: str) -> dict | None:
    """Move one request out of pending, once. Returns it, or None if it already
    left: two clicks on approve must not run the command twice."""
    with db._lock:
        _ensure()
        c = db._db()
        cur = c.execute("UPDATE approvals SET status=?, updated=? WHERE id=? AND status='pending'",
                        (status, time.time(), aid))
        c.commit()
        if not cur.rowcount:
            return None
        return _rows(c.execute("SELECT * FROM approvals WHERE id=?", (aid,)))[0]


def approval_finish(aid: str, result: str):
    with db._lock:
        _ensure()
        c = db._db()
        c.execute("UPDATE approvals SET status='ran', updated=?, result=? WHERE id=?",
                  (time.time(), result[:8000], aid))
        c.commit()


def _intention(command: str) -> str:
    """A command without its flags. `gh pr merge 503 --squash` and `--merge` are one
    intention worded two ways; 503 and 504 are two, however alike they look."""
    return " ".join(word for word in (command or "").split() if not word.startswith("-"))


def approval_pending_like(agent: str, command: str, kind: str = "command",
                          session: str | None = None) -> dict | None:
    """A request from this session already waiting for this same command. Near enough
    is not enough: a merge of one PR read as "still held" on another's held merge, and
    so never got a request of its own or any answer."""
    if session is None:
        import session as sess
        current = sess.CURRENT.get()
        session = current.id if current else None
    wanted = _intention(command)
    for row in approvals(status="pending"):
        if (row["agent"] == agent and row.get("kind", "command") == kind and row.get("session") == session
                and _intention(row["command"]) == wanted):
            return row
    return None
