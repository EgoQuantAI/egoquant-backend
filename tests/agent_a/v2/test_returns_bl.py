"""Black-Litterman expected returns for portfolio method v2 (egoquant/agents/quant/v2/returns_bl.py, section 7).

Run with:  pytest -v tests/agent_a/v2/test_returns_bl.py      (no network; one test reads the committed price snapshot)
"""

import random
from datetime import date

import numpy as np
import pytest

from egoquant.agents.market_intel.snapshot import SnapshotPriceSource, agent_c_snapshot_path
from egoquant.agents.quant import core as qc
from egoquant.agents.quant.v2.returns_bl import (black_litterman_returns, reference_weights, view_matrix)
from egoquant.models import AssetInfo, PortfolioView


def info(asset_id, asset_class="equity", fee=0.0, **meta) -> AssetInfo:
    return AssetInfo(asset_id=asset_id, asset_class=asset_class, expense_ratio=fee, **meta)


def view(kind, key, excess=0.005, confidence=0.30) -> PortfolioView:
    return PortfolioView(kind=kind, key=key, annual_excess=excess, confidence=confidence)


# Six assets: two US equity funds, one tech fund, a bond fund, a short bond fund, gold.
IDS = ["VTI", "VXUS", "QQQ", "AGG", "SHY", "IAU"]
CLASSES = ["equity", "equity", "equity", "bond", "bond", "commodity"]
ASSETS = [info("VTI", fee=0.0003, area="stocks,us_stocks"), info("VXUS", fee=0.0008, area="stocks,international_stocks"),
          info("QQQ", fee=0.0020, area="technology", sector="technology"),
          info("AGG", "bond", fee=0.0003, area="bonds"), info("SHY", "bond", fee=0.0015, area="government_bonds"),
          info("IAU", "commodity", fee=0.0025, area="gold")]
FEES = np.array([a.expense_ratio for a in ASSETS])


def covariance() -> np.ndarray:
    vol = np.array([0.16, 0.17, 0.22, 0.05, 0.02, 0.15])
    corr = np.array([[1.00, 0.85, 0.90, 0.10, 0.00, 0.05],
                     [0.85, 1.00, 0.80, 0.10, 0.00, 0.10],
                     [0.90, 0.80, 1.00, 0.05, -0.05, 0.00],
                     [0.10, 0.10, 0.05, 1.00, 0.60, 0.20],
                     [0.00, 0.00, -0.05, 0.60, 1.00, 0.10],
                     [0.05, 0.10, 0.00, 0.20, 0.10, 1.00]])
    return np.outer(vol, vol) * corr


MIX = {"equity": 0.5, "bond": 0.4, "commodity": 0.1}
W_REF = reference_weights(IDS, CLASSES, MIX)


def bl(views=(), assumptions=None, cov=None, assets=ASSETS, **kw):
    return black_litterman_returns(covariance() if cov is None else cov, IDS, CLASSES, assumptions, W_REF, list(views),
                                   assets, **kw)


# ==========================================================================
# reference_weights
# ==========================================================================
def test_reference_weights_follow_the_class_mix_clusters_and_members():
    ids = ["A", "B", "C", "D", "E", "F"]
    classes = ["equity", "equity", "equity", "equity", "bond", "bond"]
    cluster = {"A": 0, "B": 0, "C": 0, "D": 1, "E": 2, "F": 2}            # equity: 2 clusters (3 and 1 funds); bonds: 1
    w = reference_weights(ids, classes, {"equity": 0.6, "bond": 0.4}, cluster)
    assert w == pytest.approx([0.1, 0.1, 0.1, 0.3, 0.2, 0.2])
    assert w.sum() == pytest.approx(1.0)


