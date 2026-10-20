"""The chain when a live feed fails: committed snapshot of REAL prices -> SYNTHETIC (opt-in), and a
failing crypto feed (Binance HTTP 451) dropping only crypto. Everything offline: feeds are fakes."""

import gzip
import json
from datetime import date, timedelta

import pytest
import requests

from egoquant.agents.market_intel import (
    CachedPriceSource,
    InvestorInterests,
    MarketIntelligenceAgent,
    PriceSourceError,
    RateLimitError,
    RoutedPriceSource,
    SnapshotPriceSource,
    SyntheticPriceSource,
    TiingoPriceSource,
    build_snapshot,
)
from egoquant.agents.market_intel.catalog import DEFAULT_CATALOG
from egoquant.agents.market_intel.snapshot import calendar_days, default_history_rows, snapshot_order
from egoquant.models import AgentConfig

SNAP_AS_OF = date(2026, 9, 30)


class RealLookingSource(SyntheticPriceSource):
    """Stands in for a live feed in tests (invented numbers, but labeled like a real feed). Starts in 2004 so it
    can serve the goal simulation's ~20-year window (AgentConfig.goal_history_days), like Tiingo does."""
    name = "tiingo"
    data_source = "historical"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("origin", date(2004, 1, 2))
        super().__init__(*args, **kwargs)


class FailingSource:
    name = "tiingo"
    data_source = "historical"

    def __init__(self, message="Tiingo rate limit reached (HTTP 429); try again later"):
        self.message = message

    def fetch_adjusted_close(self, tickers, start, end):
        raise PriceSourceError(self.message)


BLOCKED = "Binance refused the request from this location (HTTP 451)"


@pytest.fixture(scope="module")
def snapshot_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("snap") / "prices_snapshot.json.gz"
    catalog = [i for i in DEFAULT_CATALOG if i.product_type != "stock"]
    return str(build_snapshot(SNAP_AS_OF, catalog=catalog, path=path, source=RealLookingSource()))


def agent(snapshot_file, source=None, interests=None, **kw):
    interests = interests or InvestorInterests(investor_id="t", interest_areas=["stocks", "bonds"], max_r_level=3)
    kw.setdefault("use_snapshot", True)            # conftest switches the snapshot off by default
    return MarketIntelligenceAgent(interests, source=source or FailingSource(), snapshot_path=snapshot_file, **kw)


def test_snapshot_file_round_trips(snapshot_file):
    snap = SnapshotPriceSource(snapshot_file)
    assert snap.prices_as_of == SNAP_AS_OF and snap.data_source == "historical"
    assert snap.label == "real prices as of 2026-09-30, cached (Tiingo)"          # names the provider
    assert snap.sources["VT"] == "tiingo" and snap.prices.index.max().date() <= SNAP_AS_OF
    frame = snap.fetch_adjusted_close(["VT", "NOPE"], date(2026, 1, 1), SNAP_AS_OF)
    assert list(frame.columns) == ["VT"]


def test_failed_live_feed_switches_to_the_real_price_snapshot(snapshot_file):
    c = agent(snapshot_file, fallback_to_synthetic=True)          # the snapshot still comes first
    snapshot = c.get_market_snapshot(None, SNAP_AS_OF, 300)
    assert snapshot.data_source == "historical"                   # real prices, not synthetic
    r = c.last_report
    assert r.source == "snapshot" and r.prices_as_of == SNAP_AS_OF
    assert r.price_note == "real prices as of 2026-09-30, cached (Tiingo)" and "429" in r.fallback_reason
    assert set(r.price_sources.values()) == {"snapshot"}
    assert c.get_market_snapshot(None, SNAP_AS_OF, 260).data_source == "historical"   # stays on it


def test_staleness_is_judged_from_the_snapshot_date(snapshot_file):
    later = SNAP_AS_OF + timedelta(days=20)                       # well past stale_days=7
    c = agent(snapshot_file)
    snapshot = c.get_market_snapshot(None, later, 300)
    assert snapshot.prices.index.max().date() <= SNAP_AS_OF and not c.last_report.dropped
    assert c.last_report.prices_as_of == SNAP_AS_OF


