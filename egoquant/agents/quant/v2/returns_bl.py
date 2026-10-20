"""Expected returns for portfolio method v2: Black-Litterman (docs/PORTFOLIO_V2.md, sections 3 and 7).

    reference_weights(...)          the neutral allocation the prior is read off from: the class's baseline mix, spread
                                    over the class's clusters
    black_litterman_returns(...)    annual expected returns per asset, NET of each fund's expense ratio

Pure and deterministic: numpy only, no I/O, no randomness, no LLM. Annual returns are ASSUMPTIONS, never historical means
and never forecasts.

The method (Black and Litterman 1992, He and Litterman 1999):
  prior      pi = delta * Sigma * w_ref   (reverse optimisation of the reference allocation), then shifted per asset class
             so the class's reference-weighted average is the explicit class assumption, when one is given
  views      P: one row per view, equal weights over the assets it applies to (the single matching rule is
             egoquant.preferences.views.assets_for_view); q = P pi + annual_excess
  posterior  mu = pi + tau Sigma P' (P tau Sigma P' + Omega)^-1 (q - P pi),  Omega = diag(P tau Sigma P') * (1/confidence - 1)
             so confidence 1 means the view is met exactly and confidence 0.30 moves it 30% of the way
  guard      an asset that no view names never moves by more than the largest move a view asks of its own assets
             (confidence x excess): without it a view on a near-riskless asset moves volatile funds through their
             covariance by more than the view itself (seen on real prices: +0.5% on cash took 0.54% off an energy fund)
  net        mu - expense_ratio, per asset

Choices where the contract was open (the simplest reading; Onur may overrule):
  * `class_assumptions` is keyed by asset class ("equity", "bond", ...). A key that is an asset id sets that asset's own
    assumption (AgentConfig.expected_returns is keyed by asset id). Assumptions are GROSS of fees; the fee is subtracted last.
  * A covariance that is not positive definite is repaired by lifting its negative or tiny eigenvalues to a floor.
  * A view that applies to no asset is skipped. Singular systems (two views of confidence 1 on the same assets) are
    solved in the least-squares sense instead of failing.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from egoquant.models import AssetInfo, PortfolioView
from egoquant.preferences.views import assets_for_view

DEFAULT_DELTA = 3.0         # risk aversion of the market-implied prior (PROVISIONAL)
DEFAULT_TAU = 0.05          # uncertainty of the prior (PROVISIONAL)
_EIGEN_FLOOR = 1e-10


# ==========================================================================
# The reference allocation
# ==========================================================================
def reference_weights(asset_ids: list[str], asset_classes: list[str], class_mix: dict[str, float],
                      cluster_of: dict[str, int] | None = None) -> np.ndarray:
    """The neutral allocation: each class gets its share of `class_mix`, split equally over the class's clusters, and
    equally over the members of a cluster. Weights are non-negative and sum to 1.

    * Classes of `class_mix` that no asset belongs to are dropped and the rest is scaled back to 1.
    * `cluster_of` maps asset id -> cluster id; an asset it does not name is its own cluster. None = every asset is.
    * No share left (no class of the mix is present, or all shares are zero) = equal weights over every asset.
    * An empty universe gives an empty array.
    """
    n = len(asset_ids)
    if len(asset_classes) != n:
        raise ValueError("asset_ids and asset_classes must have the same length")
    if len(set(asset_ids)) != n:
        raise ValueError("asset_ids must be unique")
    for name, share in class_mix.items():
        if not np.isfinite(share) or share < 0:
            raise ValueError(f"class_mix[{name!r}] must be a finite, non-negative share")
    if n == 0:
        return np.zeros(0)

    present = {c for c in asset_classes}
    shares = {c: float(class_mix[c]) for c in sorted(present) if class_mix.get(c, 0.0) > 0.0}
    total = sum(shares.values())
    if total <= 0.0:
        return np.full(n, 1.0 / n)

    weights = np.zeros(n)
    for asset_class, share in shares.items():
        groups: dict[object, list[int]] = {}
        for i, (asset_id, c) in enumerate(zip(asset_ids, asset_classes)):
            if c != asset_class:
                continue
            key = ("cluster", cluster_of[asset_id]) if cluster_of and asset_id in cluster_of else ("asset", asset_id)
            groups.setdefault(key, []).append(i)
        per_group = share / total / len(groups)
        for members in groups.values():
            weights[members] = per_group / len(members)
    return weights


# ==========================================================================
# Views as matrices
# ==========================================================================
def view_matrix(views: Sequence[PortfolioView], asset_ids: Sequence[str], assets: Sequence[AssetInfo],
                ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(P, annual_excess, confidence) for the views that apply to at least one of `asset_ids`, in the given order.
    Row i of P holds equal weights (summing to 1) over the assets view i applies to."""
    position = {a: i for i, a in enumerate(asset_ids)}
    in_universe = [a for a in assets if a.asset_id in position]
    rows, excess, confidence = [], [], []
    for v in views:
        matched = [position[a.asset_id] for a in assets_for_view(v.kind, v.key, in_universe)]
        if not matched:
            continue
        row = np.zeros(len(asset_ids))
        row[matched] = 1.0 / len(matched)
        rows.append(row)
        excess.append(v.annual_excess)
        confidence.append(v.confidence)
    return np.array(rows).reshape(len(rows), len(asset_ids)), np.array(excess), np.array(confidence)


