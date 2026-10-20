"""Agent C's time limit for NEW price requests (fetch_universe(max_seconds=...)): a slow or rate-limited feed can never
hold a web build for minutes. Core funds and holdings are always priced; cached prices are always used; the funds
left out say why. SYNTHETIC prices, no network.

Deterministic: a fake clock stands in for the waiting. The slow feed advances it instead of sleeping, and the agent's
deadline reads it (`agent._time` is replaced by an object with `monotonic()`), so nothing here depends on the speed of
the machine."""

import pytest

from egoquant.agents.market_intel import agent as agent_module
from tests.agent_c.test_market_intel import AS_OF, CORE, _BudgetSource, c_agent, interests


class FakeClock:
    """`time` as Agent C sees it: monotonic() is whatever the slow feed has advanced it to."""

    def __init__(self):
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(agent_module, "_time", fake)                         # the max_seconds deadline reads this
    return fake


class _SlowSource(_BudgetSource):
    """Every NEW request takes `delay` seconds of the fake clock (a slow live feed)."""

    def __init__(self, delay: float, clock: FakeClock, **kwargs):
        super().__init__(**kwargs)
        self.delay, self.clock = delay, clock

    def fetch_adjusted_close(self, tickers, start, end):
        if any(t not in self.cached for t in tickers):
            self.clock.advance(self.delay)
        return super().fetch_adjusted_close(tickers, start, end)


def _agent(src, clock):
    return c_agent(interests("technology", "healthcare", instrument_style="individual"), source=src,
                   clock=clock.monotonic)                                    # the live-budget guard reads the same clock


def test_no_new_request_after_the_time_limit_but_core_funds_are_priced(clock):
    delay = 0.05
    src = _SlowSource(delay, clock)
    n_core = len(CORE)
    # Room for the core funds (they are always priced) and exactly two optional requests: the third would start at
    # (n_core + 2) * delay, after the deadline of (n_core + 1.5) * delay.
    max_seconds = (n_core + 1.5) * delay
    agent = _agent(src, clock)
    agent.fetch_universe(AS_OF, 252, budget={"us_listed": 100}, max_seconds=max_seconds)
    assert set(CORE) <= set(agent.selection.tickers)                         # core funds: always priced
    optional = [t for t in src.calls if t not in CORE]
    assert len(optional) == 2                                                # stopped right at the time limit
    assert clock.now == pytest.approx(1_000.0 + (n_core + 2) * delay)       # and nothing was waited for after that
    late = [e for e in agent.selection.not_fetched if "not fetched in time" in e.reason]
    assert late and all("used once the price cache has it" in e.reason for e in late)


def test_core_funds_alone_can_use_up_the_time_and_then_no_optional_request_is_made(clock):
    src = _SlowSource(0.05, clock)
    agent = _agent(src, clock)
    agent.fetch_universe(AS_OF, 252, budget={"us_listed": 100}, max_seconds=0.12)      # < 4 core funds x 0.05 s
    assert set(CORE) <= set(agent.selection.tickers)
    assert not [t for t in src.calls if t not in CORE]


def test_cached_prices_are_still_used_after_the_time_limit(clock):
    src = _SlowSource(0.05, clock, cached=["AAPL", "LLY"])
    agent = _agent(src, clock)
    agent.fetch_universe(AS_OF, 252, budget={"us_listed": 100}, max_seconds=0.0)
    assert {"AAPL", "LLY"} <= set(agent.selection.tickers)
    assert not [t for t in src.calls if t not in CORE]                     # no optional request at all


def test_no_time_limit_is_the_old_behaviour(clock):
    a, b = _SlowSource(0.0, clock), _SlowSource(0.0, clock)
    one = _agent(a, clock)
    two = _agent(b, clock)
    one.fetch_universe(AS_OF, 252, budget={"us_listed": 12})
    two.fetch_universe(AS_OF, 252, budget={"us_listed": 12}, max_seconds=None)
    assert one.selection.tickers == two.selection.tickers and a.calls == b.calls
