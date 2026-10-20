"""SYNTHETIC investor behavior for demos and tests of Agent B.

EVERYTHING here is invented: the market path (two equity crashes with rebounds, a hyped unprofitable stock with
two price spikes), every investor's behavior, their game choices and the stock's fundamentals.
All data produced here is labeled data_source="synthetic".

How it works: a seeded market path is generated, then each synthetic investor follows a simple,
explicit rule set day by day (deposit monthly, panic-sell at a given loss, take profits on winners,
chase rallies, buy hyped stocks...). The resulting positions, cash flows, trades and prices are
exactly what a brokerage export would contain, so Agent B analyzes them through the same code path
it would use for real data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from egoquant.agents.behavioral.models import (
    AssetFundamentals,
    BehavioralData,
    GameResult,
    Trade,
    TradingHistory,
)

HISTORY_START = "2021-10-01"
HISTORY_END = "2026-09-30"   # same as the Agent A demo's as_of

# Invented asset set for the history: (asset_id, class, annual mean, annual vol)
_HISTORY_ASSETS = [
    ("EQ_GLOBAL",   "equity", 0.08, 0.10),
    ("EQ_EMERGING", "equity", 0.09, 0.13),
    ("BOND_GOVT",   "bond",   0.03, 0.04),
    ("CASH_MM",     "cash",   0.025, 0.0),
    ("MEME_CO",     "equity", 0.00, 0.45),   # a single hyped stock (see FUNDAMENTALS)
]
ASSET_CLASSES = {a: c for a, c, _, _ in _HISTORY_ASSETS}
FUNDS = ["EQ_GLOBAL", "EQ_EMERGING"]
FUNDAMENTALS = {
    "EQ_GLOBAL": AssetFundamentals(instrument="fund"),
    "EQ_EMERGING": AssetFundamentals(instrument="fund"),
    "BOND_GOVT": AssetFundamentals(instrument="fund"),
    "CASH_MM": AssetFundamentals(instrument="fund"),
    "MEME_CO": AssetFundamentals(instrument="stock", profitable=False, debt_to_equity=3.5),
}

# Eckel-Grossman six-gamble payoffs (Dave, Eckel, Johnson & Rojas, 2010), as in the team document
EG_PAYOFFS = {1: (28, 28), 2: (24, 36), 3: (20, 44), 4: (16, 52), 5: (12, 60), 6: (2, 70)}


def eckel_grossman(choice: int, played: str = "2026-09-20T10:00:00") -> GameResult:
    low, high = EG_PAYOFFS[choice]
    return GameResult(choice=choice, payoff_low=low, payoff_high=high, version="eg6-v1", timestamp=played)


@dataclass(frozen=True)
class PriceWindow:
    """An injected move. `total` is compounded over `days` business days, applied to `assets`."""

    start: str
    days: int
    total: float
    assets: tuple[str, ...]


DEFAULT_WINDOWS = (
    # Two broad equity crashes (bonds dip a little in the first one)
    PriceWindow("2022-04-04", 35, -0.24, ("EQ_GLOBAL", "EQ_EMERGING", "MEME_CO")),
    PriceWindow("2022-04-04", 35, -0.04, ("BOND_GOVT",)),
    PriceWindow("2025-03-03", 30, -0.26, ("EQ_GLOBAL", "EQ_EMERGING", "MEME_CO")),
    PriceWindow("2025-03-03", 30, -0.02, ("BOND_GOVT",)),
    # Rebounds after each crash (most of the fall is recovered within a few months)
    PriceWindow("2022-05-23", 60, 0.22, ("EQ_GLOBAL", "EQ_EMERGING")),
    PriceWindow("2025-04-14", 50, 0.24, ("EQ_GLOBAL", "EQ_EMERGING")),
    # Two hype spikes and collapses in the single stock
    PriceWindow("2023-03-01", 15, 0.80, ("MEME_CO",)),
    PriceWindow("2023-03-22", 40, -0.55, ("MEME_CO",)),
    PriceWindow("2024-09-02", 15, 0.90, ("MEME_CO",)),
    PriceWindow("2024-09-23", 40, -0.60, ("MEME_CO",)),
)


def synthetic_history_returns(start: str = HISTORY_START, end: str = HISTORY_END, seed: int = 11,
                              windows: tuple[PriceWindow, ...] = DEFAULT_WINDOWS) -> pd.DataFrame:
    """Daily returns per asset: correlated noise plus deterministic injected moves."""
    dates = pd.bdate_range(start, end)
    ids = [a for a, _, _, _ in _HISTORY_ASSETS]
    mean = np.array([m for _, _, m, _ in _HISTORY_ASSETS]) / 252.0
    vol = np.array([v for _, _, _, v in _HISTORY_ASSETS]) / np.sqrt(252.0)
    n = len(ids)
    corr = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            pair = {ids[i], ids[j]}
            if "CASH_MM" in pair:
                rho = 0.0
            elif pair == {"EQ_GLOBAL", "EQ_EMERGING"}:
                rho = 0.85
            elif "MEME_CO" in pair and ("EQ_GLOBAL" in pair or "EQ_EMERGING" in pair):
                rho = 0.4
            else:
                rho = 0.1
            corr[i, j] = corr[j, i] = rho
    rng = np.random.default_rng(seed)
    noise = rng.multivariate_normal(np.zeros(n), corr * np.outer(vol, vol), size=len(dates))
    returns = mean + noise
    returns[:, ids.index("CASH_MM")] = mean[ids.index("CASH_MM")]

    for w in windows:
        first = dates.searchsorted(pd.Timestamp(w.start))
        last = min(first + w.days, len(dates))
        daily = (1.0 + w.total) ** (1.0 / w.days) - 1.0
        for a in w.assets:
            k = ids.index(a)
            returns[first:last, k] = daily + noise[first:last, k] * 0.5
    return pd.DataFrame(returns, index=dates, columns=ids)


@dataclass(frozen=True)
class InvestorPolicy:
    """A simple, explicit behavioral rule set for one synthetic investor."""

    target: dict[str, float]                          # target weights, must sum to 1
    initial_deposit: float = 100_000.0
    monthly_deposit: float = 1_000.0
    # Panic selling
    panic_thresholds: tuple[float, ...] = ()          # i-th panic happens at the i-th loss level
    panic_sell_fraction: float = 0.70                 # share of equity sold in a panic
    rearm_below_drawdown: float = 0.03                # next panic only after losses recover below this
    reentry_after_days: int = 60                      # back to target after this many days in cash
    # Overtrading
    churn_every_days: int = 0                         # >0: swap money between the two equity funds
    churn_fraction: float = 0.10
    # Disposition effect: periodically take profits on the biggest winner, never sell losers
    take_profit_every_days: int = 0
    take_profit_gain: float = 0.03
    take_profit_fraction: float = 0.25
    # Return chasing: buy an equity fund after a strong 20-day run
    chase_gain: float | None = None
    chase_size: float = 0.04                          # share of the account per chase buy
    chase_cooldown_days: int = 10
    # Hype chasing: buy the hyped stock after a big 20-day spike
    hype_gain: float | None = None
    hype_size: float = 0.04
    hype_cooldown_days: int = 5


def simulate_history(policy: InvestorPolicy, returns: pd.DataFrame,
                     peak_window_days: int = 252) -> TradingHistory:
    """Run the policy day by day over the market path and record what a broker would export."""
    ids = list(returns.columns)
    cash_i = ids.index("CASH_MM")
    fund_idx = [ids.index(a) for a in FUNDS]
    equity_idx = [i for i, a in enumerate(ids) if ASSET_CLASSES[a] == "equity"]
    meme_i = ids.index("MEME_CO")
    target = np.array([policy.target.get(a, 0.0) for a in ids])
    if abs(target.sum() - 1.0) > 1e-9:
        raise ValueError("policy target weights must sum to 1")

    dates = returns.index
    rets = returns.to_numpy()
    prices = 100.0 * np.cumprod(1.0 + rets, axis=0)
    values = policy.initial_deposit * target
    cost = values.copy()                              # cost basis per asset (for take-profit decisions)
    rows = [values.copy()]
    flows: dict[pd.Timestamp, float] = {dates[0]: policy.initial_deposit}
    trades: list[Trade] = []

    def record(t: int, i: int, amount: float) -> None:
        """Trade `amount` of asset i against cash (positive = buy). Keeps cost basis consistent."""
        if abs(amount) <= 1e-9 or i == cash_i:
            return
        if amount < 0:
            fraction = min(1.0, -amount / values[i]) if values[i] > 0 else 0.0
            cost[i] *= (1.0 - fraction)
        else:
            cost[i] += amount
        values[i] += amount
        values[cash_i] -= amount
        trades.append(Trade(trade_date=dates[t].date(), asset_id=ids[i],
                            side="buy" if amount > 0 else "sell", value=float(abs(amount))))

    for i in range(len(ids)):
        if i != cash_i and values[i] > 0:
            trades.append(Trade(trade_date=dates[0].date(), asset_id=ids[i], side="buy", value=float(values[i])))

    index_level, levels = 1.0, [1.0]
    panics_done, out_since, armed = 0, None, True
    churn_toggle, last_chase, last_hype = 0, -10**9, -10**9

    for t in range(1, len(dates)):
        day = dates[t]
        previous_total = values.sum()
        values = values * (1.0 + rets[t])

        # Monthly deposit on the first business day of a month: lands in cash, then invested.
        flow = 0.0
        if day.month != dates[t - 1].month and policy.monthly_deposit > 0:
            flow = policy.monthly_deposit
            flows[day] = flow
            values[cash_i] += flow
            if out_since is None:
                for i in range(len(ids)):
                    record(t, i, flow * target[i])

        index_level *= (values.sum() - flow) / previous_total
        levels.append(index_level)
        drawdown = 1.0 - index_level / max(levels[-peak_window_days:])
        if drawdown < policy.rearm_below_drawdown:
            armed = True

        trailing = prices[t] / prices[max(t - 20, 0)] - 1.0 if t >= 20 else np.zeros(len(ids))

        if out_since is None and armed and panics_done < len(policy.panic_thresholds) \
                and drawdown >= policy.panic_thresholds[panics_done]:
            for i in equity_idx:                                    # panic: dump equities into cash
                record(t, i, -values[i] * policy.panic_sell_fraction)
            panics_done += 1
            out_since, armed = t, False
        elif out_since is not None and t - out_since >= policy.reentry_after_days:
            desired = values.sum() * target                         # buy back to target weights
            for i in range(len(ids)):
                record(t, i, desired[i] - values[i])
            out_since = None
        elif out_since is None:
            if policy.churn_every_days and t % policy.churn_every_days == 0:
                src, dst = (fund_idx[0], fund_idx[1]) if churn_toggle == 0 else (fund_idx[1], fund_idx[0])
                amount = min(values[src], policy.churn_fraction * values.sum())
                record(t, src, -amount)
                record(t, dst, amount)
                churn_toggle = 1 - churn_toggle
            if policy.take_profit_every_days and t % policy.take_profit_every_days == 0:
                gains = [(values[i] / cost[i] - 1.0, i) for i in equity_idx if values[i] > 0 and cost[i] > 0]
                winners = [g for g in gains if g[0] > policy.take_profit_gain]
                if winners:
                    _, best = max(winners)
                    proceeds = values[best] * policy.take_profit_fraction
                    record(t, best, -proceeds)
                    # Typical of the disposition effect: the money goes into the positions that are
                    # down ("they'll come back"), or into the other funds if nothing is down.
                    losers = [i for g, i in gains if g < 0 and i != best] or \
                             [i for i in fund_idx if i != best]
                    for i in losers:
                        record(t, i, proceeds / len(losers))
            if policy.chase_gain is not None and t - last_chase >= policy.chase_cooldown_days:
                hot = [i for i in fund_idx if trailing[i] >= policy.chase_gain]
                amount = policy.chase_size * values.sum()
                if hot and values[cash_i] >= amount:
                    record(t, max(hot, key=lambda i: trailing[i]), amount)
                    last_chase = t
            if policy.hype_gain is not None and t - last_hype >= policy.hype_cooldown_days \
                    and trailing[meme_i] >= policy.hype_gain:
                amount = min(policy.hype_size * values.sum(), values[cash_i])
                if amount > 0:
                    record(t, meme_i, amount)
                    last_hype = t

        rows.append(values.copy())

    positions = pd.DataFrame(rows, index=dates, columns=ids)
    return TradingHistory(
        positions=positions, external_flows=pd.Series(flows, dtype=float).sort_index(), trades=trades,
        asset_classes=dict(ASSET_CLASSES), asset_prices=pd.DataFrame(prices, index=dates, columns=ids),
        fundamentals=dict(FUNDAMENTALS),
    )


# --------------------------------------------------------------------------
# The synthetic profiles used in the demo and the tests
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SyntheticProfile:
    investor_id: str
    description: str
    risk_class: str                                   # questionnaire class given to Agent A
    policy: InvestorPolicy | None                     # None = no trading history at all
    history_window: tuple[str | None, str | None] = (None, None)
    game_choice: int | None = None
    stated_horizon_years: float | None = 10.0
    context_overrides: dict = field(default_factory=dict)   # extra InvestorContext fields for the demo


SYNTHETIC_PROFILES: dict[str, SyntheticProfile] = {
    "steady": SyntheticProfile(
        investor_id="steady", risk_class="C4", game_choice=4,
        description="Questionnaire C4 and behaves like it: about 70% equity, held through both crashes.",
        policy=InvestorPolicy(target={"EQ_GLOBAL": 0.45, "EQ_EMERGING": 0.25, "BOND_GOVT": 0.25, "CASH_MM": 0.05}),
    ),
    "panic_seller": SyntheticProfile(
        investor_id="panic_seller", risk_class="C4", game_choice=3,
        description="Questionnaire C4, but sold 70% of equities in both crashes, at about -11% and -13%.",
        policy=InvestorPolicy(target={"EQ_GLOBAL": 0.40, "EQ_EMERGING": 0.20, "BOND_GOVT": 0.30, "CASH_MM": 0.10},
                              panic_thresholds=(0.11, 0.13), reentry_after_days=150),
    ),
    "sara": SyntheticProfile(
        investor_id="sara", risk_class="C3", game_choice=2, stated_horizon_years=3.0,
        description="The team document's Sara: C3, sold everything once in 2022 at about -9%, and the "
                    "game is cautious too.",
        policy=InvestorPolicy(target={"EQ_GLOBAL": 0.30, "EQ_EMERGING": 0.10, "BOND_GOVT": 0.45, "CASH_MM": 0.15},
                              panic_thresholds=(0.09,), panic_sell_fraction=1.0, reentry_after_days=150,
                              monthly_deposit=500.0),
        history_window=(None, "2024-12-31"),
    ),
    "overtrader": SyntheticProfile(
        investor_id="overtrader", risk_class="C4", game_choice=5,
        description="Never panics, but swaps funds every two weeks and keeps most money in one fund.",
        policy=InvestorPolicy(target={"EQ_GLOBAL": 0.10, "EQ_EMERGING": 0.55, "BOND_GOVT": 0.30, "CASH_MM": 0.05},
                              churn_every_days=10, churn_fraction=0.10),
        context_overrides={"current_holdings": {"EQ_EMERGING": 0.60, "BOND_GOVT": 0.30, "CASH_MM": 0.10}},
    ),
    "disposition": SyntheticProfile(
        investor_id="disposition", risk_class="C3", game_choice=3, stated_horizon_years=8.0,
        description="Every two weeks sells part of its biggest winner and puts the money into whatever is down.",
        policy=InvestorPolicy(target={"EQ_GLOBAL": 0.30, "EQ_EMERGING": 0.20, "BOND_GOVT": 0.40, "CASH_MM": 0.10},
                              take_profit_every_days=10, take_profit_gain=0.02),
    ),
    "hype_chaser": SyntheticProfile(
        investor_id="hype_chaser", risk_class="C4", game_choice=6, stated_horizon_years=5.0,
        description="Buys funds after strong runs and keeps buying an unprofitable, indebted 'hot' stock "
                    "after its price spikes.",
        policy=InvestorPolicy(target={"EQ_GLOBAL": 0.30, "EQ_EMERGING": 0.20, "BOND_GOVT": 0.30, "CASH_MM": 0.20},
                              chase_gain=0.035, hype_gain=0.25, monthly_deposit=3_000.0),
    ),
    "newcomer": SyntheticProfile(
        investor_id="newcomer", risk_class="C4", game_choice=1, policy=None,
        description="Brand new: no trading logs, questionnaire C4, but picked the sure-thing gamble.",
    ),
    "thin_history": SyntheticProfile(
        investor_id="thin_history", risk_class="C3", game_choice=None,
        description="Two months of trading and no game: too little evidence for any behavioral limit.",
        policy=InvestorPolicy(target={"EQ_GLOBAL": 0.30, "EQ_EMERGING": 0.10, "BOND_GOVT": 0.40, "CASH_MM": 0.20}),
        history_window=("2026-08-03", None),
    ),
}


class InMemoryBehavioralDataProvider:
    """Serves BehavioralData from a dict. Implements the BehavioralDataProvider protocol."""

    def __init__(self, data: dict[str, BehavioralData]):
        self._data = dict(data)

    def get_behavioral_data(self, investor_id: str) -> BehavioralData | None:
        return self._data.get(investor_id)


def build_synthetic_data(seed: int = 11) -> dict[str, BehavioralData]:
    """Generate every synthetic profile on one shared market path (deterministic for a seed)."""
    returns = synthetic_history_returns(seed=seed)
    out: dict[str, BehavioralData] = {}
    for key, p in SYNTHETIC_PROFILES.items():
        history = None
        if p.policy is not None:
            start, end = p.history_window
            history = simulate_history(p.policy, returns.loc[start:end])
        out[key] = BehavioralData(
            investor_id=p.investor_id, history=history,
            game_result=eckel_grossman(p.game_choice) if p.game_choice else None,
            stated_horizon_years=p.stated_horizon_years, data_source="synthetic",
        )
    return out


def synthetic_behavioral_provider(seed: int = 11) -> InMemoryBehavioralDataProvider:
    return InMemoryBehavioralDataProvider(build_synthetic_data(seed))