def test_reference_weights_without_clusters_split_each_class_equally():
    w = reference_weights(["A", "B", "C"], ["equity", "equity", "bond"], {"equity": 0.7, "bond": 0.3})
    assert w == pytest.approx([0.35, 0.35, 0.3])
    # an asset the cluster map does not name is its own cluster
    w = reference_weights(["A", "B", "C"], ["equity", "equity", "equity"], {"equity": 1.0}, {"A": 0, "B": 0})
    assert w == pytest.approx([0.25, 0.25, 0.5])


def test_reference_weights_drop_classes_without_assets_and_rescale():
    w = reference_weights(["A", "B"], ["equity", "bond"], {"equity": 0.4, "bond": 0.2, "crypto": 0.4})
    assert w == pytest.approx([2 / 3, 1 / 3])


def test_reference_weights_fall_back_to_equal_weights_when_no_share_is_left():
    assert reference_weights(["A", "B", "C"], ["equity", "equity", "bond"], {"crypto": 1.0}) == pytest.approx([1 / 3] * 3)
    assert reference_weights(["A", "B"], ["equity", "bond"], {}) == pytest.approx([0.5, 0.5])
    assert reference_weights(["A", "B"], ["equity", "bond"], {"equity": 0.0, "bond": 0.0}) == pytest.approx([0.5, 0.5])


def test_reference_weights_edge_cases():
    assert reference_weights([], [], {"equity": 1.0}).shape == (0,)
    assert reference_weights(["A"], ["bond"], {"equity": 1.0}) == pytest.approx([1.0])
    assert reference_weights(["A"], ["equity"], {"equity": 0.3}) == pytest.approx([1.0])
    for bad in ({"equity": -0.1}, {"equity": float("nan")}, {"equity": float("inf")}):
        with pytest.raises(ValueError):
            reference_weights(["A"], ["equity"], bad)
    with pytest.raises(ValueError):
        reference_weights(["A", "B"], ["equity"], {"equity": 1.0})
    with pytest.raises(ValueError):
        reference_weights(["A", "A"], ["equity", "equity"], {"equity": 1.0})


def test_reference_weights_are_deterministic_and_never_negative_for_random_universes():
    rng = random.Random(7)
    classes_all = ["equity", "bond", "cash", "commodity", "crypto"]
    for _ in range(200):
        n = rng.randint(0, 30)
        ids = [f"X{i}" for i in range(n)]
        classes = [rng.choice(classes_all) for _ in ids]
        mix = {c: rng.random() for c in classes_all if rng.random() < 0.8}
        cluster = {a: rng.randint(0, 5) for a in ids if rng.random() < 0.9}
        w = reference_weights(ids, classes, mix, cluster)
        assert (w >= 0).all() and (w.sum() == pytest.approx(1.0) if n else w.size == 0)
        assert np.array_equal(w, reference_weights(ids, classes, mix, cluster))


# ==========================================================================
# no views: the prior
# ==========================================================================
def test_no_views_gives_the_prior_delta_sigma_wref():
    sigma = covariance()
    assert bl(assets=[]) == pytest.approx(3.0 * sigma @ W_REF)
    assert bl(delta=2.0, assets=[]) == pytest.approx(2.0 * sigma @ W_REF)
    assert bl(views=[], assets=[]) == pytest.approx(bl(assets=[]))


def test_fees_are_subtracted_per_fund():
    assert bl() == pytest.approx(bl(assets=[]) - FEES)
    gross = {"equity": 0.07, "bond": 0.03, "commodity": 0.02}
    assert bl(assumptions=gross) == pytest.approx(bl(assumptions=gross, assets=[]) - FEES)
    # a fund Agent C did not describe has no fee; extra descriptions are ignored
    assert bl(assets=[info("VTI", fee=0.01), info("NOT_IN_UNIVERSE", fee=0.5)])[:2] == pytest.approx(
        [bl(assets=[])[0] - 0.01, bl(assets=[])[1]])


