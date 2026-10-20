"""A slow live feed switches the WHOLE build to the committed snapshot (live budget 15 s of waiting, or two requests over 8 s),
and EGOQUANT_PRICE_MODE=snapshot reads the snapshot directly with no live request of any kind. A fake clock stands in for
the waiting (nothing here sleeps); a feed that fails the test if it is called stands in for the network."""

from __future__ import annotations

from datetime import date

import pytest

from egoquant import settings
from egoquant.agents.market_intel import InvestorInterests, MarketIntelligenceAgent
from egoquant.agents.market_intel import agent as agent_module
from egoquant.agents.market_intel import directory as directory_module
from egoquant.pipeline import run_pipeline
from tests.agent_c.test_price_snapshot import SNAP_AS_OF, RealLookingSource, snapshot_file  # noqa: F401  (fixture)
from tests.helpers import make_answers


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TimedFeed:
    """Answers every request correctly; each takes `seconds` on the fake clock. Not a SyntheticPriceSource itself
    (Agent C never replaces synthetic prices by the snapshot)."""

    name = "tiingo"
    data_source = "historical"

    def __init__(self, clock: Clock, seconds: float, costs_a_request: bool = True):
        self.clock, self.seconds, self.costs = clock, seconds, costs_a_request
        self.requests, self.inner = 0, RealLookingSource()

    def needs_request(self, ticker, start, end):
        return self.costs

    def fetch_adjusted_close(self, tickers, start, end):
        self.requests += 1
        self.clock.now += self.seconds
        return self.inner.fetch_adjusted_close(tickers, start, end)


def agent(snapshot_file, feed, clock, **kw):
    interests = InvestorInterests(investor_id="t", interest_areas=["stocks", "bonds", "tech"], max_r_level=4)
    kw.setdefault("use_snapshot", True)
    return MarketIntelligenceAgent(interests, source=feed, snapshot_path=snapshot_file, clock=clock, directory=None, **kw)


# ---------------------------------------------------------------------------------------- the whole-run slow-feed guard
def test_requests_that_add_up_past_the_live_budget_switch_the_whole_run_to_the_snapshot(snapshot_file):
    clock = Clock()
    feed = TimedFeed(clock, seconds=4.0)                       # none is "slow" alone, but 4 x 4 s = 16 s > 15 s
    c = agent(snapshot_file, feed, clock, live_budget_seconds=15.0, slow_request_seconds=8.0)
    c.fetch_universe(SNAP_AS_OF, 300)
    assert feed.requests == 4                                  # stopped right after the request that broke the budget
    assert c.source.name == "snapshot" and "answered too slowly" in (c.fallback_reason or "")
    assert c.last_fetch.fetched                                # the build still delivered a universe, on the snapshot


def test_two_slow_requests_switch_the_run_even_inside_the_live_budget(snapshot_file):
    clock = Clock()
    feed = TimedFeed(clock, seconds=9.0)
    c = agent(snapshot_file, feed, clock, live_budget_seconds=1_000.0, slow_request_seconds=8.0)
    c.fetch_universe(SNAP_AS_OF, 300)
    assert feed.requests == 2 and c.source.name == "snapshot"


def test_one_slow_request_alone_does_not_switch(snapshot_file):
    clock = Clock()
    feed = TimedFeed(clock, seconds=0.0)
    c = agent(snapshot_file, feed, clock, live_budget_seconds=1_000.0, slow_request_seconds=8.0)
    original = feed.fetch_adjusted_close
    calls = []

    def one_slow(tickers, start, end):
        calls.append(tickers)
        clock.now += 9.0 if len(calls) == 1 else 0.1
        return original(tickers, start, end)

    feed.fetch_adjusted_close = one_slow
    c.fetch_universe(SNAP_AS_OF, 300)
    assert c.source is feed and c.fallback_reason is None


def test_a_fast_feed_is_left_alone(snapshot_file):
    clock = Clock()
    feed = TimedFeed(clock, seconds=0.2)
    c = agent(snapshot_file, feed, clock, live_budget_seconds=15.0, slow_request_seconds=8.0)
    c.fetch_universe(SNAP_AS_OF, 300)
    assert c.source is feed and c.fallback_reason is None and feed.requests > 8


def test_prices_that_cost_no_request_never_count_as_waiting(snapshot_file):
    clock = Clock()
    feed = TimedFeed(clock, seconds=60.0, costs_a_request=False)       # "cached": needs_request is False
    c = agent(snapshot_file, feed, clock, live_budget_seconds=15.0, slow_request_seconds=8.0)
    c.fetch_universe(SNAP_AS_OF, 300)
    assert c.source is feed and c.fallback_reason is None


def test_without_a_snapshot_to_switch_to_a_slow_answer_is_still_accepted(snapshot_file):
    clock = Clock()
    feed = TimedFeed(clock, seconds=30.0)
    c = agent(snapshot_file, feed, clock, live_budget_seconds=15.0, slow_request_seconds=8.0, use_snapshot=False,
              fallback_to_synthetic=False)
    report = c.fetch_universe(SNAP_AS_OF, 300)
    assert c.source is feed and report.fetched                         # slow, but better than no portfolio at all


def test_the_limits_come_from_settings_and_the_environment(monkeypatch):
    monkeypatch.delenv("EGOQUANT_LIVE_BUDGET_SECONDS", raising=False)
    monkeypatch.delenv("EGOQUANT_SLOW_REQUEST_SECONDS", raising=False)
    assert settings.live_budget_seconds() == 15.0 and settings.slow_request_seconds() == 8.0
    monkeypatch.setenv("EGOQUANT_LIVE_BUDGET_SECONDS", "6")
    monkeypatch.setenv("EGOQUANT_SLOW_REQUEST_SECONDS", "x")
    assert settings.live_budget_seconds() == 6.0 and settings.slow_request_seconds() == 8.0


