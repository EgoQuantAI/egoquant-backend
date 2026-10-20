"""Demo-proofing: a failed live feed may switch the run to SYNTHETIC prices, but only when asked, and
always labeled synthetic (never passed off as real data). The real-price snapshot, which comes first in
the chain, is switched off here; tests/agent_c/test_price_snapshot.py covers it."""

from datetime import date

import pytest

from egoquant.agents.market_intel import InvestorInterests, MarketIntelligenceAgent, PriceSourceError

AS_OF = date(2026, 9, 30)


class FailingSource:
    name = "tiingo"
    data_source = "historical"

    def fetch_adjusted_close(self, tickers, start, end):
        raise PriceSourceError("Tiingo rate limit reached (HTTP 429); try again later")


def agent(**kw):
    interests = InvestorInterests(investor_id="t", interest_areas=["stocks", "bonds"], max_r_level=3)
    return MarketIntelligenceAgent(interests, source=FailingSource(), use_snapshot=False, **kw)


def test_without_fallback_a_failed_feed_is_an_error():
    with pytest.raises(PriceSourceError):
        agent(fallback_to_synthetic=False).get_market_snapshot(None, AS_OF, 300)


def test_fallback_switches_the_whole_run_to_labeled_synthetic_prices():
    c = agent(fallback_to_synthetic=True)
    snapshot = c.get_market_snapshot(None, AS_OF, 300)
    assert snapshot.data_source == "synthetic"
    assert c.last_report.data_source == "synthetic" and "429" in c.last_report.fallback_reason
    again = c.get_market_snapshot(None, AS_OF, 600)          # later calls stay synthetic: no mixing
    assert again.data_source == "synthetic"


def test_fallback_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("EGOQUANT_DATA_FALLBACK", "synthetic")
    assert agent().get_market_snapshot(None, AS_OF, 300).data_source == "synthetic"
    monkeypatch.setenv("EGOQUANT_DATA_FALLBACK", "")
    with pytest.raises(PriceSourceError):
        agent().get_market_snapshot(None, AS_OF, 300)


def test_missing_key_at_setup_falls_back_only_when_allowed(monkeypatch):
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    interests = InvestorInterests(investor_id="t", interest_areas=["stocks"], max_r_level=3)
    with pytest.raises(PriceSourceError):
        MarketIntelligenceAgent(interests, fallback_to_synthetic=False, use_snapshot=False)
    c = MarketIntelligenceAgent(interests, fallback_to_synthetic=True, use_snapshot=False)
    assert c.source.data_source == "synthetic" and "TIINGO_API_KEY" in c.fallback_reason