def test_the_class_assumption_is_the_reference_weighted_class_average_before_fees():
    gross = {"equity": 0.07, "bond": 0.03}
    mu = bl(assumptions=gross) + FEES
    for cls, target in gross.items():
        idx = [i for i, c in enumerate(CLASSES) if c == cls]
        assert W_REF[idx] @ mu[idx] / W_REF[idx].sum() == pytest.approx(target)
    prior = bl(assets=[])
    assert mu[5] == pytest.approx(prior[5])                                   # commodity has no assumption: unchanged
    assert mu[0] - mu[1] == pytest.approx(prior[0] - prior[1])                # the shift is per class, not per asset


def test_an_assumption_keyed_by_asset_id_sets_that_asset_and_the_nonfinite_ones_are_refused():
    mu = bl(assumptions={"equity": 0.06, "IAU": 0.025}) + FEES
    assert mu[5] == pytest.approx(0.025)
    for bad in ({"equity": float("nan")}, {"IAU": float("inf")}):
        with pytest.raises(ValueError):
            bl(assumptions=bad)


def test_returns_are_assumptions_never_historical_means():
    """Two different return histories behind the same covariance give the same expected returns: only cov and the
    reference allocation (and the views) enter."""
    rng = np.random.default_rng(1)
    base = rng.normal(0, 0.01, (500, 6))
    for drift in (0.0, 0.002, -0.002):
        sample = base + drift
        assert bl(cov=np.cov(base.T) * 252) == pytest.approx(bl(cov=np.cov(sample.T) * 252), abs=1e-12)


# ==========================================================================
# views
# ==========================================================================
def test_a_positive_view_raises_exactly_the_matching_assets_when_the_others_are_uncorrelated():
    diagonal = np.diag(covariance().diagonal())
    prior = bl(cov=diagonal)
    mu = bl([view("sector", "technology")], cov=diagonal)
    assert mu[2] > prior[2]
    assert mu[[0, 1, 3, 4, 5]] == pytest.approx(prior[[0, 1, 3, 4, 5]])
    mu = bl([view("area", "stocks")], cov=diagonal)
    assert (mu[[0, 1]] > prior[[0, 1]]).all() and mu[[2, 3, 4, 5]] == pytest.approx(prior[[2, 3, 4, 5]])
    mu = bl([view("area", "stocks", excess=-0.005)], cov=diagonal)
    assert (mu[[0, 1]] < prior[[0, 1]]).all() and mu[[2, 3, 4, 5]] == pytest.approx(prior[[2, 3, 4, 5]])


def test_a_view_moves_the_view_portfolio_by_confidence_times_the_excess():
    prior = bl()
    P, excess, _ = view_matrix([view("area", "stocks")], IDS, ASSETS)
    for confidence in (0.1, 0.30, 0.75):
        mu = bl([view("area", "stocks", 0.005, confidence)])
        assert P @ (mu - prior) == pytest.approx(confidence * excess)


def test_confidence_one_means_the_view_is_met():
    gross = {"equity": 0.065, "bond": 0.03, "commodity": 0.02}
    prior = bl(assumptions=gross) + FEES
    views = [view("sector", "technology", 0.012, 1.0), view("area", "gold", -0.004, 1.0)]
    mu = bl(views, assumptions=gross) + FEES
    P, excess, _ = view_matrix(views, IDS, ASSETS)
    assert P @ mu == pytest.approx(P @ prior + excess, abs=1e-12)
    # and two overlapping views of confidence 1 that agree are met together (singular system, least squares)
    agree = [view("area", "stocks", 0.01, 1.0), view("area", "us_stocks", 0.01, 1.0)]
    mu = bl(agree) + FEES
    P, excess, _ = view_matrix(agree, IDS, ASSETS)
    assert np.isfinite(mu).all() and P[0] @ mu == pytest.approx(P[0] @ (bl() + FEES) + 0.01, abs=1e-6)


