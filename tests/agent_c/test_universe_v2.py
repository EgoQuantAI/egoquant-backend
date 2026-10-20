"""Portfolio method v2 in Agent C (docs/PORTFOLIO_V2.md): every eligible product is sent, nothing is trimmed, the
metadata Agent A screens on is filled, the price cache holds the whole universe, and a build never asks Tiingo for more
than its quota. Under v1 everything is byte-identical to before (golden hashes). Offline: the committed snapshot."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

import pandas as pd
import pytest

from egoquant import settings
from egoquant.agents.market_intel import CachedPriceSource, InvestorInterests, MarketIntelligenceAgent
from egoquant.agents.market_intel.agent import ELIGIBLE_AREA, MAX_UNIVERSE, select_universe
from egoquant.agents.market_intel.catalog import DEFAULT_CATALOG, Instrument, region_of
from egoquant.agents.market_intel.snapshot import YAHOO_SNAPSHOT_PATH, load_snapshot
from egoquant.demo.personas import PERSONAS
from egoquant.preferences import build_form, load_preference_bank, to_interest_fields
from egoquant.risk_profile import build_risk_profile, load_all, to_agent_c_limits

AS_OF = date(2026, 10, 16)               # the committed snapshot's own last day
_ALL = load_all()
_PREFS = load_preference_bank()


def interests_of(key: str) -> InvestorInterests:
    p = PERSONAS[key]
    bank, cfg = _ALL
    profile = build_risk_profile(p.answers(), bank, cfg, assessed_at=pd.Timestamp(AS_OF, tz="UTC").to_pydatetime())
    form = build_form(_PREFS, profile, cfg, p.trading_platforms)
    return InvestorInterests(investor_id="g", trading_platforms=p.trading_platforms, held_assets=[],
                             **to_interest_fields(p.preference_answers(), form, _PREFS), **to_agent_c_limits(profile, cfg))


def agent_for(key: str, method: str, source=None, **kw) -> MarketIntelligenceAgent:
    return MarketIntelligenceAgent(interests_of(key), source=source or load_snapshot(str(YAHOO_SNAPSHOT_PATH)),
                                   directory=None, use_snapshot=False, portfolio_method=method, **kw)


# ---------------------------------------------------------------------------------- v1 is byte-identical to before
GOLDEN_V1 = {          # sha256 prefix of (selection before and after pricing, assets, prices) from the committed snapshot,
                       # computed on a PRE-v2 checkout of origin/oct19: v1 must still produce exactly this
    "no_loss_saver": "e886f474fe3fed3a7fe0", "cautious_retiree": "377ca6dedbdde9e36f6c",
    "balanced_saver": "32ccccc9b75ce5313930", "growth_investor": "826ce00514b737f46c47",
    "crypto_trader": "51a10210e88fcb6fcb3f", "eager_beginner": "02c149b5d77782ef317a",
}


@pytest.mark.parametrize("key", sorted(GOLDEN_V1))
def test_v1_output_is_byte_identical_to_before_the_v2_work(key):
    agent = agent_for(key, "v1")
    first = agent.selection.model_dump_json()
    agent.fetch_universe(AS_OF, 504, history_days=5040)
    snap = agent.get_market_snapshot(None, AS_OF, 504)
    digest = hashlib.sha256()
    for part in (first, agent.selection.model_dump_json(), "".join(a.model_dump_json() for a in snap.assets)):
        digest.update(part.encode())
    digest.update(pd.util.hash_pandas_object(snap.prices, index=True).to_numpy().tobytes())
    assert digest.hexdigest()[:20] == GOLDEN_V1[key]
    assert all(a.area is None and a.region is None and a.sector is None and a.history_start is None
               and a.product_type is None for a in snap.assets)                      # the v2 fields stay empty under v1


def test_the_flag_decides_the_method_and_an_explicit_argument_wins(monkeypatch):
    monkeypatch.delenv("EGOQUANT_PORTFOLIO_METHOD", raising=False)
    assert not MarketIntelligenceAgent(interests_of("growth_investor"), source=load_snapshot(str(YAHOO_SNAPSHOT_PATH)),
                                       directory=None, use_snapshot=False).all_eligible
    monkeypatch.setenv("EGOQUANT_PORTFOLIO_METHOD", "v2")
    assert MarketIntelligenceAgent(interests_of("growth_investor"), source=load_snapshot(str(YAHOO_SNAPSHOT_PATH)),
                                   directory=None, use_snapshot=False).all_eligible
    assert not agent_for("growth_investor", "v1").all_eligible


# ---------------------------------------------------------------------------------- v2 sends every eligible product
def test_v2_sends_far_more_than_the_v1_trimmed_universe_and_none_of_the_limits_apply():
    interests = interests_of("crypto_trader")                 # R5, individual stocks, crypto: v1 trims hardest here
    v1 = select_universe(interests, DEFAULT_CATALOG)
    v2 = select_universe(interests, DEFAULT_CATALOG, all_eligible=True)
    assert len(v1.instruments) <= 100 and len(v2.instruments) > 200
    stocks = [i for i in v2.instruments if i.product_type == "stock"]
    assert len(stocks) > 50                                    # v1: at most 50 single stocks
    per_area: dict[str, int] = {}
    for i in v2.instruments:
        for r in i.reasons:
            if r.startswith("interest:"):
                per_area[r] = per_area.get(r, 0) + 1
    assert max(per_area.values(), default=0) > 25 or len(stocks) > 25                      # v1: at most 25 per cluster
    assert {t for t in v1.tickers} <= set(v2.tickers)         # everything v1 sends is in v2 as well
    assert not v2.deprioritized                                # nothing was left out by a budget


def test_v2_keeps_funds_of_one_exposure_group_apart_for_the_screening_to_choose_between():
    by = {i.ticker: i for i in DEFAULT_CATALOG}
    group = {i.ticker for i in DEFAULT_CATALOG if i.exposure_group == by["VOO"].exposure_group}
    assert {"VOO", "IVV", "SPY"} <= group
    v2 = select_universe(interests_of("growth_investor"), DEFAULT_CATALOG, all_eligible=True)
    assert group <= set(v2.tickers) and v2.alternates == {}                         # all of them, no fallbacks needed
    v1 = select_universe(interests_of("growth_investor"), DEFAULT_CATALOG)
    assert len(group & set(v1.tickers)) <= 1                                         # v1: one fund stands for the group


def test_products_matching_no_interest_are_eligible_and_walked_last():
    interests = interests_of("balanced_saver")
    v2 = select_universe(interests, DEFAULT_CATALOG, all_eligible=True)
    other = [i for i in v2.instruments if i.reasons == ["eligible"]]
    assert other, "the products that match none of the investor's interests must still be sent"
    assert v2.queues[ELIGIBLE_AREA] == [i.ticker for i in other]
    order = v2.fetch_order
    assert order.index(other[0].ticker) > max(order.index(t) for a in interests.interest_areas for t in v2.queues.get(a, []))


@pytest.mark.parametrize("key", sorted(PERSONAS))
def test_the_eligibility_rules_still_apply_under_v2(key):
    interests = interests_of(key)
    v2 = select_universe(interests, DEFAULT_CATALOG, all_eligible=True)
    by = {i.ticker: i for i in DEFAULT_CATALOG}
    for entry in v2.instruments:
        inst = by[entry.ticker]
        assert inst.r_level <= interests.max_r_level
        assert inst.product_type not in interests.blocked_product_types
        assert inst.sector not in set(interests.avoid_sectors) or inst.core
    core_allowed = {i.ticker for i in DEFAULT_CATALOG if i.core and i.r_level <= interests.max_r_level}
    assert core_allowed <= set(v2.tickers)                                            # the core funds are always sent


def test_the_safety_ceiling_limits_the_candidates_and_keeps_the_important_ones_first():
    interests = interests_of("growth_investor")
    foreign_area = next(a for a in ("long_term_bonds", "inflation_protected", "altcoins") if a not in interests.interest_areas)
    extra = [Instrument(ticker=f"ZZ{n:03d}", name=f"Fund {n}", asset_class="equity", product_type="equity_etf", r_level=3,
                        areas=[foreign_area], expense_ratio=0.001) for n in range(300)]
    v2 = select_universe(interests, DEFAULT_CATALOG + extra, all_eligible=True)
    assert len(v2.instruments) == MAX_UNIVERSE and len(v2.fetch_order) == MAX_UNIVERSE
    assert v2.deprioritized and all(e.ticker.startswith("ZZ") for e in v2.deprioritized)   # the tail of the rest is cut
    assert all(i.ticker in v2.tickers for i in DEFAULT_CATALOG if i.core and i.r_level <= interests.max_r_level)
    matches = [i.ticker for i in v2.instruments if any(r.startswith("interest:") for r in i.reasons)]
    assert matches and all(t in v2.fetch_order[:len(matches) + 10] for t in matches)    # interests are priced before the rest
    assert any("safety ceiling" in n for n in v2.notes)


def test_interest_assets_can_be_narrowed_to_the_top_of_each_area():
    agent = agent_for("growth_investor", "v2")
    everything, narrow = agent.interest_assets(), agent.interest_assets(per_area=3)
    assert narrow <= everything and 0 < len(narrow) < len(everything) / 2
    areas = [a for a in agent.selection.interest_areas if agent.selection.queues.get(a)]
    assert len(narrow) <= 3 * len(areas)
    for area in areas:                                                             # each area keeps its first queued products
        assert set(agent.selection.queues[area][:3]) & set(agent.selection.tickers) <= narrow


def test_interest_assets_are_the_interest_matches_and_holdings_only():
    agent = agent_for("growth_investor", "v2")
    kept = agent.interest_assets()
    assert kept and kept < set(agent.selection.tickers)
    for entry in agent.selection.instruments:
        wanted = any(r.startswith("interest:") or r == "held" for r in entry.reasons)
        assert (entry.ticker in kept) == wanted


# ---------------------------------------------------------------------------------- metadata for Agent A's screening
def test_v2_fills_the_asset_metadata_agent_a_needs():
    agent = agent_for("growth_investor", "v2")
    agent.fetch_universe(AS_OF, 504, history_days=5040)
    snap = agent.get_market_snapshot(None, AS_OF, 504)
    assert len(snap.assets) > 150
    by = {i.ticker: i for i in DEFAULT_CATALOG}
    for a in snap.assets:
        inst = by[a.asset_id]
        assert a.product_type == inst.product_type and a.sector == inst.sector
        assert a.area == (inst.areas[0] if inst.areas else None) and a.region == region_of(inst)
        assert a.history_start is not None and a.history_start <= AS_OF
    vt = next(a for a in snap.assets if a.asset_id == "VT")
    assert vt.history_start.year <= 2008 and vt.region == "global"                  # the 20-year history, not the 2-year window


def test_regions_are_deterministic_and_sensible():
    by = {i.ticker: i for i in DEFAULT_CATALOG}
    assert region_of(by["VOO"]) == "US" and region_of(by["VWO"]) == "emerging" and region_of(by["MCHI"]) == "emerging"
    assert region_of(by["VEA"]) == "developed ex-US" and region_of(by["GLD"]) == "global" and region_of(by["VT"]) == "global"
    assert {region_of(i) for i in DEFAULT_CATALOG} <= {"US", "developed ex-US", "emerging", "global"}


# ---------------------------------------------------------------------------------- the price cache holds the universe
def test_the_memory_cache_default_is_250_under_v1_and_400_under_v2(monkeypatch):
    monkeypatch.delenv("EGOQUANT_PRICE_MEMORY_FILES", raising=False)
    monkeypatch.setenv("EGOQUANT_PORTFOLIO_METHOD", "v1")
    assert settings.price_memory_cache_files() == 250
    monkeypatch.setenv("EGOQUANT_PORTFOLIO_METHOD", "v2")
    assert settings.price_memory_cache_files() == 400 >= MAX_UNIVERSE
    monkeypatch.setenv("EGOQUANT_PRICE_MEMORY_FILES", "300")
    assert settings.price_memory_cache_files() == 300                               # an explicit value wins under both


class Counting:
    name = "tiingo"
    data_source = "historical"

    def __init__(self):
        self.requests, self.us_listed, self.snap = 0, 0, load_snapshot(str(YAHOO_SNAPSHOT_PATH))
        self.markets = {i.ticker: i.market for i in DEFAULT_CATALOG}

    def needs_request(self, ticker, start, end):
        return True

    def fetch_adjusted_close(self, tickers, start, end):
        self.requests += len(tickers)
        self.us_listed += sum(self.markets.get(t) == "us_listed" for t in tickers)       # Tiingo's share (Binance is separate)
        return self.snap.fetch_adjusted_close(tickers, start, end)


def _fresh_memory(monkeypatch):
    monkeypatch.setattr(CachedPriceSource, "_memory", type(CachedPriceSource._memory)())


def test_all_292_products_stay_in_memory_under_v2_and_thrash_under_the_v1_limit(tmp_path, monkeypatch):
    _fresh_memory(monkeypatch)
    inner = Counting()
    cached = CachedPriceSource(inner, directory=tmp_path, today=lambda: date(2026, 10, 19))
    tickers = [i.ticker for i in DEFAULT_CATALOG]
    start = AS_OF - timedelta(days=7400)
    cached.fetch_adjusted_close(tickers, start, AS_OF)                              # fills disk and memory
    monkeypatch.delenv("EGOQUANT_PRICE_MEMORY_FILES", raising=False)
    monkeypatch.setenv("EGOQUANT_PORTFOLIO_METHOD", "v2")
    _fresh_memory(monkeypatch)
    again = CachedPriceSource(inner, directory=tmp_path, today=lambda: date(2026, 10, 19))
    again.fetch_adjusted_close(tickers, start, AS_OF)                               # read back from disk, remembered
    assert len(CachedPriceSource._memory) == len(DEFAULT_CATALOG) == 292             # nothing evicted
    monkeypatch.setenv("EGOQUANT_PORTFOLIO_METHOD", "v1")
    _fresh_memory(monkeypatch)
    CachedPriceSource(inner, directory=tmp_path, today=lambda: date(2026, 10, 19)).fetch_adjusted_close(tickers, start, AS_OF)
    assert len(CachedPriceSource._memory) == 250                                    # the old default would thrash


def test_the_compact_memory_copy_returns_exactly_what_the_disk_copy_returns(tmp_path, monkeypatch):
    _fresh_memory(monkeypatch)
    inner = Counting()
    cached = CachedPriceSource(inner, directory=tmp_path, today=lambda: date(2026, 10, 19))
    cached.fetch_adjusted_close(["VT", "BTCUSDT"], date(2007, 6, 1), AS_OF)
    path = tmp_path / "VT.json"
    on_disk = CachedPriceSource._memory.pop(path)                                    # force the next read from the file
    assert "values" in on_disk and "prices" not in on_disk                           # compact arrays, not a dict of text
    a = cached._load("VT", date(2010, 1, 4), AS_OF)                                  # from disk (and remembered again)
    b = cached._load("VT", date(2010, 1, 4), AS_OF)                                  # from the compact memory copy
    pd.testing.assert_series_equal(a, b)
    assert a.index.dtype == b.index.dtype and a.index.is_monotonic_increasing                # same resolution as before
    pd.testing.assert_series_equal(b, inner.snap.prices["VT"].dropna().loc["2010-01-04":AS_OF.isoformat()],
                                   check_names=False, check_freq=False, check_index_type=False)


# ---------------------------------------------------------------------------------- the Tiingo quota
def test_a_v2_build_never_asks_for_more_new_prices_than_the_budget_and_the_hourly_quota(tmp_path, monkeypatch):
    _fresh_memory(monkeypatch)
    monkeypatch.setattr("egoquant.agents.market_intel.agent.tiingo_requests_in_last", lambda *a, **k: 0)
    inner = Counting()
    agent = MarketIntelligenceAgent(interests_of("crypto_trader"), source=CachedPriceSource(inner, directory=tmp_path),
                                    directory=None, use_snapshot=False, portfolio_method="v2")
    agent.fetch_universe(AS_OF, 504, history_days=5040)
    fixed = sum(1 for i in agent.selection.instruments if "core" in i.reasons or "held" in i.reasons)
    assert 0 < inner.us_listed <= 40 + fixed                                        # the budget of 40, plus the core funds
    # an almost used-up hour: only what is left of the 50 (core funds, which must be priced, still go through)
    _fresh_memory(monkeypatch)
    monkeypatch.setattr("egoquant.agents.market_intel.agent.tiingo_requests_in_last", lambda *a, **k: 47)
    other = Counting()
    agent2 = MarketIntelligenceAgent(interests_of("crypto_trader"),
                                     source=CachedPriceSource(other, directory=tmp_path / "second"),
                                     directory=None, use_snapshot=False, portfolio_method="v2")
    agent2.fetch_universe(AS_OF, 504, history_days=5040)
    assert other.us_listed <= 3 + fixed


def test_prices_already_cached_cost_no_request_however_many_products_are_sent(tmp_path, monkeypatch):
    _fresh_memory(monkeypatch)
    inner = Counting()
    warm = CachedPriceSource(inner, directory=tmp_path, today=lambda: date(2026, 10, 19))
    for inst in DEFAULT_CATALOG:
        warm.fetch_adjusted_close([inst.ticker], AS_OF - timedelta(days=7400), AS_OF)    # wider than the 20-year window
    inner.requests = 0
    _fresh_memory(monkeypatch)
    monkeypatch.setattr("egoquant.agents.market_intel.agent.tiingo_requests_in_last", lambda *a, **k: 49)
    agent = MarketIntelligenceAgent(interests_of("crypto_trader"),
                                    source=CachedPriceSource(inner, directory=tmp_path, today=lambda: date(2026, 10, 19)),
                                    directory=None, use_snapshot=False, portfolio_method="v2")
    agent.fetch_universe(AS_OF, 504, history_days=5040)
    snap = agent.get_market_snapshot(None, AS_OF, 504)
    assert inner.requests == 0 and len(snap.assets) > 150
