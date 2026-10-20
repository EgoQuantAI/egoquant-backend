"""Portfolio method v2, targets (egoquant/agents/quant/v2/targets.py): ERC, HRP, maximum diversification.

Pure maths on synthetic covariances: no prices, no network, no randomness except seeded numpy generators.
"""

import time

import numpy as np
import pytest
from scipy.cluster.hierarchy import linkage, to_tree
from scipy.optimize import minimize
from scipy.spatial.distance import squareform

from egoquant.agents.quant.core import diversification_ratio, risk_contributions
from egoquant.agents.quant.v2 import targets
from egoquant.agents.quant.v2.targets import erc_target, fit_to_caps, hrp_target, max_diversification_target

ALL = [erc_target, hrp_target, max_diversification_target]
ids = [f.__name__ for f in ALL]


def random_cov(n: int, seed: int = 0, vol_low: float = 0.02, vol_high: float = 0.30) -> np.ndarray:
    """A realistic covariance: random correlations (positive definite) and annual volatilities 2% to 30%."""
    rng = np.random.default_rng(seed)
    corr = np.corrcoef(rng.normal(size=(n, n + 6)) + rng.normal(size=(1, n + 6)))      # one common factor
    vol = rng.uniform(vol_low, vol_high, n)
    return corr * np.outer(vol, vol)


def one_sleeve(n: int) -> list:
    return [(np.ones(n, dtype=bool), 1.0)]


def two_sleeves(n: int, level: float = 0.4) -> tuple[list, np.ndarray]:
    stocks = np.arange(n) < n // 2
    return [(stocks, level), (~stocks, 1.0 - level)], stocks


# ==========================================================================
# 1. Properties every target must have
# ==========================================================================
@pytest.mark.parametrize("build", ALL, ids=ids)
def test_sums_to_one_is_non_negative_finite_and_below_the_upper_bound(build):
    n = 14
    cov = random_cov(n, seed=3)
    sleeves, _ = two_sleeves(n, 0.55)
    upper = np.full(n, 0.30)
    t = build(cov, np.ones(n, dtype=bool), sleeves, upper)
    assert t is not None and t.shape == (n,)
    assert np.isfinite(t).all()
    assert t.sum() == pytest.approx(1.0, abs=1e-12)
    assert (t >= 0).all()
    assert (t <= upper + 1e-12).all()


@pytest.mark.parametrize("build", ALL, ids=ids)
def test_each_sleeve_gets_exactly_its_share(build):
    n = 12
    sleeves, stocks = two_sleeves(n, 0.35)
    t = build(random_cov(n, seed=5), np.ones(n, dtype=bool), sleeves, np.full(n, 0.5))
    assert t[stocks].sum() == pytest.approx(0.35, abs=1e-12)
    assert t[~stocks].sum() == pytest.approx(0.65, abs=1e-12)


@pytest.mark.parametrize("build", ALL, ids=ids)
def test_same_input_gives_the_identical_output_and_inputs_are_not_changed(build):
    n = 16
    cov, allowed, upper = random_cov(n, seed=9), np.ones(n, dtype=bool), np.full(n, 0.3)
    sleeves, _ = two_sleeves(n)
    before = (cov.copy(), allowed.copy(), upper.copy(), [(m.copy(), s) for m, s in sleeves])
    first = build(cov, allowed, sleeves, upper)
    second = build(cov, allowed, sleeves, upper)
    assert np.array_equal(first, second)
    assert np.array_equal(cov, before[0]) and np.array_equal(allowed, before[1]) and np.array_equal(upper, before[2])
    assert all(np.array_equal(m, b[0]) and s == b[1] for (m, s), b in zip(sleeves, before[3]))


@pytest.mark.parametrize("build", ALL, ids=ids)
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_the_result_does_not_depend_on_the_column_order_of_the_inputs(build, seed):
    n = 15
    cov = random_cov(n, seed=seed)
    allowed = np.ones(n, dtype=bool)
    allowed[4] = False
    upper = np.random.default_rng(seed).uniform(0.15, 0.4, n)
    sleeves, _ = two_sleeves(n, 0.45)
    base = build(cov, allowed, sleeves, upper)
    perm = np.random.default_rng(100 + seed).permutation(n)
    shuffled = build(cov[np.ix_(perm, perm)], allowed[perm], [(m[perm], s) for m, s in sleeves], upper[perm])
    assert np.allclose(shuffled, base[perm], atol=1e-7)


