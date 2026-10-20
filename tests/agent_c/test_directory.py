"""Tests for Agent C's investment directory and the cluster walk in fetch_universe.

All inputs are small hand-made rows shaped like the real sources (Nasdaq screener, Nasdaq symbol
directory, Tiingo supported tickers, Tiingo IEX); prices are SYNTHETIC. No network access is needed.
"""

from datetime import date

import pytest

from egoquant.agents.market_intel import (
    DEFAULT_CATALOG,
    InvestorInterests,
    MarketIntelligenceAgent,
    RateLimitError,
    SyntheticPriceSource,
)
from egoquant.agents.market_intel.directory import (
    build_directory,
    classify_etf,
    classify_stock,
    etf_signature,
    load_directory,
    merge_catalog,
)
from egoquant.agents.market_intel.sources import TiingoUsage

AS_OF = date(2026, 9, 30)
TODAY = date(2026, 10, 18)


# ==========================================================================
# 1. Classification
# ==========================================================================
@pytest.mark.parametrize("sector, industry, country, cap, expected", [
    ("Technology", "Semiconductors", "United States", 4e12, ["technology"]),
    ("Finance", "Real Estate Investment Trusts", "United States", 5e10, ["real_estate"]),
    ("Industrials", "Biotechnology: Laboratory Analytical Instruments", "United States", 5e10, ["healthcare"]),
    ("Consumer Discretionary", "Catalog/Specialty Distribution", "China", 3e11, ["consumer", "china"]),
    ("Technology", "Semiconductors", "Taiwan", 1e12, ["technology", "international_stocks"]),
    ("Energy", "Oil & Gas Production", "United States", 1e9, ["energy", "small_caps"]),
])
def test_stocks_are_clustered_by_industry_region_and_size(sector, industry, country, cap, expected):
    areas, _ = classify_stock("Some Company Common Stock", sector, industry, country, cap)
    assert areas == expected


@pytest.mark.parametrize("name, sector, cap, reason", [
    ("Acme Acquisition Corp Warrant", "Finance", 1e9, "not a common share"),
    ("Acme Corp 6.5% Notes due 2030", "Finance", 1e9, "not a common share"),
    ("Tiny Corp Common Stock", "Technology", 5e7, "market value"),
    ("Mystery Corp Common Stock", "", 5e9, "no industry"),
])
def test_stocks_that_are_left_out_say_why(name, sector, cap, reason):
    assert reason in classify_stock(name, sector, "", "United States", cap)


@pytest.mark.parametrize("name, areas, r_level", [
    ("SPDR Gold Shares", ["gold"], 3),
    ("Goldman Sachs Access Treasury 0-1 Year ETF", ["government_bonds"], 1),
    ("iShares 20+ Year Treasury Bond ETF", ["long_term_bonds"], 3),
    ("iShares 0-3 Month Treasury Bond ETF", ["cash"], 1),
    ("iShares iBoxx $ High Yield Corporate Bond ETF", ["corporate_bonds"], 3),
    ("iShares TIPS Bond ETF", ["inflation_protected"], 2),
    ("iShares Biotechnology ETF", ["healthcare"], 4),
    ("VanEck Semiconductor ETF", ["technology"], 4),
    ("VanEck Gold Miners ETF", ["materials"], 4),
    ("SPDR Dow Jones Industrial Average ETF Trust", ["stocks", "us_stocks"], 3),
    ("iShares MSCI Emerging Markets ex China ETF", ["emerging_markets"], 4),
    ("iShares China Large-Cap ETF", ["china", "emerging_markets"], 4),
    ("iShares Bitcoin Trust ETF", ["crypto"], 5),
    ("Bitwise Crypto Industry Innovators ETF", ["technology"], 4),
    ("Vanguard S&P 500 ETF", ["stocks", "us_stocks"], 3),
    ("Schwab U.S. Dividend Equity ETF", ["dividend_stocks"], 3),
])
def test_etf_names_map_to_clusters_with_the_catalog_r_level_rule(name, areas, r_level):
    entry = classify_etf(name)
    assert not isinstance(entry, str), entry
    assert entry.areas == areas and entry.r_level == r_level


