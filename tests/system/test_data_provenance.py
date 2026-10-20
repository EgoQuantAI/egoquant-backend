"""After the committed snapshot replaced the live feed, every place that tells the investor where prices come from
names the real provider and the day the prices run to (not the request's date): the report banner, the API fields
the screen reads, the advisor's facts, and the trade-check notes. With a healthy source nothing changes."""

from __future__ import annotations

from datetime import date

import egoquant_routes
from egoquant.advisor.context import build_portfolio_facts
from egoquant.agents.market_intel import PriceSourceError, SyntheticPriceSource
from egoquant.pipeline import run_pipeline
from tests.helpers import make_answers

AS_OF = date(2026, 10, 18)                  # a Sunday: the request's date is later than the snapshot's (2026-10-16)


class DeadFeed:
    name = "tiingo"
    data_source = "historical"

    def fetch_adjusted_close(self, tickers, start, end):
        raise PriceSourceError("Tiingo could not be reached (network error: ConnectTimeout)")


def _run(monkeypatch, source):
    monkeypatch.setenv("EGOQUANT_PRICE_SNAPSHOT", "on")
    return run_pipeline(make_answers(gl_score=25), investor_id="t", goal_type="growth", horizon_years=10, as_of=AS_OF,
                        price_source=source, interest_areas=["stocks", "bonds"])


def test_after_a_snapshot_fallback_every_place_names_the_provider_and_the_snapshot_day(monkeypatch):
    run = _run(monkeypatch, DeadFeed())
    out = egoquant_routes._portfolio_out(run.profile, AS_OF, run, None, None)
    assert out.as_of == AS_OF and out.prices_as_of == date(2026, 10, 16)             # the request's day vs the prices'
    assert "Yahoo Finance" in out.price_note and "2026-10-16" in out.price_note
    assert out.data_source == "historical"                                           # still real prices
    banners = " ".join(out.report["banners"])
    assert "LIVE PRICES UNAVAILABLE" in banners and "Yahoo Finance" in banners and "2026-10-16" in banners
    facts = build_portfolio_facts(out.model_dump(mode="json"))                       # what the advisor is told
    assert facts["prices_as_of"] == "2026-10-16" and "Yahoo Finance" in facts["price_note"]
    note = egoquant_routes._price_source_note(run)                                   # the trade check's note
    assert note and "Yahoo Finance" in note and "2026-10-16" in note and "Live prices were unavailable" in note


def test_with_a_healthy_source_nothing_claims_a_snapshot(monkeypatch):
    run = _run(monkeypatch, SyntheticPriceSource())
    out = egoquant_routes._portfolio_out(run.profile, AS_OF, run, None, None)
    assert out.prices_as_of is None and out.price_note is None
    assert not any("LIVE PRICES UNAVAILABLE" in b for b in out.report["banners"])
    facts = build_portfolio_facts(out.model_dump(mode="json"))
    assert facts["prices_as_of"] == "2026-10-18" and facts["price_note"] is None     # falls back to the request's day
    assert egoquant_routes._price_source_note(run) is None


def test_in_snapshot_price_mode_the_wording_is_calm_everywhere(monkeypatch):
    monkeypatch.setenv("EGOQUANT_PRICE_MODE", "snapshot")
    monkeypatch.setenv("EGOQUANT_PRICE_SNAPSHOT", "on")
    run = run_pipeline(make_answers(gl_score=25), investor_id="t", goal_type="growth", horizon_years=10, as_of=AS_OF,
                       interest_areas=["stocks", "bonds"])
    out = egoquant_routes._portfolio_out(run.profile, AS_OF, run, None, None)
    assert out.snapshot_first and out.prices_as_of == date(2026, 10, 16) and out.fallback_reason is None
    assert out.price_note.startswith("saved snapshot of real prices as of 16 Oct 2026") and "Yahoo Finance" in out.price_note
    note = egoquant_routes._price_source_note(run)
    assert note and "unavailable" not in note and "16 Oct 2026" in note and "2026-10-16" in note
    banners = " ".join(out.report["banners"])
    assert "LIVE PRICES UNAVAILABLE" not in banners and "Prices: saved snapshot" in banners
    assert run.report.prices_as_of == date(2026, 10, 16)                     # the report header says the snapshot day