@pytest.mark.parametrize("build", ALL, ids=ids)
def test_excluded_assets_and_assets_with_no_room_get_nothing(build):
    n = 10
    allowed = np.ones(n, dtype=bool)
    allowed[2] = False
    upper = np.full(n, 0.4)
    upper[7] = 0.0
    t = build(random_cov(n, seed=4), allowed, one_sleeve(n), upper)
    assert t[2] == 0.0 and t[7] == 0.0
    assert t.sum() == pytest.approx(1.0, abs=1e-12)


@pytest.mark.parametrize("build", ALL, ids=ids)
def test_a_sleeve_with_a_share_but_no_allowed_asset_gives_none(build):
    n = 8
    cov = random_cov(n, seed=6)
    stocks = np.arange(n) < 4
    allowed = ~stocks                                    # every stock excluded
    assert build(cov, allowed, [(stocks, 0.4), (~stocks, 0.6)], np.full(n, 0.5)) is None
    # the same sleeve with a share of 0 is simply skipped
    t = build(cov, allowed, [(stocks, 0.0), (~stocks, 1.0)], np.full(n, 0.5))
    assert t is not None and t[stocks].sum() == 0.0 and t.sum() == pytest.approx(1.0)
    # an empty mask, and no sleeve at all
    assert build(cov, np.ones(n, dtype=bool), [(np.zeros(n, dtype=bool), 1.0)], np.ones(n)) is None
    assert build(cov, np.ones(n, dtype=bool), [], np.ones(n)) is None


@pytest.mark.parametrize("build", ALL, ids=ids)
def test_a_sleeve_of_one_asset_holds_its_whole_share(build):
    n = 6
    stocks = np.array([True, False, False, False, False, False])
    t = build(random_cov(n, seed=8), np.ones(n, dtype=bool), [(stocks, 0.3), (~stocks, 0.7)], np.full(n, 0.9))
    assert t[0] == pytest.approx(0.3, abs=1e-12)
    assert t.sum() == pytest.approx(1.0, abs=1e-12)


@pytest.mark.parametrize("build", ALL, ids=ids)
def test_a_single_asset_universe(build):
    t = build(np.array([[0.04]]), np.array([True]), [(np.array([True]), 1.0)], np.array([1.0]))
    assert t is not None and t.tolist() == [1.0]


@pytest.mark.parametrize("build", ALL, ids=ids)
def test_all_assets_exactly_at_the_cap_get_the_cap(build):
    n = 4
    t = build(random_cov(n, seed=2), np.ones(n, dtype=bool), one_sleeve(n), np.full(n, 0.25))
    assert np.allclose(t, 0.25, atol=1e-12) and t.sum() == pytest.approx(1.0)


@pytest.mark.parametrize("build", ALL, ids=ids)
def test_bounds_that_cannot_hold_the_share_still_give_a_target_that_sums_to_one(build):
    """Two assets capped at 30% cannot carry 100%: both sit at their cap and the target still sums to 1 (the
    projection that follows decides what is feasible)."""
    t = build(random_cov(2, seed=1), np.ones(2, dtype=bool), one_sleeve(2), np.full(2, 0.3))
    assert np.isfinite(t).all() and t.sum() == pytest.approx(1.0, abs=1e-12)
    assert (t >= 0.3 - 1e-12).all()


@pytest.mark.parametrize("build", ALL, ids=ids)
def test_a_zero_variance_asset_such_as_cash_gives_a_finite_target(build):
    n = 6
    cov = random_cov(n, seed=11)
    cov[0, :] = 0.0
    cov[:, 0] = 0.0
    t = build(cov, np.ones(n, dtype=bool), one_sleeve(n), np.full(n, 0.4))
    assert np.isfinite(t).all() and t.sum() == pytest.approx(1.0, abs=1e-12)
    assert (t <= 0.4 + 1e-12).all()


@pytest.mark.parametrize("build", ALL, ids=ids)
def test_a_covariance_with_a_nan_gives_none_not_a_nan_target(build):
    cov = random_cov(5, seed=1)
    cov[1, 2] = cov[2, 1] = np.nan
    assert build(cov, np.ones(5, dtype=bool), one_sleeve(5), np.ones(5)) is None