def test_more_confidence_moves_the_view_further_and_a_bigger_excess_moves_it_further():
    tech = [bl([view("sector", "technology", 0.005, c)])[2] for c in (0.1, 0.3, 0.6, 1.0)]
    assert tech == sorted(tech) and tech[0] > bl()[2]
    tech = [bl([view("sector", "technology", e, 0.3)])[2] for e in (-0.01, 0.0, 0.01, 0.02)]
    assert tech == sorted(tech) and tech[1] == pytest.approx(bl()[2])


def test_correlated_assets_follow_a_view_in_the_direction_of_the_correlation():
    prior, mu = bl(), bl([view("sector", "technology", 0.01, 1.0)])
    assert mu[0] > prior[0] and mu[1] > prior[1]            # US and world stocks are correlated with the tech fund
    assert mu[4] < prior[4]                                 # the short bond fund has a slightly negative correlation


def test_views_that_apply_to_no_asset_are_skipped():
    assert bl([view("area", "crypto"), view("asset", "BTCUSDT"), view("sector", "energy"), view("region", "emerging")]
              ) == pytest.approx(bl())
    only_gold = bl([view("area", "gold")])
    assert bl([view("area", "crypto"), view("area", "gold")]) == pytest.approx(only_gold)
    assert bl([view("asset", "IAU")]) == pytest.approx(only_gold)         # the same single asset by another kind
    # an asset described by Agent C that is not in the universe is not part of any view
    outside = ASSETS + [info("VEA", area="gold")]
    assert bl([view("area", "gold")], assets=outside) == pytest.approx(only_gold, abs=0.0)


def test_the_order_of_the_views_does_not_matter():
    views = [view("sector", "technology", 0.01, 0.4), view("area", "gold", -0.005, 0.3), view("area", "bonds", 0.003, 0.5)]
    assert bl(views) == pytest.approx(bl(list(reversed(views))), abs=1e-14)


def test_the_same_inputs_give_byte_identical_results():
    views = [view("sector", "technology"), view("area", "gold", -0.005)]
    gross = {"equity": 0.06, "bond": 0.03}
    first = bl(views, assumptions=gross)
    assert first.tobytes() == bl(views, assumptions=gross).tobytes()
    assert first.tobytes() == black_litterman_returns(covariance().copy(), list(IDS), list(CLASSES), dict(gross),
                                                      W_REF.copy(), list(views), list(ASSETS)).tobytes()


def test_the_inputs_are_not_changed():
    sigma, w = covariance(), W_REF.copy()
    gross = {"equity": 0.06}
    views = [view("sector", "technology")]
    black_litterman_returns(sigma, IDS, CLASSES, gross, w, views, ASSETS)
    assert np.array_equal(sigma, covariance()) and np.array_equal(w, W_REF) and gross == {"equity": 0.06}
    assert views[0] == view("sector", "technology")


# ==========================================================================
# robustness
# ==========================================================================
def test_a_covariance_that_is_not_positive_definite_is_repaired_not_refused():
    singular = np.outer(np.ones(6), np.ones(6)) * 0.02                   # rank 1
    indefinite = covariance() - 0.05 * np.eye(6)                          # negative eigenvalues
    asymmetric = covariance() + np.triu(np.full((6, 6), 1e-4), 1)
    for cov in (singular, indefinite, asymmetric, np.zeros((6, 6))):
        for views in ([], [view("sector", "technology"), view("area", "gold", 0.01, 1.0)]):
            mu = bl(views, cov=cov)
            assert mu.shape == (6,) and np.isfinite(mu).all()


def test_edge_sizes():
    assert black_litterman_returns(np.zeros((0, 0)), [], [], None, np.zeros(0), [view("area", "x")], []).shape == (0,)
    one = black_litterman_returns(np.array([[0.04]]), ["A"], ["equity"], None, np.array([1.0]), [], [info("A", fee=0.01)])
    assert one == pytest.approx([3.0 * 0.04 - 0.01])
    tilted = black_litterman_returns(np.array([[0.04]]), ["A"], ["equity"], None, np.array([1.0]),
                                     [view("asset", "A", 0.01, 1.0)], [info("A")])
    assert tilted == pytest.approx([3.0 * 0.04 + 0.01])
    anchored = black_litterman_returns(np.array([[0.04]]), ["A"], ["equity"], {"equity": 0.05}, np.array([1.0]), [],
                                       [info("A", fee=0.01)])
    assert anchored == pytest.approx([0.04])


