import db


def test_memory_roundtrip_and_fts():
    mid = db.add_memory("t", "the flux capacitor needs plutonium every morning", "fact")
    hits = db.search_memories("what does the flux capacitor need")
    assert any(m["id"] == mid for m in hits)
    assert db.delete_memory(mid)
    hits = db.search_memories("flux capacitor plutonium")
    assert not any(m["id"] == mid for m in hits)


def test_memory_sync_merge_and_tombstones():
    rows = [{"guid": "g1", "ts": 100.0, "author": "a", "kind": "fact", "text": "sync fact one"}]
    assert db.merge_memories(rows, []) == 1
    assert db.merge_memories(rows, []) == 0  # idempotent
    assert db.merge_memories([], [{"guid": "g1", "ts": 200.0}]) == 1
    # tombstone wins over a re-arriving row
    assert db.merge_memories(rows, []) == 0


def test_playbook_merge_lww():
    db.merge_playbooks([{"ts": 10, "author": "a", "site": "x.com", "title": "t", "content": "old"}])
    db.merge_playbooks([{"ts": 20, "author": "b", "site": "x.com", "title": "t", "content": "new"}])
    db.merge_playbooks([{"ts": 5, "author": "c", "site": "x.com", "title": "t", "content": "stale"}])
    books = db.find_playbooks("x.com")
    assert books[0]["content"] == "new"


def test_cron_crud_and_monitor_fields():
    jid = db.add_cron("cto", "job", "daily 09:00", "do things", "cto", 999.0,
                      monitor_cmd="echo hi")
    job = db.get_cron(jid)
    assert job["monitor_cmd"] == "echo hi" and job["status"] == "pending"
    assert not any(j["id"] == jid for j in db.due_cron(1000.0))  # pending never fires
    assert db.update_cron(jid, status="active", note="already reported X")
    assert db.get_cron(jid)["note"] == "already reported X"
    assert not any(j["id"] == jid for j in db.due_cron(500.0))   # not due yet
    assert any(j["id"] == jid for j in db.due_cron(1000.0))      # due now
    assert db.delete_cron(jid)


def test_usage_summary():
    db.add_usage("cto", "minimax", "MiniMax-M3", 100, 50)
    db.add_usage("cto", "minimax", "MiniMax-M3", 10, 5)
    s = db.usage_summary(1)
    row = next(r for r in s["per_agent"] if r["agent"] == "cto")
    assert row["in_tokens"] >= 110 and row["calls"] >= 2


def test_summaries():
    db.set_summary("cto", 42, "digest text")
    s = db.get_summary("cto")
    assert s["upto_id"] == 42 and s["text"] == "digest text"
