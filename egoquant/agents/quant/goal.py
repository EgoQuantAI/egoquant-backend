"""Goal-based simulation for Agent A: how likely is each approved portfolio to reach the investor's goal?

Method (deterministic, no return assumptions):
  * Daily returns of the held funds come from Agent C's REAL price history: about 20 years, so the sample
    holds a full market cycle including the 2008-09 crisis (a block bootstrap can only replay the regimes
    in its sample; ten strong years alone made almost every goal look certain).
  * A path is built by drawing whole blocks of `block_days` consecutive trading days (about a month) at
    random from that history and chaining them. Whole blocks keep crashes and rebounds clustered the way
    they happened. A fixed seed makes the result reproducible.
  * Every portfolio is run on the SAME paths (common random numbers), so differences between portfolios
    come from the portfolios, not from luck.
  * Each month: grow the portfolio by its block return (rebalanced back to target weights), take off the
    funds' annual fees / 12, then add the monthly contribution (or take the withdrawal). Wealth never
    goes below zero.

Funds with SHORTER histories (a 2024 Bitcoin ETF, a coin listed in 2021) are not allowed to shrink the
window for everyone: cutting ten years of data to the youngest fund's two would drop every crash before
it. complete_history fills their missing early years instead, with the standard method for return
histories of different lengths (Stambaugh 1997, "Analyzing investments whose histories differ in
length", J. Financial Economics 45; Page 2013, "How to combine long and short return histories
efficiently", Financial Analysts Journal 69): regress the short fund's daily returns on the long-history
funds it moves with over the period they overlap, then rebuild each missing day as
    betas . (the long funds' ACTUAL returns that day) + one of the short fund's own residuals,
with residuals drawn in whole blocks (same block length as the simulation) from a fixed seed. The
long funds' real crashes therefore reach the short fund through its betas. The regression intercept
(alpha) is NOT carried into the rebuilt years: estimated from a short window it is mostly noise (two
bull years gave one stock +40% a year of "alpha"), so it is set to zero, the standard prior for
mispricing (Pastor & Stambaugh 1999, "Costs of equity capital and model mispricing", J. Finance 54).
Every completed fund is reported (own start, days filled, the funds used, betas, R^2, the alpha dropped).

HINDSIGHT CORRECTION (equilibrium_means). Single stocks and equity funds are chosen from TODAY's most
traded list, so their own past returns carry survivorship / look-ahead bias (today's winners won in the
sample; Brown, Goetzmann, Ibbotson & Ross 1992, "Survivorship bias in performance studies", Review of
Financial Studies 5). Equities and crypto-assets therefore keep every REAL daily move (volatility,
correlations, crashes), but their average daily return is set to what their market sensitivity earned:
beta x the broad market's average over the same period (VT, the world equity fund; zero alpha: the
CAPM / Black-Litterman equilibrium prior, Black & Litterman 1992). Commodities keep their own averages.

RATE ANCHOR (anchor_rate_means). Bonds and cash do not keep their sample average either: it reflects the interest
rates of the PAST window, while a bond's return over the next years is close to the yield at the start (Leibowitz
1986; the standard basis of capital-market assumptions). Each bond / cash fund keeps every real daily move, with its
average set to the rate the market paid at the decision date: the short rate (SHORT_RATE_PROXY's trailing
SHORT_RATE_WINDOW-day return, annualized) for cash, plus BOND_TERM_PREMIUM for bonds. Found by the calibration
backtest (eval/results/zc_z2_*): with sample averages, bond-only portfolios ended below the predicted median in 100%
of past 5-year cases (2012-2021 starts, high-rate history before low rates and 2022); anchored, 42-54%.
Only historical numbers are used; nothing here is a return forecast.

WHAT WOULD CHANGE THE ODDS (goal_levers). For the chosen portfolio only, the same simulation is re-run with
ONE input changed: the monthly amount needed for a 50 / 75 / 90% chance (bisection; the chance can only
rise with the monthly amount, because each path's wealth does), the chance if the goal date is 2 or 5 years
later (the same paths, extended with further blocks from a second fixed seed), and the goal amount reached
in 50 / 75 / 90% of the paths (percentiles of the final wealth already simulated).

HOW MUCH THE PERIOD MATTERS (period_checks). The chosen portfolio is also run on paths drawn only from the
older half of the history and only from the newer half. The gap between the two shows how much the answer
depends on which market years the past happened to hold, an uncertainty far larger than the simulation's
own sampling error (std_error, about half a percentage point with 10,000 paths).

REGIME-AWARE SAMPLER (egoquant/regime/, on by default; EGOQUANT_REGIME_MC=0 turns it off). analyze_goal takes
`sampler`, a regime decision from egoquant.regime.decide. When it is active, each path follows a market-regime chain
(calm / stressed / crisis, from a Hidden Markov Model fitted offline) and each month's block is drawn only from past
blocks of that month's regime, so crises last as long as they did in the past; the levers' longer horizons continue
each path's regime chain and the history-half checks use the same rule within each half. Without it (sampler=None)
every line below runs exactly as before. The result records which sampler ran (GoalAnalysis.sampler, left out of
the output when None).

Everything here is HYPOTHETICAL, based on past data, and not a forecast.
"""