def test_nonsense_inputs_raise_value_error():
    sigma = covariance()
    with pytest.raises(ValueError):
        black_litterman_returns(sigma[:5, :5], IDS, CLASSES, None, W_REF, [], ASSETS)
    with pytest.raises(ValueError):
        black_litterman_returns(sigma, IDS, CLASSES[:5], None, W_REF, [], ASSETS)
    with pytest.raises(ValueError):
        black_litterman_returns(sigma, IDS, CLASSES, None, W_REF[:5], [], ASSETS)
    with pytest.raises(ValueError):
        black_litterman_returns(sigma, ["VTI"] * 6, CLASSES, None, W_REF, [], ASSETS)
    for bad in (np.nan, np.inf):
        broken = sigma.copy()
        broken[0, 1] = bad
        with pytest.raises(ValueError):
            black_litterman_returns(broken, IDS, CLASSES, None, W_REF, [], ASSETS)
    with pytest.raises(ValueError):
        black_litterman_returns(sigma, IDS, CLASSES, None, np.full(6, np.nan), [], ASSETS)
    for kw in ({"delta": 0.0}, {"tau": 0.0}, {"delta": -1.0}):
        with pytest.raises(ValueError):
            bl(**kw)


def test_random_universes_and_views_always_give_finite_results_that_obey_the_view_algebra():
    rng = np.random.default_rng(20261019)
    pyrng = random.Random(20261019)
    for _ in range(150):
        n = int(rng.integers(1, 14))
        a = rng.normal(0, 0.1, (n, int(rng.integers(1, n + 3))))
        cov = a @ a.T + np.diag(rng.uniform(0, 0.02, n))                   # possibly rank deficient
        ids = [f"X{i}" for i in range(n)]
        classes = [pyrng.choice(["equity", "bond", "cash"]) for _ in ids]
        areas = ["a", "b", "c"]
        assets = [info(i, c, fee=pyrng.uniform(0, 0.005), area=pyrng.choice(areas)) for i, c in zip(ids, classes)]
        w = reference_weights(ids, classes, {"equity": 0.5, "bond": 0.4, "cash": 0.1})
        views = [view("area", pyrng.choice(areas), pyrng.uniform(-0.02, 0.02), pyrng.uniform(0.01, 1.0))
                 for _ in range(pyrng.randint(0, 4))]
        assumptions = {"equity": 0.06} if pyrng.random() < 0.5 else None
        mu = black_litterman_returns(cov, ids, classes, assumptions, w, views, assets)
        assert mu.shape == (n,) and np.isfinite(mu).all()
        assert np.array_equal(mu, black_litterman_returns(cov, ids, classes, assumptions, w, views, assets))
        if len(views) == 1:
            P, excess, conf = view_matrix(views, ids, assets)
            prior = black_litterman_returns(cov, ids, classes, None, w, [], assets)
            posterior = black_litterman_returns(cov, ids, classes, None, w, views, assets)
            assert P @ (posterior - prior) == pytest.approx(conf * excess, abs=1e-8)


