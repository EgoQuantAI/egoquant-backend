"""Tests for Agent C (market data job). Run with:  pytest -v tests/agent_c

Layout:  1) chatbot input   2) universe selection   3) snapshot building   4) sources   5) Agent A integration
All data is SYNTHETIC or hand-made, and HTTP is faked; no network access is needed.
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from egoquant import settings
from egoquant.agents.behavioral import NoBehavioralEvidence
from egoquant.agents.market_intel import (
    DEFAULT_CATALOG,
    BinancePriceSource,
    CachedPriceSource,
    CsvPriceSource,
    InvestorInterests,
    MarketDataUnavailable,
    MarketIntelligenceAgent,
    PriceSourceError,
    RoutedPriceSource,
    SyntheticPriceSource,
    TiingoPriceSource,
    build_live_source,
    normalize_interest_area,
    normalize_platform,
    select_universe,
)
from egoquant.agents.quant import QuantAgent
from egoquant.demo.stubs import StubBehavioralAgent
from egoquant.models import AgentConfig, InvestorContext

AS_OF = date(2026, 9, 30)
CORE = ["BIL", "AGG", "SHY", "VT"]


def c_agent(intr, **kwargs) -> MarketIntelligenceAgent:
    """Agent C on SYNTHETIC data unless a source is given (the real default needs the internet)."""
    kwargs.setdefault("source", SyntheticPriceSource())
    return MarketIntelligenceAgent(intr, **kwargs)


def interests(*areas, **kwargs) -> InvestorInterests:
    return InvestorInterests(investor_id="t", interest_areas=list(areas) or ["stocks"], **kwargs)


# ==========================================================================
# 1. Chatbot input
# ==========================================================================
@pytest.mark.parametrize("raw, expected", [
    ("Stocks", "stocks"), ("Real estate", "real_estate"), ("Gold & metals", "precious_metals"), ("gold", "gold"),
    ("silver", "precious_metals"), ("Commodities", "commodities"), ("oil", "commodities"),
    ("bitcoin", "crypto"), ("  Emerging-Markets ", "emerging_markets"), ("TIPS", "inflation_protected"),
    ("small caps", "small_caps"), ("dividends", "dividend_stocks"), ("global bonds", "international_bonds"),
])
def test_interest_aliases_normalize(raw, expected):
    assert normalize_interest_area(raw) == expected


def test_unknown_interest_is_rejected_with_allowed_list():
    with pytest.raises(ValidationError, match="allowed"):
        interests("beanie babies")


def test_interests_are_deduplicated_and_held_tickers_uppercased():
    i = InvestorInterests(investor_id="t", interest_areas=["stocks", "Equities", "gold"], held_assets=["vti"])
    assert i.interest_areas == ["stocks", "gold"]
    assert i.held_assets == ["VTI"]


def test_at_least_one_interest_required():
    with pytest.raises(ValidationError):
        InvestorInterests(investor_id="t", interest_areas=[])


# ==========================================================================
# 2. Universe selection
# ==========================================================================
def test_core_always_included_and_interests_add_sleeves():
    sel = select_universe(interests("real_estate", "technology"))
    assert sel.tickers[:4] == CORE
    assert {"VNQ", "QQQ"} <= set(sel.tickers)
    vnq = next(i for i in sel.instruments if i.ticker == "VNQ")
    assert vnq.reasons == ["interest:real_estate"] and vnq.asset_class == "equity"


def test_gold_commodities_and_crypto_are_investable_assets():
    sel = select_universe(interests("gold", "precious_metals", "commodities", "crypto"))
    by_ticker = {i.ticker: i for i in sel.instruments}
    assert {"IAU", "SLV", "PDBC", "IBIT", "BTCUSDT", "ETHUSDT"} <= set(by_ticker)
    assert {by_ticker[t].asset_class for t in ("IAU", "SLV", "PDBC")} == {"commodity"}
    assert {by_ticker[t].asset_class for t in ("IBIT", "BTCUSDT")} == {"crypto"}
    assert not sel.briefing_only and not sel.watchlist
    assert by_ticker["IAU"].r_level == 3 and by_ticker["SLV"].r_level == 4 and by_ticker["IBIT"].r_level == 5


def test_interests_the_investor_may_not_hold_go_to_the_watchlist():
    sel = select_universe(interests("gold", "crypto", max_r_level=2))
    assert "IAU" not in sel.tickers and "IBIT" not in sel.tickers
    assert {"IAU", "IBIT"} <= set(sel.watchlist_tickers)
    assert "VT" not in sel.watchlist_tickers                         # a core fund, not one of their interests
    assert any(e.ticker == "IAU" and "R3" in e.reason for e in sel.excluded)


def test_every_catalog_rating_follows_the_r_level_rule():
    rule = {"cash_etf": {1}, "bond_etf": {1, 2, 3}, "equity_etf": {3, 4}, "reit_etf": {4}, "stock": {4},
            "commodity_etp": {3, 4}, "crypto_etp": {5}, "crypto_spot": {5}}
    for inst in DEFAULT_CATALOG:
        assert inst.r_level in rule[inst.product_type], inst.ticker


def test_r_level_filter_and_suitability_flag():
    sel = select_universe(interests("emerging_markets", "crypto", max_r_level=3))
    assert "VWO" not in sel.tickers
    assert any(e.ticker == "VWO" and "R4" in e.reason for e in sel.excluded)
    assert any("crypto" in f for f in sel.suitability_flags)
    assert any("emerging_markets" in f for f in sel.suitability_flags)


def test_low_r_level_removes_core_equity():
    sel = select_universe(interests("stocks", max_r_level=2))
    assert "VT" not in sel.tickers and "BIL" in sel.tickers


def test_blocked_product_type():
    sel = select_universe(interests("real_estate", blocked_product_types=["reit_etf"]))
    assert "VNQ" not in sel.tickers
    assert any("knowledge" in f for f in sel.suitability_flags)


def test_held_but_disallowed_asset_stays_priceable_and_is_marked_for_exclusion():
    sel = select_universe(interests("stocks", max_r_level=3, held_assets=["QQQ", "XYZ"]))
    assert "QQQ" in sel.tickers and sel.assets_to_exclude == ["QQQ"]
    assert any("XYZ" in n for n in sel.notes)


def test_selection_is_deterministic():
    a = select_universe(interests("gold", "stocks", "bonds"))
    b = select_universe(interests("bonds", "gold", "stocks"))
    assert a.tickers == b.tickers


# ==========================================================================
# 3. Snapshot building
# ==========================================================================
def test_snapshot_matches_agent_a_contract():
    agent = c_agent(interests("stocks", "real_estate"))
    snap = agent.get_market_snapshot(None, AS_OF, 504)
    assert list(snap.prices.columns) == [a.asset_id for a in snap.assets]
    assert snap.prices.index.max().date() <= AS_OF
    assert len(snap.prices) == 505
    assert not snap.prices.isna().any().any() and (snap.prices > 0).all().all()
    assert snap.data_source == "synthetic"
    bonds = [a for a in snap.assets if a.asset_class == "bond"]
    assert all(a.duration_years for a in bonds)
    assert all(a.duration_years is None for a in snap.assets if a.asset_class != "bond")


def test_no_look_ahead_and_point_in_time_consistency():
    agent = c_agent(interests("stocks"))
    early = agent.get_market_snapshot(None, date(2026, 6, 30), 300).prices
    late = agent.get_market_snapshot(None, AS_OF, 504).prices
    assert early.index.max() <= pd.Timestamp(2026, 6, 30)
    overlap = early.index.intersection(late.index)
    assert len(overlap) > 100
    pd.testing.assert_frame_equal(early.loc[overlap], late.loc[overlap])


def test_asset_ids_subset_and_unknown():
    agent = c_agent(interests("stocks"))
    snap = agent.get_market_snapshot(["VT", "BIL"], AS_OF, 300)
    assert list(snap.prices.columns) == ["BIL", "VT"]  # universe order
    with pytest.raises(ValueError, match="approved universe"):
        agent.get_market_snapshot(["IBIT"], AS_OF, 300)


def _csv_prices(tmp_path, mutate=None, n=400):
    dates = pd.bdate_range(end=pd.Timestamp(AS_OF), periods=n)
    rng = np.random.default_rng(1)
    frame = pd.DataFrame({t: 100 * np.cumprod(1 + rng.normal(0.0002, 0.01, n)) for t in CORE + ["VNQ"]},
                         index=dates)
    if mutate:
        mutate(frame)
    path = tmp_path / "prices.csv"
    frame.rename_axis("date").reset_index().to_csv(path, index=False)
    return path


def test_csv_gaps_filled_late_asset_dropped_future_rows_ignored(tmp_path):
    def mutate(f):
        f.iloc[100:102, f.columns.get_loc("VT")] = np.nan          # 2-day holiday gap -> filled
        f.iloc[:300, f.columns.get_loc("VNQ")] = np.nan            # only 100 rows -> dropped
    path = _csv_prices(tmp_path, mutate)
    future = pd.read_csv(path)
    future.loc[len(future)] = ["2026-10-05"] + [1e6] * 5             # row after as_of must be ignored
    future.to_csv(path, index=False)

    agent = c_agent(interests("real_estate"), source=CsvPriceSource(path))
    snap = agent.get_market_snapshot(None, AS_OF, 504)
    report = agent.last_report
    assert "VNQ" not in snap.prices.columns
    assert any(d.ticker == "VNQ" and "history" in d.reason for d in report.dropped)
    assert report.filled_values.get("VT") == 2
    assert snap.prices.index.max().date() == AS_OF
    assert snap.data_source == "historical"


def test_long_gap_and_stale_assets_are_dropped(tmp_path):
    def mutate(f):
        f.iloc[200:210, f.columns.get_loc("VNQ")] = np.nan
    agent = c_agent(interests("real_estate"), source=CsvPriceSource(_csv_prices(tmp_path, mutate)))
    agent.get_market_snapshot(None, AS_OF, 300)
    assert any(d.ticker == "VNQ" and "gap" in d.reason for d in agent.last_report.dropped)

    def stale(f):
        f.iloc[-10:, f.columns.get_loc("VNQ")] = np.nan
    agent = c_agent(interests("real_estate"), source=CsvPriceSource(_csv_prices(tmp_path, stale)))
    agent.get_market_snapshot(None, AS_OF, 300)
    assert any(d.ticker == "VNQ" and "stale" in d.reason for d in agent.last_report.dropped)


def test_missing_core_asset_raises(tmp_path):
    path = _csv_prices(tmp_path, lambda f: f.drop(columns=["BIL"], inplace=True))
    agent = c_agent(interests("stocks"), source=CsvPriceSource(path))
    with pytest.raises(MarketDataUnavailable, match="BIL"):
        agent.get_market_snapshot(None, AS_OF, 300)


def test_drawdown_alert(tmp_path):
    def crash(f):
        f.iloc[-5:, f.columns.get_loc("VNQ")] *= 0.85
    agent = c_agent(interests("real_estate"), source=CsvPriceSource(_csv_prices(tmp_path, crash)))
    agent.get_market_snapshot(None, AS_OF, 300)
    alerts = agent.last_report.alerts
    assert [a.ticker for a in alerts] == ["VNQ"]
    assert alerts[0].drawdown_from_peak <= -0.10
    assert "not a forecast" in alerts[0].message


def test_risk_free_rate_from_cash_or_config():
    agent = c_agent(interests("stocks"))
    snap = agent.get_market_snapshot(None, AS_OF, 504)
    assert 0.0 < snap.risk_free_rate_annual < 0.08
    assert "BIL" in agent.last_report.risk_free_method
    fixed = c_agent(interests("stocks"), risk_free_rate_annual=0.02)
    assert fixed.get_market_snapshot(None, AS_OF, 504).risk_free_rate_annual == 0.02


def test_cache_serves_preloaded_range():
    calls = []

    class CountingSource(SyntheticPriceSource):
        def fetch_adjusted_close(self, tickers, start, end):
            calls.append((start, end))
            return super().fetch_adjusted_close(tickers, start, end)

    agent = c_agent(interests("stocks"), source=CountingSource())
    agent.preload(date(2022, 1, 1), AS_OF)
    for as_of in (date(2025, 6, 30), date(2026, 1, 30), AS_OF):
        agent.get_market_snapshot(None, as_of, 300)
    assert len(calls) == 1


# ==========================================================================
# 4. Sources
# ==========================================================================
class _FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, url, params, headers, timeout):
        self.calls.append((url, params, headers))
        return self.responses[url.rsplit("/", 2)[-2]]


def test_tiingo_parses_adjusted_close_and_skips_unknown():
    rows = [{"date": "2026-09-28T00:00:00.000Z", "close": 10, "adjClose": 9.5},
            {"date": "2026-09-29T00:00:00.000Z", "close": 11, "adjClose": 10.5}]
    session = _FakeSession({"vt": _FakeResponse(200, rows), "zzz": _FakeResponse(404)})
    source = TiingoPriceSource(api_key="k", session=session)
    frame = source.fetch_adjusted_close(["VT", "ZZZ"], date(2026, 9, 1), AS_OF)
    assert list(frame.columns) == ["VT"]
    assert frame["VT"].tolist() == [9.5, 10.5]
    assert frame.index[0] == pd.Timestamp(2026, 9, 28) and frame.index.tz is None
    assert session.calls[0][1]["endDate"] == "2026-09-30"
    assert session.calls[0][2]["Authorization"] == "Token k"


def test_tiingo_auth_error_and_key_resolution(monkeypatch):
    with pytest.raises(PriceSourceError, match="refused"):
        TiingoPriceSource(api_key="bad", session=_FakeSession({"vt": _FakeResponse(401)})) \
            .fetch_adjusted_close(["VT"], date(2026, 9, 1), AS_OF)
    monkeypatch.setenv("TIINGO_API_KEY", "from-env")
    assert TiingoPriceSource(session=object()).api_key == "from-env"                  # the key comes from the environment
    monkeypatch.delenv("TIINGO_API_KEY")
    with pytest.raises(PriceSourceError, match="TIINGO_API_KEY is not set"):          # no built-in key any more
        TiingoPriceSource(session=object())
    assert not hasattr(settings, "TIINGO_API_KEY")


class _CountingSource:
    name, data_source = "fake", "historical"

    def __init__(self, prices: pd.DataFrame):
        self.prices, self.calls = prices, []

    def fetch_adjusted_close(self, tickers, start, end):
        self.calls.append((tuple(tickers), start, end))
        present = [t for t in tickers if t in self.prices.columns]
        return self.prices.loc[pd.Timestamp(start):pd.Timestamp(end), present]


def test_cache_serves_covered_ranges_from_disk(tmp_path):
    prices = SyntheticPriceSource().fetch_adjusted_close(["VT", "AGG"], date(2025, 1, 1), AS_OF)
    inner = _CountingSource(prices)
    cache = CachedPriceSource(inner, tmp_path, today=lambda: date(2026, 10, 1))
    first = cache.fetch_adjusted_close(["VT", "AGG", "ZZZ"], date(2025, 1, 1), AS_OF)
    assert list(first.columns) == ["VT", "AGG"] and len(inner.calls) == 3          # one call per ticker
    again = CachedPriceSource(inner, tmp_path, today=lambda: date(2026, 10, 1))   # a new run, same disk
    inside = again.fetch_adjusted_close(["VT", "AGG"], date(2025, 6, 1), date(2026, 6, 30))
    assert len(inner.calls) == 3                                       # served from disk
    pd.testing.assert_frame_equal(inside, prices.loc["2025-06-01":"2026-06-30"], check_freq=False)
    again.fetch_adjusted_close(["ZZZ"], date(2025, 1, 1), AS_OF)
    assert len(inner.calls) == 4                                       # unknown tickers are not cached


def test_cache_keeps_tickers_fetched_before_a_rate_limit(tmp_path):
    prices = SyntheticPriceSource().fetch_adjusted_close(["VT", "AGG", "SHY"], date(2025, 1, 1), AS_OF)

    class _LimitedSource(_CountingSource):
        def fetch_adjusted_close(self, tickers, start, end):
            if len(self.calls) == 2:
                raise PriceSourceError("Tiingo rate limit reached (HTTP 429); try again later")
            return super().fetch_adjusted_close(tickers, start, end)

    limited = _LimitedSource(prices)
    with pytest.raises(PriceSourceError, match="rate limit"):
        CachedPriceSource(limited, tmp_path, today=lambda: date(2026, 10, 5)).fetch_adjusted_close(
            ["VT", "AGG", "SHY"], date(2025, 1, 1), AS_OF)
    retry = _CountingSource(prices)
    CachedPriceSource(retry, tmp_path, today=lambda: date(2026, 10, 5)).fetch_adjusted_close(
        ["VT", "AGG", "SHY"], date(2025, 1, 1), AS_OF)
    assert retry.calls == [(("SHY",), date(2025, 1, 1), AS_OF)]       # VT and AGG came from disk


def test_cache_refetches_beyond_the_cached_range_or_an_unfinished_day(tmp_path):
    prices = SyntheticPriceSource().fetch_adjusted_close(["VT"], date(2025, 1, 1), AS_OF)
    inner = _CountingSource(prices)
    CachedPriceSource(inner, tmp_path, today=lambda: AS_OF).fetch_adjusted_close(["VT"], date(2025, 1, 1), AS_OF)
    later = CachedPriceSource(inner, tmp_path, today=lambda: date(2026, 10, 5))
    later.fetch_adjusted_close(["VT"], date(2025, 1, 1), AS_OF)        # AS_OF was "today" at fetch time
    assert len(inner.calls) == 2
    later.fetch_adjusted_close(["VT"], date(2025, 1, 1), AS_OF)        # now final: from disk
    later.fetch_adjusted_close(["VT"], date(2024, 1, 1), AS_OF)        # earlier start: refetch
    assert len(inner.calls) == 3


def test_synthetic_assets_independent_of_universe():
    src = SyntheticPriceSource(seed=3)
    small = src.fetch_adjusted_close(["VT"], date(2025, 1, 1), AS_OF)
    big = src.fetch_adjusted_close(["VT", "AGG", "VNQ"], date(2025, 1, 1), AS_OF)
    pd.testing.assert_series_equal(small["VT"], big["VT"])


# ==========================================================================
# 5. Integration with Agent A
# ==========================================================================
def _context(risk_class, **kw):
    base = dict(investor_id="t", risk_class=risk_class, goal_type="growth", horizon_years=10,
                min_cash_buffer=0.05, max_acceptable_loss=0.25)
    base.update(kw)
    return InvestorContext(**base)


@pytest.mark.parametrize("risk_class", ["C1", "C2", "C3", "C4", "C5"])
@pytest.mark.parametrize("areas", [("stocks", "real_estate"), ("crypto",), ("technology", "bonds")])
def test_agent_a_runs_on_agent_c_data(risk_class, areas):
    agent_c = c_agent(interests(*areas))
    result = QuantAgent(agent_c, NoBehavioralEvidence(), AgentConfig(run_sensitivity=False)) \
        .run(_context(risk_class), AS_OF)
    assert result.final.status != "infeasible"
    assert set(result.final.weights) == set(agent_c.selection.tickers)
    assert result.menu.data_quality.passed


def test_agent_a_with_holdings_and_behavioral_limit():
    agent_c = c_agent(interests("stocks", held_assets=["VTI", "AGG", "BIL"]))
    context = _context("C4", current_holdings={"VTI": 0.5, "AGG": 0.3, "BIL": 0.2})
    result = QuantAgent(agent_c, StubBehavioralAgent(max_equity=0.5), AgentConfig(run_sensitivity=False)) \
        .run(context, AS_OF)
    assert result.final.equity_weight <= 0.5 + 1e-6
    assert result.final.implementation is not None


def test_price_table_feeds_backtest_provider():
    from egoquant.agents.quant.backtest import HistoricalMarketProvider

    agent_c = c_agent(interests("stocks"))
    prices, assets = agent_c.price_table(date(2023, 1, 1), AS_OF)
    provider = HistoricalMarketProvider(prices, assets)
    snap = provider.get_market_snapshot(None, date(2025, 6, 30), 252)
    assert snap.prices.index.max() <= pd.Timestamp(2025, 6, 30)
    assert list(prices.columns) == agent_c.selection.tickers


# ==========================================================================
# 6. Trading platforms and real-data routing
# ==========================================================================
@pytest.mark.parametrize("raw, expected", [
    ("Binance", "binance"), ("IBKR", "interactive_brokers"), ("moomoo", "futu_moomoo"),
    ("Charles Schwab", "charles_schwab"), ("binance.com", "binance"),
])
def test_platform_aliases(raw, expected):
    assert normalize_platform(raw) == expected


def test_unknown_platform_rejected():
    with pytest.raises(ValidationError, match="supported"):
        interests("stocks", trading_platforms=["My Uncle's Bank"])


def test_binance_user_gets_binance_crypto_not_ibit():
    sel = select_universe(interests("crypto", "real_estate", trading_platforms=["Binance"], held_assets=["btc"]))
    assert {"BTCUSDT", "ETHUSDT"} <= set(sel.tickers)
    assert "held" in next(i for i in sel.instruments if i.ticker == "BTCUSDT").reasons
    assert "IBIT" not in sel.tickers + sel.watchlist_tickers
    assert any(e.ticker == "VNQ" and "Binance" in e.reason for e in sel.excluded)
    assert any("model allocation" in n for n in sel.notes)          # core ETFs not on Binance


def test_us_broker_user_gets_ibit_and_no_binance_pairs():
    sel = select_universe(interests("crypto", "real_estate", trading_platforms=["IBKR"]))
    assert "IBIT" in sel.tickers and "VNQ" in sel.tickers
    assert not {"BTCUSDT", "ETHUSDT"} & set(sel.tickers + sel.watchlist_tickers)
    assert not any("model allocation" in n for n in sel.notes)


def test_no_platform_given_is_noted():
    sel = select_universe(interests("stocks"))
    assert any("platform not given" in n for n in sel.notes)


class _FakeBinanceSession:
    """Serves daily klines for BTCUSDT; unknown symbols get HTTP 400 like Binance."""

    def __init__(self, n_days=1200, end=date(2026, 10, 17)):
        self.days = pd.date_range(end=pd.Timestamp(end), periods=n_days, freq="D")
        self.calls = 0

    def get(self, url, params, timeout):
        self.calls += 1
        if params["symbol"] != "BTCUSDT":
            return _FakeResponse(400, {"code": -1121, "msg": "Invalid symbol."})
        day_ms = 86_400_000
        rows = []
        for i, day in enumerate(self.days):
            open_ms = int(day.timestamp() * 1000)
            if params["startTime"] <= open_ms <= params["endTime"]:
                close = 50_000 + 10 * i
                rows.append([open_ms, "0", "0", "0", str(close), "0", open_ms + day_ms - 1, "0", 0, "0", "0", "0"])
        return _FakeResponse(200, rows[: params["limit"]])


def test_binance_paginates_and_skips_unclosed_candle():
    session = _FakeBinanceSession(end=date(2026, 10, 17))
    now = pd.Timestamp(2026, 10, 17, 12).timestamp()                 # Oct 17 candle still open
    source = BinancePriceSource(session=session, clock=lambda: now)
    frame = source.fetch_adjusted_close(["BTCUSDT", "NOPEUSDT"], date(2023, 8, 1), date(2026, 10, 17))
    assert list(frame.columns) == ["BTCUSDT"]
    assert frame.index.max() == pd.Timestamp(2026, 10, 16)
    assert frame.index.min() == pd.Timestamp(2023, 8, 1)
    assert frame.index.is_unique and session.calls >= 2              # more than 1000 days -> 2+ pages


def test_routed_source_mixes_feeds_and_aligns_calendars(tmp_path):
    binance = BinancePriceSource(session=_FakeBinanceSession(end=AS_OF), clock=lambda: 4e9)
    us = CsvPriceSource(_csv_prices(tmp_path))
    routed = RoutedPriceSource({"us_listed": us, "crypto_spot": binance})
    agent = c_agent(interests("crypto", "real_estate", trading_platforms=["IBKR", "Binance"], max_r_level=4),
                    source=routed)                                     # crypto (R5) -> watchlist
    snap = agent.get_market_snapshot(None, AS_OF, 300)
    assert agent.last_report.price_sources["VT"] == "csv"
    assert snap.data_source == "historical"
    report = agent.get_watchlist_snapshot(AS_OF, 60)
    assert report.price_sources == {"BTCUSDT": "binance"}
    assert any(d.ticker == "ETHUSDT" for d in report.dropped)          # fake Binance only knows BTC
    assert any(d.ticker == "IBIT" for d in report.dropped)             # not in the CSV
    assert agent.last_watchlist_prices.index.dayofweek.max() == 6      # crypto alone keeps weekends


def test_weekend_rows_dropped_when_crypto_mixed_with_etfs(tmp_path):
    binance = BinancePriceSource(session=_FakeBinanceSession(end=AS_OF), clock=lambda: 4e9)
    routed = RoutedPriceSource({"us_listed": CsvPriceSource(_csv_prices(tmp_path)), "crypto_spot": binance})
    agent = c_agent(interests("stocks"), source=routed)
    raw = routed.fetch_adjusted_close(["VT", "BTCUSDT"], date(2025, 6, 1), AS_OF)
    clean, _, _ = agent._clean(raw, ["VT", "BTCUSDT"], AS_OF)
    assert clean.index.dayofweek.max() <= 4


def test_default_is_cached_real_data_and_explains_a_missing_key(monkeypatch):
    monkeypatch.setenv("TIINGO_API_KEY", "test-key")
    source = build_live_source({"us_listed", "crypto_spot"})          # no network call yet
    assert source.name == "binance+tiingo" and source.data_source == "historical"
    assert all(isinstance(s, CachedPriceSource) for s in source.sources.values())
    assert MarketIntelligenceAgent(interests("stocks")).source.name == "tiingo"
    monkeypatch.delenv("TIINGO_API_KEY")
    with pytest.raises(PriceSourceError, match="tiingo.com"):
        MarketIntelligenceAgent(interests("stocks"))


def test_export_and_replay_csv(tmp_path):
    agent = c_agent(interests("crypto", trading_platforms=["Binance"], max_r_level=4))   # crypto -> watchlist
    path = tmp_path / "saved.csv"
    agent.export_prices(date(2024, 1, 1), AS_OF, str(path))
    replay = c_agent(interests("crypto", trading_platforms=["Binance"], max_r_level=4), source=CsvPriceSource(path))
    a = agent.get_market_snapshot(None, AS_OF, 300).prices
    b = replay.get_market_snapshot(None, AS_OF, 300).prices
    pd.testing.assert_frame_equal(a, b, check_freq=False)
    assert set(replay.get_watchlist_snapshot(AS_OF).delivered) == {"BTCUSDT", "ETHUSDT"}


# ==========================================================================
# 7. Instrument preferences, the universe budget and the whole Binance market
# ==========================================================================
def test_funds_style_offers_no_single_stocks_and_individual_replaces_industry_funds():
    funds = select_universe(interests("technology", "healthcare"))
    assert "QQQ" in funds.tickers and "XLV" in funds.tickers
    assert not any(i.product_type == "stock" for i in funds.instruments)
    single = select_universe(interests("technology", "healthcare", instrument_style="individual"))
    assert {"AAPL", "MSFT", "LLY"} <= set(single.tickers) and not {"QQQ", "XLV"} & set(single.tickers)
    mix = select_universe(interests("technology", instrument_style="mix"))
    assert "QQQ" in mix.tickers and "AAPL" in mix.tickers


def test_single_stocks_carry_the_instrument_cap_to_agent_a():
    agent = c_agent(interests("technology", instrument_style="individual"))
    assets = {a.asset_id: a for a in agent.get_market_snapshot(None, AS_OF, 300).assets}
    assert assets["AAPL"].max_weight == pytest.approx(0.10) and assets["VT"].max_weight is None


def test_avoided_industries_are_left_out_with_a_reason():
    from egoquant.agents.market_intel import Instrument

    oil_fund = Instrument(ticker="OILY", name="An oil fund", asset_class="equity", product_type="equity_etf",
                          r_level=3, areas=["dividend_stocks"], sector="energy")
    sel = select_universe(interests("dividend_stocks", avoid_sectors=["energy"]), catalog=[*DEFAULT_CATALOG, oil_fund])
    assert "OILY" not in sel.tickers and "SCHD" in sel.tickers
    assert any(e.ticker == "OILY" and "avoid the energy industry" in e.reason for e in sel.excluded)
    assert any("broad funds" in n for n in sel.notes)
    with pytest.raises(ValidationError, match="avoid"):
        interests("energy", avoid_sectors=["energy"])


def test_budget_keeps_core_and_serves_every_pick_before_seconds():
    areas = ["technology", "healthcare", "financials", "energy", "consumer"]
    sel = select_universe(interests(*areas, instrument_style="individual"), max_instruments=10)
    assert len(sel.tickers) == 10 and sel.tickers[:4] == CORE
    stocks = [i for i in sel.instruments if i.product_type == "stock"]
    assert {i.reasons[0] for i in stocks} == {f"interest:{a}" for a in areas}    # one per industry first
    assert sel.deprioritized and all("universe" in d.reason for d in sel.deprioritized)


def test_budget_limits_single_stocks_and_crypto_per_universe():
    sel = select_universe(interests("technology", "healthcare", "financials", instrument_style="individual",
                                    trading_platforms=["IBKR", "Binance"]), max_instruments=40, max_single_stocks=4)
    assert sum(i.product_type == "stock" for i in sel.instruments) == 4
    crypto = select_universe(interests("crypto", "altcoins", trading_platforms=["Binance"]), max_crypto=3)
    assert sum(i.asset_class == "crypto" for i in crypto.instruments) == 3


# ---- exposure groups, relevance and the data budget ----
def test_every_exposure_group_holds_one_kind_of_fund():
    from egoquant.agents.market_intel.catalog import EXPOSURE_GROUPS

    by_ticker = {i.ticker: i for i in DEFAULT_CATALOG}
    for group, members in EXPOSURE_GROUPS.items():
        kinds = {(by_ticker[t].asset_class, by_ticker[t].product_type, by_ticker[t].r_level) for t in members}
        assert len(kinds) == 1, group
        assert all(by_ticker[t].exposure_group == group for t in members)


def test_only_one_fund_per_exposure_goes_to_agent_a_the_rest_are_fallbacks():
    sel = select_universe(interests("us_stocks", "gold", "bonds"))
    assert "VTI" in sel.tickers and not {"VOO", "IVV", "SPY"} & set(sel.tickers)
    assert sel.alternates["VTI"][:3] == ["VOO", "IVV", "SPY"]
    assert "IAU" in sel.tickers and sel.alternates["IAU"] == ["GLD", "GLDM", "SGOL"]
    assert sel.alternates["AGG"] == ["BND", "SCHZ"]                  # a core fund represents its group


def test_a_holding_represents_its_exposure_group():
    sel = select_universe(interests("us_stocks", held_assets=["SPY"]))
    assert "SPY" in sel.tickers and not {"VTI", "VOO"} & set(sel.tickers)
    assert sel.alternates["SPY"][:2] == ["VTI", "VOO"]


def test_instruments_matching_more_interests_come_first():
    sel = select_universe(interests("emerging_markets", "china", max_r_level=4))
    assert sel.fetch_order[:4] == CORE and sel.fetch_order[4] == "MCHI"     # china AND emerging markets


class _BudgetSource:
    """A 'live' source on SYNTHETIC prices: counts requests, knows which tickers are cached on disk."""

    name = "fake-live"
    data_source = "synthetic"

    def __init__(self, cached=(), no_data=(), rate_limit_after=None):
        self.inner = SyntheticPriceSource()
        self.cached, self.no_data, self.rate_limit_after = set(cached), set(no_data), rate_limit_after
        self.calls: list[str] = []

    def needs_request(self, ticker, start, end):
        return ticker not in self.cached

    def fetch_adjusted_close(self, tickers, start, end):
        for t in tickers:
            if t not in self.cached:
                if self.rate_limit_after is not None and len(self.calls) >= self.rate_limit_after:
                    from egoquant.agents.market_intel import RateLimitError
                    raise RateLimitError("fake rate limit")
                self.calls.append(t)
        return self.inner.fetch_adjusted_close([t for t in tickers if t not in self.no_data], start, end)


def test_fetch_budget_prices_the_most_relevant_first_and_reports_the_rest():
    src = _BudgetSource()
    agent = c_agent(interests("technology", "healthcare", instrument_style="individual"), source=src)
    order = list(agent.selection.fetch_order)
    report = agent.fetch_universe(AS_OF, 252, budget={"us_listed": 8})
    assert src.calls == order[:8]                                    # core first, then interests round-robin
    assert agent.selection.tickers == [t for t in agent.selection.tickers if t in order[:8]]
    assert len(agent.selection.tickers) == 8 and report.new_requests == {"us_listed": 8}
    assert {e.ticker for e in agent.selection.not_fetched} == set(order[8:])
    assert all("data budget" in e.reason for e in agent.selection.not_fetched)
    assert any("priced 8 instrument(s)" in n for n in agent.selection.notes)


def test_cached_prices_cost_nothing_against_the_budget():
    src = _BudgetSource(cached=["BIL", "AGG", "SHY", "VT", "AAPL", "LLY"])
    agent = c_agent(interests("technology", "healthcare", instrument_style="individual"), source=src)
    report = agent.fetch_universe(AS_OF, 252, budget={"us_listed": 2})
    assert {"AAPL", "LLY"} <= set(agent.selection.tickers) and len(src.calls) == 2
    assert set(report.from_cache) == {"BIL", "AGG", "SHY", "VT", "AAPL", "LLY"}


def test_core_funds_are_fetched_even_with_no_budget_left():
    src = _BudgetSource()
    agent = c_agent(interests("technology"), source=src)
    agent.fetch_universe(AS_OF, 252, budget={"us_listed": 0})
    assert src.calls == CORE and agent.selection.tickers == CORE


def test_a_fund_without_data_is_replaced_by_one_tracking_the_same_market():
    src = _BudgetSource(no_data=["VTI", "VOO"])
    agent = c_agent(interests("us_stocks"), source=src)
    report = agent.fetch_universe(AS_OF, 252)
    assert report.replaced == {"VTI": "IVV"} and "IVV" in agent.selection.tickers and "VTI" not in agent.selection.tickers
    ivv = next(i for i in agent.selection.instruments if i.ticker == "IVV")
    assert "interest:us_stocks" in ivv.reasons and "replaces:VTI" in ivv.reasons
    assert any("IVV, which tracks the same market" in n for n in agent.selection.notes)


def test_a_rate_limit_keeps_what_was_already_priced():
    src = _BudgetSource(rate_limit_after=6)
    agent = c_agent(interests("technology", "healthcare", instrument_style="individual"), source=src)
    report = agent.fetch_universe(AS_OF, 252)
    assert len(agent.selection.tickers) == 6 and report.rate_limited == ["us_listed"]
    assert all("rate limit" in e.reason for e in agent.selection.not_fetched)


def test_agent_a_snapshot_after_the_fetch_makes_no_new_requests():
    src = _BudgetSource()
    agent = c_agent(interests("technology", "gold"), source=src)
    agent.fetch_universe(AS_OF, 252)
    calls = len(src.calls)
    snap = agent.get_market_snapshot(agent.selection.tickers, AS_OF, 252)
    assert len(src.calls) == calls and list(snap.prices.columns) == agent.selection.tickers


class _FakeMarketSession:
    """exchangeInfo + 24h tickers, like Binance, including the listings that must be left out."""

    PAIRS = [("BTCUSDT", "BTC", 900.0), ("ETHUSDT", "ETH", 500.0), ("NEWUSDT", "NEW", 400.0), ("BNBUSDT", "BNB", 300.0),
             ("AAPLBUSDT", "AAPLB", 800.0), ("USDCUSDT", "USDC", 999.0), ("PAXGUSDT", "PAXG", 50.0),
             ("SOLUSDT", "SOL", 350.0), ("SOLBTC", "SOL", 10.0), ("OLDUSDT", "OLD", 1.0)]

    def get(self, url, params, timeout):
        if url.endswith("exchangeInfo"):
            symbols = [{"symbol": s, "baseAsset": b, "quoteAsset": s[len(b):], "status": "TRADING",
                        "isSpotTradingAllowed": True} for s, b, _ in self.PAIRS]
            symbols.append({"symbol": "DEADUSDT", "baseAsset": "DEAD", "quoteAsset": "USDT", "status": "BREAK"})
            return _FakeResponse(200, {"symbols": symbols})
        return _FakeResponse(200, [{"symbol": s, "quoteVolume": str(v)} for s, _, v in self.PAIRS])


def test_binance_market_lists_crypto_assets_only_by_volume():
    from egoquant.agents.market_intel import BinanceMarket

    pairs = BinanceMarket(session=_FakeMarketSession()).ranked_pairs()
    assert [p[0] for p in pairs] == ["BTCUSDT", "ETHUSDT", "NEWUSDT", "SOLUSDT", "BNBUSDT", "OLDUSDT"]
    assert BinanceMarket.is_crypto_asset("BNB") and not BinanceMarket.is_crypto_asset("NVDAB")


def test_crypto_eligible_investor_gets_the_whole_market_ranked_by_volume():
    from egoquant.agents.market_intel import BinanceMarket

    market = BinanceMarket(session=_FakeMarketSession())
    eligible = c_agent(interests("stocks", "altcoins", trading_platforms=["Binance", "IBKR"]), binance_market=market)
    coins = [i.ticker for i in eligible.catalog if "altcoins" in i.areas and i.asset_class == "crypto"]
    assert coins == ["NEWUSDT", "SOLUSDT", "BNBUSDT", "OLDUSDT"]                   # catalog-only XRP is not listed
    assert eligible.selection.tickers[-1] == "NEWUSDT" or "NEWUSDT" in eligible.selection.tickers
    not_allowed = c_agent(interests("stocks", "altcoins", max_r_level=4, trading_platforms=["Binance"]),
                          binance_market=market)
    assert len(not_allowed.catalog) == len(DEFAULT_CATALOG)                        # no listing without crypto access


def test_recent_listings_are_dropped_instead_of_cutting_everyone_s_history(tmp_path):
    frame = SyntheticPriceSource().fetch_adjusted_close(CORE + ["VTI"], date(2024, 1, 1), AS_OF)
    frame.loc[: pd.Timestamp(2025, 6, 1), "VTI"] = np.nan                           # "listed" in June 2025
    path = tmp_path / "late.csv"
    frame.rename_axis("date").reset_index().to_csv(path, index=False)
    agent = c_agent(interests("stocks"), source=CsvPriceSource(path))
    snap = agent.get_market_snapshot(None, AS_OF, 400)
    assert "VTI" not in snap.prices.columns and len(snap.prices) == 401
    assert any(d.ticker == "VTI" and "later than the rest" in d.reason for d in agent.last_report.dropped)


def test_crypto_market_report_prices_every_listed_coin():
    from egoquant.agents.market_intel import BinanceMarket

    agent = c_agent(interests("crypto", "altcoins", trading_platforms=["Binance"]),
                    binance_market=BinanceMarket(session=_FakeMarketSession()))
    agent.source = SyntheticPriceSource(catalog=agent.catalog)                      # offline prices for every coin
    report = agent.get_crypto_market(AS_OF, lookback_days=60)
    assert [e.ticker for e in report.entries] == ["BTCUSDT", "ETHUSDT", "NEWUSDT", "SOLUSDT", "BNBUSDT", "OLDUSDT"]
    assert report.entries[0].volume_rank == 1 and report.entries[0].quote_volume_24h == 900.0
    assert all(e.change_30d is not None for e in report.entries)
    with pytest.raises(ValueError, match="only for investors"):
        c_agent(interests("stocks")).get_crypto_market(AS_OF)
