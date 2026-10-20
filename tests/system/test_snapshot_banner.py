"""When the live feed fails and Agent C falls back to the committed snapshot of REAL prices, the report says so
(provider and the date the prices run to); with a healthy source or synthetic prices it adds no such banner."""

from __future__ import annotations

from datetime import date

from egoquant.agents.market_intel import PriceSourceError, SyntheticPriceSource
from egoquant.pipeline import run_pipeline
from tests.helpers import make_answers

AS_OF = date(2026, 10, 16)           # the committed snapshot's own date, so it is not stale


class DeadFeed:
    name = "tiingo"
    data_source = "historical"

    def fetch_adjusted_close(self, tickers, start, end):
        raise PriceSourceError("Tiingo could not be reached (network error: ConnectTimeout)")


def _banners(monkeypatch, source):
    monkeypatch.setenv("EGOQUANT_PRICE_SNAPSHOT", "on")
    out = run_pipeline(make_answers(gl_score=25), investor_id="t", goal_type="growth", horizon_years=10, as_of=AS_OF,
                       price_source=source, interest_areas=["stocks", "bonds"])
    return out, out.report.banners


def test_snapshot_fallback_is_announced_with_its_provider_and_date(monkeypatch):
    out, banners = _banners(monkeypatch, DeadFeed())
    assert out.market_report.source == "snapshot" and out.report.data_source == "historical"
    note = [b for b in banners if b.startswith("LIVE PRICES UNAVAILABLE")]
    assert len(note) == 1
    assert "Yahoo Finance" in note[0] and "2026-10-16" in note[0] and "Markets after 2026-10-16" in note[0]


def test_no_banner_when_the_prices_are_not_from_the_snapshot(monkeypatch):
    _, banners = _banners(monkeypatch, SyntheticPriceSource())
    assert not any(b.startswith("LIVE PRICES UNAVAILABLE") for b in banners)


def test_the_report_header_and_briefing_line_give_the_snapshot_day_not_the_request_day(monkeypatch):
    from egoquant.report import render_html
    monkeypatch.setenv("EGOQUANT_PRICE_SNAPSHOT", "on")
    request_day = date(2026, 10, 18)                                                 # the snapshot ends on the 16th
    run = run_pipeline(make_answers(gl_score=25), investor_id="t", goal_type="growth", horizon_years=10, as_of=request_day,
                       price_source=DeadFeed(), interest_areas=["stocks", "bonds"])
    assert run.report.prices_as_of == date(2026, 10, 16) and "Yahoo Finance" in run.report.price_note
    html = render_html(run.report)
    assert "market data as of 16 Oct 2026" in html and "market data as of 18 Oct 2026" not in html
    healthy = run_pipeline(make_answers(gl_score=25), investor_id="t", goal_type="growth", horizon_years=10,
                           as_of=request_day, price_source=SyntheticPriceSource(), interest_areas=["stocks", "bonds"])
    assert healthy.report.prices_as_of is None and "market data as of 18 Oct 2026" in render_html(healthy.report)


def test_the_price_fields_are_in_the_dump_only_when_a_snapshot_was_used(monkeypatch):
    """Reports built on live or synthetic prices serialise exactly as before (the regime golden hashes them)."""
    monkeypatch.setenv("EGOQUANT_PRICE_SNAPSHOT", "on")
    snap = run_pipeline(make_answers(gl_score=25), investor_id="t", goal_type="growth", horizon_years=10, as_of=AS_OF,
                        price_source=DeadFeed(), interest_areas=["stocks", "bonds"])
    live = run_pipeline(make_answers(gl_score=25), investor_id="t", goal_type="growth", horizon_years=10, as_of=AS_OF,
                        price_source=SyntheticPriceSource(), interest_areas=["stocks", "bonds"])
    assert snap.report.model_dump(mode="json")["prices_as_of"] == "2026-10-16"
    assert "Yahoo Finance" in snap.report.model_dump(mode="json")["price_note"]
    assert "prices_as_of" not in live.report.model_dump(mode="json") and "price_note" not in live.report.model_dump_json()
