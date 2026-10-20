"""IBKR history replay prices when the live feed is down: the committed snapshot of REAL prices, never invented ones.

Without prices the replay values every day at the last trade price, so Agent B cannot see how far the market had
fallen when the investor sold. Run with: pytest -v tests/test_ibkr_price_fallback.py (no network).
"""

from datetime import date, timedelta

import pandas as pd
import pytest

import egoquant_routes as routes
from egoquant.agents.market_intel.snapshot import agent_c_snapshot_path, load_snapshot

SNAPSHOT = load_snapshot(str(agent_c_snapshot_path()))
END = SNAPSHOT.prices_as_of
START = END - timedelta(days=300)
SYMBOLS = ["VT", "AGG"]


def dead_feed(*args, **kwargs):
    raise OSError("feed down")


class LiveSource:
    def fetch_adjusted_close(self, symbols, start, end):
        return pd.DataFrame({"VT": [1.0, 2.0]}, index=pd.to_datetime(["2026-01-02", "2026-01-05"]))


@pytest.fixture
def feed_down(monkeypatch):
    monkeypatch.setattr(routes, "build_live_source", dead_feed)
    monkeypatch.delenv("EGOQUANT_PRICE_SNAPSHOT", raising=False)


def test_a_dead_live_feed_falls_back_to_the_committed_snapshot(feed_down):
    prices = routes._ibkr_prices(SYMBOLS, START, END, [])
    assert set(prices.columns) == set(SYMBOLS)
    assert prices.index.min() >= pd.Timestamp(START) and prices.index.max() <= pd.Timestamp(END)
    assert len(prices) > 150 and prices.notna().all().all()
    pd.testing.assert_frame_equal(prices, SNAPSHOT.fetch_adjusted_close(SYMBOLS, START, END))      # real prices


def test_live_prices_win_when_the_feed_answers(monkeypatch):
    monkeypatch.setattr(routes, "build_live_source", lambda *a, **k: LiveSource())
    prices = routes._ibkr_prices(SYMBOLS, START, END, [])
    assert list(prices.columns) == ["VT"] and len(prices) == 2                       # not the snapshot


def test_snapshot_price_mode_sends_no_live_request_at_all(monkeypatch):
    monkeypatch.setenv("EGOQUANT_PRICE_MODE", "snapshot")
    monkeypatch.delenv("EGOQUANT_PRICE_SNAPSHOT", raising=False)
    monkeypatch.setattr(routes, "build_live_source", lambda *a, **k: pytest.fail("snapshot mode must not ask the live feed"))
    prices = routes._ibkr_prices(SYMBOLS, START, END, [])
    pd.testing.assert_frame_equal(prices, SNAPSHOT.fetch_adjusted_close(SYMBOLS, START, END))


def test_no_symbols_means_no_request(monkeypatch):
    monkeypatch.setattr(routes, "build_live_source", lambda *a, **k: pytest.fail("no feed call without symbols"))
    assert routes._ibkr_prices([], START, END, []).empty


def test_a_snapshot_that_is_too_old_is_not_used(feed_down):
    far_end = END + timedelta(days=routes._IBKR_SNAPSHOT_MAX_AGE_DAYS + 1)
    assert routes._ibkr_prices(SYMBOLS, START, far_end, []).empty


def test_the_snapshot_switch_is_respected(feed_down, monkeypatch):
    monkeypatch.setenv("EGOQUANT_PRICE_SNAPSHOT", "off")
    assert routes._ibkr_prices(SYMBOLS, START, END, []).empty


def test_an_unreadable_snapshot_never_fails_the_build(feed_down, monkeypatch):
    def broken(path):
        raise ValueError("damaged file")

    monkeypatch.setattr(routes, "load_snapshot", broken)
    assert routes._ibkr_prices(SYMBOLS, START, END, []).empty


def test_symbols_the_snapshot_does_not_hold_are_simply_missing(feed_down):
    prices = routes._ibkr_prices(["VT", "NOT_A_TICKER"], START, END, [])
    assert list(prices.columns) == ["VT"]


def test_end_to_end_with_prices_the_replay_has_no_trade_price_days(feed_down):
    """The demo's balanced investor: with snapshot prices nothing is valued at trade prices."""
    from pathlib import Path
    from egoquant.agents.behavioral.ibkr import parse_ibkr_report

    text = (Path(__file__).resolve().parents[1] / "docs" / "demo_data" / "ibkr_balanced_investor.xml").read_text("utf-8")
    report = parse_ibkr_report(text, today=date(END.year, 12, 31))
    catalog = routes.DEFAULT_CATALOG
    history = routes.ibkr_history_from_report(report, END, catalog, synthetic=False)
    assert history.valued_at_trade_price == []