# ==========================================================================
# real prices (the committed snapshot) and the six personas' views
# ==========================================================================
def test_on_real_prices_the_priors_are_sane_and_the_views_do_what_they_say():
    tickers = ["VT", "VTI", "VXUS", "QQQ", "XLE", "AGG", "SHY", "BIL", "IAU"]
    classes = ["equity"] * 5 + ["bond", "bond", "cash", "commodity"]
    prices = SnapshotPriceSource(agent_c_snapshot_path()).fetch_adjusted_close(tickers, date(2021, 10, 16), date(2026, 10, 16))
    returns = qc.compute_returns(prices[tickers].dropna())
    cov, _ = qc.estimate_annual_covariance(returns, "ledoit_wolf", 0.94, 0.1, 252)
    assets = [info(t, c, fee=0.0005, area=t.lower()) for t, c in zip(tickers, classes)]
    w = reference_weights(tickers, classes, {"equity": 0.5, "bond": 0.3, "cash": 0.1, "commodity": 0.1})
    gross = {"equity": 0.065, "bond": 0.035, "cash": 0.03, "commodity": 0.02}
    prior = black_litterman_returns(cov, tickers, classes, gross, w, [], assets)
    assert np.isfinite(prior).all() and ((prior > -0.02) & (prior < 0.12)).all()
    mu_equity = (w[:5] @ (prior[:5] + 0.0005)) / w[:5].sum()
    assert mu_equity == pytest.approx(0.065)
    tilted = black_litterman_returns(cov, tickers, classes, gross, w, [view("asset", "XLE", 0.01, 1.0)], assets)
    assert tilted[4] - prior[4] > 0.005                                     # the energy fund moves up by about the excess
    assert tilted[4] - prior[4] == pytest.approx(0.01, abs=1e-9)           # confidence 1: met exactly


def test_a_personas_views_on_the_real_catalog_and_real_prices_tilt_what_they_name():
    """The three v2 inputs together: the persona's interests -> build_views -> black_litterman_returns."""
    from egoquant.agents.market_intel import DEFAULT_CATALOG
    from egoquant.agents.market_intel.agent import MarketIntelligenceAgent
    from egoquant.demo.personas import interest_fields
    from egoquant.preferences.views import assets_for_view, build_views

    by_ticker = {i.ticker: i for i in DEFAULT_CATALOG}
    tickers = [t for t in ("VT", "VTI", "VXUS", "VWO", "QQQ", "XLK", "XLE", "XLV", "VNQ", "AGG", "SHY", "BIL", "IEF", "IAU",
                           "PDBC") if t in by_ticker]
    assets = [MarketIntelligenceAgent._asset_info(by_ticker[t]).model_copy(update={
        "area": ",".join(by_ticker[t].areas), "sector": by_ticker[t].sector}) for t in tickers]
    classes = [a.asset_class for a in assets]
    prices = SnapshotPriceSource(agent_c_snapshot_path()).fetch_adjusted_close(tickers, date(2021, 10, 16), date(2026, 10, 16))
    cov, _ = qc.estimate_annual_covariance(qc.compute_returns(prices[tickers].dropna()), "ledoit_wolf", 0.94, 0.1, 252)
    w_ref = reference_weights(tickers, classes, {"equity": 0.6, "bond": 0.3, "cash": 0.05, "commodity": 0.05})
    gross = {"equity": 0.065, "bond": 0.035, "cash": 0.03, "commodity": 0.02}

    views = build_views(interest_fields("growth_investor"), [], [], assets)       # likes technology, healthcare; avoids energy
    assert {(v.kind, v.key) for v in views} >= {("sector", "technology"), ("sector", "energy")}
    prior = black_litterman_returns(cov, tickers, classes, gross, w_ref, [], assets)
    mu = black_litterman_returns(cov, tickers, classes, gross, w_ref, views, assets)
    assert np.isfinite(mu).all() and (np.abs(mu - prior) < 0.01).all()           # a tilt of half a point, not a bet
    for v in views:                                   # each view alone moves its own assets by confidence x excess
        alone = black_litterman_returns(cov, tickers, classes, gross, w_ref, [v], assets)
        P, excess, conf = view_matrix([v], tickers, assets)
        assert (P @ (alone - prior))[0] == pytest.approx(conf[0] * excess[0], abs=1e-12)


