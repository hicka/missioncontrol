"""An agent that says it will be woken, and owns no watch, has actually stopped.

From a real stall: the CTO was told by a teammate that a watch was armed on
#398's CI, ended its turn to wait for it, and was never woken — watch results go
to the agent that armed the watch and to nobody else. Every step succeeded, so
nothing anywhere reported a problem; the merge and deploy simply never happened.
"""
import asyncio

import pytest

import db
import orchestrator
from orchestrator import awaiting_a_wake


# The real sentences, from both sides of that exchange.
CTO_STALL = ("Posted. Now I wait for the watch to wake me on CI completion + APPROVE, "
             "then merge, deploy, and report.")
CTO_PLAN = "Steward's watch is armed on the new head; deploy fires on the green wake."
STEWARD_OK = ("Deploy 21213413 for PR #397 is running on master 135e824bc. "
              "Watcher armed, will report on its own.")
NOT_WAITING = "PR #398 is merged at 79f5605 and the deploy finished. Prod is current."


def test_the_real_stall_sentence_is_recognised():
    assert awaiting_a_wake(CTO_STALL)
    assert awaiting_a_wake(CTO_PLAN)


def test_an_agent_reporting_its_own_armed_watch_is_the_same_shape():
    """Identical wording; what separates them is whether a watch is owned."""
    assert awaiting_a_wake(STEWARD_OK)


def test_a_finished_report_is_not_a_wait():
    assert not awaiting_a_wake(NOT_WAITING)
    assert not awaiting_a_wake("I woke up and did the merge.")
