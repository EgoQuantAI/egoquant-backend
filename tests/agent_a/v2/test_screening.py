"""Portfolio method v2, universe screening (egoquant/agents/quant/v2/screening.py). Synthetic returns plus the committed
snapshot; no network."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from egoquant.agents.quant.v2.screening import ScreenResult, screen_universe
from egoquant.models import AssetInfo

DAYS = 1200


def _returns(n_groups=6, per_group=4, days=DAYS, seed=1, noise=0.0005):
    """n_groups independent factors; each group holds `per_group` near-copies of its factor (+ a little noise)."""
    rng = np.random.default_rng(seed)
    factors = rng.normal(0, 0.01, size=(days, n_groups))
    cols, data = [], []
    for g in range(n_groups):
        for k in range(per_group):
            cols.append(f"G{g}_{k}")
            data.append(factors[:, g] + rng.normal(0, noise, size=days))
    index = pd.bdate_range("2020-01-01", periods=days)
    return pd.DataFrame(np.array(data).T, index=index, columns=cols)


def _assets(columns, **overrides):
    return [AssetInfo(asset_id=c, asset_class="equity", **overrides.get(c, {})) for c in columns]


# ---------------------------------------------------------------------- determinism and shape of the answer
def test_same_input_same_answer():
    r = _returns()
    a, b = screen_universe(r, _assets(r.columns), max_representatives=6), screen_universe(r, _assets(r.columns), max_representatives=6)
    assert a == b and isinstance(a, ScreenResult)


def test_kept_follows_the_input_column_order_whatever_the_order_is():
    r = _returns()
    shuffled = r[list(reversed(r.columns))]
    for frame in (r, shuffled):
        res = screen_universe(frame, _assets(frame.columns), max_representatives=6)
        positions = [list(frame.columns).index(c) for c in res.kept]
        assert positions == sorted(positions)
    # the SET of representatives is the same either way (ties are broken by ticker, not by position)
    assert set(screen_universe(r, _assets(r.columns), max_representatives=6).kept) == \
        set(screen_universe(shuffled, _assets(shuffled.columns), max_representatives=6).kept)


def test_cluster_ids_are_numbered_by_first_member_in_input_order_and_cover_every_screened_asset():
    r = _returns()
    res = screen_universe(r, _assets(r.columns), max_representatives=6)
    assert sorted(set(res.cluster_of.values())) == list(range(len(res.clusters)))
    assert set(res.cluster_of) == set(r.columns) and not res.dropped_short_history
    firsts = [min(list(r.columns).index(m) for m in g) for g in res.clusters]
    assert firsts == sorted(firsts)
    for k, g in enumerate(res.clusters):
        assert all(res.cluster_of[m] == k for m in g)
    assert sorted(m for g in res.clusters for m in g) == sorted(r.columns)            # a partition


# ---------------------------------------------------------------------- the cluster count
@pytest.mark.parametrize("limit", [1, 2, 3, 6, 10, 24, 100])
def test_cluster_count_is_within_the_maximum(limit):
    r = _returns()
    res = screen_universe(r, _assets(r.columns), max_representatives=limit)
    assert 1 <= len(res.clusters) <= limit and len(res.kept) <= limit


def test_kept_never_exceeds_the_maximum_plus_the_forced_assets():
    r = _returns()
    forced = {"G0_1", "G0_2", "G3_0"}
    res = screen_universe(r, _assets(r.columns), forced=forced, max_representatives=4)
    assert len(res.kept) <= 4 + len(forced) and forced <= set(res.kept)


# ---------------------------------------------------------------------- near-duplicate funds land together
def test_near_duplicate_funds_share_a_cluster_and_one_is_kept():
    r = _returns()
    res = screen_universe(r, _assets(r.columns), max_representatives=6)
    assert len(res.clusters) == 6
    for g in range(6):
        ids = {res.cluster_of[f"G{g}_{k}"] for k in range(4)}
        assert len(ids) == 1                                                          # the four copies are together
    assert len(res.kept) == 6                                                         # one per group


# ---------------------------------------------------------------------- the representative rule
def test_representative_is_the_cheapest_then_longest_history_then_most_liquid_then_ticker():
    r = _returns(n_groups=1, per_group=5)
    cols = list(r.columns)                                                            # G0_0 .. G0_4, one cluster
    base = {c: dict(expense_ratio=0.0010) for c in cols}
    # cheapest wins
    over = {**base, "G0_3": dict(expense_ratio=0.0003)}
    assert screen_universe(r, _assets(cols, **over), max_representatives=1).kept == ["G0_3"]
    # equal fee: the longest history wins (G0_2 has more non-missing returns than the rest)
    longer = r.copy()
    for c in cols:
        if c != "G0_2":
            longer.loc[longer.index[:50], c] = np.nan
    assert screen_universe(longer, _assets(cols, **base), max_representatives=1, min_history_days=100).kept == ["G0_2"]
    # equal fee and history: the most liquid tier (1 before 2)
    liquid = {c: dict(expense_ratio=0.0010, liquidity_tier=2) for c in cols}
    liquid["G0_4"] = dict(expense_ratio=0.0010, liquidity_tier=1)
    assert screen_universe(r, _assets(cols, **liquid), max_representatives=1).kept == ["G0_4"]
    # everything equal: the smallest ticker
    assert screen_universe(r, _assets(cols, **base), max_representatives=1).kept == ["G0_0"]


# ---------------------------------------------------------------------- forced assets
def test_forced_assets_are_always_kept_even_when_expensive_and_in_a_crowded_cluster():
    r = _returns()
    fees = {c: dict(expense_ratio=0.0010) for c in r.columns}
    fees["G2_3"] = dict(expense_ratio=0.02)
    res = screen_universe(r, _assets(r.columns, **fees), forced={"G2_3"}, max_representatives=6)
    assert "G2_3" in res.kept
    group = next(g for g in res.clusters if "G2_3" in g)
    assert set(group) == {f"G2_{k}" for k in range(4)}
    assert group[0] != "G2_3" and group.index("G2_3") == 1                            # representative first, forced next
    assert len(res.kept) == 7                                                         # the representative AND the forced one


def test_a_forced_asset_that_is_already_the_representative_adds_nothing():
    r = _returns()
    fees = {c: dict(expense_ratio=0.0010) for c in r.columns}
    fees["G1_2"] = dict(expense_ratio=0.0001)                                          # the cheapest of its cluster
    res = screen_universe(r, _assets(r.columns, **fees), forced={"G1_2"}, max_representatives=6)
    assert len(res.kept) == 6 and "G1_2" in res.kept and next(g for g in res.clusters if "G1_2" in g)[0] == "G1_2"


def test_a_forced_id_that_is_not_a_column_is_ignored():
    r = _returns()
    res = screen_universe(r, _assets(r.columns), forced={"NOPE"}, max_representatives=6)
    assert "NOPE" not in res.kept and len(res.kept) == 6


# ---------------------------------------------------------------------- short history
def test_products_with_too_little_history_are_listed_never_lost_silently():
    r = _returns()
    young = r.copy()
    young.loc[young.index[:-100], "G4_1"] = np.nan                                    # only 100 returns
    res = screen_universe(young, _assets(young.columns), max_representatives=6, min_history_days=750)
    assert res.dropped_short_history == ["G4_1"]
    assert "G4_1" not in res.kept and "G4_1" not in res.cluster_of and all("G4_1" not in g for g in res.clusters)
    every = set(res.cluster_of) | set(res.dropped_short_history)
    assert every == set(young.columns)                                                # nothing vanished


def test_a_forced_short_history_product_is_kept_on_its_own():
    r = _returns()
    young = r.copy()
    young.loc[young.index[:-100], "G4_1"] = np.nan
    res = screen_universe(young, _assets(young.columns), forced={"G4_1"}, max_representatives=6, min_history_days=750)
    assert "G4_1" in res.kept and res.dropped_short_history == []
    assert next(g for g in res.clusters if "G4_1" in g) == ["G4_1"]
    assert len(res.kept) <= 6 + 1


def test_dropped_short_history_keeps_the_input_order():
    r = _returns()
    young = r.copy()
    for c in ("G5_0", "G1_3", "G3_2"):
        young.loc[young.index[:-50], c] = np.nan
    res = screen_universe(young, _assets(young.columns), max_representatives=6, min_history_days=750)
    assert res.dropped_short_history == [c for c in young.columns if c in {"G5_0", "G1_3", "G3_2"}]


# ---------------------------------------------------------------------- edge cases
def test_empty_universe():
    res = screen_universe(pd.DataFrame(index=pd.bdate_range("2020-01-01", periods=10)), [])
    assert res == ScreenResult(kept=[], cluster_of={}, clusters=[], dropped_short_history=[])


def test_one_asset():
    r = _returns(n_groups=1, per_group=1)
    res = screen_universe(r, _assets(r.columns), max_representatives=5)
    assert res.kept == ["G0_0"] and res.clusters == [["G0_0"]] and res.cluster_of == {"G0_0": 0}


def test_two_assets_and_a_maximum_of_one_merge_them():
    r = _returns(n_groups=2, per_group=1)
    assert len(screen_universe(r, _assets(r.columns), max_representatives=1).clusters) == 1
    assert len(screen_universe(r, _assets(r.columns), max_representatives=2).clusters) == 2


def test_everything_short_gives_an_empty_screen_and_a_full_dropped_list():
    r = _returns()
    res = screen_universe(r, _assets(r.columns), min_history_days=DAYS + 1)
    assert res.kept == [] and res.clusters == [] and res.dropped_short_history == list(r.columns)


def test_a_constant_column_and_non_overlapping_histories_do_not_break_it():
    r = _returns()
    r["CASHLIKE"] = 0.0                                                               # no variance at all
    r["OLD"] = np.where(np.arange(len(r)) < 600, r["G0_0"], np.nan)
    r["NEW"] = np.where(np.arange(len(r)) >= 600, r["G0_0"], np.nan)                  # never present together with OLD
    res = screen_universe(r, _assets(r.columns), max_representatives=8, min_history_days=500)
    assert set(res.cluster_of) == set(r.columns)
    assert res.cluster_of["CASHLIKE"] != res.cluster_of["G1_0"]
    assert np.isfinite(len(res.kept))


def test_missing_metadata_defaults_to_free_and_liquid():
    r = _returns(n_groups=1, per_group=3)
    res = screen_universe(r, [], max_representatives=1)                               # no AssetInfo at all
    assert res.kept == ["G0_0"]


def test_the_input_frame_is_not_modified():
    r = _returns()
    before = r.copy()
    screen_universe(r, _assets(r.columns), max_representatives=6)
    pd.testing.assert_frame_equal(r, before)


# ---------------------------------------------------------------------- speed and the real universe
def test_292_assets_by_4900_days_in_under_three_seconds():
    rng = np.random.default_rng(7)
    days, n = 4900, 292
    market = rng.normal(0, 0.01, size=days)
    sectors = rng.normal(0, 0.008, size=(days, 11))
    load = rng.integers(0, 11, size=n)
    data = 0.6 * market[:, None] + sectors[:, load] + rng.normal(0, 0.006, size=(days, n))
    frame = pd.DataFrame(data, index=pd.bdate_range("2007-01-01", periods=days), columns=[f"A{i:03d}" for i in range(n)])
    for i in range(0, n, 9):                                                          # a third of the products are younger
        frame.iloc[: rng.integers(500, 3500), i] = np.nan
    assets = _assets(frame.columns)
    screen_universe(frame, assets)                                                    # warm-up (imports, BLAS threads)
    start = time.perf_counter()
    res = screen_universe(frame, assets)
    assert time.perf_counter() - start < 3.0
    assert len(res.clusters) <= 120 and set(res.cluster_of) | set(res.dropped_short_history) == set(frame.columns)


def test_the_committed_snapshot_puts_the_s_and_p_500_funds_in_one_cluster_and_keeps_requested_assets():
    from egoquant.agents.market_intel.catalog import DEFAULT_CATALOG
    from egoquant.agents.market_intel.snapshot import YAHOO_SNAPSHOT_PATH, SnapshotPriceSource

    prices = SnapshotPriceSource(str(YAHOO_SNAPSHOT_PATH)).prices
    returns = prices.pct_change(fill_method=None).iloc[1:]
    assets = [AssetInfo(asset_id=i.ticker, asset_class=i.asset_class, expense_ratio=i.expense_ratio,
                        liquidity_tier=i.liquidity_tier) for i in DEFAULT_CATALOG if i.ticker in returns.columns]
    res = screen_universe(returns, assets, forced={"BTCUSDT", "ETHA"})
    assert res.cluster_of["VOO"] == res.cluster_of["IVV"] == res.cluster_of["SPY"] == res.cluster_of["VTI"]
    assert {"BTCUSDT", "ETHA"} <= set(res.kept)                                       # ETHA has little history but is forced
    assert {"FBTC", "IBIT"} <= set(res.dropped_short_history)
    assert len(res.kept) <= 120 + 2
