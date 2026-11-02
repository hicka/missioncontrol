"""A reported outcome has to name something the turn actually saw.

Taken from a real failure: an agent planned a deploy, was woken by an unrelated
CI watch, and wrote the plan up as a result — quoting a deployment id four hours
stale that belonged to a different PR. It never ran a deploy or a status check.
"""
import asyncio

import pytest

import orchestrator
from orchestrator import unevidenced_ids

# The two messages, verbatim in substance, that this has to tell apart.
FABRICATED = ("Deploy `21212088` for PR #397 landed clean on `135e824bc` "
              "(Beyond UI product view rebuild). Prod now at `135e824bc`.")
GENUINE = "Deploy 21213413 for PR #397 landed clean on `135e824bc`. Prod now at `135e824bc`."

CI_WAKE = ("watch 'acmeshop master CI 35786222854 (PR #397)' resolved: the thing you "
           'were watching succeeded / completed. Latest poll output: '
           '{"conclusion":"success","status":"completed"}')
DEPLOY_WAKE = ("watch 'acmeshop deploy 21213413 (PR #397)' resolved: the thing you were "
               "watching succeeded / completed. Latest poll output: deployment 21213413: "
               "finished - Rebuild the product view on a Beyond UI record page (#397)")


def test_the_fabricated_claim_is_caught():
    """Woken by a CI watch that says nothing about any deployment."""
    assert unevidenced_ids(FABRICATED, [CI_WAKE]) == ["21212088"]


def test_the_genuine_claim_passes_on_the_watch_alone():
    """The agent ran no command; the watch that woke it is evidence enough."""
    assert unevidenced_ids(GENUINE, [DEPLOY_WAKE]) == []


def test_a_command_this_turn_is_evidence():
    tool_out = "deployment 21213413: finished - Rebuild the product view (#397)"
    assert unevidenced_ids(GENUINE, [tool_out]) == []


def test_old_history_is_not_evidence():
    """The stale id was sitting in conversation from four hours earlier, which is
    exactly where it was picked up, so history must not count."""
    assert unevidenced_ids(FABRICATED, []) == ["21212088"]


def test_a_claim_with_no_identifier_is_left_alone():
    assert unevidenced_ids("The deploy finished and prod looks healthy.", []) == []


def test_talking_about_an_id_without_claiming_an_outcome_is_fine():
    assert unevidenced_ids("I am about to watch run 35786222854.", []) == []


def test_short_numbers_are_not_identifiers():
    """PR numbers, counts and ports must not trip it."""
    assert unevidenced_ids("PR #397 merged, 36 files, port 8899 is live.", []) == []


def test_several_unseen_ids_are_all_named():
    text = "Deploy 21212088 landed and run 35786222854 passed."
    assert unevidenced_ids(text, []) == ["21212088", "35786222854"]


def test_a_partially_evidenced_claim_names_only_the_unseen_one():
    text = "Deploy 21212088 landed after run 35786222854 passed."
    assert unevidenced_ids(text, [CI_WAKE]) == ["21212088"]


# ---------- the warning reaches the owner ----------

def _orch():
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    return o






# ---------- a warning nobody reads is not a warning ----------

# Both real, from the round-ups the first version flagged. Each recollects
# something already checked; neither asserts a new outcome.
RECAP = ("Liveness check, deep night. Latest deploy 21213534 already reported clean "
         "at 03:07, no watches active.")
HEARTBEAT = ("Heartbeat at 03:11, deep night. Acmeshop prod clean at e8ce8a14, "
             "deploy 21212088 seen earlier, no urgent promises outstanding.")


def test_a_status_round_up_is_not_a_claim():
    assert unevidenced_ids(RECAP, []) == []
    assert unevidenced_ids(HEARTBEAT, []) == []


def test_the_bare_noun_deploy_does_not_trigger_it():
    """'deploy 123456789' is a reference; 'deployed' is an assertion."""
    assert unevidenced_ids("Checking deploy 21213534 now.", []) == []
    assert unevidenced_ids("Deployed 21213534.", []) == ["21213534"]


def test_an_id_in_a_different_sentence_is_not_the_claim():
    """Otherwise any message mentioning both reads as a fabrication."""
    text = "The migration finished. Separately, run 35786222854 is still queued."
    assert unevidenced_ids(text, []) == []


def test_a_claim_buried_in_a_round_up_is_still_caught():
    assert unevidenced_ids("All quiet. Deploy 21212088 landed clean just now.", []) == ["21212088"]


def test_the_fabrication_survives_the_narrowing():
    """The whole point: narrowing must not let the original through."""
    assert unevidenced_ids(FABRICATED, [CI_WAKE]) == ["21212088"]


def _report_as(o, monkeypatch, evidence, text):
    import kernel_store
    aid = list(o.agents)[0]
    row = kernel_store.session_create("worker", aid, title="t", status="working")
    worker = o.workers.worker_for(row)
    worker.evidence = [evidence]
    asyncio.run(worker.deliver_report("ready", text))
    return kernel_store.reports_of(row["id"])[0]["summary"]


def test_the_owner_sees_the_claim_and_the_warning(monkeypatch):
    """Never silence: a blocked report would leave the owner with nothing."""
    joined = _report_as(_orch(), monkeypatch, CI_WAKE, FABRICATED)
    assert "unverified" in joined and "21212088" in joined
    assert "landed clean" in joined


def test_a_sound_claim_is_posted_untouched(monkeypatch):
    assert "unverified" not in _report_as(_orch(), monkeypatch, DEPLOY_WAKE, GENUINE)