# ---------------------------------------------------------------------------------------- snapshot price mode
def forbid_the_network(monkeypatch):
    def no(*a, **k):
        raise AssertionError("a live request was attempted")
    monkeypatch.setattr(agent_module, "build_live_source", no)
    monkeypatch.setattr(agent_module, "BinanceMarket", no)
    for name in ("fetch_tiingo_iex", "fetch_nasdaq_stocks", "fetch_nasdaq_symbols", "fetch_tiingo_tickers"):
        monkeypatch.setattr(directory_module, name, no)


def test_snapshot_mode_makes_no_live_request_and_labels_the_snapshot_calmly(monkeypatch):
    monkeypatch.setenv("EGOQUANT_PRICE_MODE", "snapshot")
    monkeypatch.setenv("EGOQUANT_PRICE_SNAPSHOT", "on")
    forbid_the_network(monkeypatch)
    monkeypatch.setattr(agent_module.date, "today", classmethod(lambda cls: date(2026, 10, 19)), raising=False) \
        if hasattr(agent_module.date, "today") and False else None
    interests = InvestorInterests(investor_id="t", interest_areas=["stocks", "bonds", "crypto"], max_r_level=5,
                                  trading_platforms=["binance", "interactive_brokers"])
    c = MarketIntelligenceAgent(interests, max_snapshot_age_days=10_000)         # no source given: the real default path
    assert c.snapshot_first and c.source.name == "snapshot" and c.fallback_reason is None
    c.fetch_universe(date(2026, 10, 16), 504, history_days=5040)
    c.get_market_snapshot(None, date(2026, 10, 16), 504)
    r = c.last_report
    assert r.snapshot_first and r.fallback_reason is None and r.source == "snapshot"
    assert r.price_note.startswith("saved snapshot of real prices as of 16 Oct 2026") and "Yahoo Finance" in r.price_note
    assert "alarm" not in r.price_note.lower() and "unavailable" not in r.price_note.lower()


def test_the_default_mode_is_live_and_a_fallback_keeps_its_old_label(snapshot_file, monkeypatch):
    monkeypatch.delenv("EGOQUANT_PRICE_MODE", raising=False)
    assert settings.price_mode() == "live"
    monkeypatch.setenv("EGOQUANT_PRICE_MODE", "SNAPSHOT ")
    assert settings.price_mode() == "snapshot"
    monkeypatch.setenv("EGOQUANT_PRICE_MODE", "snapshots")
    assert settings.price_mode() == "live"
    clock = Clock()
    c = agent(snapshot_file, TimedFeed(clock, seconds=9.0), clock, live_budget_seconds=1_000.0, slow_request_seconds=8.0)
    c.fetch_universe(SNAP_AS_OF, 300)
    c.get_market_snapshot(None, SNAP_AS_OF, 300)
    assert not c.last_report.snapshot_first and c.last_report.fallback_reason
    assert c.last_report.price_note.startswith("real prices as of 2026-09-30, cached")


def test_snapshot_mode_falls_back_to_live_when_the_snapshot_is_off_or_too_old(monkeypatch):
    monkeypatch.setenv("EGOQUANT_PRICE_MODE", "snapshot")
    monkeypatch.setenv("EGOQUANT_PRICE_SNAPSHOT", "on")                  # tests/agent_c/conftest.py switches it off
    sentinel = TimedFeed(Clock(), 0.0)
    monkeypatch.setattr(agent_module, "build_live_source", lambda *a, **k: sentinel)
    monkeypatch.setattr(agent_module, "BinanceMarket", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network")))
    interests = InvestorInterests(investor_id="t", interest_areas=["stocks", "bonds"], max_r_level=3)
    old = MarketIntelligenceAgent(interests, directory=None, max_snapshot_age_days=1)        # the snapshot is older than a day
    assert not old.snapshot_first and old.source is sentinel and any("more than 1 days ago" in n for n in old.notes)
    off = MarketIntelligenceAgent(interests, directory=None, use_snapshot=False)
    assert not off.snapshot_first and off.source is sentinel and any("switched off" in n for n in off.notes)


def test_snapshot_mode_switches_the_background_price_builder_and_the_directory_download_off(monkeypatch, tmp_path):
    monkeypatch.setenv("EGOQUANT_WARM", "on")
    monkeypatch.setenv("EGOQUANT_PRICE_MODE", "snapshot")
    assert not settings.warm_enabled()
    monkeypatch.setenv("EGOQUANT_PRICE_MODE", "live")
    assert settings.warm_enabled()
    monkeypatch.setenv("EGOQUANT_PRICE_MODE", "snapshot")
    forbid_the_network(monkeypatch)
    assert directory_module.load_directory(path=tmp_path / "directory.json", refresh=True) is None      # nothing saved, no request


def test_a_pipeline_run_in_snapshot_mode_carries_the_calm_banner_not_the_alarm(monkeypatch):
    monkeypatch.setenv("EGOQUANT_PRICE_MODE", "snapshot")
    monkeypatch.setenv("EGOQUANT_PRICE_SNAPSHOT", "on")
    forbid_the_network(monkeypatch)
    out = run_pipeline(make_answers(gl_score=25), investor_id="t", goal_type="growth", horizon_years=10,
                       as_of=date(2026, 10, 16), interest_areas=["stocks", "bonds"])
    banners = " ".join(out.report.banners)
    assert "Prices: saved snapshot of real prices as of 16 Oct 2026" in banners and "Markets after 16 Oct 2026" in banners
    assert "LIVE PRICES UNAVAILABLE" not in banners
    assert out.market_report.snapshot_first and out.market_report.fallback_reason is None
