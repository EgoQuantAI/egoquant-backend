"""Portfolio method v2, targets: equal risk contribution, hierarchical risk parity, maximum diversification.

Each function returns a RAW target weight vector over the whole menu (it sums to the sum of the sleeve shares,
1 when the sleeves cover the portfolio). It is not final: Onur's candidates module projects it onto the feasible
set (class caps, volatility and stress limits, group cap) with `solve_portfolio(target_weights=...)`.
The functions are pure and deterministic: numpy and scipy only, no randomness, no I/O, no LLM.

The three share one frame (docs/PORTFOLIO_V2.md, section 7):
  * `sleeves` = [(boolean mask, share), ...] exactly like `target()` in QuantAgent.build_candidates: the stock
    sleeve gets `level`, the rest 1 - level. Sleeves are expected to be disjoint (not checked).
  * Inside a sleeve the method works on the sub-covariance of the sleeve's allowed assets only. An asset is
    allowed when `allowed[i]` is true and `upper[i] > 0`. A sleeve with a share above 0 and no allowed asset
    makes the whole target impossible: None. If no sleeve has a share above 0 there is nothing to build: None.
  * The sleeve's weights are scaled to its share, then the per-asset upper bounds are enforced by a
    deterministic clip-and-redistribute loop (`fit_to_caps`, 50 passes at most): a weight above its bound is
    set to the bound and the excess goes to the other assets of the sleeve in proportion to their method weights.
  * If a sleeve's bounds add up to less than its share, the bounds cannot all hold. Then every asset of the
    sleeve sits at its bound and the rest of the share is spread in proportion to the method weights, so the
    target still sums to 1 and the projection (not this module) decides what is feasible.

Methods (the weights inside one sleeve):
  * ERC, Spinu (2013), "An algorithm for computing risk parity weights": minimise 0.5 y'Sigma y - (1/n) sum ln y_i
    over y > 0 (strictly convex, solved by damped Newton steps), then w = y / sum(y). At the optimum every asset
    contributes the same share of the portfolio variance.
  * HRP, Lopez de Prado (2016), "Building diversified portfolios that outperform out of sample", Journal of
    Portfolio Management 42: correlation distance sqrt(0.5 (1 - rho)), single-linkage tree, quasi-diagonal order
    (the tree's leaf order, the calmer child first at every merge so that the input order cannot matter),
    recursive bisection where each half gets weight in inverse proportion to its variance (inside a half,
    inverse-variance weights). Exact ties between distances (for example a diagonal covariance with equal
    variances) leave the tree to the input order; real covariances do not tie. Assets whose covariance rows are identical are one and the same
    exposure: they enter the tree once and share their weight equally, so identical assets get equal weights
    whatever their number and order (plain bisection would give 1/2, 1/4, 1/4 to three copies).
  * Maximum diversification, Choueifaty and Coignard (2008), "Toward maximum diversification", Journal of
    Portfolio Management 35: maximise (sigma'w) / sqrt(w'Sigma w). Written as min y'Sigma y subject to
    sigma'y = 1, y >= 0, w = y / sum(y). With x_i = sigma_i y_i this is the long-only minimum-variance
    portfolio of the CORRELATION matrix (sum x = 1, x >= 0, w_i proportional to x_i / sigma_i), which is what is
    solved: same optimum, far better conditioned. Identical assets share their weight equally here too.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np
from scipy.cluster.hierarchy import leaves_list, linkage
from scipy.optimize import minimize
from scipy.spatial.distance import squareform

MAX_CAP_ITERATIONS = 50          # clip-and-redistribute passes (contract, section 7)
_EPS = 1e-12                     # a share or a bound at or below this counts as zero
_JITTER = 1e-10                  # relative ridge on the diagonal: a zero-variance asset must not break the maths
_NEWTON_STEPS = 200

Sleeve = tuple[np.ndarray, float]


# ==========================================================================
# 1. Bounds: clip and redistribute
# ==========================================================================
def fit_to_caps(raw: np.ndarray, cap: np.ndarray, total: float, max_iterations: int = MAX_CAP_ITERATIONS) -> np.ndarray:
    """Weights that sum to `total`, follow `raw` (non-negative, sums to 1) and stay at or below `cap`.

    Each pass sets every weight above its bound to the bound and gives the excess to the assets still below
    their bound, in proportion to `raw`. A pass fixes at least one asset, so it ends in at most len(raw) passes;
    if `max_iterations` is not enough, the rest is handed out in proportion to the room left under the bounds
    (which can never exceed a bound). If the bounds add up to less than `total`, see the module docstring.
    """
    raw = np.asarray(raw, dtype=float)
    cap = np.asarray(cap, dtype=float)
    w = total * raw
    if cap.sum() <= total + _EPS:
        return cap + (total - cap.sum()) * raw                       # the bounds cannot hold the share
    free = np.ones(len(w), dtype=bool)
    for _ in range(max_iterations):
        over = free & (w > cap + _EPS)
        if not over.any():
            return np.maximum(w, 0.0)
        w[over] = cap[over]
        free &= ~over
        base = raw[free]
        if base.sum() <= _EPS:
            break
        w[free] = (total - w[~free].sum()) * base / base.sum()
    w = np.minimum(np.maximum(w, 0.0), cap)
    deficit = total - w.sum()
    if deficit > _EPS:
        room = cap - w
        w = w + deficit * room / room.sum()
    return np.maximum(w, 0.0)


# ==========================================================================
# 2. Weights inside one sleeve (sub-covariance in, weights that sum to 1 out)
# ==========================================================================
def _prepare(cov: np.ndarray) -> np.ndarray:
    """Symmetric copy with a negligible ridge, so a zero-variance asset (cash) has a positive variance."""
    cov = (cov + cov.T) / 2.0
    ridge = _JITTER * max(float(np.mean(np.diag(cov))), 1e-12)
    return cov + ridge * np.eye(len(cov))


def _correlation(cov: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    sd = np.sqrt(np.diag(cov))
    return np.clip(cov / np.outer(sd, sd), -1.0, 1.0), sd


def _erc_weights(cov: np.ndarray) -> np.ndarray:
    """Equal risk contribution (Spinu 2013): damped Newton on 0.5 y'Sigma y - (1/n) sum ln y."""
    m = len(cov)
    if m == 1:
        return np.ones(1)
    cov = _prepare(cov)
    corr, sd = _correlation(cov)
    y = 1.0 / (sd * np.sqrt(corr.sum()))                    # the optimum has y'Sigma y = 1; start there

    def objective(v: np.ndarray) -> float:
        return float(0.5 * v @ cov @ v - np.log(v).sum() / m)

    for _ in range(_NEWTON_STEPS):
        sy = cov @ y
        if np.max(np.abs(y * sy - 1.0 / m)) * m < 1e-13:      # risk contributions equal to 1e-13 relative
            break
        grad = sy - 1.0 / (m * y)
        step = np.linalg.solve(cov + np.diag(1.0 / (m * y**2)), -grad)
        t = 1.0
        falling = step < 0
        if falling.any():
            t = min(1.0, 0.9 * float(np.min(-y[falling] / step[falling])))     # stay inside y > 0
        f0, slope = objective(y), float(grad @ step)
        while objective(y + t * step) > f0 + 1e-4 * t * slope and t > 1e-14:
            t *= 0.5
        if t <= 1e-14:
            break
        y = y + t * step
    return y / y.sum()


