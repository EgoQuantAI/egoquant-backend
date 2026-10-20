"""A cache directory that cannot be written (read-only disk, a path that is a file, a missing drive) costs speed,
never the request: prices are kept in memory, the monthly ticker ledger still counts, the directory is used unsaved."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from egoquant.agents.market_intel.directory import Directory, load_directory
from egoquant.agents.market_intel.sources import CachedPriceSource, TiingoUsage

START, END = date(2026, 1, 5), date(2026, 3, 31)


class Inner:
    name = "fake"
    data_source = "historical"

    def __init__(self):
        self.calls = 0

    def fetch_adjusted_close(self, tickers, start, end):
        self.calls += 1
        index = pd.bdate_range(start, end)
        return pd.DataFrame({t: [100.0 + i for i in range(len(index))] for t in tickers}, index=index)


@pytest.fixture(params=["path_is_a_file", "parent_is_a_file"])
def broken_dir(request, tmp_path):
    """A cache directory that mkdir cannot create (what a read-only volume or a missing drive does, portably)."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    return blocker / "cache" if request.param == "path_is_a_file" else blocker / "deeper" / "cache"


def test_fetched_prices_are_served_from_memory_when_the_disk_cannot_be_written(broken_dir):
    inner = Inner()
    source = CachedPriceSource(inner, directory=broken_dir, today=lambda: date(2026, 4, 2))
    frame = source.fetch_adjusted_close(["TESTWRITE"], START, END)             # no exception
    assert list(frame.columns) == ["TESTWRITE"] and len(frame) > 40
    again = source.fetch_adjusted_close(["TESTWRITE"], START, END)             # answered from memory
    assert inner.calls == 1 and again.equals(frame)
    assert source.needs_request("TESTWRITE", START, END) is False


def test_the_monthly_ticker_ledger_still_counts_in_memory(broken_dir):
    ledger = TiingoUsage(path=broken_dir / "usage.json", limit=2, today=lambda: date(2026, 4, 2))
    ledger.record("AAA")
    ledger.record("BBB")                                                       # no exception
    assert ledger.allows("AAA") and not ledger.allows("CCC")                    # the limit of 2 distinct tickers holds


def test_a_directory_that_cannot_be_saved_is_still_used(broken_dir, monkeypatch):
    from egoquant.agents.market_intel import directory as module
    built = Directory(built_on=date(2026, 4, 2), rules_version=module.RULES_VERSION, entries=[])
    monkeypatch.setattr(module, "fetch_tiingo_iex", lambda: [])
    monkeypatch.setattr(module, "fetch_nasdaq_stocks", lambda: [])
    monkeypatch.setattr(module, "fetch_nasdaq_symbols", lambda: [])
    monkeypatch.setattr(module, "fetch_tiingo_tickers", lambda: [])
    monkeypatch.setattr(module, "build_directory", lambda *a, **k: built)
    result = load_directory(today=date(2026, 4, 2), path=broken_dir / "directory.json")
    assert result is built