from __future__ import annotations

import zlib
from typing import TYPE_CHECKING, NamedTuple

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_serializer

if TYPE_CHECKING:
    from egoquant.regime.hook import RegimeDecision

LABEL = "Hypothetical simulation based on past market data; not a forecast."
FAN_PERCENTILES = (10, 25, 50, 75, 90)
BACKFILL_METHOD = ("regression on long-history funds plus resampled residual blocks, zero alpha "
                   "(Stambaugh 1997; Page 2013; Pastor & Stambaugh 1999)")
EQUILIBRIUM_METHOD = ("average return set to beta x the broad market's average over the same period "
                      "(zero alpha, CAPM / Black-Litterman equilibrium prior)")
EQUILIBRIUM_CLASSES = ("equity", "crypto")      # picked from today's most traded: survivorship-biased
MARKET_PROXIES = ("VT", "VTI", "SPY", "IVV", "VOO")   # broad equity market, first one present
ANCHOR_SLACK_DAYS = 10          # a fund whose data starts within this many days of the window start is "long"
MAX_FACTORS = 5                 # PROVISIONAL: long-history funds a short fund is regressed on (most correlated)
SAMPLE_PATHS = 25               # paths of the chosen portfolio kept for display (the simulation uses all)
LEVER_TARGETS = (0.50, 0.75, 0.90)   # PROVISIONAL: chances the "what would change your odds" levers aim for
LEVER_EXTRA_YEARS = (2, 5)           # PROVISIONAL: later goal dates tried
LEVER_ROUND = 10.0                   # monthly amounts are rounded up to this many dollars
MAX_HORIZON_YEARS = 60.0             # GoalSpec's upper limit
RATE_ANCHOR = True                   # PROVISIONAL: bond / cash averages anchored to the short rate at the decision date
SHORT_RATE_PROXY = "BIL"             # 1-3 month T-bill fund (a core fund): its trailing return is the cash rate paid
SHORT_RATE_WINDOW = 63               # trading days (~3 months)
BOND_TERM_PREMIUM = 0.015            # PROVISIONAL: bonds' average = short rate + this (best of 0.5/1.0/1.5% in Z2)
RATE_ANCHOR_CLASSES = ("cash", "bond")
RATE_ANCHOR_METHOD = ("average return set to the short rate at the decision date (bonds: plus a term premium), real "
                      "daily moves kept (Leibowitz 1986: a bond's return over the next years is close to its "
                      "starting yield)")


class InsufficientHistory(ValueError):
    """Too little price history for a meaningful simulation; the caller builds the portfolio without one."""


class GoalSpec(BaseModel):
    start_amount: float = Field(gt=0)
    monthly_flow: float = 0.0                 # + contribution, - withdrawal, each month
    goal_amount: float = Field(gt=0)
    horizon_years: float = Field(gt=0, le=60)

    @property
    def months(self) -> int:
        return max(1, round(self.horizon_years * 12))


class PortfolioOutcome(BaseModel):
    menu_index: int                           # index in the candidate menu
    style: str
    equity_weight: float
    prob_goal: float                          # share of paths ending at or above the goal
    p10: float                                # final wealth percentiles
    p50: float
    p90: float
    worst5_shortfall: float                   # average amount missing vs the goal in the worst 5% of paths
    prob_depleted: float                      # share of paths where the money ran out (withdrawals)
    drawdown_p50: float = 0.0                 # max peak-to-trough fall of the account along a path, median path
    drawdown_p90: float = 0.0                 # ... in the worst 10% of paths (90th percentile of max drawdown)
    prob_drawdown_20: float = 0.0             # share of paths that fall 20% or more from a previous high


