"""Telling someone being addressed from a name that merely appears."""
import asyncio

import orchestrator


def _orch():
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    return o






# ---------- being addressed, versus merely being spelled ----------

def test_a_name_inside_a_longer_word_is_not_a_mention():
    """An agent called Max was being picked out of 'maximum'."""
    pattern = orchestrator._mention("Max", "max")
    assert not pattern.search("run it at maximum speed")
    assert not pattern.search("maximise the throughput")
    assert pattern.search("Max, can you look at this")
    assert pattern.search("@Max please check")
    assert pattern.search("ask Max.")


def test_a_bare_id_is_not_a_mention_but_an_at_id_is():
    """Ids are short words: 'ops' turns up in sentences about nobody."""
    pattern = orchestrator._mention("Operations", "ops")
    assert not pattern.search("this is a devops problem")
    assert not pattern.search("our ops are fine")
    assert pattern.search("@ops can you take this")


# ---------- routing the unaddressed ----------