def _positive_definite(cov: np.ndarray) -> np.ndarray:
    """The symmetric part of `cov`; if its smallest eigenvalue is below the floor, the eigenvalues are lifted to it."""
    cov = (cov + cov.T) / 2.0
    values, vectors = np.linalg.eigh(cov)
    if values[0] >= _EIGEN_FLOOR:
        return cov
    return (vectors * np.maximum(values, _EIGEN_FLOOR)) @ vectors.T


# ==========================================================================
# Black-Litterman
# ==========================================================================
def black_litterman_returns(cov: np.ndarray, asset_ids: list[str], asset_classes: list[str],
                            class_assumptions: dict[str, float] | None, w_ref: np.ndarray,
                            views: list[PortfolioView], assets: list[AssetInfo], delta: float = DEFAULT_DELTA,
                            tau: float = DEFAULT_TAU) -> np.ndarray:
    """Annual expected returns, one per asset in `asset_ids` order, NET of the expense ratio in `assets`.

    cov              annual covariance, rows and columns in `asset_ids` order
    class_assumptions  gross annual assumptions per asset class (or per asset id); None = the prior as it is
    w_ref            reference weights (see `reference_weights`)
    views            see egoquant.preferences.views; with no view the result is the (shifted) prior, net of fees
    """
    ids = list(asset_ids)
    n = len(ids)
    sigma = np.asarray(cov, dtype=float)
    weights = np.asarray(w_ref, dtype=float)
    if len(asset_classes) != n or sigma.shape != (n, n) or weights.shape != (n,):
        raise ValueError("cov, asset_ids, asset_classes and w_ref must describe the same assets")
    if len(set(ids)) != n:
        raise ValueError("asset_ids must be unique")
    if not (delta > 0 and tau > 0):
        raise ValueError("delta and tau must be positive")
    if n == 0:
        return np.zeros(0)
    if not (np.isfinite(sigma).all() and np.isfinite(weights).all()):
        raise ValueError("cov and w_ref must be finite")
    sigma = _positive_definite(sigma)

    # --- prior: reverse optimisation of the reference allocation, anchored to the class assumptions ----------------
    pi = delta * sigma @ weights
    pi = _anchor(pi, ids, list(asset_classes), weights, class_assumptions or {})

    # --- views: He-Litterman posterior -----------------------------------------------------------------------------
    P, excess, confidence = view_matrix(views, ids, assets)
    mu = pi
    if len(excess):
        tau_sigma = tau * sigma
        own_variance = np.einsum("ij,jk,ik->i", P, tau_sigma, P)
        omega = own_variance * (1.0 / confidence - 1.0)
        system = P @ tau_sigma @ P.T + np.diag(omega)
        # q - P pi is the annual excess by construction, so it is not recomputed from rounded numbers.
        weights_on_views = np.linalg.lstsq(system, excess, rcond=None)[0]
        move = tau_sigma @ P.T @ weights_on_views
        # Guard (not in the textbook): an asset that no view names moves through its covariance with the views' assets
        # only, and a view on a very quiet asset (cash) can then move a volatile one by more than the view itself.
        # Such a spill-over never exceeds the largest move any view asks of its own assets, confidence x excess.
        spill = P.sum(axis=0) == 0
        bound = float(np.max(confidence * np.abs(excess)))
        move[spill] = np.clip(move[spill], -bound, bound)
        mu = pi + move

    fees = {a.asset_id: a.expense_ratio for a in assets}
    return mu - np.array([fees.get(a, 0.0) for a in ids])


def _anchor(pi: np.ndarray, ids: list[str], classes: list[str], w_ref: np.ndarray,
            assumptions: dict[str, float]) -> np.ndarray:
    """Shift the prior per asset class so its reference-weighted average is the class assumption; an assumption keyed
    by an asset id sets that asset's own value."""
    pi = pi.copy()
    for asset_class in sorted(set(classes)):
        if asset_class not in assumptions:
            continue
        target = float(assumptions[asset_class])
        if not np.isfinite(target):
            raise ValueError(f"the assumption for {asset_class!r} must be finite")
        members = np.array([i for i, c in enumerate(classes) if c == asset_class])
        mass = w_ref[members].sum()
        average = (w_ref[members] @ pi[members]) / mass if mass > 0 else pi[members].mean()
        pi[members] += target - average
    for i, asset_id in enumerate(ids):
        if asset_id in assumptions:
            if not np.isfinite(assumptions[asset_id]):
                raise ValueError(f"the assumption for {asset_id!r} must be finite")
            pi[i] = float(assumptions[asset_id])
    return pi


__all__ = ["DEFAULT_DELTA", "DEFAULT_TAU", "black_litterman_returns", "reference_weights", "view_matrix"]