class Lever(BaseModel):
    """The chosen portfolio's chance with ONE input changed (same portfolio, same simulated market paths)."""

    kind: str                                 # "monthly" | "horizon" | "goal"
    value: float                              # the changed input: monthly flow ($), horizon (years) or goal ($)
    prob_goal: float                          # chance of reaching the (possibly changed) goal with that change
    target: float | None = None               # the chance aimed for (monthly and goal levers)


class PeriodCheck(BaseModel):
    """The chosen portfolio simulated on blocks from only one half of the history."""

    start: str                                # first day of the history this half draws from
    end: str
    prob_goal: float
    p50: float


class FanPoint(BaseModel):
    year: float
    p10: float
    p25: float
    p50: float
    p75: float
    p90: float


class BackfillNote(BaseModel):
    """One fund whose missing early history was completed (see complete_history)."""

    ticker: str
    own_start: str                            # first day of its REAL data
    filled_days: int                          # trading days rebuilt before that
    factors: list[str]                        # long-history funds it was regressed on
    betas: list[float]
    r_squared: float                          # how much of its daily moves those funds explain
    alpha_dropped: float = 0.0                # annualized intercept of the short-window fit, NOT used
    method: str = BACKFILL_METHOD


class MeanAdjustment(BaseModel):
    """One equity / crypto asset whose average return was set by the hindsight correction."""

    ticker: str
    beta: float                               # to the broad market proxy over the whole window
    sample_return: float                      # annualized average daily return in the sample (arithmetic)
    adjusted_return: float                    # beta x the market's annualized average
    market: str
    method: str = EQUILIBRIUM_METHOD


class RateAnchorNote(BaseModel):
    """One bond / cash fund whose average return was set by the rate anchor."""

    ticker: str
    asset_class: str
    sample_return: float                      # annualized average daily return in the sample (arithmetic)
    adjusted_return: float                    # the anchored rate, annualized the same way


class RateAnchor(BaseModel):
    short_rate: float                         # annualized short rate at the decision date (full precision: What-If
                                              # replays the report with exactly this rate)
    proxy: str                                # the fund it is read from
    window_days: int
    term_premium: float                       # added for bonds
    assets: list[RateAnchorNote] = Field(default_factory=list)
    method: str = RATE_ANCHOR_METHOD


class GoalAnalysis(BaseModel):
    spec: GoalSpec
    history_start: str
    history_end: str
    history_days: int
    n_paths: int
    block_days: int
    seed: int
    outcomes: list[PortfolioOutcome]          # one per approved portfolio, best first
    chosen_index: int                         # menu index of the chosen portfolio
    chosen_reason: str
    fan: list[FanPoint]                       # chosen portfolio, yearly
    total_contributed: float                  # start amount plus all monthly flows (no growth)
    backfilled: list[BackfillNote] = Field(default_factory=list)   # short histories that were completed
    mean_adjustments: list[MeanAdjustment] = Field(default_factory=list)   # hindsight correction, per asset
    history_note: str = ""                    # how the history window was chosen
    sample_paths: list[list[float]] = Field(default_factory=list)  # a few monthly wealth paths of the chosen
    std_error: float = 0.0                    # sampling error of the chosen chance: sqrt(p(1-p)/n_paths)
    levers: list[Lever] = Field(default_factory=list)              # what would change the odds (chosen only)
    period_checks: list[PeriodCheck] = Field(default_factory=list)  # older half vs newer half of the history
    label: str = LABEL
    rate_anchor: RateAnchor | None = None     # bond / cash averages set to the rate at the decision date
    sampler: dict | None = None               # which sampler ran (egoquant/regime); None = block bootstrap (EGOQUANT_REGIME_MC=0)

    @model_serializer(mode="wrap")
    def _omit_unset_sampler(self, handler):
        """Without the regime flag the output is exactly what it was before the sampler existed."""
        data = handler(self)
        if isinstance(data, dict):
            if self.sampler is None:
                data.pop("sampler", None)
            if self.rate_anchor is None:              # no anchor (no short-rate fund): the output as before it
                data.pop("rate_anchor", None)
        return data