@pytest.mark.parametrize("build", ALL, ids=ids)
def test_a_full_size_universe_is_fast(build):
    n = 120
    cov = random_cov(n, seed=21)
    sleeves, _ = two_sleeves(n, 0.6)
    start = time.perf_counter()
    t = build(cov, np.ones(n, dtype=bool), sleeves, np.full(n, 0.3))
    assert time.perf_counter() - start < 3.0
    assert t.sum() == pytest.approx(1.0, abs=1e-12) and (t <= 0.3 + 1e-12).all()


# ==========================================================================
# 2. Clip and redistribute
# ==========================================================================
def test_fit_to_caps_moves_the_excess_to_the_other_assets_in_proportion():
    w = fit_to_caps(np.array([0.9, 0.06, 0.04]), np.array([0.5, 1.0, 1.0]), 1.0)
    assert w == pytest.approx([0.5, 0.3, 0.2])


def test_fit_to_caps_can_clip_several_assets_in_a_row():
    w = fit_to_caps(np.array([0.6, 0.3, 0.07, 0.03]), np.array([0.25, 0.25, 1.0, 1.0]), 1.0)
    assert w == pytest.approx([0.25, 0.25, 0.35, 0.15])


def test_fit_to_caps_never_exceeds_a_bound_even_when_the_loop_runs_out():
    rng = np.random.default_rng(0)
    raw = np.sort(rng.dirichlet(np.full(200, 0.05)))[::-1]
    cap = np.full(200, 0.012)                                                       # 200 x 1.2% = 240%: a total of 100% fits
    w = fit_to_caps(raw, cap, 1.0, max_iterations=2)                                # too few passes on purpose
    assert w.sum() == pytest.approx(1.0, abs=1e-12) and (w <= cap + 1e-12).all()
    assert targets.MAX_CAP_ITERATIONS == 50
    w50 = fit_to_caps(raw, cap, 1.0)
    assert w50.sum() == pytest.approx(1.0, abs=1e-12) and (w50 <= cap + 1e-12).all()


def test_fit_to_caps_hands_out_room_when_the_free_assets_have_no_method_weight():
    # the method gave the only free asset nothing; the room left under the bounds takes the excess
    w = fit_to_caps(np.array([1.0, 0.0, 0.0]), np.array([0.4, 0.5, 0.5]), 1.0)
    assert w.sum() == pytest.approx(1.0) and (w <= [0.4, 0.5, 0.5]).all() and w[0] == pytest.approx(0.4)


# ==========================================================================
# 3. Equal risk contribution
# ==========================================================================
def test_erc_risk_contributions_are_equal_within_a_tenth_of_a_percent():
    for seed in range(5):
        n = 12
        cov = random_cov(n, seed=seed)
        w = erc_target(cov, np.ones(n, dtype=bool), one_sleeve(n), np.ones(n))
        rc = risk_contributions(w, cov)
        assert np.max(np.abs(rc / (1.0 / n) - 1.0)) < 1e-3


def test_erc_risk_contributions_are_equal_inside_each_sleeve():
    n = 16
    cov = random_cov(n, seed=13)
    sleeves, stocks = two_sleeves(n, 0.6)
    t = erc_target(cov, np.ones(n, dtype=bool), sleeves, np.ones(n))
    for members in (stocks, ~stocks):
        idx = np.flatnonzero(members)
        rc = risk_contributions(t[idx], cov[np.ix_(idx, idx)])
        assert np.max(np.abs(rc * len(idx) - 1.0)) < 1e-3


def test_erc_of_uncorrelated_assets_is_inverse_volatility():
    vol = np.array([0.05, 0.10, 0.20, 0.40])
    w = erc_target(np.diag(vol**2), np.ones(4, dtype=bool), one_sleeve(4), np.ones(4))
    assert w == pytest.approx((1 / vol) / (1 / vol).sum(), abs=1e-9)


def test_erc_of_identical_assets_is_equal_weight():
    cov = np.full((5, 5), 0.02) + np.eye(5) * 0.02                                  # equal variances, equal correlations
    w = erc_target(cov, np.ones(5, dtype=bool), one_sleeve(5), np.ones(5))
    assert w == pytest.approx(np.full(5, 0.2), abs=1e-9)


def test_erc_gives_the_calmer_asset_more_weight():
    cov = random_cov(10, seed=17)
    w = erc_target(cov, np.ones(10, dtype=bool), one_sleeve(10), np.ones(10))
    calmest, wildest = int(np.argmin(np.diag(cov))), int(np.argmax(np.diag(cov)))
    assert w[calmest] > w[wildest]


