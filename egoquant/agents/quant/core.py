"""Pure quantitative functions for Agent A (version 2).

Rules for this file:
  * NumPy / Pandas / SciPy only. No Pydantic, no agents, no LLM.
  * Same inputs always give the same outputs (reproducible, easy to unit test).
  * Everything computed from history is an ESTIMATE, not a forecast.

Notation used in comments:
  w      portfolio weights (vector, sums to 1, long-only)
  Sigma  annualized covariance matrix of asset returns
  mu     expected returns (an explicit ASSUMPTION, never the historical mean)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from scipy.optimize import linprog, minimize

TOLERANCE = 1e-6  # numerical tolerance for "constraint satisfied" and "constraint binding"
EQ_SLACK = 1e-10   # slack when SLSQP retries an equality as two inequalities (far below TOLERANCE)


# ==========================================================================
# 1. Market data validation
# ==========================================================================
@dataclass
class PriceValidation:
    issues: list[str] = field(default_factory=list)      # block the run
    warnings: list[str] = field(default_factory=list)    # suspicious, not blocking
    n_observations: int = 0
    first_date: date | None = None
    last_date: date | None = None


def validate_prices(
    prices: pd.DataFrame,
    expected_asset_ids: list[str],
    as_of: date,
    min_rows: int,
    max_daily_move: float = 0.5,
    stale_days: int = 7,
) -> PriceValidation:
    """Check dates, identifiers, missing values and look-ahead before any maths happens."""
    result = PriceValidation(n_observations=len(prices))

    if not isinstance(prices.index, pd.DatetimeIndex):
        result.issues.append("price index is not a DatetimeIndex")
        return result  # nothing else can be checked reliably

    if len(prices) > 0:
        result.first_date = prices.index.min().date()
        result.last_date = prices.index.max().date()

    if not prices.index.is_monotonic_increasing:
        result.issues.append("dates are not sorted ascending")
    if prices.index.has_duplicates:
        result.issues.append("duplicate dates found")

    # Look-ahead bias: no row may be dated after the decision date.
    if result.last_date is not None and result.last_date > as_of:
        result.issues.append(f"data contains dates after as_of ({result.last_date} > {as_of})")

    # Asset identifiers must match the metadata exactly.
    missing_cols = sorted(set(expected_asset_ids) - set(prices.columns))
    extra_cols = sorted(set(prices.columns) - set(expected_asset_ids))
    if missing_cols:
        result.issues.append(f"prices missing for assets: {missing_cols}")
    if extra_cols:
        result.issues.append(f"prices have unknown assets: {extra_cols}")

    if prices.isna().any().any():
        result.issues.append("prices contain missing values (NaN)")
    if (prices.select_dtypes("number") <= 0).any().any():
        result.issues.append("prices contain zero or negative values")
    if len(prices) < min_rows:
        result.issues.append(f"not enough history: {len(prices)} rows, need at least {min_rows}")

    # Warnings only: unusual but possibly legitimate.
    daily_moves = prices.pct_change().abs()
    if (daily_moves > max_daily_move).any().any():
        result.warnings.append(f"daily move above {max_daily_move:.0%} (bad tick or unadjusted split?)")
    if result.last_date is not None and (as_of - result.last_date).days > stale_days:
        result.warnings.append(f"latest price is {(as_of - result.last_date).days} days older than as_of")

    return result


# ==========================================================================
# 2. Returns and covariance estimation
# ==========================================================================
def compute_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """Simple periodic returns r_t = P_t / P_{t-1} - 1 from ADJUSTED prices."""
    return prices.pct_change().dropna()


def sample_covariance(returns: pd.DataFrame) -> np.ndarray:
    """Plain sample covariance (per period, not annualized)."""
    return returns.cov().to_numpy()


def ewma_weights(n_obs: int, decay: float) -> np.ndarray:
    """Normalized exponential weights, newest observation weighted most."""
    raw = decay ** np.arange(n_obs - 1, -1, -1)
    return raw / raw.sum()


def ewma_covariance(returns: pd.DataFrame, decay: float) -> np.ndarray:
    """Exponentially weighted covariance (zero-mean convention, RiskMetrics style).

    Sigma = sum_t  weight_t * r_t r_t'      with weight_t proportional to decay^(T-1-t)
    """
    x = returns.to_numpy()
    weights = ewma_weights(len(x), decay)
    return (x.T * weights) @ x


def shrink_to_diagonal(cov: np.ndarray, shrinkage: float) -> np.ndarray:
    """Blend the covariance with its own diagonal: (1-d) * Sigma + d * diag(Sigma).

    A fixed-intensity stabilizer. It lowers estimated correlations, which tames noisy optimizer
    weights. The intensity d is a guess here; ledoit_wolf_covariance estimates it from the data.
    """
    return (1.0 - shrinkage) * cov + shrinkage * np.diag(np.diag(cov))


def ledoit_wolf_covariance(returns: pd.DataFrame) -> tuple[np.ndarray, float]:
    """Ledoit-Wolf shrinkage of the CORRELATION matrix toward the identity.

    Why correlations and not raw covariances: our assets have wildly different volatilities (cash
    0.5%, emerging equity 22%). A scaled-identity target on raw covariances would distort that.
    Shrinking the standardized returns keeps every asset's own volatility untouched and only
    pulls the noisy off-diagonal correlations toward zero.

    With z_t the standardized returns, S = Z'Z / T and target F = I:
        d2 = ||S - F||^2                          (distance between sample and target)
        b2 = (1/T^2) * sum_t ||z_t z_t' - S||^2   (estimation noise of S), capped at d2
        intensity = b2 / d2                       (0 = trust the sample, 1 = use the target)

    Returns (period covariance, intensity). Reference: Ledoit & Wolf (2004), J. Multivariate Anal.
    """
    x = returns.to_numpy()
    t, p = x.shape
    centered = x - x.mean(axis=0)
    std = centered.std(axis=0)                      # population std (ddof=0) so diag(S) = 1
    safe_std = np.where(std > 0, std, 1.0)          # guard against a constant series
    z = centered / safe_std
    sample_corr = z.T @ z / t

    target_scale = float(np.trace(sample_corr) / p)
    d2 = float(np.sum((sample_corr - target_scale * np.eye(p)) ** 2))
    # sum_t ||z_t z_t' - S||^2 = sum_t ||z_t||^4 - T * ||S||^2   (expanded, no loop needed)
    row_norm_sq = np.sum(z**2, axis=1)
    b2 = float((np.sum(row_norm_sq**2) - t * np.sum(sample_corr**2)) / t**2)
    b2 = min(max(b2, 0.0), d2)
    intensity = 0.0 if d2 <= 0 else b2 / d2

    shrunk_corr = (1.0 - intensity) * sample_corr + intensity * target_scale * np.eye(p)
    cov = np.outer(std, std) * shrunk_corr * (t / (t - 1.0))   # rescale to the ddof=1 convention
    return cov, float(intensity)


def ensure_positive_definite(cov: np.ndarray, floor: float = 1e-10) -> np.ndarray:
    """Symmetrize and, if needed, lift the smallest eigenvalue so Sigma is invertible."""
    cov = (cov + cov.T) / 2.0
    smallest = np.linalg.eigvalsh(cov).min()
    if smallest < floor:
        cov = cov + (floor - smallest) * np.eye(len(cov))
    return cov


def estimate_annual_covariance(
    returns: pd.DataFrame,
    estimator: str,
    ewma_decay: float,
    shrinkage: float,
    periods_per_year: int,
) -> tuple[np.ndarray, float]:
    """Full covariance pipeline: estimate -> shrink -> repair -> annualize.

    Returns (annual covariance, shrinkage intensity actually used).
    """
    if estimator == "sample":
        cov, intensity = shrink_to_diagonal(sample_covariance(returns), shrinkage), shrinkage
    elif estimator == "ewma":
        cov, intensity = shrink_to_diagonal(ewma_covariance(returns, ewma_decay), shrinkage), shrinkage
    elif estimator == "ledoit_wolf":
        cov, intensity = ledoit_wolf_covariance(returns)
    else:
        raise ValueError(f"unknown covariance estimator: {estimator}")
    return ensure_positive_definite(cov) * periods_per_year, intensity


def condition_number(cov: np.ndarray) -> float:
    """Numerical-stability diagnostic: large values mean fragile optimization."""
    return float(np.linalg.cond(cov))


def ewma_volatility(period_returns: np.ndarray, decay: float, periods_per_year: int) -> float:
    """Annualized EWMA volatility of one return series (the 'volatility regime' estimate)."""
    weights = ewma_weights(len(period_returns), decay)
    variance = float(np.sum(weights * period_returns**2))
    return float(np.sqrt(variance * periods_per_year))


def volatility_regime(
    ewma_vol: float, long_run_vol: float, low: float, high: float, min_scale: float
) -> tuple[float, str, float]:
    """Classify the volatility regime and compute the risk-budget scale.

    ratio = recent (EWMA) volatility / long-run volatility of the same market proxy.
    label = "high" above `high`, "low" below `low`, otherwise "normal".
    scale = min(1, high / ratio), floored at min_scale. This is the volatility-targeting rule
    min(1, sigma_target / sigma_hat) with sigma_target = high * long_run_vol, so there is a
    dead band: nothing changes until recent volatility exceeds `high` times its long-run level.
    The scale never exceeds 1, so it can only tighten the risk budget (no leverage).
    """
    ratio = ewma_vol / long_run_vol if long_run_vol > 0 else 1.0
    label = "high" if ratio > high else "low" if ratio < low else "normal"
    scale = min(1.0, high / ratio) if ratio > 0 else 1.0
    return float(ratio), label, float(max(min_scale, scale))


# ==========================================================================
# 3. Portfolio risk metrics
# ==========================================================================
def portfolio_volatility(w: np.ndarray, cov: np.ndarray) -> float:
    """sigma_p = sqrt(w' Sigma w)."""
    return float(np.sqrt(max(w @ cov @ w, 0.0)))


def risk_contributions(w: np.ndarray, cov: np.ndarray) -> np.ndarray:
    """Share of portfolio variance caused by each asset: w_i (Sigma w)_i / (w' Sigma w). Sums to 1."""
    variance = float(w @ cov @ w)
    if variance <= 0:
        return np.zeros_like(w)
    return w * (cov @ w) / variance


def diversification_ratio(w: np.ndarray, cov: np.ndarray) -> float:
    """(sum_i w_i sigma_i) / sigma_p. Equals 1 for a single asset; higher means more diversification."""
    sigma_p = portfolio_volatility(w, cov)
    if sigma_p <= 0:
        return 1.0
    return float(w @ np.sqrt(np.diag(cov)) / sigma_p)


def effective_number_of_holdings(w: np.ndarray) -> float:
    """1 / sum(w_i^2): how many equal-sized positions the portfolio behaves like."""
    return float(1.0 / np.sum(w**2))


def historical_var_cvar(portfolio_returns: np.ndarray, confidence: float) -> tuple[float, float, int]:
    """1-period historical VaR and expected shortfall (positive losses) plus the tail sample size.

    The tail size matters: with 252 daily returns and 95% confidence only about 13 points sit in
    the tail, so CVaR is a noisy estimate. We report the count instead of hiding it.
    """
    cutoff = np.quantile(portfolio_returns, 1.0 - confidence)
    tail = portfolio_returns[portfolio_returns <= cutoff]
    return float(-cutoff), float(-tail.mean()), int(len(tail))


def max_drawdown(portfolio_returns: np.ndarray) -> float:
    """Largest peak-to-trough fall of the constant-weight path (positive number)."""
    wealth = np.concatenate([[1.0], np.cumprod(1.0 + portfolio_returns)])
    running_peak = np.maximum.accumulate(wealth)
    return float(-(wealth / running_peak - 1.0).min())


# ==========================================================================
# 4. Stress tests (deterministic, hypothetical)
# ==========================================================================
def scenario_shock_vector(
    asset_classes: list[str],
    durations: list[float | None],
    convexities: list[float | None],
    class_shocks: dict[str, float],
    rate_shock_bp: float,
) -> np.ndarray:
    """Return of each asset under a scenario.

    shock_i = class shock of asset i  +  rate effect (bonds only)
    rate effect = -duration * dy + 0.5 * convexity * dy^2,   dy = rate move as a fraction
    """
    dy = rate_shock_bp / 10_000.0
    shocks = np.zeros(len(asset_classes))
    for i, (asset_class, duration, convexity) in enumerate(zip(asset_classes, durations, convexities)):
        shocks[i] = class_shocks.get(asset_class, 0.0)
        if asset_class == "bond" and duration is not None:
            shocks[i] += -duration * dy + 0.5 * (convexity or 0.0) * dy**2
    return shocks


def scenario_portfolio_return(w: np.ndarray, shocks: np.ndarray) -> float:
    """R_p = sum_i w_i R_i  (a hypothetical outcome, not a forecast)."""
    return float(w @ shocks)


def stressed_covariance(cov: np.ndarray, risky_mask: np.ndarray, floor_correlation: float) -> np.ndarray:
    """Covariance with every correlation between risky assets raised to at least `floor_correlation`.

    Volatilities are unchanged. Cash-like assets keep their original correlations. This mimics
    the common crisis pattern where diversification fades. It is a hypothetical test, not a forecast.
    """
    vol = np.sqrt(np.diag(cov))
    corr = cov / np.outer(vol, vol)
    stressed = corr.copy()
    risky = np.flatnonzero(risky_mask)
    for i in risky:
        for j in risky:
            if i != j:
                stressed[i, j] = max(corr[i, j], floor_correlation)
    return ensure_positive_definite(np.outer(vol, vol) * stressed)


def equity_drop_to_reach_loss(w: np.ndarray, equity_mask: np.ndarray, loss_limit: float) -> float | None:
    """Reverse stress test: how far must equities fall (everything else flat) to lose `loss_limit`?

    drop = loss_limit / equity_weight. Returns None when the portfolio has no equity, or when even
    a 100% equity wipe-out would not reach the limit.
    """
    equity_weight = float(w @ equity_mask)
    if equity_weight <= 1e-12:
        return None
    drop = loss_limit / equity_weight
    return float(drop) if drop <= 1.0 else None


# ==========================================================================
# 5. Baseline weights and the legacy volatility-targeting rule
# ==========================================================================
def build_baseline_weights(
    asset_classes: list[str],
    excluded: list[bool],
    class_mix: dict[str, float],
    equity_cap: float,
) -> np.ndarray:
    """Fixed-weight reference: class mix split equally across allowed assets in each class."""
    mix = dict(class_mix)
    if mix.get("equity", 0.0) > equity_cap:  # move any equity above the cap into bonds
        mix["bond"] = mix.get("bond", 0.0) + mix["equity"] - equity_cap
        mix["equity"] = equity_cap

    weights = np.zeros(len(asset_classes))
    for cls, share in mix.items():
        members = [i for i, c in enumerate(asset_classes) if c == cls and not excluded[i]]
        for i in members:
            weights[i] = share / len(members)

    total = weights.sum()
    if total <= 0:
        raise ValueError("baseline portfolio is empty: no allowed assets in the requested classes")
    return weights / total


def apply_volatility_targeting(
    weights: np.ndarray,
    estimated_vol: float,
    target_vol: float,
    cash_mask: np.ndarray,
) -> tuple[np.ndarray, float]:
    """Legacy post-hoc rule: w_vol = w_base * min(1, sigma_target / sigma_hat); freed weight to cash.

    Kept as the SIMPLE comparison strategy in the backtest. Agent A itself no longer uses it,
    because scaling after optimization can break other constraints (for example the per-asset cap on
    cash). Agent A puts the volatility limit inside the optimizer instead.
    The scale is capped at 1, so the rule can only de-risk. It does NOT guarantee drawdown protection.
    """
    scale = 1.0 if estimated_vol <= 0 else min(1.0, target_vol / estimated_vol)
    scaled = weights.copy()
    scaled[~cash_mask] *= scale
    freed = weights.sum() - scaled.sum()
    scaled[cash_mask] += freed / cash_mask.sum()
    return scaled, scale


# ==========================================================================
# 6. Constrained optimizer (with optional turnover-aware trading)
# ==========================================================================
@dataclass
class SolveResult:
    weights: np.ndarray | None          # None when infeasible
    status: str                         # "optimal" or "infeasible"
    binding: list[str]                  # labels of constraints that limit the solution
    message: str = ""


def solve_portfolio(
    cov: np.ndarray,
    asset_ids: list[str],
    upper_bounds: np.ndarray,                           # per-asset max weight (0 = excluded)
    bound_labels: list[str],                            # origin of each upper bound
    linear_rows: list[tuple[str, np.ndarray, float]],   # (label, a, b) meaning  a . w <= b
    vol_limits: list[tuple[str, float]],                # (label, max annual volatility)
    equity_mask: np.ndarray,
    equity_target: float | None = None,                 # if set: equity weight must equal it
    expected_returns: np.ndarray | None = None,         # if set: mean-variance, else min variance
    risk_aversion: float = 5.0,
    current_weights: np.ndarray | None = None,          # enables turnover terms when given
    trade_cost_rates: np.ndarray | None = None,         # one-way cost per unit traded (fraction)
    turnover_penalty: float = 0.0,                      # weight of trading cost in the objective
    turnover_cap: tuple[str, float] | None = None,      # (label, max one-way turnover)
    ridge: float = 0.0,                                 # diversification penalty: + ridge * sum(w_i^2)
    target_weights: np.ndarray | None = None,           # if set: track these weights instead of min variance
) -> SolveResult:
    """Long-only, fully invested optimizer.

    Objective
      min variance:      min  w' Sigma w  (+ ridge * w'w, which spreads weight over similar assets)
      mean-variance:     min  0.5 * lambda * w' Sigma w  -  mu' w  (+ ridge * w'w)
      target tracking:   min  |w - target|^2   (nearest feasible portfolio to a target mix)
      plus (optional)    turnover_penalty * sum_i cost_i * |w_i - current_i|
    Always subject to  sum(w) = 1,  0 <= w_i <= upper_i,  plus linear rows, volatility limits and
    an optional one-way turnover cap 0.5 * sum |w_i - current_i| <= cap.

    The absolute values are made smooth with the standard split  w - current = u - v,  u, v >= 0:
    the optimizer works on x = [w, u, v] and |w_i - current_i| becomes u_i + v_i.

    Two steps: (1) a linear program finds a feasible start (and the smallest-turnover one when
    turnover matters) or proves infeasibility, (2) SLSQP optimizes from that start.
    """
    n = len(asset_ids)
    use_turnover = current_weights is not None and (turnover_penalty > 0 or turnover_cap is not None)
    n_x = 3 * n if use_turnover else n

    def pad(row: np.ndarray) -> np.ndarray:
        """Extend a row defined on w to the full variable vector x."""
        out = np.zeros(n_x)
        out[:n] = row
        return out

    bounds = [(0.0, float(u)) for u in upper_bounds] + ([(0.0, 1.0)] * (2 * n) if use_turnover else [])

    # Equality rows: fully invested, optional fixed equity weight, and the u/v bookkeeping.
    eq_rows = [pad(np.ones(n))]
    eq_rhs = [1.0]
    if equity_target is not None:
        eq_rows.append(pad(equity_mask.astype(float)))
        eq_rhs.append(float(equity_target))
    if use_turnover:
        for i in range(n):
            row = np.zeros(n_x)
            row[i], row[n + i], row[2 * n + i] = 1.0, -1.0, 1.0        # w_i - u_i + v_i = current_i
            eq_rows.append(row)
            eq_rhs.append(float(current_weights[i]))
    a_eq, b_eq = np.array(eq_rows), np.array(eq_rhs)

    # Inequality rows  a . x <= b.
    ub_rows = [pad(row) for _, row, _ in linear_rows]
    ub_rhs = [limit for _, _, limit in linear_rows]
    if use_turnover and turnover_cap is not None:
        row = np.zeros(n_x)
        row[n:] = 0.5                                                   # 0.5 * sum(u + v) <= cap
        ub_rows.append(row)
        ub_rhs.append(float(turnover_cap[1]))
    a_ub = np.array(ub_rows) if ub_rows else None
    b_ub = np.array(ub_rhs) if ub_rows else None

    # Step 1: feasibility of the linear part (minimizing turnover when it is in play).
    lp_cost = np.zeros(n_x)
    if use_turnover:
        lp_cost[n:] = 1.0
    phase_one = linprog(lp_cost, A_ub=a_ub, b_ub=b_ub, A_eq=a_eq, b_eq=b_eq, bounds=bounds, method="highs")
    if phase_one.status != 0:
        return SolveResult(None, "infeasible", [], "linear constraints cannot all be satisfied")
    start = phase_one.x

    # Step 2: smooth optimization.
    cost_vec = np.zeros(n_x)
    if use_turnover:
        if turnover_penalty > 0 and trade_cost_rates is not None:
            cost_vec[n : 2 * n] = turnover_penalty * trade_cost_rates
            cost_vec[2 * n :] = turnover_penalty * trade_cost_rates
        cost_vec[n:] += 1e-8     # tiny tie-breaker so u and v are never both positive for no reason

    if target_weights is not None:
        target = np.asarray(target_weights, dtype=float)

        def objective(x: np.ndarray) -> float:
            d = x[:n] - target
            return float(d @ d + cost_vec @ x)

        def gradient(x: np.ndarray) -> np.ndarray:
            return np.concatenate([2.0 * (x[:n] - target), np.zeros(n_x - n)]) + cost_vec
    elif expected_returns is None:
        def objective(x: np.ndarray) -> float:
            return float(x[:n] @ cov @ x[:n] + ridge * (x[:n] @ x[:n]) + cost_vec @ x)

        def gradient(x: np.ndarray) -> np.ndarray:
            return np.concatenate([2.0 * cov @ x[:n] + 2.0 * ridge * x[:n], np.zeros(n_x - n)]) + cost_vec
    else:
        def objective(x: np.ndarray) -> float:
            return float(0.5 * risk_aversion * (x[:n] @ cov @ x[:n]) - expected_returns @ x[:n]
                         + ridge * (x[:n] @ x[:n]) + cost_vec @ x)

        def gradient(x: np.ndarray) -> np.ndarray:
            return np.concatenate([risk_aversion * (cov @ x[:n]) - expected_returns + 2.0 * ridge * x[:n],
                                   np.zeros(n_x - n)]) + cost_vec

    constraints = [{"type": "eq", "fun": lambda x: a_eq @ x - b_eq, "jac": lambda x: a_eq}]
    if a_ub is not None:
        constraints.append({"type": "ineq", "fun": lambda x: b_ub - a_ub @ x, "jac": lambda x: -a_ub})
    for _, max_vol in vol_limits:
        constraints.append({
            "type": "ineq",
            "fun": lambda x, v=max_vol: v**2 - x[:n] @ cov @ x[:n],
            "jac": lambda x: np.concatenate([-2.0 * cov @ x[:n], np.zeros(n_x - n)]),
        })

    solution = minimize(objective, start, jac=gradient, bounds=bounds, constraints=constraints,
                        method="SLSQP", options={"ftol": 1e-14, "maxiter": 500})
    if solution.status != 0:
        # SLSQP can stop at its first step with "Singular matrix C in LSQ subproblem" when the equality
        # rows and the active bounds are degenerate (e.g. near-identical funds). The same problem with each
        # equality written as two opposite inequalities (slack far below TOLERANCE) avoids that subproblem.
        split = []
        for c in constraints:
            if c["type"] == "eq":
                split.append({"type": "ineq", "fun": lambda x, f=c["fun"]: EQ_SLACK - f(x),
                              "jac": lambda x, j=c["jac"]: -j(x)})
                split.append({"type": "ineq", "fun": lambda x, f=c["fun"]: f(x) + EQ_SLACK, "jac": c["jac"]})
            else:
                split.append(c)
        solution = minimize(objective, start, jac=gradient, bounds=bounds, constraints=split,
                            method="SLSQP", options={"ftol": 1e-14, "maxiter": 500})
    weights = np.clip(solution.x[:n], 0.0, upper_bounds)
    weights[weights < 1e-8] = 0.0

    # Never trust the optimizer blindly: re-check every constraint ourselves on the final weights.
    violations = [
        abs(float(weights.sum()) - 1.0),
        max((float(row @ weights) - limit for _, row, limit in linear_rows), default=0.0),
        max((portfolio_volatility(weights, cov) - v for _, v in vol_limits), default=0.0),
    ]
    if equity_target is not None:
        violations.append(abs(float(equity_mask @ weights) - equity_target))
    if current_weights is not None and turnover_cap is not None:
        violations.append(turnover(weights, current_weights) - turnover_cap[1])
    worst_violation = max(violations)
    if worst_violation > TOLERANCE:
        return SolveResult(None, "infeasible", [], f"solver ended with violation {worst_violation:.2e}")

    # Which limits are actually touching?
    binding: list[str] = []
    for label, row, limit in linear_rows:
        if limit - float(row @ weights) <= TOLERANCE:
            binding.append(label)
    for label, max_vol in vol_limits:
        if max_vol - portfolio_volatility(weights, cov) <= TOLERANCE:
            binding.append(label)
    if current_weights is not None and turnover_cap is not None:
        if turnover_cap[1] - turnover(weights, current_weights) <= TOLERANCE:
            binding.append(turnover_cap[0])
    for i, upper in enumerate(upper_bounds):
        if 1e-12 < upper < 0.999 and weights[i] >= upper - TOLERANCE:
            binding.append(f"{bound_labels[i]}[{asset_ids[i]}]")

    return SolveResult(weights, "optimal", binding)


# ==========================================================================
# 7. Implementation: turnover and costs
# ==========================================================================
def turnover(new_weights: np.ndarray, current_weights: np.ndarray) -> float:
    """One-way turnover: 0.5 * sum |new - current|."""
    return float(0.5 * np.abs(new_weights - current_weights).sum())


def transaction_cost(new_weights: np.ndarray, current_weights: np.ndarray, cost_bps: np.ndarray) -> float:
    """Total trading cost as a fraction of portfolio value: sum |dw_i| * bps_i / 10,000."""
    return float(np.sum(np.abs(new_weights - current_weights) * cost_bps) / 10_000.0)
