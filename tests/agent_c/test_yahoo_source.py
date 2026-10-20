"""Yahoo Finance as an offline snapshot source (market_intel/yahoo.py). Everything offline: the downloader is a fake."""

import gzip
import json
import re
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from egoquant.agents.market_intel import (
    PriceSourceError,
    RateLimitError,
    RoutedPriceSource,
    SnapshotPriceSource,
    SyntheticPriceSource,
    build_snapshot,
)
from egoquant.agents.market_intel.catalog import DEFAULT_CATALOG
from egoquant.agents.market_intel.yahoo import YahooPriceSource

AS_OF = date(2026, 9, 30)
START = date(2025, 1, 1)
REPO = Path(__file__).resolve().parents[2]


class FakeYahoo:
    """Stands in for yfinance.download: invented prices for the catalog's tickers, one call per chunk."""

    def __init__(self, errors=None, tz=None):
        self.prices = SyntheticPriceSource(origin=date(2004, 1, 2))
        self.calls: list[list[str]] = []
        self.errors = errors or {}
        self.tz = tz

    def __call__(self, tickers, start, end):
        self.calls.append(list(tickers))
        known = [t for t in tickers if t not in self.errors]
        frame = self.prices.fetch_adjusted_close(known, start, end).reindex(columns=tickers)
        if self.tz:
            frame.index = frame.index.tz_localize(self.tz)
        return frame, {t: self.errors[t] for t in tickers if t in self.errors}


US = [i.ticker for i in DEFAULT_CATALOG if i.market == "us_listed"]


def test_many_tickers_per_request_and_served_from_memory_afterwards():
    fake = FakeYahoo()
    src = YahooPriceSource(download=fake, chunk_size=100)
    src.prefetch(US[:250], START, AS_OF)
    assert [len(c) for c in fake.calls] == [100, 100, 50]
    frame = src.fetch_adjusted_close(US[:3], START, AS_OF)
    assert list(frame.columns) == US[:3] and len(fake.calls) == 3          # no new request
    assert not src.needs_request(US[0], START, AS_OF)
    assert src.needs_request(US[0], date(2024, 1, 1), AS_OF)               # another range is not in memory


def test_unknown_ticker_is_a_missing_column_and_not_asked_again():
    fake = FakeYahoo(errors={"NOPE": "Quote not found for symbol: NOPE"})
    src = YahooPriceSource(download=fake)
    frame = src.fetch_adjusted_close(["VT", "NOPE"], START, AS_OF)
    assert list(frame.columns) == ["VT"]
    src.fetch_adjusted_close(["NOPE"], START, AS_OF)
    assert len(fake.calls) == 1


def test_rate_limit_keeps_what_arrived_and_raises():
    fake = FakeYahoo(errors={"BND": "YFRateLimitError('Too Many Requests. Rate limited.')"})
    src = YahooPriceSource(download=fake)
    with pytest.raises(RateLimitError):
        src.prefetch(["VT", "BND"], START, AS_OF)
    assert not src.needs_request("VT", START, AS_OF)                       # VT arrived and is kept
    assert src.needs_request("BND", START, AS_OF)                          # BND is asked again later


def test_unreachable_yahoo_is_a_source_error():
    ssl = "SSLError('Failed to perform, curl: (77) error adding trust anchors')"
    src = YahooPriceSource(download=FakeYahoo(errors={"VT": ssl, "BND": ssl}))
    with pytest.raises(PriceSourceError, match="could not be reached"):
        src.fetch_adjusted_close(["VT", "BND"], START, AS_OF)


def test_dates_are_tz_naive_trading_days():
    src = YahooPriceSource(download=FakeYahoo(tz="America/New_York"))
    frame = src.fetch_adjusted_close(["VT"], START, AS_OF)
    assert frame.index.tz is None and (frame.index == frame.index.normalize()).all()


def test_snapshot_from_yahoo_labels_each_feed_and_downloads_in_batches(tmp_path):
    catalog = [i for i in DEFAULT_CATALOG if i.product_type != "stock"]
    fake = FakeYahoo()

    class FakeBinance(SyntheticPriceSource):
        name = "binance"
        data_source = "historical"

    source = RoutedPriceSource({"us_listed": YahooPriceSource(download=fake),
                                "crypto_spot": FakeBinance(origin=date(2004, 1, 2))}, catalog)
    slept = []
    path = build_snapshot(AS_OF, catalog=catalog, path=tmp_path / "snap.json.gz", source=source,
                          max_requests_per_hour=1, sleep=slept.append)
    snap = SnapshotPriceSource(path)
    us = [i.ticker for i in catalog if i.market == "us_listed"]
    asked = [t for call in fake.calls for t in call]
    assert len(fake.calls) == -(-len(us) // 100) and sorted(asked) == sorted(us)   # batches, each ticker once
    assert slept == []                                                    # no hourly quota to wait on
    assert snap.sources["VT"] == "yahoo" and snap.sources["BTCUSDT"] == "binance"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        assert json.load(fh)["complete"] is True


def test_agent_c_falls_back_to_the_yahoo_file_and_the_regime_model_keeps_the_tiingo_file():
    """Reads the COMMITTED files on purpose (unlike the rest of tests/agent_c): it checks which file is whose."""
    import inspect

    from egoquant.agents.market_intel import InvestorInterests, MarketIntelligenceAgent, load_snapshot
    from egoquant.agents.market_intel.snapshot import SNAPSHOT_PATH, YAHOO_SNAPSHOT_PATH
    from egoquant.regime import model as regime_model

    c = MarketIntelligenceAgent(InvestorInterests(investor_id="t", interest_areas=["stocks"], max_r_level=3),
                                source=SyntheticPriceSource())
    assert c.snapshot_path == str(YAHOO_SNAPSHOT_PATH)
    assert set(SnapshotPriceSource(YAHOO_SNAPSHOT_PATH).sources.values()) == {"yahoo", "binance"}
    assert set(SnapshotPriceSource(SNAPSHOT_PATH).sources.values()) == {"tiingo", "binance"}
    # The regime model calls load_snapshot() with its default path: the Tiingo file it was fitted on.
    assert "load_snapshot()" in inspect.getsource(regime_model._snapshot_prices)
    assert inspect.signature(load_snapshot).parameters["path"].default == str(SNAPSHOT_PATH)


def test_the_app_never_depends_on_yfinance():
    assert "yfinance" not in (REPO / "requirements.txt").read_text(encoding="utf-8").lower()
    importers = [p.relative_to(REPO).as_posix() for p in (REPO / "egoquant").rglob("*.py")
                 if re.search(r"^\s*(import|from)\s+yfinance", p.read_text(encoding="utf-8"), re.M)]
    assert importers == ["egoquant/agents/market_intel/yahoo.py"]
