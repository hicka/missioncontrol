"""Hermetic test setup: server modules run against a temp data dir."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="mc-test-")
os.environ["MISSIONCONTROL_HOME"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import pytest


@pytest.fixture(autouse=True)
def _no_remote_decisions(monkeypatch):
    """Hermetic means no network, and the decision model is a network hop.

    Switching it off for the whole suite is also the point: every gate has to
    work without it, so this keeps the fallback path under test. A test that
    wants the gate stubs `decide.nouls` with the probabilities it means to try.
    """
    import decide
    monkeypatch.setattr(decide, "configured", lambda: False)