@pytest.mark.parametrize("name", [
    "ProShares UltraPro QQQ", "Direxion Daily Semiconductor Bull 3X Shares", "ProShares Short S&P500",
    "GraniteShares 2x Long NVDA Daily ETF", "JPMorgan Equity Premium Income ETF", "Global X Nasdaq 100 Covered Call ETF",
    "Innovator U.S. Equity Buffer ETF - October", "iPath Series B S&P 500 VIX Short-Term Futures ETN",
    "NEOS Nasdaq-100 High Income ETF", "WisdomTree Efficient Gold Plus Equity Strategy Fund",
])
def test_complex_products_are_excluded(name):
    assert isinstance(classify_etf(name), str)


def test_funds_tracking_the_same_index_share_a_signature():
    assert etf_signature("Vanguard S&P 500 ETF") == etf_signature("SPDR S&P 500 ETF Trust") == "s&p 500"
    assert etf_signature("Invesco S&P 500 Equal Weight ETF") != "s&p 500"
    assert etf_signature("iShares Core U.S. Aggregate Bond ETF") != etf_signature(
        "iShares Core International Aggregate Bond ETF")


# ==========================================================================
# 2. Building the directory from the four sources
# ==========================================================================
def _stock(symbol, name, sector, cap, industry="", country="United States"):
    return {"symbol": symbol, "name": name, "sector": sector, "industry": industry, "country": country,
            "marketCap": str(cap)}


def _tiingo(ticker, start="2015-01-02", end="2026-10-16"):
    return {"ticker": ticker, "exchange": "NYSE", "assetType": "Stock", "priceCurrency": "USD",
            "startDate": start, "endDate": end}


def _iex(ticker, volume, price=100.0):
    return {"ticker": ticker.lower(), "volume": volume, "prevClose": price}


STOCKS = [_stock("AAA", "Alpha Inc. Common Stock", "Technology", 5e11),
          _stock("BBB", "Beta Corp Common Stock", "Technology", 9e11),
          _stock("CCC", "Gamma Ltd Common Stock", "Technology", 2e11),
          _stock("NEW", "Newly Listed Inc Common Stock", "Technology", 3e11),
          _stock("BRK/B", "Berkshire Hathaway Inc. Class B", "Finance", 1e12, industry="Property-Casualty Insurers"),
          _stock("PRF^A", "Pref Co 5% Preferred", "Finance", 1e10)]
SYMBOLS = [{"Symbol": "GLDX", "Security Name": "Example Gold Trust", "ETF": "Y", "Test Issue": "N"},
           {"Symbol": "TQQX", "Security Name": "Example UltraPro QQQ 3x", "ETF": "Y", "Test Issue": "N"},
           {"ACT Symbol": "SMHX", "Security Name": "Example Semiconductor ETF", "ETF": "Y", "Test Issue": "N"},
           {"Symbol": "TEST", "Security Name": "Test Issue Gold ETF", "ETF": "Y", "Test Issue": "Y"}]
TIINGO = [_tiingo(t) for t in ("AAA", "BBB", "CCC", "BRK-B", "GLDX", "SMHX")] + [_tiingo("NEW", start="2026-05-01")]
IEX = [_iex("AAA", 9e6), _iex("BBB", 1e6), _iex("CCC", 5e6), _iex("BRK-B", 3e6), _iex("GLDX", 2e6),
       _iex("SMHX", 4e6), _iex("NEW", 8e6)]


def test_directory_keeps_what_tiingo_prices_ranked_by_dollar_volume():
    d = build_directory(STOCKS, SYMBOLS, TIINGO, IEX, TODAY)
    tickers = [e.ticker for e in d.entries]
    assert tickers == ["AAA", "CCC", "SMHX", "BRK-B", "GLDX", "BBB"]     # most traded first
    assert "NEW" not in tickers and d.excluded["not priced by Tiingo (or too little history)"] == 1
    assert "TQQX" not in tickers and "TEST" not in tickers and "PRF^A" not in tickers
    assert d.clusters()["technology"] == ["AAA", "CCC", "SMHX", "BBB"]
    brk = next(e for e in d.entries if e.ticker == "BRK-B")
    assert brk.areas == ["financials"] and brk.name == "Berkshire Hathaway Inc. Class B"


