"""Agent C tests never read the COMMITTED price snapshot (egoquant/agents/market_intel/data/), so their
results do not depend on whether that file exists. Tests of the snapshot pass their own file and
use_snapshot=True (tests/agent_c/test_price_snapshot.py)."""

import pytest


@pytest.fixture(autouse=True)
def _no_committed_snapshot(monkeypatch):
    monkeypatch.setenv("EGOQUANT_PRICE_SNAPSHOT", "off")


@pytest.fixture(autouse=True)
def _no_tiingo_cooldown():
    """A 429 in one test must not put the next test's fake key in a cooldown (sources.TIINGO_COOLDOWN_SECONDS)."""
    from egoquant.agents.market_intel import sources
    sources._TIINGO_COOLDOWN.clear()
    sources._TIINGO_LAST_429.clear()
    yield
    sources._TIINGO_COOLDOWN.clear()
    sources._TIINGO_LAST_429.clear()