def test_too_old_snapshot_is_not_used(snapshot_file):
    much_later = SNAP_AS_OF + timedelta(days=45)
    with pytest.raises(PriceSourceError, match="429"):            # the live feed's error, not the snapshot's
        agent(snapshot_file, fallback_to_synthetic=False).get_market_snapshot(None, much_later, 300)
    c = agent(snapshot_file, fallback_to_synthetic=True)
    assert c.get_market_snapshot(None, much_later, 300).data_source == "synthetic"
    assert "429" in c.last_report.fallback_reason and c.last_report.price_note is None


def test_missing_snapshot_falls_through_to_synthetic_or_the_error(tmp_path):
    missing = str(tmp_path / "none.json.gz")
    with pytest.raises(PriceSourceError, match="429"):
        agent(missing, fallback_to_synthetic=False).get_market_snapshot(None, SNAP_AS_OF, 300)
    assert agent(missing, fallback_to_synthetic=True).get_market_snapshot(None, SNAP_AS_OF, 300).data_source == "synthetic"


def test_snapshot_is_switched_by_the_environment(snapshot_file, monkeypatch):
    monkeypatch.setenv("EGOQUANT_PRICE_SNAPSHOT", "off")
    with pytest.raises(PriceSourceError):
        agent(snapshot_file, fallback_to_synthetic=False, use_snapshot=None).get_market_snapshot(None, SNAP_AS_OF, 300)
    monkeypatch.setenv("EGOQUANT_PRICE_SNAPSHOT", "")                # unset = on
    c = agent(snapshot_file, fallback_to_synthetic=False, use_snapshot=None)
    assert c.get_market_snapshot(None, SNAP_AS_OF, 300).data_source == "historical"


def test_missing_key_at_setup_uses_the_snapshot(snapshot_file, monkeypatch):
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    interests = InvestorInterests(investor_id="t", interest_areas=["stocks"], max_r_level=3)
    c = MarketIntelligenceAgent(interests, snapshot_path=snapshot_file, fallback_to_synthetic=False, use_snapshot=True)
    assert isinstance(c.source, SnapshotPriceSource) and "TIINGO_API_KEY" in c.fallback_reason
    assert c.get_market_snapshot(None, SNAP_AS_OF, 300).data_source == "historical"


# ---- crypto feed blocked (Binance answers HTTP 451 to US servers) ----
def crypto_investor():
    return InvestorInterests(investor_id="t", interest_areas=["stocks", "crypto"], max_r_level=5)


def test_blocked_crypto_feed_drops_only_crypto_and_keeps_real_etf_prices(snapshot_file):
    routed = RoutedPriceSource({"us_listed": RealLookingSource(), "crypto_spot": FailingSource(BLOCKED)})
    c = agent(snapshot_file, source=routed, interests=crypto_investor())
    crypto = [t for t in c.selection.tickers if c.instruments[t].market == "crypto_spot"]
    assert crypto                                                 # the investor's universe has crypto
    snapshot = c.get_market_snapshot(None, SNAP_AS_OF, 300)
    assert snapshot.data_source == "historical" and c.last_report.source == "tiingo"   # still the live feed
    assert not set(crypto) & set(snapshot.prices.columns)
    reasons = {d.ticker: d.reason for d in c.last_report.dropped}
    assert all("451" in reasons[t] for t in crypto)
    assert any("crypto spot prices could not be fetched" in n and "every other price is real" in n
               for n in c.last_report.notes)
    assert c.last_report.fallback_reason is None


def test_blocked_etf_feed_is_still_a_whole_run_switch(snapshot_file):
    routed = RoutedPriceSource({"us_listed": FailingSource(), "crypto_spot": RealLookingSource()})
    c = agent(snapshot_file, source=routed, interests=crypto_investor())
    assert c.get_market_snapshot(None, SNAP_AS_OF, 300).data_source == "historical"
    assert c.last_report.source == "snapshot"