def complete_history(prices: pd.DataFrame, block_days: int, seed: int, max_factors: int = MAX_FACTORS
                     ) -> tuple[pd.DataFrame, list[BackfillNote], str]:
    """Daily returns over the window the long-history funds cover, short histories completed.

    prices: adjusted closes, each column its own history (NaN before a fund's first price).
    Returns (daily returns without gaps, one note per completed fund, a sentence on the window).
    """
    prices = prices.sort_index()
    returns = prices.pct_change(fill_method=None)
    returns = returns.iloc[1:]
    if returns.empty:
        raise ValueError("no price history to simulate")
    first = {c: returns[c].first_valid_index() for c in returns.columns}
    if any(v is None for v in first.values()):
        raise ValueError(f"no returns for {sorted(c for c, v in first.items() if v is None)}")
    earliest = min(first.values())
    anchors = [c for c in returns.columns if first[c] <= earliest + pd.Timedelta(days=ANCHOR_SLACK_DAYS)]
    window_start = max(first[c] for c in anchors)
    out = returns.loc[window_start:].copy()
    anchor_data = out[anchors].ffill().fillna(0.0)

    notes: list[BackfillNote] = []
    for col in [c for c in out.columns if c not in anchors]:
        y = out[col]
        own = y.first_valid_index()
        overlap = y.loc[own:].dropna().index
        missing = y.loc[: own].index[:-1]                       # days before its first real return
        if len(missing) == 0:
            continue
        x_overlap = anchor_data.loc[overlap]
        corr = x_overlap.apply(lambda s: abs(float(np.corrcoef(s, y.loc[overlap])[0, 1]))
                               if s.std() > 0 else 0.0)
        factors = sorted(corr.index, key=lambda a: (-corr[a], a))[:max_factors]
        design = np.column_stack([np.ones(len(overlap)), x_overlap[factors].to_numpy()])
        coef, *_ = np.linalg.lstsq(design, y.loc[overlap].to_numpy(), rcond=None)
        residuals = y.loc[overlap].to_numpy() - design @ coef
        total = float(np.var(y.loc[overlap].to_numpy()))
        r2 = 0.0 if total <= 0 else max(0.0, 1.0 - float(np.var(residuals)) / total)

        # Residuals drawn in whole blocks (keeps volatility clusters), fixed seed per fund.
        rng = np.random.default_rng([seed, zlib.crc32(col.encode())])
        block = max(1, min(block_days, len(residuals)))
        drawn: list[float] = []
        while len(drawn) < len(missing):
            start = int(rng.integers(0, len(residuals) - block + 1))
            drawn.extend(residuals[start:start + block])
        x_missing = anchor_data.loc[missing, factors].to_numpy()
        fill = x_missing @ coef[1:] + np.array(drawn[: len(missing)])   # zero alpha (intercept not carried)
        out.loc[missing, col] = np.maximum(fill, -0.95)        # a daily return cannot lose more than everything
        notes.append(BackfillNote(ticker=col, own_start=str(own.date()), filled_days=len(missing),
                                  factors=factors, betas=[round(float(b), 4) for b in coef[1:]],
                                  r_squared=round(r2, 3), alpha_dropped=round(float(coef[0]) * 252, 4)))

    out = out.fillna(0.0)                                       # isolated gaps inside a history: no move
    if notes:
        note = (f"{len(out)} trading days from {window_start.date()}, the period the long-history funds cover; "
                f"{len(notes)} younger fund(s) completed by {BACKFILL_METHOD}.")
    else:
        note = f"{len(out)} trading days from {window_start.date()}; every fund has real data for all of it."
    return out, notes, note


def equilibrium_means(daily: pd.DataFrame, asset_classes: dict[str, str],
                      market: str | None = None) -> tuple[pd.DataFrame, list[MeanAdjustment], str | None]:
    """Hindsight correction: equities and crypto keep their real daily moves, with the average return
    each one's beta earned from the broad market over the same window (zero alpha).

    market: the broad equity market column (default: the first of MARKET_PROXIES present). Without one,
    nothing is changed. Returns (adjusted daily returns, one note per adjusted asset, the market used).
    """
    market = market if market in daily.columns else next((m for m in MARKET_PROXIES if m in daily.columns), None)
    if market is None:
        return daily, [], None
    m = daily[market].to_numpy()
    m_mean, m_var = float(m.mean()), float(m.var())
    out = daily.copy()
    notes: list[MeanAdjustment] = []
    for col in daily.columns:
        if col == market or asset_classes.get(col) not in EQUILIBRIUM_CLASSES or m_var <= 0:
            continue
        r = daily[col].to_numpy()
        beta = float(np.cov(r, m, bias=True)[0, 1] / m_var)
        target = beta * m_mean
        out[col] = r - float(r.mean()) + target
        notes.append(MeanAdjustment(ticker=col, beta=round(beta, 3), sample_return=round(float(r.mean()) * 252, 4),
                                    adjusted_return=round(target * 252, 4), market=market))
    return out, notes, market