def _hrp_weights(cov: np.ndarray) -> np.ndarray:
    """Hierarchical risk parity (Lopez de Prado 2016) on a sub-covariance of assets that are all different."""
    m = len(cov)
    if m == 1:
        return np.ones(1)
    cov = _prepare(cov)
    corr, _ = _correlation(cov)
    dist = np.sqrt(0.5 * (1.0 - corr))
    np.fill_diagonal(dist, 0.0)

    def cluster_variance(items: Sequence[int]) -> float:
        sub = cov[np.ix_(items, items)]
        ivp = 1.0 / np.diag(sub)
        ivp /= ivp.sum()
        return float(ivp @ sub @ ivp)

    # Quasi-diagonal order = the leaves of the single-linkage tree. The tree does not say which child comes first,
    # and the bisection below cuts the LIST in halves, so the choice matters: the calmer child (lower variance)
    # always goes first, which makes the order, and so the weights, independent of how the inputs were ordered.
    link = linkage(squareform((dist + dist.T) / 2.0, checks=False), method="single")
    leaves: dict[int, list[int]] = {i: [i] for i in range(m)}
    for k, (a, b, _, _) in enumerate(link):
        one, other = leaves.pop(int(a)), leaves.pop(int(b))
        if (cluster_variance(other), -len(other)) < (cluster_variance(one), -len(one)):
            one, other = other, one
        leaves[m + k] = one + other
    order = np.array(leaves[2 * m - 2])

    w = np.ones(m)
    clusters = [order]
    while clusters:
        halves = []
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            cut = len(cluster) // 2
            left, right = cluster[:cut], cluster[cut:]
            var_left, var_right = cluster_variance(left), cluster_variance(right)
            alpha = 1.0 - var_left / (var_left + var_right)                  # the calmer half gets more
            w[left] *= alpha
            w[right] *= 1.0 - alpha
            halves += [left, right]
        clusters = halves
    return w / w.sum()