def test_blocked_binance_coin_list_keeps_the_fixed_crypto_pairs(snapshot_file):
    class BlockedMarket:
        def ranked_pairs(self):
            raise PriceSourceError(BLOCKED)

    c = agent(snapshot_file, source=RealLookingSource(), interests=crypto_investor(), binance_market=BlockedMarket())
    assert "BTCUSDT" in c.selection.tickers
    c.get_market_snapshot(None, SNAP_AS_OF, 300)
    assert any("coin list could not be loaded" in n for n in c.last_report.notes)


# ---- network errors and the in-process cache ----
def test_network_errors_become_price_source_errors():
    class DownSession:
        def get(self, *a, **kw):
            raise requests.ConnectionError("no route to host")

    with pytest.raises(PriceSourceError, match="could not be reached"):
        TiingoPriceSource(api_key="k", session=DownSession()).fetch_adjusted_close(["VT"], date(2026, 1, 1), SNAP_AS_OF)


def test_cache_serves_repeated_requests_from_memory(tmp_path):
    class Counting(RealLookingSource):
        calls = 0

        def fetch_adjusted_close(self, tickers, start, end):
            Counting.calls += 1
            return super().fetch_adjusted_close(tickers, start, end)

    today = lambda: SNAP_AS_OF + timedelta(days=1)
    first = CachedPriceSource(Counting(), directory=tmp_path, today=today)
    first.fetch_adjusted_close(["VT"], date(2026, 1, 1), SNAP_AS_OF)
    (tmp_path / "VT.json").unlink()                               # e.g. the disk was wiped
    again = CachedPriceSource(Counting(), directory=tmp_path, today=today)   # a later request, same process
    assert not again.fetch_adjusted_close(["VT"], date(2026, 2, 1), SNAP_AS_OF).empty
    assert Counting.calls == 1


def test_memory_cache_is_capped_and_a_dropped_file_is_read_from_disk_again(tmp_path, monkeypatch):
    """The in-memory copy keeps at most EGOQUANT_PRICE_MEMORY_FILES files (least recently used dropped); the
    disk cache keeps everything, so the prices served are the same."""
    monkeypatch.setattr(CachedPriceSource, "_memory", type(CachedPriceSource._memory)())
    monkeypatch.setenv("EGOQUANT_PRICE_MEMORY_FILES", "2")
    today = lambda: SNAP_AS_OF + timedelta(days=1)
    src = CachedPriceSource(RealLookingSource(), directory=tmp_path, today=today)
    first = src.fetch_adjusted_close(["VT", "BND", "GLD"], date(2026, 1, 1), SNAP_AS_OF)
    assert len(CachedPriceSource._memory) == 2 and tmp_path / "VT.json" not in CachedPriceSource._memory
    again = CachedPriceSource(RealLookingSource(), directory=tmp_path, today=today)
    assert again.fetch_adjusted_close(["VT", "BND", "GLD"], date(2026, 1, 1), SNAP_AS_OF).equals(first)
    assert len(CachedPriceSource._memory) == 2


def test_two_builds_saving_the_same_ticker_at_once_both_succeed(tmp_path, monkeypatch):
    """/portfolio runs in a thread pool: two builds that fetch the same uncached ticker at the same moment used to
    share one temporary file, and one of them crashed (WinError 32 / a lost rename)."""
    import threading
    monkeypatch.setattr(CachedPriceSource, "_memory", type(CachedPriceSource._memory)())
    today = lambda: SNAP_AS_OF + timedelta(days=1)
    errors, start = [], threading.Barrier(8)

    def build():
        src = CachedPriceSource(RealLookingSource(), directory=tmp_path, today=today)
        start.wait()
        try:
            for _ in range(10):
                src._save("VT", date(2026, 1, 1), SNAP_AS_OF,
                          RealLookingSource().fetch_adjusted_close(["VT"], date(2026, 1, 1), SNAP_AS_OF)["VT"])
        except Exception as err:                                  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=build) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert json.loads((tmp_path / "VT.json").read_text(encoding="utf-8"))["ticker"] == "VT"
    assert not list(tmp_path.glob("*.tmp"))