def short_rate(prices: pd.Series, window: int = SHORT_RATE_WINDOW) -> float | None:
    """Annualized return of the last `window` trading days (a T-bill fund's price: the cash rate it paid).
    None with too little history."""
    s = prices.dropna()
    if len(s) <= window or float(s.iloc[-1 - window]) <= 0:
        return None
    return float((float(s.iloc[-1]) / float(s.iloc[-1 - window])) ** (252 / window) - 1)


def anchor_rate_means(daily: pd.DataFrame, asset_classes: dict[str, str], rate: float,
                      term_premium: float = BOND_TERM_PREMIUM, proxy: str = SHORT_RATE_PROXY,
                      window: int = SHORT_RATE_WINDOW) -> tuple[pd.DataFrame, RateAnchor]:
    """Rate anchor: each cash fund's average daily return set to `rate`, each bond fund's to `rate` +
    `term_premium` (annual, compounded daily); the real daily moves are kept. Other classes are unchanged."""
    out = daily.copy()
    notes: list[RateAnchorNote] = []
    for col in daily.columns:
        cls = asset_classes.get(col)
        if cls not in RATE_ANCHOR_CLASSES:
            continue
        annual = rate if cls == "cash" else rate + term_premium
        target = (1.0 + annual) ** (1 / 252) - 1.0
        r = daily[col].to_numpy()
        out[col] = r - float(r.mean()) + target
        notes.append(RateAnchorNote(ticker=col, asset_class=cls, sample_return=round(float(r.mean()) * 252, 4),
                                    adjusted_return=round(target * 252, 4)))
    return out, RateAnchor(short_rate=float(rate), proxy=proxy, window_days=window, term_premium=term_premium,
                           assets=notes)


def block_returns(daily: pd.DataFrame, block_days: int) -> np.ndarray:
    """Gross return of every overlapping block of `block_days` days, per asset: shape (n_blocks, n_assets)."""
    growth = np.log1p(daily.to_numpy(dtype=float))
    if len(growth) < block_days:
        raise ValueError(f"need at least {block_days} days of history, got {len(growth)}")
    cumulative = np.vstack([np.zeros(growth.shape[1]), np.cumsum(growth, axis=0)])
    return np.exp(cumulative[block_days:] - cumulative[:-block_days])


def simulate_wealth(blocks: np.ndarray, weights: np.ndarray, annual_fees: np.ndarray, spec: GoalSpec,
                    draws: np.ndarray) -> np.ndarray:
    """Wealth at the end of every month for each path: shape (n_paths, months + 1)."""
    monthly = blocks @ weights * (1.0 - float(weights @ annual_fees) / 12.0)   # gross portfolio return per block
    path_returns = monthly[draws]                                              # (n_paths, months)
    wealth = np.empty((draws.shape[0], draws.shape[1] + 1))
    wealth[:, 0] = spec.start_amount
    for m in range(draws.shape[1]):
        wealth[:, m + 1] = np.maximum(wealth[:, m] * path_returns[:, m] + spec.monthly_flow, 0.0)
    return wealth


class PathStats(NamedTuple):
    final: np.ndarray                       # wealth at the goal date, per path
    depleted: np.ndarray                    # the money ran out at some month (wealth hit 0)
    drawdowns: np.ndarray                   # largest fall from a running high, per path (= max_drawdowns)
    columns: dict[int, np.ndarray]          # wealth of every path at each requested month
    rows: np.ndarray | None                 # whole paths of the requested rows, shape (len(rows), months + 1)