def test_thinly_traded_instruments_are_left_out_when_volume_is_known():
    d = build_directory(STOCKS, SYMBOLS, TIINGO, IEX + [_iex("BBB", 100)], TODAY)
    assert "BBB" not in [e.ticker for e in d.entries]
    without_volume = build_directory(STOCKS, SYMBOLS, TIINGO, None, TODAY)
    assert [e.ticker for e in without_volume.entries][:2] == ["BRK-B", "BBB"]   # by market value instead


def test_merged_catalog_keeps_core_first_and_curated_metadata():
    d = build_directory(STOCKS + [_stock("AAPL", "Apple Inc. Common Stock", "Technology", 4e12)], SYMBOLS,
                        TIINGO + [_tiingo("AAPL")], IEX + [_iex("AAPL", 1e8)], TODAY)
    merged = merge_catalog(d, DEFAULT_CATALOG)
    assert [i.ticker for i in merged[:4]] == ["BIL", "AGG", "SHY", "VT"]
    assert merged[4].ticker == "AAPL" and merged[4].max_weight == pytest.approx(0.10)
    assert {"QQQ", "IAU"} <= {i.ticker for i in merged}                   # curated extras stay available
    assert len({i.ticker for i in merged}) == len(merged)


def test_load_directory_uses_a_fresh_saved_copy_without_network(tmp_path, monkeypatch):
    path = tmp_path / "directory.json"
    path.write_text(build_directory(STOCKS, SYMBOLS, TIINGO, IEX, TODAY).model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("egoquant.agents.market_intel.directory.fetch_nasdaq_stocks",
                        lambda *a, **k: pytest.fail("a fresh directory must not be rebuilt"))
    assert load_directory(today=TODAY, path=path).entries[0].ticker == "AAA"


def test_load_directory_falls_back_to_the_saved_copy_when_offline(tmp_path, monkeypatch):
    import requests

    def offline(*args, **kwargs):
        raise requests.ConnectionError("no network")

    path = tmp_path / "directory.json"
    path.write_text(build_directory(STOCKS, SYMBOLS, TIINGO, IEX, date(2026, 1, 1)).model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("egoquant.agents.market_intel.directory.fetch_nasdaq_stocks", offline)
    assert load_directory(today=TODAY, path=path).built_on == date(2026, 1, 1)           # stale but usable
    assert load_directory(today=TODAY, path=tmp_path / "none.json") is None             # curated catalog then


# ==========================================================================
# 3. Agent C walks the clusters
# ==========================================================================
def _many_tech_stocks(n=40):
    rows = [_stock(f"T{i:02d}", f"Tech {i} Inc Common Stock", "Technology", 1e12 - i * 1e9) for i in range(n)]
    return build_directory(rows, [], [_tiingo(r["symbol"]) for r in rows],
                           [_iex(r["symbol"], 1e7 - i * 1e5) for i, r in enumerate(rows)], TODAY)


class _Source:
    """SYNTHETIC prices for a given catalog; some tickers have no data; 'live' when requests are counted."""

    name = "fake-live"
    data_source = "synthetic"

    def __init__(self, catalog, no_data=(), live=True, rate_limit=None):
        self.inner = SyntheticPriceSource(catalog=catalog)
        self.no_data, self.live, self.rate_limit = set(no_data), live, rate_limit
        self.calls: list[str] = []

    def needs_request(self, ticker, start, end):
        return self.live

    def fetch_adjusted_close(self, tickers, start, end):
        if self.rate_limit is not None and len(self.calls) >= self.rate_limit:
            raise RateLimitError("fake")
        self.calls += list(tickers)
        return self.inner.fetch_adjusted_close([t for t in tickers if t not in self.no_data], start, end)


def _agent(directory, source_factory=None, **interest_fields):
    catalog = merge_catalog(directory, DEFAULT_CATALOG)
    interests = InvestorInterests(investor_id="t", **{"interest_areas": ["technology"], "instrument_style": "individual",
                                                      **interest_fields})
    source = (source_factory or (lambda c: _Source(c)))(catalog)
    return MarketIntelligenceAgent(interests, source=source, directory=directory), source


def test_cluster_walk_takes_the_most_traded_and_caps_each_area():
    agent, src = _agent(_many_tech_stocks())
    agent.fetch_universe(AS_OF, 252, budget={"us_listed": 100}, max_per_area=25)
    stocks = [t for t in agent.selection.tickers if t.startswith("T")]
    assert stocks == [f"T{i:02d}" for i in range(25)]                   # most traded 25 of 40


def test_an_instrument_without_data_is_replaced_by_the_next_in_its_cluster():
    agent, src = _agent(_many_tech_stocks(), lambda c: _Source(c, no_data={"T00", "T03"}))
    report = agent.fetch_universe(AS_OF, 252, budget={"us_listed": 100}, max_per_area=5)
    assert [t for t in agent.selection.tickers if t.startswith("T")] == ["T01", "T02", "T04", "T05", "T06"]
    assert {e.ticker for e in report.not_fetched} >= {"T00", "T03"} and report.n_not_fetched >= 2


def test_the_budget_stops_new_requests_but_core_funds_are_always_priced():
    agent, src = _agent(_many_tech_stocks())
    agent.fetch_universe(AS_OF, 252, budget={"us_listed": 7})
    assert src.calls[:4] == ["BIL", "AGG", "SHY", "VT"] and len(src.calls) == 7
    assert agent.selection.tickers == ["BIL", "AGG", "SHY", "VT", "T00", "T01", "T02"]


def test_single_stock_cap_holds_across_areas():
    agent, _ = _agent(_many_tech_stocks())
    agent.fetch_universe(AS_OF, 252, budget={"us_listed": 100}, max_single_stocks=3)
    assert sum(i.product_type == "stock" for i in agent.selection.instruments) == 3


def test_crypto_investors_get_the_whole_binance_market_priced_in_parallel():
    from egoquant.agents.market_intel import Instrument

    coins = [Instrument(ticker=f"C{i:02d}USDT", name=f"Coin {i}", asset_class="crypto", product_type="crypto_spot",
                        r_level=5, areas=["altcoins"], market="crypto_spot", synthetic_mu=0.1, synthetic_vol=0.9,
                        synthetic_equity_loading=0.4) for i in range(30)]
    catalog = [*DEFAULT_CATALOG, *coins]
    interests = InvestorInterests(investor_id="t", interest_areas=["altcoins"], trading_platforms=["Binance", "IBKR"])
    src = _Source(catalog)
    agent = MarketIntelligenceAgent(interests, source=src, catalog=catalog, directory=None)
    report = agent.fetch_universe(AS_OF, 252, max_crypto=5)
    assert {f"C{i:02d}USDT" for i in range(30)} <= set(src.calls)       # every coin priced (prefetch)
    assert report.crypto_market_priced >= 30
    assert sum(i.asset_class == "crypto" for i in agent.selection.instruments) == 5


def test_tiingo_usage_refuses_new_tickers_near_the_monthly_limit(tmp_path):
    usage = TiingoUsage(tmp_path / "usage.json", limit=2, today=lambda: date(2026, 10, 18))
    usage.record("AAA")
    usage.record("BBB")
    assert usage.allows("AAA") and not usage.allows("CCC")             # refreshing a known ticker is fine
    next_month = TiingoUsage(tmp_path / "usage.json", limit=2, today=lambda: date(2026, 11, 2))
    assert next_month.allows("CCC")


def test_a_directory_built_with_older_rules_is_rebuilt(tmp_path, monkeypatch):
    path = tmp_path / "directory.json"
    old = build_directory(STOCKS, SYMBOLS, TIINGO, IEX, TODAY).model_copy(update={"rules_version": "old"})
    path.write_text(old.model_dump_json(), encoding="utf-8")
    calls = []
    monkeypatch.setattr("egoquant.agents.market_intel.directory.fetch_nasdaq_stocks",
                        lambda *a, **k: calls.append(1) or STOCKS)
    monkeypatch.setattr("egoquant.agents.market_intel.directory.fetch_nasdaq_symbols", lambda *a, **k: SYMBOLS)
    monkeypatch.setattr("egoquant.agents.market_intel.directory.fetch_tiingo_tickers", lambda *a, **k: TIINGO)
    monkeypatch.setattr("egoquant.agents.market_intel.directory.fetch_tiingo_iex", lambda *a, **k: IEX)
    rebuilt = load_directory(today=TODAY, path=path)
    assert calls and rebuilt.rules_version != "old"


def _coin(ticker, vol):
    from egoquant.agents.market_intel import Instrument

    return Instrument(ticker=ticker, name=ticker, asset_class="crypto", product_type="crypto_spot", r_level=5,
                      areas=["altcoins"], market="crypto_spot", synthetic_mu=0.0, synthetic_vol=vol)


def test_a_stablecoin_is_left_out_and_the_next_coin_used():
    catalog = [*[i for i in DEFAULT_CATALOG if "altcoins" not in i.areas],
               _coin("STBLUSDT", 0.01), _coin("REALUSDT", 0.9), _coin("NEXTUSDT", 0.9)]
    interests = InvestorInterests(investor_id="t", interest_areas=["altcoins"], trading_platforms=["Binance", "IBKR"])
    agent = MarketIntelligenceAgent(interests, source=_Source(catalog), catalog=catalog, directory=None)
    report = agent.fetch_universe(AS_OF, 252, max_per_area=2)
    assert [t for t in agent.selection.tickers if t.endswith("USDT") and t not in ("BTCUSDT", "ETHUSDT")] \
        == ["REALUSDT", "NEXTUSDT"]
    assert any(e.ticker == "STBLUSDT" and "stablecoin" in e.reason for e in report.not_fetched)


def test_a_late_listing_is_replaced_by_the_next_in_its_cluster():
    import pandas as pd

    class _LateSource(_Source):
        def fetch_adjusted_close(self, tickers, start, end):
            frame = super().fetch_adjusted_close(tickers, start, end)
            return frame.loc[pd.Timestamp(end) - pd.Timedelta(days=360):] if "T00" in tickers else frame

    agent, _ = _agent(_many_tech_stocks(), lambda c: _LateSource(c))
    report = agent.fetch_universe(AS_OF, 252, budget={"us_listed": 100}, max_per_area=3)
    assert [t for t in agent.selection.tickers if t.startswith("T")] == ["T01", "T02", "T03"]
    assert any(e.ticker == "T00" and "later than the analysis window" in e.reason for e in report.not_fetched)
    assert set(agent.get_market_snapshot(agent.selection.tickers, AS_OF, 252).prices.columns) == set(agent.selection.tickers)


def test_without_volume_etfs_are_not_ranked_alphabetically_but_left_to_the_curated_catalog():
    d = build_directory(STOCKS, SYMBOLS, TIINGO, [], TODAY)
    assert all(e.kind == "stock" for e in d.entries) and d.excluded["no trading volume to rank it"] == 2
    assert [e.ticker for e in d.entries][:2] == ["BRK-B", "BBB"]                 # stocks by market value


def test_a_rebuild_without_volume_today_ranks_with_the_saved_volumes(tmp_path, monkeypatch):
    import requests

    def rate_limited(*args, **kwargs):
        raise requests.HTTPError("429 hourly request allocation")

    path = tmp_path / "directory.json"
    old = build_directory(STOCKS, SYMBOLS, TIINGO, IEX, date(2026, 10, 1)).model_copy(update={"rules_version": "old"})
    path.write_text(old.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("egoquant.agents.market_intel.directory.fetch_tiingo_iex", rate_limited)
    monkeypatch.setattr("egoquant.agents.market_intel.directory.fetch_nasdaq_stocks", lambda *a, **k: STOCKS)
    monkeypatch.setattr("egoquant.agents.market_intel.directory.fetch_nasdaq_symbols", lambda *a, **k: SYMBOLS)
    monkeypatch.setattr("egoquant.agents.market_intel.directory.fetch_tiingo_tickers", lambda *a, **k: TIINGO)
    rebuilt = load_directory(today=TODAY, path=path)
    assert rebuilt.rules_version != "old" and "Tiingo IEX volume (saved)" in rebuilt.sources
    assert [e.ticker for e in rebuilt.entries] == [e.ticker for e in old.entries]     # same ranking, new rules


def test_structured_protection_funds_are_excluded():
    assert isinstance(classify_etf("Innovator Equity Defined Protection ETF - 2 Yr to April 2028"), str)


@pytest.mark.parametrize("name, expected", [
    ("Horizon Kinetics Inflation Beneficiaries ETF", None),          # an equity fund, not inflation-protected bonds
    ("Vanguard Short-Term Inflation-Protected Securities ETF", ["inflation_protected"]),
    ("Schwab U.S. TIPS ETF", ["inflation_protected"]),
])
def test_inflation_protected_needs_bond_wording(name, expected):
    entry = classify_etf(name)
    assert (entry if isinstance(entry, str) else entry.areas) == (expected or entry)


def test_municipal_funds_tracking_the_same_market_share_a_signature():
    assert etf_signature("iShares National Muni Bond ETF") == etf_signature("Vanguard Tax-Exempt Bond ETF")
    assert etf_signature("iShares Short-Term National Muni Bond ETF") != etf_signature("iShares National Muni Bond ETF")


@pytest.mark.parametrize("name, asset_class, r_level", [
    ("Global X 1-3 Month T-Bill ETF", "cash", 1),
    ("Texas Capital Government Money Market ETF", "cash", 1),
    ("iShares Treasury Floating Rate Bond ETF", "cash", 1),
    ("JPMorgan Ultra-Short Income ETF", "bond", 2),          # credit risk: never "cash" (Agent A treats cash as riskless)
    ("iShares Floating Rate Bond ETF", "bond", 2),
])
def test_only_treasury_bills_and_government_money_market_count_as_cash(name, asset_class, r_level):
    entry = classify_etf(name)
    assert (entry.asset_class, entry.r_level) == (asset_class, r_level)


class _BrokenFeed:
    name = "tiingo"
    data_source = "historical"

    def needs_request(self, ticker, start, end):
        return True

    def fetch_adjusted_close(self, tickers, start, end):
        from egoquant.agents.market_intel import PriceSourceError
        raise PriceSourceError("Tiingo refused the API key (HTTP 401)")


def test_fetch_universe_switches_the_whole_run_to_synthetic_only_when_allowed():
    from egoquant.agents.market_intel import PriceSourceError

    interests = InvestorInterests(investor_id="t", interest_areas=["stocks", "bonds"], max_r_level=3)
    with pytest.raises(PriceSourceError):
        MarketIntelligenceAgent(interests, source=_BrokenFeed(), fallback_to_synthetic=False).fetch_universe(AS_OF, 252)
    agent = MarketIntelligenceAgent(interests, source=_BrokenFeed(), fallback_to_synthetic=True)
    agent.fetch_universe(AS_OF, 252)
    snapshot = agent.get_market_snapshot(agent.selection.tickers, AS_OF, 252)
    assert snapshot.data_source == "synthetic" and "401" in agent.last_report.fallback_reason
    assert set(snapshot.prices.columns) == set(agent.selection.tickers)


def test_tiingo_ticker_list_keeps_only_the_us_usd_rows_build_directory_reads():
    import io
    import zipfile

    from egoquant.agents.market_intel.directory import fetch_tiingo_tickers

    csv_text = ("ticker,exchange,assetType,priceCurrency,startDate,endDate\n"
                "VTI,NYSE ARCA,ETF,USD,2001-05-24,2026-10-16\n"
                "SAP,XETRA,Stock,EUR,2001-01-02,2026-10-16\n"
                "AAPL,NASDAQ,Stock,USD,1980-12-12,2026-10-16\n"
                "XYZ,OTC,Stock,USD,2005-01-01,2026-10-16\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("supported_tickers.csv", csv_text)

    class Response:
        content = buf.getvalue()

        def raise_for_status(self):
            pass

    class Session:
        def get(self, *a, **k):
            return Response()

    rows = fetch_tiingo_tickers(Session())
    assert [r["ticker"] for r in rows] == ["VTI", "AAPL"]
    assert set(rows[0]) == {"ticker", "exchange", "priceCurrency", "startDate", "endDate"}
