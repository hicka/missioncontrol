"""Reaching back through chat history: paging, jumping, and search."""
import db


def _seed(thread="dm:hist", n=25):
    return [db.add_message(thread, "user" if i % 2 else "hist", f"history line {i}")
            for i in range(n)]


def test_thread_view_pages_backwards_without_gaps_or_repeats():
    msgs = _seed()
    seen, cursor = [], None
    for _ in range(10):
        page = db.thread_view("dm:hist", "hist", limit=7, before=cursor)
        if not page:
            break
        seen = page + seen
        cursor = (page[0]["ts"], page[0]["id"])
    ids = [m["id"] for m in seen]
    assert ids == sorted(ids)                       # oldest first, still in order
    assert len(ids) == len(set(ids))                # no message served twice
    assert set(ids) >= {m["id"] for m in msgs}      # and none skipped


def test_page_boundary_holds_when_timestamps_collide():
    """Two messages written in the same tick must not hide each other: the
    cursor carries the row id, not just the timestamp."""
    rows = [db.add_message("dm:tie", "tie", f"same tick {i}") for i in range(4)]
    with db._lock:
        db._db().execute("UPDATE messages SET ts = 5000.0 WHERE id IN (?,?,?,?)",
                         tuple(r["id"] for r in rows))
        db._db().commit()
    first = db.thread_view("dm:tie", None, limit=2)
    second = db.thread_view("dm:tie", None, limit=2,
                            before=(first[0]["ts"], first[0]["id"]))
    assert [m["id"] for m in second + first] == sorted(r["id"] for r in rows)


def test_search_finds_a_message_older_than_one_page():
    db.add_message("dm:hist", "hist", "the quarterly osprey report is finished")
    for i in range(40):
        db.add_message("dm:hist", "hist", f"filler {i}")
    recent = db.thread_view("dm:hist", "hist", limit=20)
    assert not any("osprey" in m["text"] for m in recent)   # off the newest page
    hits = db.search_messages("osprey")
    assert any("osprey" in m["text"] for m in hits)         # but findable


def test_search_requires_every_word_and_matches_a_prefix():
    db.add_message("dm:hist", "hist", "renew the harbour permit on friday")
    db.add_message("dm:hist", "hist", "renew the parking permit on monday")
    assert len(db.search_messages("harbour permit")) == 1
    assert len(db.search_messages("renew permit")) == 2
    assert len(db.search_messages("harb")) == 1              # still typing
    assert db.search_messages("   ") == []
    assert db.search_messages('"') == []                     # no FTS syntax error


def test_get_message_resolves_a_search_hit_for_jumping():
    m = db.add_message("dm:hist", "hist", "a message worth jumping to")
    assert db.get_message(m["id"])["text"] == "a message worth jumping to"
    assert db.get_message(10_000_000) is None