def stream_wealth(blocks: np.ndarray, weights: np.ndarray, annual_fees: np.ndarray, spec: GoalSpec,
                  draws: np.ndarray, extra: np.ndarray | None = None, keep_months=(), keep_rows=None) -> PathStats:
    """simulate_wealth + max_drawdowns without the (n_paths, months + 1) wealth matrix: one wealth vector is stepped
    month by month and everything the results need is tracked on the way. The same float operations in the same
    order, so every number is bit-identical to the matrix version, but memory no longer grows with horizon x the
    number of portfolios (a 60-year shortlist of 20 kept ~1.2 GB of matrices). `extra` continues every path after
    `draws` (the levers' later goal dates: np.hstack([draws, extra]) without the copy)."""
    monthly = blocks @ weights * (1.0 - float(weights @ annual_fees) / 12.0)   # gross portfolio return per block
    n_main = draws.shape[1]
    months = n_main + (0 if extra is None else extra.shape[1])
    wealth = np.full(draws.shape[0], float(spec.start_amount))
    peak = wealth.copy()
    depleted = np.zeros(len(wealth), dtype=bool)
    keep_months = set(keep_months)
    columns = {0: wealth.copy()} if 0 in keep_months else {}
    rows = None
    if keep_rows is not None:
        rows = np.empty((len(keep_rows), months + 1))
        rows[:, 0] = wealth[keep_rows]
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdowns = np.where(peak > 0, 1.0 - wealth / peak, 0.0)
        for m in range(months):
            index = draws[:, m] if m < n_main else extra[:, m - n_main]
            wealth = np.maximum(wealth * monthly[index] + spec.monthly_flow, 0.0)
            depleted |= wealth <= 0
            np.maximum(peak, wealth, out=peak)
            np.maximum(drawdowns, np.where(peak > 0, 1.0 - wealth / peak, 0.0), out=drawdowns)
            if m + 1 in keep_months:
                columns[m + 1] = wealth.copy()
            if rows is not None:
                rows[:, m + 1] = wealth[keep_rows]
    return PathStats(wealth, depleted, drawdowns, columns, rows)