# ---- the pipeline's path (fetch_universe) on the snapshot ----
def test_fetch_universe_falls_back_to_the_snapshot(snapshot_file):
    c = agent(snapshot_file)
    c.fetch_universe(SNAP_AS_OF, 300)
    snapshot = c.get_market_snapshot(c.selection.tickers, SNAP_AS_OF, 300)
    assert snapshot.data_source == "historical" and c.last_report.source == "snapshot"
    assert "429" in c.last_report.fallback_reason


# ---- the builder: polite to the shared Tiingo quota ----
class QuotaFeed(RealLookingSource):
    """A live-looking US feed where every ticker costs a request; optional 429 or monthly limit."""

    def __init__(self, rate_limit_after=None, refused=()):
        super().__init__()
        self.requests, self.rate_limit_after, self.refused = 0, rate_limit_after, set(refused)

    def needs_request(self, ticker, start, end):
        return True

    def allows_new(self, ticker):
        return ticker not in self.refused

    def fetch_adjusted_close(self, tickers, start, end):
        self.requests += 1
        if self.rate_limit_after is not None and self.requests > self.rate_limit_after:
            raise RateLimitError("Tiingo rate limit reached (HTTP 429); try again later")
        return super().fetch_adjusted_close(tickers, start, end)


FUNDS = [i for i in DEFAULT_CATALOG if i.market == "us_listed" and i.product_type != "stock"][:8]


def read(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def test_builder_caps_new_requests_per_hour(tmp_path):
    now, waits = [0.0], []
    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds
    feed = QuotaFeed()
    build_snapshot(SNAP_AS_OF, catalog=FUNDS, path=tmp_path / "s.json.gz", max_requests_per_hour=3,
                   source=feed, clock=lambda: now[0], sleep=sleep)
    assert feed.requests == 8 and len(waits) == 2                 # 3 + 3 + 2 requests, two hourly waits
    assert all(w >= 3600 for w in waits)
    assert read(tmp_path / "s.json.gz")["complete"] is True


def test_builder_never_sleeps_a_negative_time_when_the_clock_jumps(tmp_path):
    """A suspended PC can make the oldest request look more than an hour old right after the wait check."""
    ticks = iter([0.0, 1.0, 4000.0] + [4001.0] * 50)
    waits = []
    build_snapshot(SNAP_AS_OF, catalog=FUNDS, path=tmp_path / "s.json.gz", max_requests_per_hour=1, source=QuotaFeed(),
                   clock=lambda: next(ticks), sleep=waits.append)
    assert waits and all(w >= 0 for w in waits)


def test_builder_stops_at_the_first_rate_limit_and_keeps_what_it_has(tmp_path):
    cached = FUNDS[-1].ticker                                     # already on disk: costs no request

    class PartlyCached(QuotaFeed):
        def needs_request(self, ticker, start, end):
            return ticker != cached

        def fetch_adjusted_close(self, tickers, start, end):
            if tickers == [cached]:                               # served from disk, no request
                return RealLookingSource.fetch_adjusted_close(self, tickers, start, end)
            return super().fetch_adjusted_close(tickers, start, end)

    feed = PartlyCached(rate_limit_after=2)
    build_snapshot(SNAP_AS_OF, catalog=FUNDS, path=tmp_path / "s.json.gz", source=feed, sleep=pytest.fail)
    payload = read(tmp_path / "s.json.gz")
    assert feed.requests == 3                                     # 2 ok, then the 429; nothing new after it
    assert len(payload["prices"]) == 3 and cached in payload["prices"]
    assert payload["complete"] is False and "429" in payload["stopped"]
    assert SnapshotPriceSource(tmp_path / "s.json.gz").prices.shape[1] == 3   # partial file is usable


def test_builder_skips_tickers_over_the_monthly_limit(tmp_path):
    refused = FUNDS[-1].ticker
    feed = QuotaFeed(refused={refused})
    build_snapshot(SNAP_AS_OF, catalog=FUNDS, path=tmp_path / "s.json.gz", source=feed)
    payload = read(tmp_path / "s.json.gz")
    assert feed.requests == 7 and refused in payload["skipped"] and refused not in payload["prices"]


def test_snapshot_holds_the_history_the_goal_monte_carlo_asks_for(snapshot_file):
    rows = AgentConfig().goal_history_days
    assert default_history_rows() == max(rows, AgentConfig().lookback_days)
    payload = read(snapshot_file)
    assert payload["history_rows"] == default_history_rows()
    assert date.fromisoformat(payload["start"]) == SNAP_AS_OF - timedelta(days=calendar_days(rows))
    c = agent(snapshot_file)                                      # the goal path's request, served in full
    core = [t for t in c.selection.tickers if c.instruments[t].core]
    assert c.get_market_snapshot(core, SNAP_AS_OF, rows).prices.shape[0] == rows + 1


def test_builder_history_can_be_set(tmp_path):
    build_snapshot(SNAP_AS_OF, catalog=FUNDS[:2], path=tmp_path / "s.json.gz", source=QuotaFeed(), history_rows=504)
    assert read(tmp_path / "s.json.gz")["start"] == (SNAP_AS_OF - timedelta(days=calendar_days(504))).isoformat()


def test_builder_fetches_core_funds_first_and_stocks_only_when_asked():
    order = snapshot_order(DEFAULT_CATALOG)
    n_core = sum(i.core for i in DEFAULT_CATALOG)
    assert all(i.core for i in order[:n_core]) and not any(i.product_type == "stock" for i in order)
    assert any(i.product_type == "stock" for i in snapshot_order(DEFAULT_CATALOG, include_stocks=True))


def _tiny_snapshot(tmp_path, sources):
    import gzip
    import json
    path = tmp_path / "s.json.gz"
    prices = {t: {"2026-09-29": 10.0, "2026-09-30": 10.1} for t in (sources or {"VT": "x"})}
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"as_of": "2026-09-30", "sources": sources, "prices": prices}, fh)
    return SnapshotPriceSource(str(path))