# ==========================================================================
# 4. Hierarchical risk parity
# ==========================================================================
def reference_hrp(cov: np.ndarray) -> np.ndarray:
    """The recursive bisection of Lopez de Prado (2016) as written in the paper (snippets 16.2 to 16.4), for assets
    that are all different. The paper leaves open which child of a merge comes first; here the calmer one, as the
    module documents. Written independently of the module under test."""
    sd = np.sqrt(np.diag(cov))
    dist = np.sqrt(0.5 * (1.0 - np.clip(cov / np.outer(sd, sd), -1.0, 1.0)))
    np.fill_diagonal(dist, 0.0)
    tree = to_tree(linkage(squareform(dist, checks=False), method="single"))

    def cluster_var(items):
        sub = cov[np.ix_(items, items)]
        ivp = 1.0 / np.diag(sub)
        ivp /= ivp.sum()
        return ivp @ sub @ ivp

    def quasi_diagonal(node):                                                       # snippet 16.2 with a fixed child order
        if node.is_leaf():
            return [node.id]
        first, second = quasi_diagonal(node.left), quasi_diagonal(node.right)
        if (cluster_var(second), -len(second)) < (cluster_var(first), -len(first)):
            first, second = second, first
        return first + second

    order = quasi_diagonal(tree)
    n = len(cov)
    w = np.ones(n)
    groups = [list(order)]
    while groups:
        groups = [g[j:k] for g in groups for j, k in ((0, len(g) // 2), (len(g) // 2, len(g))) if len(g) > 1]
        for i in range(0, len(groups), 2):
            left, right = groups[i], groups[i + 1]
            alpha = 1.0 - cluster_var(left) / (cluster_var(left) + cluster_var(right))
            w[left] *= alpha
            w[right] *= 1.0 - alpha
    return w / w.sum()


@pytest.mark.parametrize("n", [3, 7, 12, 25])
def test_hrp_matches_the_published_algorithm(n):
    cov = random_cov(n, seed=n)
    t = hrp_target(cov, np.ones(n, dtype=bool), one_sleeve(n), np.ones(n))
    assert t == pytest.approx(reference_hrp(cov), abs=1e-8)       # the module adds a 1e-10 relative ridge


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 8])
def test_hrp_gives_identical_assets_equal_weights(n):
    cov = np.full((n, n), 0.03)                                                     # every pair perfectly correlated, same variance
    t = hrp_target(cov, np.ones(n, dtype=bool), one_sleeve(n), np.ones(n))
    assert t == pytest.approx(np.full(n, 1.0 / n), abs=1e-12)


def test_hrp_copies_of_one_asset_share_that_assets_weight_equally():
    base = random_cov(4, seed=3)
    ids_ = [0, 1, 1, 1, 2, 3]                                                       # asset 1 appears three times
    cov = base[np.ix_(ids_, ids_)]
    t = hrp_target(cov, np.ones(6, dtype=bool), one_sleeve(6), np.ones(6))
    alone = hrp_target(base, np.ones(4, dtype=bool), one_sleeve(4), np.ones(4))
    assert t[1] == pytest.approx(t[2]) == pytest.approx(t[3])
    assert t[1] + t[2] + t[3] == pytest.approx(alone[1], abs=1e-12)
    assert t[0] == pytest.approx(alone[0]) and t[4] == pytest.approx(alone[2]) and t[5] == pytest.approx(alone[3])


def test_hrp_a_calm_block_gets_more_weight_than_a_wild_block_and_no_asset_is_dropped():
    cov = np.zeros((6, 6))
    cov[:3, :3] = 0.0004 * (0.5 + 0.5 * np.eye(3))                                  # calm block, volatility about 2%
    cov[3:, 3:] = 0.09 * (0.5 + 0.5 * np.eye(3))                                    # wild block, volatility 30%
    t = hrp_target(cov, np.ones(6, dtype=bool), one_sleeve(6), np.ones(6))
    assert t[:3].sum() > 0.9 and (t > 0).all()


# ==========================================================================
# 5. Maximum diversification
# ==========================================================================
def direct_max_diversification(cov: np.ndarray) -> np.ndarray:
    """The textbook form: min y'Sigma y subject to sigma'y = 1, y >= 0, then w = y / sum(y) (slow reference)."""
    n = len(cov)
    sd = np.sqrt(np.diag(cov))
    y0 = np.full(n, 1.0 / sd.sum())
    scale = float(np.mean(np.diag(cov)))
    res = minimize(
        lambda y: float(y @ cov @ y) / scale, y0, jac=lambda y: 2 * cov @ y / scale, method="SLSQP",
        bounds=[(0, None)] * n, constraints=[{"type": "eq", "fun": lambda y: sd @ y - 1.0, "jac": lambda y: sd}],
        options={"maxiter": 1000, "ftol": 1e-16},
    )
    y = np.maximum(res.x, 0.0)
    return y / y.sum()


@pytest.mark.parametrize("seed", range(6))
def test_max_diversification_ratio_is_at_least_that_of_equal_weight_and_inverse_volatility(seed):
    n = 15
    cov = random_cov(n, seed=seed)
    w = max_diversification_target(cov, np.ones(n, dtype=bool), one_sleeve(n), np.ones(n))
    equal = np.full(n, 1.0 / n)
    inverse_vol = (1 / np.sqrt(np.diag(cov))) / (1 / np.sqrt(np.diag(cov))).sum()
    assert diversification_ratio(w, cov) >= diversification_ratio(equal, cov)
    assert diversification_ratio(w, cov) >= diversification_ratio(inverse_vol, cov) - 1e-9


@pytest.mark.parametrize("seed", range(4))
def test_max_diversification_reaches_the_optimum_of_the_textbook_formulation(seed):
    n = 10
    cov = random_cov(n, seed=40 + seed)
    w = max_diversification_target(cov, np.ones(n, dtype=bool), one_sleeve(n), np.ones(n))
    oracle = direct_max_diversification(cov)
    assert diversification_ratio(w, cov) >= diversification_ratio(oracle, cov) - 1e-6
    assert w == pytest.approx(oracle, abs=2e-3)


def test_max_diversification_meets_the_optimality_conditions():
    """At the optimum the marginal 'cost' (R x)_i of every held asset is equal and no unheld asset is cheaper."""
    n = 20
    cov = random_cov(n, seed=33)
    w = max_diversification_target(cov, np.ones(n, dtype=bool), one_sleeve(n), np.ones(n))
    sd = np.sqrt(np.diag(cov))
    x = w * sd / (w * sd).sum()
    marginal = (cov / np.outer(sd, sd)) @ x
    held = x > 1e-7
    assert held.sum() >= 2
    assert np.ptp(marginal[held]) < 1e-5
    if (~held).any():
        assert marginal[~held].min() >= marginal[held].max() - 1e-5


def test_max_diversification_of_uncorrelated_assets_is_inverse_volatility():
    vol = np.array([0.05, 0.10, 0.20, 0.40])
    w = max_diversification_target(np.diag(vol**2), np.ones(4, dtype=bool), one_sleeve(4), np.ones(4))
    assert w == pytest.approx((1 / vol) / (1 / vol).sum(), abs=1e-6)


def test_max_diversification_gives_a_duplicated_fund_the_same_weight_as_its_twin():
    cov = random_cov(6, seed=5)
    twin = np.vstack([np.hstack([cov, cov[:, [0]]]), np.hstack([cov[[0], :], cov[[0], [0]].reshape(1, 1)])])   # asset 6 = asset 0
    w = max_diversification_target(twin, np.ones(7, dtype=bool), one_sleeve(7), np.ones(7))
    assert w[0] == pytest.approx(w[6])                                              # identical assets share their weight equally
    assert w.sum() == pytest.approx(1.0)


# ==========================================================================
# 6. Used the way the candidate builder will use them
# ==========================================================================
@pytest.mark.parametrize("build", ALL, ids=ids)
@pytest.mark.parametrize("level", [0.2, 0.4, 0.8])
def test_the_stock_share_follows_the_level_with_the_per_fund_cap(build, level):
    n = 40
    cov = random_cov(n, seed=77)
    stocks = np.arange(n) < 25
    allowed = np.ones(n, dtype=bool)
    allowed[[3, 30]] = False
    upper = np.where(stocks, 0.10, 0.30)                                            # single stocks 10%, funds 30%
    t = build(cov, allowed, [(stocks, level), (~stocks, 1.0 - level)], upper)
    assert t[stocks].sum() == pytest.approx(level, abs=1e-12)
    assert (t <= upper + 1e-12).all() and t[3] == 0.0 and t[30] == 0.0