def max_drawdowns(wealth: np.ndarray) -> np.ndarray:
    """Largest fall from a running high, per path (monthly values, so contributions soften it)."""
    peaks = np.maximum.accumulate(wealth, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        falls = np.where(peaks > 0, 1.0 - wealth / peaks, 0.0)
    return falls.max(axis=1)


def outcome(menu_index: int, style: str, equity: float, final: np.ndarray, depleted: np.ndarray,
            goal: float, drawdowns: np.ndarray | None = None) -> PortfolioOutcome:
    worst = np.sort(final)[: max(1, int(np.ceil(0.05 * len(final))))]
    dd = drawdowns if drawdowns is not None else np.zeros(len(final))
    return PortfolioOutcome(
        menu_index=menu_index, style=style, equity_weight=equity,
        prob_goal=float(np.mean(final >= goal)),
        p10=float(np.percentile(final, 10)), p50=float(np.percentile(final, 50)), p90=float(np.percentile(final, 90)),
        worst5_shortfall=float(np.mean(np.maximum(goal - worst, 0.0))),
        prob_depleted=float(np.mean(depleted)),
        drawdown_p50=float(np.percentile(dd, 50)), drawdown_p90=float(np.percentile(dd, 90)),
        prob_drawdown_20=float(np.mean(dd >= 0.20)),
    )


def rank(outcomes: list[PortfolioOutcome], vols: dict[int, float], tie: float) -> list[PortfolioOutcome]:
    """Best first: highest goal probability; within `tie` of the best, the smaller worst-5% shortfall,
    then the lower volatility (reach the goal with the least risk)."""
    best = max(o.prob_goal for o in outcomes)
    contenders = [o for o in outcomes if o.prob_goal >= best - tie - 1e-12]
    rest = [o for o in outcomes if o not in contenders]
    contenders.sort(key=lambda o: (o.worst5_shortfall, vols.get(o.menu_index, 0.0), o.menu_index))
    rest.sort(key=lambda o: (-o.prob_goal, o.worst5_shortfall, o.menu_index))
    return contenders + rest


def _chance(blocks: np.ndarray, w: np.ndarray, fees: np.ndarray, spec: GoalSpec, draws: np.ndarray,
            extra: np.ndarray | None = None) -> float:
    return float(np.mean(stream_wealth(blocks, w, fees, spec, draws, extra).final >= spec.goal_amount))


def goal_levers(blocks: np.ndarray, w: np.ndarray, fees: np.ndarray, spec: GoalSpec, draws: np.ndarray,
                final: np.ndarray, seed: int, extend=None) -> list[Lever]:
    """What would change the chosen portfolio's odds, one input at a time, on the same paths.
    extend(extra_months, seed): the regime sampler's continuation of every path (None: uniform blocks, as before)."""
    current = float(np.mean(final >= spec.goal_amount))
    levers: list[Lever] = []
    targets = [t for t in LEVER_TARGETS if t > current + 0.01][:2]       # the next two chances above today's

    def chance_at(flow: float) -> float:
        return _chance(blocks, w, fees, spec.model_copy(update={"monthly_flow": flow}), draws)

    # Monthly amount: the smallest one (rounded up to LEVER_ROUND) with at least the target chance.
    step = max(spec.goal_amount / spec.months, LEVER_ROUND)
    for target in targets:
        lo, hi = spec.monthly_flow, spec.monthly_flow + step
        for _ in range(6):                                              # widen until the target is reachable
            if chance_at(hi) >= target:
                break
            lo, hi = hi, spec.monthly_flow + (hi - spec.monthly_flow) * 2
        else:
            continue                                                    # not reachable with a sensible amount
        while hi - lo > 1.0:
            mid = (lo + hi) / 2
            lo, hi = (lo, mid) if chance_at(mid) >= target else (mid, hi)
        flow = float(np.ceil(hi / LEVER_ROUND) * LEVER_ROUND)
        levers.append(Lever(kind="monthly", value=flow, prob_goal=chance_at(flow), target=target))

    # Later goal date: the same paths, extended with further blocks (second fixed seed).
    if current < LEVER_TARGETS[-1]:
        longest = min(spec.horizon_years + max(LEVER_EXTRA_YEARS), MAX_HORIZON_YEARS)
        extra_months = max(0, round(longest * 12) - spec.months)
        extra = extend(extra_months, seed + 1) if extend is not None else None
        if extra is None:
            extra = np.random.default_rng(seed + 1).integers(0, len(blocks), size=(draws.shape[0], extra_months))
        for years in LEVER_EXTRA_YEARS:
            horizon = spec.horizon_years + years
            if horizon > MAX_HORIZON_YEARS:
                continue
            later = spec.model_copy(update={"horizon_years": horizon})
            levers.append(Lever(kind="horizon", value=horizon,
                                prob_goal=_chance(blocks, w, fees, later, draws, extra[:, : later.months - spec.months])))

    # Smaller goal: the amount reached in `target` of the paths (rounded down to $1,000 when large).
    for target in targets:
        amount = float(np.percentile(final, (1.0 - target) * 100))
        amount = float(np.floor(amount / 1000) * 1000) if amount >= 10_000 else float(np.floor(amount / 10) * 10)
        if amount > 0:
            levers.append(Lever(kind="goal", value=amount, prob_goal=float(np.mean(final >= amount)), target=target))
    return levers


def period_checks(daily: pd.DataFrame, blocks: np.ndarray, w: np.ndarray, fees: np.ndarray, spec: GoalSpec,
                  n_paths: int, block_days: int, seed: int, sampler: RegimeDecision | None = None) -> list[PeriodCheck]:
    """The chosen portfolio on paths drawn only from the older half of the blocks, then only the newer half
    (with an active regime sampler: regime chains restricted to that half's blocks)."""
    mid = len(blocks) // 2
    if mid < 1:
        return []
    rng = np.random.default_rng(seed + 2)
    out = []
    for lo, hi in ((0, mid), (mid, len(blocks))):
        draws = sampler.range_draws(lo, hi, n_paths, spec.months, rng) if sampler is not None else None
        if draws is None:
            draws = rng.integers(lo, hi, size=(n_paths, spec.months))
        final = stream_wealth(blocks, w, fees, spec, draws).final
        del draws                                               # one half's draws at a time
        # block i covers trading days i .. i + block_days - 1
        out.append(PeriodCheck(start=str(daily.index[lo].date()), end=str(daily.index[hi + block_days - 2].date()),
                               prob_goal=float(np.mean(final >= spec.goal_amount)),
                               p50=float(np.percentile(final, 50))))
    return out


def analyze_goal(daily: pd.DataFrame, candidates: list[tuple[int, str, float, dict[str, float]]],
                 annual_fees: dict[str, float], vols: dict[int, float], spec: GoalSpec, *, n_paths: int,
                 block_days: int, seed: int, tie: float, backfilled: list[BackfillNote] | None = None,
                 history_note: str = "", mean_adjustments: list[MeanAdjustment] | None = None,
                 sampler: RegimeDecision | None = None, rate_anchor: RateAnchor | None = None,
                 select=None) -> GoalAnalysis:
    """Simulate every candidate (menu_index, style, equity weight, weights) on the same paths and rank them.
    sampler: an egoquant.regime decision (regime-aware draws, or its recorded fallback); None = the block bootstrap."""
    if daily.empty:
        raise ValueError("no price history to simulate")
    assets = list(daily.columns)
    blocks = block_returns(daily, block_days)
    draws = sampler.draws(len(blocks), n_paths, spec.months, seed) if sampler is not None else None
    if draws is None:                                           # the block bootstrap (always, without the flag)
        rng = np.random.default_rng(seed)
        draws = rng.integers(0, len(blocks), size=(n_paths, spec.months))
    fees = np.array([annual_fees.get(a, 0.0) for a in assets])

    # Only each portfolio's outcome statistics are kept, never its wealth matrix; the chosen one is streamed once
    # more after ranking for the fan chart and the display paths (same draws, so the same numbers).
    outcomes: list[PortfolioOutcome] = []
    weights_by_index: dict[int, np.ndarray] = {}
    for menu_index, style, equity, weights in candidates:
        missing = sorted(a for a, w in weights.items() if w > 1e-9 and a not in assets)
        if missing:
            raise ValueError(f"no price history for held assets {missing}")
        w = np.array([weights.get(a, 0.0) for a in assets])
        stats = stream_wealth(blocks, w, fees, spec, draws)
        weights_by_index[menu_index] = w
        outcomes.append(outcome(menu_index, style, equity, stats.final, stats.depleted, spec.goal_amount,
                                stats.drawdowns))

    if select is not None:                                      # portfolio method v2: egoquant/agents/quant/v2/selection.py
        ranked, reason = select(outcomes, vols, tie)
        chosen = ranked[0]
    else:
        ranked = rank(outcomes, vols, tie)
        chosen = ranked[0]
        best = max(o.prob_goal for o in outcomes)
        contenders = [o for o in ranked if o.prob_goal >= best - tie - 1e-12]      # the same band rank() used
        if len(contenders) > 1:
            reason = (f"Best chance of reaching the goal among the approved portfolios: {best:.1%}. {len(contenders)} "
                      f"portfolios were within {tie:.0%} of it (a difference the simulation cannot reliably tell apart); "
                      f"of those, this one ({chosen.prob_goal:.1%}) had the smallest shortfall in the worst 5% of outcomes.")
        else:
            reason = f"Highest chance of reaching the goal ({chosen.prob_goal:.1%}) among the approved portfolios."

    w = weights_by_index[chosen.menu_index]
    fan_months = sorted({*range(0, spec.months + 1, 12), spec.months})
    step = max(1, n_paths // SAMPLE_PATHS)                      # evenly spaced paths, for display only
    shown = np.arange(0, n_paths, step)[:SAMPLE_PATHS]
    chosen_stats = stream_wealth(blocks, w, fees, spec, draws, keep_months=fan_months, keep_rows=shown)
    fan = []
    for month in fan_months:
        values = np.percentile(chosen_stats.columns[month], FAN_PERCENTILES)
        fan.append(FanPoint(year=round(month / 12, 2), **dict(zip(("p10", "p25", "p50", "p75", "p90"), map(float, values)))))
    samples = [[round(float(v), 2) for v in path] for path in chosen_stats.rows]
    return GoalAnalysis(
        spec=spec, history_start=str(daily.index.min().date()), history_end=str(daily.index.max().date()),
        history_days=len(daily), n_paths=n_paths, block_days=block_days, seed=seed, outcomes=ranked,
        chosen_index=chosen.menu_index, chosen_reason=reason, fan=fan,
        total_contributed=spec.start_amount + spec.monthly_flow * spec.months,
        backfilled=list(backfilled or []), history_note=history_note, sample_paths=samples,
        mean_adjustments=list(mean_adjustments or []),
        std_error=float(np.sqrt(chosen.prob_goal * (1.0 - chosen.prob_goal) / n_paths)),
        levers=goal_levers(blocks, w, fees, spec, draws, chosen_stats.final, seed,
                           extend=sampler.extend if sampler is not None else None),
        period_checks=period_checks(daily, blocks, w, fees, spec, n_paths, block_days, seed, sampler),
        rate_anchor=rate_anchor,
        sampler=sampler.record() if sampler is not None else None,      # last: after the levers and half checks
    )
