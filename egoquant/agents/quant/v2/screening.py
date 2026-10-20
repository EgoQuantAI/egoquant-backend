"""Portfolio method v2, stage 1: universe screening (docs/PORTFOLIO_V2.md sections 3 and 7).

A universe of ~290 products holds many near-copies (VTI, VT, VOO, IVV, SPY ...) that look different to an optimizer but
are one bet. `screen_universe` groups the products by how they move together and keeps ONE representative per group (the
cheapest, then the longest history, then the most liquid, then the ticker), plus every product the investor asked for.

Pure and deterministic: numpy, pandas and scipy only, no randomness, no I/O. Same input, same output, whatever the machine.

    distance        d(i, j) = sqrt(0.5 * (1 - rho_ij)) on the pairwise-complete correlation of daily returns
    clustering      average linkage, cut with fcluster(criterion="maxclust") into at most `max_representatives` clusters
    representative  min by (expense_ratio, -history_days, liquidity_tier, asset_id) inside a cluster
    forced assets   always kept, so a cluster may keep several products

Choices made where the contract leaves room (docs/PORTFOLIO_V2.md section 7):
  * `history_days` of a product = its number of non-missing daily returns in `returns`.
  * A product with fewer than `min_history_days` (and not forced) is NOT clustered: it is listed in `dropped_short_history`
    and is absent from `kept`, `cluster_of` and `clusters`. A forced product with too little history is kept as a cluster
    of its own (its correlations would be unreliable), so `len(kept) <= max_representatives + len(forced)` still holds.
  * Cluster ids are numbered 0..K-1 in order of the first member in the INPUT column order, so they do not depend on the
    internals of the linkage routine.
  * Inside `clusters[k]` the kept members come first (the representative, then forced members in input order), then the rest in
    input order. A forced id that is not a column of `returns` is ignored (there is nothing to keep).
  * Missing metadata for a column counts as expense_ratio 0 and liquidity_tier 1.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

from egoquant.models import AssetInfo

MIN_OVERLAP = 60            # fewest common days for a pairwise correlation to count (else: uncorrelated, distance sqrt(0.5))


@dataclass(frozen=True)
class ScreenResult:
    kept: list[str]                  # representatives and forced assets, in the INPUT column order
    cluster_of: dict[str, int]       # every clustered or forced input asset id -> cluster id 0..K-1
    clusters: list[list[str]]        # members per cluster, kept members first
    dropped_short_history: list[str]


def _pairwise_correlation(x: np.ndarray) -> np.ndarray:
    """Pearson correlation using, for each pair, only the rows where both are present (NaN = missing).

    Vectorised with masked sums, so 300 products x 5,000 days take a few matrix products. Pairs with fewer than
    MIN_OVERLAP common rows, or with no variance on the common rows, get correlation 0."""
    mask = np.isfinite(x)
    m = mask.astype(float)
    v = np.where(mask, x, 0.0)
    n = m.T @ m                                   # common rows per pair
    sx = v.T @ m                                  # sum of x_i over the rows where j is present
    sy = sx.T                                     # sum of x_j over the rows where i is present
    sxy = v.T @ v
    sxx = (v * v).T @ m
    syy = sxx.T
    num = n * sxy - sx * sy
    den = (n * sxx - sx * sx) * (n * syy - sy * sy)
    with np.errstate(divide="ignore", invalid="ignore"):
        rho = num / np.sqrt(den)
    rho[(n < MIN_OVERLAP) | ~np.isfinite(rho) | (den <= 1e-24)] = 0.0
    rho = np.clip((rho + rho.T) / 2.0, -1.0, 1.0)
    np.fill_diagonal(rho, 1.0)
    return rho


def _labels(distance: np.ndarray, k: int) -> np.ndarray:
    """Cluster label per row, at most `k` clusters, numbered 0..K-1 by first appearance in the input order."""
    n = distance.shape[0]
    if n == 1:
        return np.zeros(1, dtype=int)
    tree = linkage(squareform(distance, checks=False), method="average")
    raw = fcluster(tree, t=max(1, min(k, n)), criterion="maxclust")
    order: dict[int, int] = {}
    for label in raw:                              # first appearance in input order -> 0, 1, 2 ...
        order.setdefault(int(label), len(order))
    return np.array([order[int(label)] for label in raw], dtype=int)


def screen_universe(returns: pd.DataFrame, assets: list[AssetInfo], forced: set[str] | None = None,
                    max_representatives: int = 120, min_history_days: int = 750) -> ScreenResult:
    columns = [str(c) for c in returns.columns]
    info = {a.asset_id: a for a in assets}
    column_set = set(columns)
    forced_ids = {a for a in (forced or set()) if a in column_set}
    history = {c: int(returns[col].notna().sum()) for c, col in zip(columns, returns.columns)}

    dropped = [c for c in columns if history[c] < min_history_days and c not in forced_ids]
    lonely = [c for c in columns if history[c] < min_history_days and c in forced_ids]     # forced, too little history
    screened = [c for c in columns if history[c] >= min_history_days]
    screened_set = set(screened)

    cluster_of: dict[str, int] = {}
    members: list[list[str]] = []
    if screened:
        x = returns.loc[:, [col for col, c in zip(returns.columns, columns) if c in screened_set]].to_numpy(dtype=float)
        rho = _pairwise_correlation(x)
        distance = np.sqrt(0.5 * (1.0 - rho))
        np.fill_diagonal(distance, 0.0)
        labels = _labels(distance, max_representatives)
        members = [[] for _ in range(int(labels.max()) + 1)]
        for asset_id, label in zip(screened, labels):
            members[int(label)].append(asset_id)           # screened keeps the input order, so does each member list

    def meta(asset_id: str) -> tuple[float, int, int, str]:
        a = info.get(asset_id)
        return (a.expense_ratio if a else 0.0, -history[asset_id], a.liquidity_tier if a else 1, asset_id)

    kept: set[str] = set(forced_ids)
    clusters: list[list[str]] = []
    for group in members:
        representative = min(group, key=meta)
        kept.add(representative)
        shown = [representative] + [a for a in group if a in forced_ids and a != representative]
        clusters.append(shown + [a for a in group if a not in shown])
    for asset_id in lonely:                                 # a forced product with too little history stands alone
        clusters.append([asset_id])
    for k, group in enumerate(clusters):
        for asset_id in group:
            cluster_of[asset_id] = k

    return ScreenResult(kept=[c for c in columns if c in kept], cluster_of=cluster_of, clusters=clusters,
                        dropped_short_history=dropped)


__all__ = ["MIN_OVERLAP", "ScreenResult", "screen_universe"]
