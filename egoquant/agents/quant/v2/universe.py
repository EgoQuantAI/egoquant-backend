"""Portfolio method v2, stage 1: screen the eligible universe down to representatives and find groups of near-duplicates.

Agent C sends every eligible product (up to the full catalog). One optimization over ~300 near-collinear funds is slow and
unstable, and a minimum-variance optimizer cannot tell VTI from VT from VIG, so it keeps only the cheapest or calmest.
Here the universe is clustered by return correlation (inside each asset class, so a class is never lost), one
representative per cluster is kept (lowest fee, longest history, most liquid, then ticker), and the funds that remain
near-duplicates of each other are grouped so that a group cap can limit them together.

Pure and deterministic: no randomness, no I/O. docs/PORTFOLIO_V2.md sections 3 and 7.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

from egoquant.models import AssetInfo

MAX_REPRESENTATIVES = 80          # PROVISIONAL: the work budget of the optimizer (one solve ~0.1 s at 80 assets, 1.5 s at 292)
MIN_HISTORY_DAYS = 200            # PROVISIONAL: the model's lookback is about 504 days; shorter histories are reported
GROUP_DISTANCE = 0.16             # PROVISIONAL: sqrt(0.5 * (1 - rho)) <= 0.16  <=>  rho >= about 0.95 = "near duplicates"


@dataclass
class V2State:
    """What the v2 stages share for one run; attached to the MarketModel as `model.v2`."""

    n_universe: int
    kept: list[str]
    cluster_of: dict[str, int]                      # every screened asset -> its screening cluster
    group_of: dict[str, int] = field(default_factory=dict)   # kept asset -> near-duplicate group id (among the kept)
    groups: list[list[str]] = field(default_factory=list)
    dropped_short_history: list[str] = field(default_factory=list)
    screening: str = "screening.py"                 # which implementation ran (the fallback is named)

    def group_masks(self, asset_ids: list[str]) -> list[np.ndarray]:
        """One boolean mask per near-duplicate group with at least two members."""
        index = {a: i for i, a in enumerate(asset_ids)}
        masks = []
        for members in self.groups:
            if len(members) < 2:
                continue
            m = np.zeros(len(asset_ids), dtype=bool)
            for a in members:
                if a in index:
                    m[index[a]] = True
            if m.sum() >= 2:
                masks.append(m)
        return masks


def _distance(returns: pd.DataFrame) -> np.ndarray:
    corr = np.corrcoef(returns.to_numpy(dtype=float), rowvar=False)
    corr = np.nan_to_num(corr, nan=0.0)
    np.fill_diagonal(corr, 1.0)
    d = np.sqrt(np.clip(0.5 * (1.0 - corr), 0.0, None))
    return (d + d.T) / 2.0


def _fallback_screen(returns: pd.DataFrame, assets: list[AssetInfo], forced: set[str] | None,
                     max_representatives: int, min_history_days: int):
    """Same contract as v2/screening.py (Damla). Used only until that module exists."""
    forced = forced or set()
    ids = list(returns.columns)
    info = {a.asset_id: a for a in assets}
    n_obs = {a: int(returns[a].notna().sum()) for a in ids}
    short = [a for a in ids if n_obs[a] < min_history_days and a not in forced]
    usable = [a for a in ids if a not in short]
    if len(usable) <= 1:
        return usable, {a: 0 for a in usable}, [usable] if usable else [], short
    k = max(1, min(max_representatives, len(usable)))
    z = linkage(squareform(_distance(returns[usable]), checks=False), method="average")
    labels = fcluster(z, t=k, criterion="maxclust") - 1
    clusters: dict[int, list[str]] = {}
    for a, c in zip(usable, labels):
        clusters.setdefault(int(c), []).append(a)

    def key(a: str):
        x = info.get(a)
        return (x.expense_ratio if x else 0.0, -n_obs[a], x.liquidity_tier if x else 1, a)

    kept: set[str] = set()
    groups: list[list[str]] = []
    for c in sorted(clusters):
        members = sorted(clusters[c], key=key)
        rep = [m for m in members if m in forced] or members[:1]
        kept.update(rep)
        groups.append(rep + [m for m in members if m not in rep])
    cluster_of = {a: i for i, g in enumerate(groups) for a in g}
    return [a for a in ids if a in kept], cluster_of, groups, short


def _screen_class(returns: pd.DataFrame, assets: list[AssetInfo], forced: set[str], budget: int):
    try:
        from egoquant.agents.quant.v2.screening import screen_universe        # Damla's module (docs section 7)
    except ImportError:
        kept, cluster_of, _, short = _fallback_screen(returns, assets, forced, budget, MIN_HISTORY_DAYS)
        return kept, cluster_of, short, "fallback"
    result = screen_universe(returns, assets, forced=forced, max_representatives=budget,
                             min_history_days=MIN_HISTORY_DAYS)
    return list(result.kept), dict(result.cluster_of), list(result.dropped_short_history), "screening.py"


def screen_assets(returns: pd.DataFrame, assets: list[AssetInfo], forced: set[str],
                  max_representatives: int = MAX_REPRESENTATIVES) -> tuple[list[str], dict[str, int], list[str], str]:
    """Representatives per asset class (a class is never lost), budget shared in proportion to the class size."""
    by_class: dict[str, list[AssetInfo]] = {}
    for a in assets:
        by_class.setdefault(a.asset_class, []).append(a)
    total = max(1, len(assets))
    kept: list[str] = []
    cluster_of: dict[str, int] = {}
    short: list[str] = []
    impl = "screening.py"
    offset = 0
    for cls in sorted(by_class):
        members = by_class[cls]
        budget = max(1, int(np.ceil(max_representatives * len(members) / total)))
        ids = [a.asset_id for a in members]
        k, c, s, impl_c = _screen_class(returns[ids], members, forced & set(ids), min(budget, len(ids)))
        kept += k
        short += s
        top = max(c.values(), default=-1)
        cluster_of.update({a: v + offset for a, v in c.items()})
        offset += top + 1
        impl = impl_c if impl_c == "fallback" else impl
    order = {a.asset_id: i for i, a in enumerate(assets)}
    return sorted(set(kept), key=order.get), cluster_of, sorted(short, key=order.get), impl


def near_duplicate_groups(returns: pd.DataFrame, asset_classes: dict[str, str]) -> list[list[str]]:
    """Funds of one class whose returns are almost the same (rho >= ~0.95): average-linkage groups cut at GROUP_DISTANCE."""
    ids = list(returns.columns)
    groups: list[list[str]] = []
    for cls in sorted(set(asset_classes.values())):
        members = [a for a in ids if asset_classes[a] == cls]
        if len(members) < 2:
            continue
        z = linkage(squareform(_distance(returns[members]), checks=False), method="average")
        labels = fcluster(z, t=GROUP_DISTANCE, criterion="distance")
        by: dict[int, list[str]] = {}
        for a, g in zip(members, labels):
            by.setdefault(int(g), []).append(a)
        groups += [by[g] for g in sorted(by) if len(by[g]) >= 2]
    return groups


def reduce_model(model, keep: list[str], state: V2State):
    """The same MarketModel restricted to `keep` (original order). Pairwise quantities are sliced, the rest recomputed."""
    from egoquant.agents.quant import core as qc
    index = [i for i, a in enumerate(model.asset_ids) if a in set(keep)]
    ids = [model.asset_ids[i] for i in index]
    sub = np.ix_(index, index)
    cov = model.cov_annual[sub]
    reduced = dataclasses.replace(
        model,
        asset_ids=ids,
        assets=[model.assets[i] for i in index],
        asset_classes=[model.asset_classes[i] for i in index],
        equity_mask=model.equity_mask[index],
        cash_mask=model.cash_mask[index],
        returns_all=model.returns_all[ids],
        returns_window=model.returns_window[ids],
        cov_annual=cov,
        cov_long_run=model.cov_long_run[sub],
        cov_stressed=model.cov_stressed[sub],
        cond_number=qc.condition_number(cov),
        scenario_shocks={name: v[index] for name, v in model.scenario_shocks.items()},
        data_hash=model.data_hash + "|v2:" + ",".join(ids),
    )
    reduced.v2 = state                                           # a dynamic attribute: read with getattr(model, "v2", None)
    return reduced


def screen_model(agent, model, context):
    """Hook H1 (prepare_market_model): under v2 the model is replaced by its screened version."""
    excluded = set(context.excluded_assets)
    forced = (set(context.interest_assets) | set(context.current_holdings or {})) - excluded
    candidates = [a for a in model.assets if a.asset_id not in excluded]
    kept, cluster_of, short, impl = screen_assets(model.returns_all, candidates, forced)
    kept_set = set(kept) | {a for a in forced if a in set(model.asset_ids)}
    keep = [a for a in model.asset_ids if a in kept_set]
    classes = dict(zip(model.asset_ids, model.asset_classes))
    groups = near_duplicate_groups(model.returns_all[keep], {a: classes[a] for a in keep})
    state = V2State(n_universe=len(model.asset_ids), kept=keep, cluster_of=cluster_of,
                    group_of={a: i for i, g in enumerate(groups) for a in g}, groups=groups,
                    dropped_short_history=short, screening=impl)
    return reduce_model(model, keep, state)
