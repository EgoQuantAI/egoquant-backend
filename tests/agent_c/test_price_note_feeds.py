"""The saved-prices note names only the feeds of the instruments that were actually priced: a portfolio universe with no crypto pair does
not say "crypto from Binance" (TRACKER F13).

SnapshotPriceSource.origin_for / label_for / calm_label_for on a tiny file. The same through /portfolio in snapshot price mode:
tests/system/test_price_note_in_reports.py (this folder switches the snapshot off for every test)."""

from __future__ import annotations

import gzip
import json

import pytest

from egoquant.agents.market_intel.snapshot import SnapshotPriceSource

SOURCES = {"VT": "yahoo", "AAPL": "yahoo", "SHY": "tiingo", "BTCUSDT": "binance", "ETHUSDT": "binance"}


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "s.json.gz"
    prices = {t: {"2026-09-29": 10.0, "2026-09-30": 10.1} for t in SOURCES}
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"as_of": "2026-09-30", "sources": SOURCES, "prices": prices}, fh)
    return SnapshotPriceSource(str(path))


def test_the_whole_file_label_is_unchanged(source):
    assert source.origin == "Tiingo and Yahoo Finance; crypto from Binance"
    assert source.label == "real prices as of 2026-09-30, cached (Tiingo and Yahoo Finance; crypto from Binance)"
    assert source.calm_label == "saved snapshot of real prices as of 30 Sep 2026, Tiingo and Yahoo Finance; crypto from Binance"
    assert source.label_for(None) == source.label and source.calm_label_for(None) == source.calm_label


@pytest.mark.parametrize("tickers, origin", [
    (["VT"], "Yahoo Finance"),
    (["VT", "AAPL"], "Yahoo Finance"),
    (["VT", "SHY"], "Tiingo and Yahoo Finance"),
    (["VT", "BTCUSDT"], "Yahoo Finance; crypto from Binance"),
    (["BTCUSDT", "ETHUSDT"], "crypto from Binance"),
    (["SHY"], "Tiingo"),
    (["NOT_IN_THE_FILE"], ""),
    ([], ""),
])
def test_only_the_feeds_of_the_priced_instruments_are_named(source, tickers, origin):
    assert source.origin_for(tickers) == origin
    assert source.label_for(tickers) == "real prices as of 2026-09-30, cached" + (f" ({origin})" if origin else "")
    assert source.calm_label_for(tickers) == "saved snapshot of real prices as of 30 Sep 2026" + (f", {origin}" if origin else "")
    assert source.origin_for(iter(tickers)) == origin                               # any iterable of tickers, a generator included
