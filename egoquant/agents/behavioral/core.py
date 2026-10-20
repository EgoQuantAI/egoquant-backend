"""Pure behavioral computations for Agent B (the Behavioral Twin).

Rules for this file (same as egoquant/agents/quant/core.py):
  * NumPy / Pandas only. No Pydantic, no agents, no LLM.
  * Same inputs always give the same outputs.
  * Everything here describes PAST behavior. It is evidence, not a prediction.

Vocabulary used in comments:
  V_t     total account value at the end of day t (all positions, cash included)
  F_t     external flow on day t (+ deposit, - withdrawal)
  r_t     time-weighted return of the account on day t
  I_t     growth index, I_t = prod(1 + r)
  dd_t    drawdown from the rolling peak of I, as a POSITIVE fraction (0.12 = down 12%)
  spell   a run of consecutive days with dd_t >= spell_floor (one "bad patch" in the market)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252
TRADING_DAYS_PER_MONTH = 21


# ==========================================================================
# 1. Account returns and drawdowns
# ==========================================================================
def time_weighted_returns(total_value: pd.Series, external_flows: pd.Series) -> pd.Series:
    """Daily time-weighted return r_t = (V_t - F_t) / V_{t-1} - 1.

    Deposits and withdrawals are removed, so adding money never looks like a gain.
    Trades between positions (sell equity, keep cash) do not change V_t, so they do not
    change r_t either. The first day, and any day after an empty account, gets r_t = 0.
    """
    flows = external_flows.reindex(total_value.index, fill_value=0.0).astype(float)
    previous = total_value.shift(1)
    returns = (total_value - flows) / previous - 1.0
    usable = previous.notna() & (previous > 0)
    return returns.where(usable, 0.0).astype(float)


def growth_index(returns: pd.Series) -> pd.Series:
    """I_t = prod_{s<=t} (1 + r_s). Starts at 1 + r_0 (= 1 because r_0 = 0)."""
    return (1.0 + returns).cumprod()


def rolling_drawdown(index: pd.Series, window: int) -> pd.Series:
    """dd_t = 1 - I_t / max(I over the last `window` days). Positive fraction.

    A rolling peak (rather than the all-time peak) measures losses against recent highs,
    which is what an investor actually feels. It also lets two crashes years apart count
    as two separate events even if the account never regained its old peak in between.
    """
    peak = index.rolling(window, min_periods=1).max()
    return (1.0 - index / peak).clip(lower=0.0)


def drawdown_spells(drawdown: np.ndarray, floor: float) -> list[tuple[int, int]]:
    """Inclusive (start, end) positions of every run of consecutive days with dd >= floor."""
    spells: list[tuple[int, int]] = []
    start: int | None = None
    for i, value in enumerate(drawdown):
        if value >= floor and start is None:
            start = i
        elif value < floor and start is not None:
            spells.append((start, i - 1))
            start = None
    if start is not None:
        spells.append((start, len(drawdown) - 1))
    return spells


# ==========================================================================
# 2. Panic selling (capitulation) episodes
# ==========================================================================
@dataclass
class Episode:
    """The investor net-sold a large share of risky holdings while already down."""

    spell_start: int
    spell_end: int
    trigger_pos: int            # day the selling started
    trigger_drawdown: float     # dd on that day = the loss at which they gave up
    sold_fraction: float        # net risky value sold in the window / risky value the day before
    spell_depth: float          # deepest dd in the whole spell


@dataclass
class HeldThrough:
    """A meaningful drawdown the investor sat through WITHOUT capitulating."""

    spell_start: int
    spell_end: int
    depth: float


def detect_episodes(
    drawdown: np.ndarray,
    risky_value: np.ndarray,
    risky_net_sold: np.ndarray,
    risky_gross_sold: np.ndarray,
    outflows: np.ndarray,
    spell_floor: float,
    min_trigger_drawdown: float,
    min_sell_fraction: float,
    sell_window_days: int,
    max_withdrawal_share: float,
    min_informative_depth: float,
) -> tuple[list[Episode], list[HeldThrough]]:
    """Find capitulation episodes and held-through drawdowns. All arrays are aligned by day.

    risky_value[t]       end-of-day value of risky positions
    risky_net_sold[t]    value of risky sells minus risky buys on day t
    risky_gross_sold[t]  value of risky sells on day t
    outflows[t]          withdrawals on day t, as a positive amount

    Rules (thresholds come from BehaviorConfig, so they are versioned):
      * At most ONE episode per spell: the first qualifying day.
      * A day qualifies if dd >= min_trigger_drawdown, there is net risky selling that day, and
        net risky selling over the next `sell_window_days` days is at least `min_sell_fraction`
        of the risky value held the day before.
      * Selling that mostly funds a withdrawal is a liquidity need, not panic, and is skipped.
      * A spell with no episode and depth >= min_informative_depth is "held through".
    """
    n = len(drawdown)
    episodes: list[Episode] = []
    held: list[HeldThrough] = []

    for start, end in drawdown_spells(drawdown, spell_floor):
        depth = float(drawdown[start:end + 1].max())
        found: Episode | None = None
        for t in range(start, end + 1):
            if drawdown[t] < min_trigger_drawdown or risky_net_sold[t] <= 0 or t == 0:
                continue
            base = risky_value[t - 1]
            if base <= 0:
                continue
            stop = min(t + sell_window_days, n)
            gross = float(risky_gross_sold[t:stop].sum())
            if gross > 0 and float(outflows[t:stop].sum()) >= max_withdrawal_share * gross:
                continue  # funding a withdrawal
            fraction = float(risky_net_sold[t:stop].sum()) / float(base)
            if fraction >= min_sell_fraction:
                found = Episode(start, end, t, float(drawdown[t]), min(fraction, 1.0), depth)
                break
        if found is not None:
            episodes.append(found)
        elif depth >= min_informative_depth:
            held.append(HeldThrough(start, end, depth))
    return episodes, held


def panic_cost(values_before: np.ndarray, price_start: np.ndarray, price_end: np.ndarray,
               actual_growth: float) -> float:
    """What a panic sale cost: growth of simply HOLDING the pre-sale positions, minus actual growth.

    values_before   position values the day before the sale (cash included)
    price_start/end prices on that day and at the end of the comparison window; NaN = no price
                    (plain cash), treated as no change
    actual_growth   I_end / I_start of the real account (time-weighted, so deposits don't count)

    Positive = the sale left the investor worse off than doing nothing.
    """
    total = float(values_before.sum())
    if total <= 0:
        return 0.0
    ratio = np.where(np.isfinite(price_start) & np.isfinite(price_end) & (price_start > 0),
                     price_end / np.where(price_start > 0, price_start, 1.0), 1.0)
    held_growth = float((values_before * ratio).sum() / total)
    return held_growth - actual_growth


# ==========================================================================
# 3. Trading-style features
# ==========================================================================
def annual_turnover(gross_traded_value: float, mean_account_value: float, n_days: int) -> float:
    """One-way turnover per year: (buys + sells) / 2 / average account value, annualized.

    Uses the same one-way convention as Agent A (0.5 * sum |dw|).
    """
    if mean_account_value <= 0 or n_days <= 0:
        return 0.0
    return float(gross_traded_value / 2.0 / mean_account_value * TRADING_DAYS_PER_YEAR / n_days)


def trades_per_month(n_trades: int, n_days: int) -> float:
    if n_days <= 0:
        return 0.0
    return float(n_trades / (n_days / TRADING_DAYS_PER_MONTH))


def concentration_p90(position_weights: pd.DataFrame) -> float:
    """90th percentile over days of the largest single-position weight (cash excluded upstream)."""
    largest = position_weights.max(axis=1).to_numpy()
    return float(np.quantile(largest, 0.90)) if len(largest) else 0.0


def implied_holding_years(mean_risky_value: float, risky_sold_value: float, n_days: int) -> float | None:
    """Average holding period implied by selling speed: risky value held / risky value sold per year.

    None when nothing risky was ever sold (holding period longer than the history).
    """
    if risky_sold_value <= 0 or n_days <= 0 or mean_risky_value <= 0:
        return None
    sold_per_year = risky_sold_value * TRADING_DAYS_PER_YEAR / n_days
    return float(mean_risky_value / sold_per_year)


# ==========================================================================
# 4. Disposition effect (Odean 1998): sell winners, hold losers
# ==========================================================================
@dataclass
class DispositionCounts:
    realized_gains: int = 0
    realized_losses: int = 0
    paper_gains: int = 0
    paper_losses: int = 0
    sell_days: int = 0

    @property
    def pgr(self) -> float | None:
        total = self.realized_gains + self.paper_gains
        return self.realized_gains / total if total else None

    @property
    def plr(self) -> float | None:
        total = self.realized_losses + self.paper_losses
        return self.realized_losses / total if total else None

    @property
    def effect(self) -> float | None:
        """PGR - PLR. Positive = sells winners more readily than losers."""
        if self.pgr is None or self.plr is None:
            return None
        return self.pgr - self.plr


def disposition_counts(prices: np.ndarray, initial_values: np.ndarray,
                       trades: list[tuple[int, int, str, float]]) -> DispositionCounts:
    """Odean's counting method on one account.

    prices          (n_days, n_assets) prices of the RISKY assets; NaN where unknown
    initial_values  position values on day 0 (their cost basis is the day-0 price: an assumption,
                    because purchases before the history started are not visible)
    trades          (day, asset_index, side, value), at end-of-day prices

    On every day with at least one risky sale, each held risky asset counts once as a realized
    gain/loss (if sold that day) or a paper gain/loss (if not), judged against its average cost.
    """
    n_days, n_assets = prices.shape
    units = np.zeros(n_assets)
    cost = np.zeros(n_assets)
    for i in range(n_assets):
        if initial_values[i] > 0 and np.isfinite(prices[0, i]) and prices[0, i] > 0:
            units[i] = initial_values[i] / prices[0, i]
            cost[i] = initial_values[i]

    by_day: dict[int, list[tuple[int, str, float]]] = {}
    for day, i, side, value in trades:
        by_day.setdefault(day, []).append((i, side, value))

    counts = DispositionCounts()
    for day in sorted(by_day):
        todays = by_day[day]
        sold = {i for i, side, _ in todays if side == "sell"}
        if sold:
            counts.sell_days += 1
            for i in range(n_assets):
                price = prices[day, i]
                if units[i] <= 1e-12 or not np.isfinite(price):
                    continue
                basis = cost[i] / units[i]
                if price > basis:
                    if i in sold:
                        counts.realized_gains += 1
                    else:
                        counts.paper_gains += 1
                elif price < basis:
                    if i in sold:
                        counts.realized_losses += 1
                    else:
                        counts.paper_losses += 1
        for i, side, value in todays:            # update holdings after classifying the day
            price = prices[day, i]
            if not np.isfinite(price) or price <= 0:
                continue
            if side == "buy":
                units[i] += value / price
                cost[i] += value
            elif units[i] > 0:
                sold_units = min(units[i], value / price)
                cost[i] *= (units[i] - sold_units) / units[i]   # average cost per unit unchanged
                units[i] -= sold_units
    return counts


# ==========================================================================
# 5. Buying after rallies: return chasing and hype chasing
# ==========================================================================
def trailing_returns(prices: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """r_t = P_t / P_{t-lookback} - 1 for every asset (NaN for the first `lookback` days)."""
    return prices / prices.shift(lookback) - 1.0


def rally_percentiles(trailing: pd.DataFrame) -> pd.DataFrame:
    """Where each day's trailing return sits within that asset's own history (0 = worst, 1 = best).

    Uses the whole visible history of the asset, which is fine for DESCRIBING past behavior.
    """
    return trailing.rank(pct=True)


def chase_share(buy_values: np.ndarray, buy_flags: np.ndarray) -> float | None:
    """Share of buy value that was flagged (bought after a rally, or a hype buy). None if no buys."""
    total = float(buy_values.sum())
    if total <= 0:
        return None
    return float(buy_values[buy_flags].sum() / total)


# ==========================================================================
# 6. Diagnostics and helpers
# ==========================================================================
def implied_risk_aversion(equity_share: float, equity_premium: float, equity_vol: float,
                          min_share: float = 0.02) -> float | None:
    """Back out lambda from the textbook optimal-share rule  w* = (mu - r) / (lambda * sigma^2).

    lambda = premium / (w * sigma^2). This is a DIAGNOSTIC that rests on ASSUMED premium and
    volatility. It is never fed into the optimizer. None when the share is too small to divide by.
    """
    if equity_share < min_share or equity_vol <= 0:
        return None
    return float(equity_premium / (equity_share * equity_vol ** 2))


def class_for_equity_share(share: float, class_caps: list[tuple[str, float]]) -> str:
    """Lowest class whose equity cap covers `share`. class_caps = [(class, max_equity)] C1..C5."""
    for label, cap in class_caps:
        if share <= cap + 1e-12:
            return label
    return class_caps[-1][0]


def ceil_to(value: float, step: float) -> float:
    """Round UP to a multiple of `step` (used so a clamped limit is never tighter than reachable)."""
    return float(np.ceil(value / step - 1e-12) * step)