def test_label_names_the_provider_and_never_a_guessed_one(tmp_path):
    # funds from Yahoo + crypto from Binance (the committed Agent C snapshot), Tiingo only, and a file that records none
    assert _tiny_snapshot(tmp_path, {"VT": "yahoo", "AAPL": "yahoo", "BTCUSDT": "binance"}).label ==         "real prices as of 2026-09-30, cached (Yahoo Finance; crypto from Binance)"
    assert _tiny_snapshot(tmp_path, {"VT": "tiingo", "BTCUSDT": "binance"}).label ==         "real prices as of 2026-09-30, cached (Tiingo; crypto from Binance)"
    assert _tiny_snapshot(tmp_path, {"VT": "yahoo", "SHY": "tiingo"}).label ==         "real prices as of 2026-09-30, cached (Tiingo and Yahoo Finance)"
    assert _tiny_snapshot(tmp_path, {}).label == "real prices as of 2026-09-30, cached"


def test_a_damaged_snapshot_file_is_a_source_error_never_a_crash(tmp_path, snapshot_file):
    good = open(snapshot_file, "rb").read()
    import gzip
    import json
    cases = {"truncated": good[: len(good) // 3], "not gzip": b"this is not a gzip file", "empty": b"",
             "not an object": gzip.compress(b"[1, 2, 3]"), "no prices": gzip.compress(json.dumps({"as_of": "2026-09-30"}).encode()),
             "bad date": gzip.compress(json.dumps({"as_of": "soon", "prices": {}}).encode())}
    for name, data in cases.items():
        path = tmp_path / f"{name.replace(' ', '_')}.json.gz"
        path.write_bytes(data)
        with pytest.raises(PriceSourceError, match="price snapshot unavailable"):
            SnapshotPriceSource(str(path))
    with pytest.raises(PriceSourceError, match="price snapshot unavailable"):
        SnapshotPriceSource(str(tmp_path / "missing.json.gz"))


def test_default_fallback_uses_the_tiingo_file_when_the_yahoo_file_is_unreadable(tmp_path, snapshot_file, monkeypatch):
    from egoquant.agents.market_intel import snapshot as snapshot_module
    bad = tmp_path / "yahoo.json.gz"
    bad.write_bytes(open(snapshot_file, "rb").read()[:1000])                 # a truncated copy
    monkeypatch.setattr(snapshot_module, "YAHOO_SNAPSHOT_PATH", bad)
    monkeypatch.setattr(snapshot_module, "SNAPSHOT_PATH", __import__("pathlib").Path(snapshot_file))
    interests = InvestorInterests(investor_id="t", interest_areas=["stocks", "bonds"], max_r_level=3)
    c = MarketIntelligenceAgent(interests, source=FailingSource(), use_snapshot=True)       # no snapshot_path: the defaults
    c.get_market_snapshot(None, SNAP_AS_OF, 300)
    assert c.last_report.source == "snapshot" and c.last_report.prices_as_of == SNAP_AS_OF
    # both unreadable: a clean source error (the route answers 503), not an exception from inside gzip
    monkeypatch.setattr(snapshot_module, "SNAPSHOT_PATH", bad)
    c2 = MarketIntelligenceAgent(interests, source=FailingSource(), use_snapshot=True, fallback_to_synthetic=False)
    with pytest.raises(PriceSourceError):
        c2.get_market_snapshot(None, SNAP_AS_OF, 300)


# ---- a rate limit before the core funds are priced: the clusters are walked on the snapshot ----
class LiveFeed:
    """A live Tiingo feed (NOT a synthetic source, so the run may switch to the snapshot), nothing on disk;
    answers 429 after `rate_limit_after` requests (0: every request, e.g. a shared key already used up)."""
    name = "tiingo"
    data_source = "historical"

    def __init__(self, rate_limit_after=0):
        self.inner, self.requests, self.rate_limit_after = RealLookingSource(), 0, rate_limit_after

    def needs_request(self, ticker, start, end):
        return True

    def fetch_adjusted_close(self, tickers, start, end):
        self.requests += 1
        if self.requests > self.rate_limit_after:
            raise RateLimitError("Tiingo rate limit reached (HTTP 429); try again later")
        return self.inner.fetch_adjusted_close(tickers, start, end)


def test_a_rate_limit_on_the_core_funds_walks_the_clusters_on_the_snapshot(snapshot_file):
    live = agent(snapshot_file, source=LiveFeed())
    live.fetch_universe(SNAP_AS_OF, 300)
    on_snapshot = agent(snapshot_file, source=SnapshotPriceSource(snapshot_file))
    on_snapshot.fetch_universe(SNAP_AS_OF, 300)
    assert isinstance(live.source, SnapshotPriceSource) and "429" in live.fallback_reason
    assert live.selection.tickers == on_snapshot.selection.tickers          # the same universe as on the snapshot
    assert len(live.selection.tickers) > 4                                   # not only the 4 core funds
    assert not any("rate limit" in e.reason for e in live.selection.not_fetched)
    snapshot = live.get_market_snapshot(None, SNAP_AS_OF, 300)
    assert [a.asset_id for a in snapshot.assets] == live.selection.tickers and live.last_report.source == "snapshot"


def test_a_rate_limit_after_the_core_funds_keeps_the_live_prices(snapshot_file):
    live = agent(snapshot_file, source=LiveFeed(rate_limit_after=4))       # the 4 core funds are priced live
    report = live.fetch_universe(SNAP_AS_OF, 300)
    assert not isinstance(live.source, SnapshotPriceSource) and live.fallback_reason is None
    assert report.rate_limited == ["us_listed"] and len(live.selection.tickers) == 4
    assert live.get_market_snapshot(None, SNAP_AS_OF, 300).data_source == "historical"


def test_without_a_usable_snapshot_a_rate_limit_keeps_the_old_behavior(snapshot_file):
    too_old = SNAP_AS_OF + timedelta(days=45)
    c = agent(snapshot_file, source=LiveFeed())
    assert not c._snapshot_usable(too_old)
    c.fetch_universe(too_old, 300)                                           # no switch inside the walk
    assert not isinstance(c.source, SnapshotPriceSource)
    assert all("rate limit" in e.reason for e in c.selection.not_fetched)
    off = agent(snapshot_file, source=LiveFeed(), use_snapshot=False)
    assert not off._snapshot_usable(SNAP_AS_OF)