def _maxdiv_weights(cov: np.ndarray) -> np.ndarray:
    """Maximum diversification (Choueifaty and Coignard 2008) on a sub-covariance of assets that are all different."""
    m = len(cov)
    if m == 1:
        return np.ones(1)
    cov = _prepare(cov)
    corr, sd = _correlation(cov)
    start = np.full(m, 1.0 / m)                                          # w proportional to 1/sigma: always feasible
    res = minimize(
        lambda x: float(x @ corr @ x), start, jac=lambda x: 2.0 * corr @ x, method="SLSQP",
        bounds=[(0.0, None)] * m, constraints=[{"type": "eq", "fun": lambda x: float(x.sum() - 1.0), "jac": lambda x: np.ones(m)}],
        options={"maxiter": 500, "ftol": 1e-15},
    )
    x = np.maximum(res.x, 0.0) if np.all(np.isfinite(res.x)) and res.x.sum() > 0 else start
    x = x / x.sum()
    if x @ corr @ x > start @ corr @ start:                              # never worse than the starting point
        x = start
    w = x / sd
    return w / w.sum()


def _without_twins(solve: Callable[[np.ndarray], np.ndarray]) -> Callable[[np.ndarray], np.ndarray]:
    """Run `solve` once per distinct asset; assets with identical covariance rows split that weight equally."""

    def run(cov: np.ndarray) -> np.ndarray:
        scale = max(float(np.max(np.abs(cov))), 1e-300)
        keys = np.round(cov / scale, 10) + 0.0                           # + 0.0 turns -0.0 into 0.0
        first: dict[bytes, int] = {}
        owner = np.array([first.setdefault(row.tobytes(), i) for i, row in enumerate(keys)])
        reps = np.unique(owner)
        if len(reps) == len(cov):
            return solve(cov)
        weights = solve(cov[np.ix_(reps, reps)])
        out = np.zeros(len(cov))
        for position, rep in enumerate(reps):
            members = owner == rep
            out[members] = weights[position] / members.sum()
        return out

    return run


# ==========================================================================
# 3. The three targets
# ==========================================================================
def _target(
    cov: np.ndarray, allowed: np.ndarray, sleeves: Sequence[Sleeve], upper: np.ndarray,
    solve: Callable[[np.ndarray], np.ndarray],
) -> np.ndarray | None:
    cov = np.asarray(cov, dtype=float)
    allowed = np.asarray(allowed, dtype=bool)
    upper = np.asarray(upper, dtype=float)
    out = np.zeros(cov.shape[0])
    built = False
    for mask, share in sleeves:
        if share <= _EPS:
            continue
        members = np.flatnonzero(np.asarray(mask, dtype=bool) & allowed & (upper > _EPS))
        if members.size == 0:
            return None
        sub = cov[np.ix_(members, members)]
        if not np.isfinite(sub).all():
            return None
        out[members] += fit_to_caps(solve(sub), upper[members], float(share))
        built = True
    return out if built else None


def erc_target(cov: np.ndarray, allowed: np.ndarray, sleeves: list[Sleeve], upper: np.ndarray) -> np.ndarray | None:
    """Equal risk contribution inside each sleeve (Spinu 2013), scaled to the sleeve's share and kept under `upper`."""
    return _target(cov, allowed, sleeves, upper, _erc_weights)


def hrp_target(cov: np.ndarray, allowed: np.ndarray, sleeves: list[Sleeve], upper: np.ndarray) -> np.ndarray | None:
    """Hierarchical risk parity inside each sleeve (Lopez de Prado 2016), scaled to the share and kept under `upper`."""
    return _target(cov, allowed, sleeves, upper, _without_twins(_hrp_weights))


def max_diversification_target(
    cov: np.ndarray, allowed: np.ndarray, sleeves: list[Sleeve], upper: np.ndarray,
) -> np.ndarray | None:
    """Maximum diversification inside each sleeve (Choueifaty and Coignard 2008), scaled to the share, under `upper`."""
    return _target(cov, allowed, sleeves, upper, _without_twins(_maxdiv_weights))
