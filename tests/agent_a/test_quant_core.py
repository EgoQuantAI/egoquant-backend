"""Agent A math core and price validation (egoquant/agents/quant/core.py). No agent, no network.

Layout:  1) math core   2) data validation
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from egoquant.agents.quant import core as qc

AS_OF = date(2026, 9, 30)


# ==========================================================================
# 1. Math core
# ==========================================================================
def _correlated_returns(n_obs: int, rho: float = 0.5, n_assets: int = 5, seed: int = 0) -> pd.DataFrame:
    cov = np.full((n_assets, n_assets), rho) + (1 - rho) * np.eye(n_assets)
    data = np.random.default_rng(seed).multivariate_normal(np.zeros(n_assets), cov * 1e-4, size=n_obs)
    return pd.DataFrame(data, columns=list("ABCDE")[:n_assets])


def test_ewma_weights_sum_to_one_and_favor_recent():
    weights = qc.ewma_weights(50, 0.94)
    assert weights.sum() == pytest.approx(1.0)
    assert weights[-1] > weights[0]


def test_fixed_shrinkage_endpoints():
    cov = np.array([[0.04, 0.02], [0.02, 0.09]])
    assert np.allclose(qc.shrink_to_diagonal(cov, 0.0), cov)
    assert np.allclose(qc.shrink_to_diagonal(cov, 1.0), np.diag([0.04, 0.09]))


def test_ledoit_wolf_intensity_is_a_probability_and_falls_with_more_data():
    _, short_history = qc.ledoit_wolf_covariance(_correlated_returns(40))
    _, long_history = qc.ledoit_wolf_covariance(_correlated_returns(4000))
    assert 0.0 <= long_history < short_history <= 1.0


def test_ledoit_wolf_keeps_each_asset_volatility_and_is_positive_definite():
    returns = _correlated_returns(120)
    cov, _ = qc.ledoit_wolf_covariance(returns)
    assert np.allclose(np.sqrt(np.diag(cov)), returns.std().to_numpy(), rtol=1e-6)   # only correlations shrink
    assert np.linalg.eigvalsh(cov).min() > 0


def test_ledoit_wolf_is_close_to_sample_covariance_with_lots_of_data():
    returns = _correlated_returns(20000, seed=3)
    cov, intensity = qc.ledoit_wolf_covariance(returns)
    assert intensity < 0.01
    assert np.allclose(cov, qc.sample_covariance(returns), rtol=0.05, atol=1e-6)


def test_estimate_annual_covariance_reports_the_intensity_used():
    returns = _correlated_returns(200)
    _, fixed = qc.estimate_annual_covariance(returns, "sample", 0.94, 0.10, 252)
    cov, estimated = qc.estimate_annual_covariance(returns, "ledoit_wolf", 0.94, 0.10, 252)
    assert fixed == 0.10 and 0.0 <= estimated <= 1.0 and cov.shape == (5, 5)
    with pytest.raises(ValueError):
        qc.estimate_annual_covariance(returns, "magic", 0.94, 0.10, 252)


def test_ensure_positive_definite_repairs_singular_matrix():
    singular = np.array([[1.0, 1.0], [1.0, 1.0]])  # perfectly correlated assets
    assert np.linalg.eigvalsh(qc.ensure_positive_definite(singular)).min() > 0


def test_volatility_regime_dead_band_and_floor():
    ratio, label, scale = qc.volatility_regime(0.10, 0.10, low=0.8, high=1.25, min_scale=0.5)
    assert (ratio, label, scale) == (1.0, "normal", 1.0)
    assert qc.volatility_regime(0.20, 0.10, 0.8, 1.25, 0.5)[2] == pytest.approx(0.625)   # 1.25 / 2.0
    assert qc.volatility_regime(0.50, 0.10, 0.8, 1.25, 0.5) == (5.0, "high", 0.5)         # floored
    assert qc.volatility_regime(0.05, 0.10, 0.8, 1.25, 0.5)[1:] == ("low", 1.0)           # never above 1


def test_min_variance_matches_closed_form():
    """With only sum(w)=1 and no binding bounds: w* = Sigma^-1 1 / (1' Sigma^-1 1)."""
    cov = np.array([[0.04, 0.006], [0.006, 0.01]])
    expected = np.linalg.solve(cov, np.ones(2))
    expected /= expected.sum()
    result = qc.solve_portfolio(cov, ["A", "B"], np.ones(2), ["system"] * 2, [], [], np.zeros(2))
    assert result.status == "optimal"
    assert np.allclose(result.weights, expected, atol=1e-5)


def test_solver_respects_bounds_and_budget():
    cov = np.diag([0.01, 0.04, 0.09])
    upper = np.array([0.5, 0.5, 0.5])
    result = qc.solve_portfolio(cov, list("ABC"), upper, ["cap"] * 3, [], [], np.zeros(3))
    assert result.weights.sum() == pytest.approx(1.0)
    assert (result.weights >= 0).all() and (result.weights <= 0.5 + 1e-9).all()
    assert "cap[A]" in result.binding  # the lowest-variance asset hits its cap


def test_solver_reports_infeasible_instead_of_raising():
    cov = np.eye(2) * 0.01
    # Two assets capped at 0.4 each can never sum to 1.
    result = qc.solve_portfolio(cov, ["A", "B"], np.array([0.4, 0.4]), ["cap"] * 2, [], [], np.zeros(2))
    assert result.status == "infeasible" and result.weights is None


def test_solver_volatility_limit_is_enforced_and_reported_binding():
    cov = np.diag([0.04, 0.0025])                     # vols 20% and 5%
    result = qc.solve_portfolio(cov, ["RISKY", "SAFE"], np.ones(2), ["s"] * 2, [], [("vol_cap", 0.12)],
                                np.array([1.0, 0.0]), equity_target=0.5)
    # Forced to hold 50% of the risky asset: vol = sqrt(0.25*0.04 + 0.25*0.0025) = 10.3% < 12%, cap slack
    assert result.status == "optimal" and "vol_cap" not in result.binding
    tight = qc.solve_portfolio(cov, ["RISKY", "SAFE"], np.ones(2), ["s"] * 2, [], [("vol_cap", 0.09)],
                               np.array([1.0, 0.0]), equity_target=0.5)
    assert tight.status == "infeasible"               # 50% risky already breaks a 9% cap


def test_turnover_penalty_keeps_the_portfolio_closer_to_current_holdings():
    cov = np.array([[0.04, 0.002], [0.002, 0.01]])
    current = np.array([0.5, 0.5])
    free = qc.solve_portfolio(cov, ["A", "B"], np.ones(2), ["s"] * 2, [], [], np.zeros(2))
    sticky = qc.solve_portfolio(cov, ["A", "B"], np.ones(2), ["s"] * 2, [], [], np.zeros(2),
                                current_weights=current, trade_cost_rates=np.array([0.001, 0.001]),
                                turnover_penalty=50.0)
    assert qc.turnover(sticky.weights, current) < qc.turnover(free.weights, current)
    off = qc.solve_portfolio(cov, ["A", "B"], np.ones(2), ["s"] * 2, [], [], np.zeros(2),
                             current_weights=current, trade_cost_rates=np.array([0.001, 0.001]),
                             turnover_penalty=0.0)
    assert np.allclose(off.weights, free.weights, atol=1e-5)   # penalty 0 = same as no turnover terms


def test_turnover_cap_is_respected_and_reported_binding():
    cov = np.array([[0.04, 0.002], [0.002, 0.01]])
    current = np.array([0.9, 0.1])
    result = qc.solve_portfolio(cov, ["A", "B"], np.ones(2), ["s"] * 2, [], [], np.zeros(2),
                                current_weights=current, turnover_cap=("cap", 0.10))
    assert qc.turnover(result.weights, current) == pytest.approx(0.10, abs=1e-5)
    assert "cap" in result.binding
    impossible = qc.solve_portfolio(cov, ["A", "B"], np.ones(2), ["s"] * 2, [], [], np.array([1.0, 0.0]),
                                    equity_target=0.2, current_weights=current, turnover_cap=("cap", 0.05))
    assert impossible.status == "infeasible"          # moving from 90% to 20% in A needs turnover 0.7


def test_risk_contributions_sum_to_one():
    cov = np.array([[0.04, 0.01], [0.01, 0.02]])
    contributions = qc.risk_contributions(np.array([0.6, 0.4]), cov)
    assert contributions.sum() == pytest.approx(1.0)


def test_effective_number_of_holdings():
    assert qc.effective_number_of_holdings(np.array([0.25] * 4)) == pytest.approx(4.0)
    assert qc.effective_number_of_holdings(np.array([1.0, 0.0])) == pytest.approx(1.0)


def test_legacy_volatility_targeting_never_adds_leverage():
    weights = np.array([0.5, 0.3, 0.2])          # last asset is cash
    cash = np.array([False, False, True])
    low_vol, scale_low = qc.apply_volatility_targeting(weights, estimated_vol=0.05, target_vol=0.10, cash_mask=cash)
    assert scale_low == 1.0 and np.allclose(low_vol, weights)       # calm market: unchanged
    high_vol, scale_high = qc.apply_volatility_targeting(weights, estimated_vol=0.20, target_vol=0.10, cash_mask=cash)
    assert scale_high == pytest.approx(0.5)
    assert high_vol.sum() == pytest.approx(1.0)                      # still fully invested
    assert (high_vol[:2] <= weights[:2] + 1e-12).all()               # risky weights only fall
    assert high_vol[2] > weights[2]                                  # freed weight sits in cash


def test_scenario_return_uses_duration_and_convexity_for_rate_shock():
    shocks = qc.scenario_shock_vector(["equity", "bond", "cash"], [None, 7.0, None], [None, None, None],
                                      {"equity": -0.10}, 100.0)
    assert np.allclose(shocks, [-0.10, -0.07, 0.0])                  # bond: -7y * 1% = -7%
    assert qc.scenario_portfolio_return(np.array([0.5, 0.3, 0.2]), shocks) == pytest.approx(-0.071)
    with_convexity = qc.scenario_shock_vector(["bond"], [7.0], [60.0], {}, 100.0)
    assert with_convexity[0] == pytest.approx(-0.07 + 0.5 * 60.0 * 0.01**2)   # +0.3% from curvature


def test_stressed_correlation_raises_volatility_of_a_diversified_portfolio():
    vols = np.array([0.20, 0.06, 0.005])
    corr = np.array([[1.0, 0.1, 0.0], [0.1, 1.0, 0.0], [0.0, 0.0, 1.0]])
    cov = corr * np.outer(vols, vols)
    risky = np.array([True, True, False])
    stressed = qc.stressed_covariance(cov, risky, 0.9)
    w = np.array([0.4, 0.5, 0.1])
    assert qc.portfolio_volatility(w, stressed) > qc.portfolio_volatility(w, cov)
    assert np.allclose(np.sqrt(np.diag(stressed)), vols, rtol=1e-6)      # volatilities untouched
    assert stressed[0, 2] == pytest.approx(0.0, abs=1e-9)                # cash keeps its correlations


def test_reverse_stress_closed_form():
    w, equity = np.array([0.4, 0.6]), np.array([1.0, 0.0])
    assert qc.equity_drop_to_reach_loss(w, equity, 0.20) == pytest.approx(0.5)   # 40% equity: needs -50%
    assert qc.equity_drop_to_reach_loss(w, equity, 0.60) is None                 # would need more than -100%
    assert qc.equity_drop_to_reach_loss(np.array([0.0, 1.0]), equity, 0.1) is None


def test_turnover_and_cost():
    new, old = np.array([0.6, 0.4]), np.array([0.4, 0.6])
    assert qc.turnover(new, old) == pytest.approx(0.2)
    assert qc.transaction_cost(new, old, np.array([10.0, 20.0])) == pytest.approx((0.2 * 10 + 0.2 * 20) / 10_000)


def test_drawdown_and_var_on_known_path():
    returns = np.array([0.10, -0.20, 0.05])
    assert qc.max_drawdown(returns) == pytest.approx(0.20)
    var, cvar, n_tail = qc.historical_var_cvar(np.linspace(-0.05, 0.05, 101), 0.95)
    assert cvar >= var > 0 and n_tail == 6


# ==========================================================================
# 2. Data validation
# ==========================================================================
def _clean_prices(rows: int = 300) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-09-30", periods=rows)
    return pd.DataFrame({"A": np.linspace(100, 120, rows), "B": np.linspace(50, 60, rows)}, index=dates)


def test_clean_prices_pass():
    report = qc.validate_prices(_clean_prices(), ["A", "B"], AS_OF, min_rows=253)
    assert report.issues == []


def test_validation_catches_nan_lookahead_missing_asset_and_short_history():
    prices = _clean_prices()
    prices.iloc[5, 0] = np.nan
    assert any("missing values" in i for i in qc.validate_prices(prices, ["A", "B"], AS_OF, 253).issues)
    assert any("after as_of" in i for i in qc.validate_prices(_clean_prices(), ["A", "B"], date(2026, 9, 1), 253).issues)
    assert any("missing for assets" in i for i in qc.validate_prices(_clean_prices(), ["A", "B", "C"], AS_OF, 253).issues)
    assert any("not enough history" in i for i in qc.validate_prices(_clean_prices(100), ["A", "B"], AS_OF, 253).issues)


def test_validation_catches_unsorted_dates():
    assert any("sorted" in i for i in qc.validate_prices(_clean_prices().iloc[::-1], ["A", "B"], AS_OF, 253).issues)