def test_a_positive_view_never_makes_any_asset_fall_by_more_than_the_view_on_these_covariances():
    """Not a theorem (an asset with a very negative beta on the view could fall further), but it holds on the
    covariances v2 actually meets: the fixture and the committed real prices."""
    prior = bl()
    for kind, key in [("area", "stocks"), ("sector", "technology"), ("area", "bonds"), ("area", "gold"),
                      ("area", "government_bonds"), ("asset", "VTI")]:
        mu = bl([view(kind, key, 0.005, 0.30)])
        assert (mu - prior).min() >= -0.005, (kind, key)
        assert (mu - prior).max() <= 0.005 * 0.30 * 4 + 1e-12, (kind, key)               # nothing leaps either

    tickers = ["VT", "VTI", "VXUS", "QQQ", "XLE", "AGG", "SHY", "BIL", "IAU"]
    classes = ["equity"] * 5 + ["bond", "bond", "cash", "commodity"]
    prices = SnapshotPriceSource(agent_c_snapshot_path()).fetch_adjusted_close(tickers, date(2021, 10, 16), date(2026, 10, 16))
    cov, _ = qc.estimate_annual_covariance(qc.compute_returns(prices[tickers].dropna()), "ledoit_wolf", 0.94, 0.1, 252)
    assets = [info(t, c) for t, c in zip(tickers, classes)]
    w = reference_weights(tickers, classes, {"equity": 0.5, "bond": 0.3, "cash": 0.1, "commodity": 0.1})
    prior = black_litterman_returns(cov, tickers, classes, None, w, [], assets)
    for t in tickers:
        mu = black_litterman_returns(cov, tickers, classes, None, w, [view("asset", t, 0.005, 0.30)], assets)
        assert (mu - prior).min() >= -0.005, t
        assert (mu - prior)[tickers.index(t)] == pytest.approx(0.30 * 0.005)           # the asset itself moves by confidence x excess


def test_an_asset_no_view_names_never_moves_by_more_than_the_view_asks_of_its_own_assets():
    """Unguarded, a +0.5% view on a near-riskless asset took 0.54% off a volatile fund on the real covariance."""
    cov = np.array([[0.0004, -0.0007, 0.0000],              # a cash fund (2% vol) and a volatile fund (30%), correlation -0.12
                    [-0.0007, 0.0900, 0.0000],
                    [0.0000, 0.0000, 0.0400]])
    ids, classes = ["CASH", "WILD", "OTHER"], ["cash", "equity", "equity"]
    assets = [info(i, c) for i, c in zip(ids, classes)]
    w = np.array([0.4, 0.3, 0.3])
    prior = black_litterman_returns(cov, ids, classes, None, w, [], assets)
    mu = black_litterman_returns(cov, ids, classes, None, w, [view("asset", "CASH", 0.005, 0.30)], assets)
    assert mu[0] - prior[0] == pytest.approx(0.30 * 0.005)                   # the named asset: confidence x excess, exact
    assert abs(mu[1] - prior[1]) <= 0.30 * 0.005 + 1e-15                     # the spill-over is bounded ...
    assert mu[1] - prior[1] == pytest.approx(-0.30 * 0.005)                  # ... and the bound binds here
    assert mu[2] - prior[2] == pytest.approx(0.0)                            # uncorrelated: untouched
    # a view that names the volatile fund too is not clipped: its own assets always get the textbook move
    both = black_litterman_returns(cov, ids, classes, None, w, [view("asset", "CASH", 0.005, 1.0)], assets)
    assert both[0] - prior[0] == pytest.approx(0.005)
    # the guard does not touch the textbook result where it does not bind
    diagonal = np.diag(np.diag(covariance()))
    unguarded = bl([view("area", "stocks", 0.01, 0.5)], cov=diagonal)
    assert unguarded[[2, 3, 4, 5]] == pytest.approx(bl(cov=diagonal)[[2, 3, 4, 5]])
