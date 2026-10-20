"""get_market_snapshot is remembered inside one agent: the same ask (assets, as_of, lookback, price source and memory) is
answered without cleaning the whole price frame again, with the same numbers as a fresh computation; any other ask misses.
SYNTHETIC prices, no network."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from egoquant.agents.market_intel import MarketIntelligenceAgent, SyntheticPriceSource
from tests.agent_c.test_market_intel import AS_OF, c_agent, interests


def make_agent(source=None):
    agent = c_agent(interests("stocks", "bonds", "technology"), source=source or SyntheticPriceSource())
    agent.fetch_universe(AS_OF, 504, history_days=2520)
    return agent


@pytest.fixture
def counted(monkeypatch):
    """How many times the expensive clean-up of the price frame runs."""
    calls = {"clean": 0}
    original = MarketIntelligenceAgent._clean

    def spy(self, *a, **k):
        calls["clean"] += 1
        return original(self, *a, **k)

    monkeypatch.setattr(MarketIntelligenceAgent, "_clean", spy)
    return calls


def same(a, b):
    pd.testing.assert_frame_equal(a.prices, b.prices)
    assert a.assets == b.assets and a.as_of == b.as_of
    assert a.risk_free_rate_annual == b.risk_free_rate_annual and a.data_source == b.data_source


def test_the_same_ask_again_is_answered_from_memory_and_equals_a_fresh_snapshot(counted):
    agent = make_agent()
    first = agent.get_market_snapshot(agent.selection.tickers, AS_OF, 504)
    after_first = counted["clean"]
    second = agent.get_market_snapshot(agent.selection.tickers, AS_OF, 504)
    assert counted["clean"] == after_first                                       # no second clean-up
    same(first, second)
    fresh = make_agent().get_market_snapshot(None, AS_OF, 504)                  # an agent that never saw the ask
    same(second, fresh)


def test_none_and_the_full_universe_are_the_same_ask(counted):
    agent = make_agent()
    a = agent.get_market_snapshot(None, AS_OF, 504)
    n = counted["clean"]
    b = agent.get_market_snapshot(list(agent.selection.tickers), AS_OF, 504)
    c = agent.get_market_snapshot(list(reversed(agent.selection.tickers)), AS_OF, 504)    # order of the ask does not matter either
    assert counted["clean"] == n
    same(a, b)
    same(a, c)


def test_a_different_ask_misses(counted):
    agent = make_agent()
    base = agent.get_market_snapshot(None, AS_OF, 504)
    n = counted["clean"]
    shorter = agent.get_market_snapshot(None, AS_OF, 252)
    assert counted["clean"] == n + 1 and len(shorter.prices) < len(base.prices)
    subset = agent.selection.tickers[:3]
    few = agent.get_market_snapshot(subset, AS_OF, 504)
    assert counted["clean"] == n + 2 and list(few.prices.columns) == [t for t in agent.selection.tickers if t in subset]
    earlier = agent.get_market_snapshot(None, AS_OF - timedelta(days=10), 504)
    assert counted["clean"] == n + 3 and earlier.prices.index.max() < base.prices.index.max()
    again = agent.get_market_snapshot(None, AS_OF, 504)                         # the first ask is still remembered
    assert counted["clean"] == n + 3
    same(base, again)


def test_a_caller_that_changes_what_it_got_cannot_change_the_next_answer(counted):
    agent = make_agent()
    first = agent.get_market_snapshot(None, AS_OF, 504)
    expected = first.prices.copy()
    first.prices.iloc[:, :] = 0.0
    first.assets[0].expense_ratio = 0.5
    second = agent.get_market_snapshot(None, AS_OF, 504)
    pd.testing.assert_frame_equal(second.prices, expected)
    assert second.assets[0].expense_ratio != 0.5


def test_the_report_of_the_remembered_ask_is_the_one_the_caller_sees(counted):
    agent = make_agent()
    agent.get_market_snapshot(None, AS_OF, 504)
    report = agent.last_report
    agent.get_market_snapshot(agent.selection.tickers[:3], AS_OF, 504)           # a different ask replaces last_report
    assert agent.last_report is not report
    agent.get_market_snapshot(None, AS_OF, 504)                                  # and asking the first again restores it
    assert agent.last_report is report


def test_a_new_price_source_or_new_prices_invalidate_the_memory(counted):
    agent = make_agent()
    agent.get_market_snapshot(None, AS_OF, 504)
    n = counted["clean"]
    agent.source = SyntheticPriceSource(seed=99)                                 # e.g. the run switched to another source
    agent._cache = None
    other = agent.get_market_snapshot(None, AS_OF, 504)
    assert counted["clean"] == n + 1
    fresh = MarketIntelligenceAgent(interests("stocks", "bonds", "technology"), source=SyntheticPriceSource(seed=99))
    fresh.fetch_universe(AS_OF, 504, history_days=2520)
    same(other, fresh.get_market_snapshot(None, AS_OF, 504))


def test_a_failed_ask_is_not_remembered(counted):
    agent = make_agent()
    with pytest.raises(ValueError):
        agent.get_market_snapshot(["NOPE"], AS_OF, 504)
    assert not agent._snapshot_memo


def test_the_memory_holds_a_few_asks_only(counted):
    agent = make_agent()
    for days in range(1, 9):
        agent.get_market_snapshot(None, AS_OF - timedelta(days=days), 504)
    assert len(agent._snapshot_memo) <= agent.SNAPSHOT_MEMO_SIZE


@pytest.mark.parametrize("memo_on", [True, False])
def test_a_pipeline_run_asks_for_the_same_snapshot_only_once(monkeypatch, memo_on):
    """The pipeline's own ask and Agent A's ask are the same universe: one whole-universe clean-up with the memory, two
    without it (the control that shows this test would notice a missing memory)."""
    from egoquant.demo.personas import PERSONAS, behavioral_agent_for
    from egoquant.pipeline import run_pipeline

    calls = {"clean": 0}
    original = MarketIntelligenceAgent._clean

    def spy(self, raw, requested, *a, **k):
        if len(requested) > 3:                                                   # the whole-universe clean-ups (not per candidate)
            calls["clean"] += 1
        return original(self, raw, requested, *a, **k)

    monkeypatch.setattr(MarketIntelligenceAgent, "_clean", spy)
    if not memo_on:
        counter = iter(range(10_000))
        monkeypatch.setattr(MarketIntelligenceAgent, "_memo_key", lambda self, *a: next(counter))   # every ask is new
    p = PERSONAS["balanced_saver"]
    out = run_pipeline(p.answers(), investor_id=p.investor_id, interest_areas=None, preferences=p.preference_answers(),
                       goal_type=p.goal_type, horizon_years=p.horizon_years, as_of=date(2026, 9, 30),
                       trading_platforms=p.trading_platforms, behavioral_agent=behavioral_agent_for(p),
                       price_source=SyntheticPriceSource(), min_cash_buffer=p.min_cash_buffer, liquidity_need=p.liquidity_need)
    assert out.approved is not None and out.agent_c.last_report is not None
    assert calls["clean"] == (1 if memo_on else 2)


def test_the_memory_changes_no_number_of_a_whole_pipeline_run(monkeypatch):
    """The same investor with the memory on and with every ask treated as new: identical proposal, review and report."""
    from egoquant.demo.personas import PERSONAS, behavioral_agent_for
    from egoquant.pipeline import run_pipeline

    def run():
        p = PERSONAS["growth_investor"]
        return run_pipeline(p.answers(), investor_id=p.investor_id, interest_areas=None, preferences=p.preference_answers(),
                            goal_type=p.goal_type, horizon_years=p.horizon_years, as_of=date(2026, 9, 30),
                            trading_platforms=p.trading_platforms, behavioral_agent=behavioral_agent_for(p),
                            price_source=SyntheticPriceSource(), min_cash_buffer=p.min_cash_buffer, liquidity_need=p.liquidity_need)

    with_memory = run()
    counter = iter(range(10_000))
    monkeypatch.setattr(MarketIntelligenceAgent, "_memo_key", lambda self, *a: next(counter))
    without = run()
    assert with_memory.result.final.model_dump_json() == without.result.final.model_dump_json()
    assert with_memory.review.model_dump_json() == without.review.model_dump_json()
    assert with_memory.report.model_dump_json() == without.report.model_dump_json()
    assert with_memory.market_report.model_dump_json() == without.market_report.model_dump_json()
