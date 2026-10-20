"""SYNTHETIC market history for Agent A's unit tests, the backtest demo and the offline stand-in agent.

EVERYTHING produced here is SYNTHETIC. The mean, volatility and correlation parameters are invented
for testing and say nothing about real markets. It differs from Agent C's SyntheticPriceSource on
purpose: it uses its own small universe (EQ_GLOBAL, BOND_GOVT, ...) and can inject a volatility
crisis, which is what the risk-budget and backtest tests need. Real prices come from Agent C.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from egoquant.models import AssetClass, AssetInfo

# Invented universe: (asset_id, class, annual mean, annual vol, duration, convexity, trading cost bps)
SYNTHETIC_UNIVERSE: list[tuple[str, AssetClass, float, float, float | None, float | None, float]] = [
    ("EQ_GLOBAL",   "equity", 0.07, 0.16,  None, None, 3.0),
    ("EQ_EMERGING", "equity", 0.08, 0.22,  None, None, 8.0),
    ("EQ_VALUE",    "equity", 0.07, 0.15,  None, None, 4.0),
    ("BOND_GOVT",   "bond",   0.03, 0.06,  7.0,  60.0, 2.0),
    ("BOND_CORP",   "bond",   0.04, 0.07,  6.0,  45.0, 5.0),
    ("CASH_MM",     "cash",   0.03, 0.005, None, None, 1.0),
]


def synthetic_asset_infos(asset_ids: list[str] | None = None) -> list[AssetInfo]:
    """Metadata for the synthetic universe (optionally a subset)."""
    return [
        AssetInfo(asset_id=a, asset_class=c, duration_years=d, convexity=cx, trading_cost_bps=cost)
        for a, c, _, _, d, cx, cost in SYNTHETIC_UNIVERSE
        if asset_ids is None or a in asset_ids
    ]


def _correlation(classes: list[str], equity_rho: float, bond_rho: float, cross_rho: float) -> np.ndarray:
    """Block correlation: within equity, within bonds, equity-bond; cash is uncorrelated."""
    n = len(classes)
    corr = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            if "cash" in (classes[i], classes[j]):
                rho = 0.0
            elif classes[i] == classes[j] == "equity":
                rho = equity_rho
            elif classes[i] == classes[j] == "bond":
                rho = bond_rho
            else:
                rho = cross_rho
            corr[i, j] = corr[j, i] = rho
    return corr


def make_synthetic_history(
    seed: int = 7,
    n_days: int = 505,
    end_date: date = date(2026, 9, 30),
    asset_ids: list[str] | None = None,
    crisis_start: int | None = None,
    crisis_len: int = 60,
    crisis_vol_multiplier: float = 2.5,
    crisis_equity_drift: float = -0.60,
) -> pd.DataFrame:
    """SYNTHETIC adjusted prices on business days ending at `end_date` (row 0 is 100).

    Calm regime: block correlations (equity 0.8, bonds 0.6, equity-bond 0.2).
    Optional crisis window [crisis_start, crisis_start + crisis_len): volatilities are multiplied,
    equity gets a strongly negative annualized drift, and correlations rise (0.95 / 0.75 / 0.5).
    The same seed always gives the same path.
    """
    universe = [row for row in SYNTHETIC_UNIVERSE if asset_ids is None or row[0] in asset_ids]
    classes = [row[1] for row in universe]
    annual_mean = np.array([row[2] for row in universe])
    annual_vol = np.array([row[3] for row in universe])

    cov_calm = _correlation(classes, 0.8, 0.6, 0.2) * np.outer(annual_vol, annual_vol) / 252.0
    rng = np.random.default_rng(seed)
    daily = rng.multivariate_normal(annual_mean / 252.0, cov_calm, size=n_days)

    if crisis_start is not None:
        end = min(crisis_start + crisis_len, n_days)
        stressed_vol = annual_vol * crisis_vol_multiplier
        cov_crisis = _correlation(classes, 0.95, 0.75, 0.5) * np.outer(stressed_vol, stressed_vol) / 252.0
        crisis_mean = np.where(np.array(classes) == "equity", crisis_equity_drift / 252.0, 0.0)
        daily[crisis_start:end] = rng.multivariate_normal(crisis_mean, cov_crisis, size=end - crisis_start)

    dates = pd.bdate_range(end=pd.Timestamp(end_date), periods=n_days)
    prices = 100.0 * np.cumprod(1.0 + daily, axis=0)
    return pd.DataFrame(prices, index=dates, columns=[row[0] for row in universe])
