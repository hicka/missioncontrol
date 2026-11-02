"""Which skills fill the catalogue budget every agent reads every turn."""
import skills


def _sk(slug, name, description, uses=0, last_used=0, avoid=""):
    return {"slug": slug, "name": name, "description": description, "avoid": avoid,
            "uses": uses, "last_used": last_used, "valid": True, "contract": {}}


CATALOG = [
    _sk("aaa-invoices", "Chase invoices", "when an invoice is overdue and needs chasing"),
    _sk("mmm-deploy-ios", "Ship the iOS build", "when releasing the mobile app to TestFlight"),
    _sk("zzz-rotate-asc", "Rotate the ASC key",
        "when the App Store Connect key is revoked and calls return 401"),
    _sk("bbb-standup", "Write the standup", "when the daily standup note is due"),
]


def test_the_relevant_skill_wins_its_slot_whatever_it_is_called():
    """Alphabetically 'zzz-rotate-asc' is last and would fall off the budget."""
    got = skills.rank(CATALOG, "the ASC key is revoked, calls are returning 401", 1)
    assert [s["slug"] for s in got] == ["zzz-rotate-asc"]


def test_an_unrelated_query_does_not_invent_relevance():
    got = skills.rank(CATALOG, "what is the weather", 4)
    assert len(got) == 4          # nothing dropped, just no ordering signal


def test_a_word_every_skill_shares_does_not_decide_the_order():
    shared = [_sk("a", "Deploy web", "deploy the website to production"),
              _sk("b", "Deploy api", "deploy the api to production"),
              _sk("c", "Deploy docs", "deploy the docs site and purge the CDN cache")]
    got = skills.rank(shared, "deploy and purge the CDN cache", 1)
    assert got[0]["slug"] == "c"   # 'cdn'/'purge' are rare, 'deploy' is not


def test_use_and_recency_break_ties_not_the_directory_listing():
    tied = [_sk("aaa", "One", "unrelated thing"),
            _sk("zzz", "Two", "unrelated thing", uses=15, last_used=9_000)]
    assert skills.rank(tied, "nothing matches here", 1)[0]["slug"] == "zzz"


def test_avoid_text_does_not_pull_a_skill_onto_the_task_it_forbids():
    """Matching `avoid` would surface a skill on exactly the wrong task."""
    pair = [_sk("right", "Submit with eas", "uploading a build to the store"),
            _sk("wrong", "ASC API", "calling the ASC API",
                avoid="uploading a build; use eas submit")]
    assert skills.rank(pair, "uploading a build to the store", 1)[0]["slug"] == "right"


def test_the_budget_is_respected():
    assert len(skills.rank(CATALOG, "invoice", 2)) == 2
    assert skills.rank(CATALOG, "invoice", 0) == []
    assert skills.rank([], "invoice", 5) == []
