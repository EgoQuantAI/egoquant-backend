"""Walk-forward backtest harness for Agent A (Milestone 7).

What it does
  At every rebalance date t a strategy sees ONLY prices up to and including t, proposes target
  weights, pays trading costs on the weights it changes, and holds the new weights from t+1 on
  (weights then drift with returns until the next rebalance). No future data is ever shown.

What it does NOT do
  It proves nothing about real markets when run on synthetic data. Use it to compare strategies on
  the same path, to catch bugs, and (later) to evaluate real stored data from Agent C.

Pieces
  HistoricalMarketProvider  Agent C stand-in over a stored price table, point-in-time only
  Decision                  what a strategy returns: weights, optionally its own volatility forecast
  run_backtest              the simulator
  make_*_strategy           Agent A and the simple comparison strategies
  BacktestResult            daily returns, weights, turnover, metrics, volatility-forecast calibration
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import numpy as np
import pandas as pd

from egoquant.agents.quant import core as qc
from egoquant.agents.quant.agent import QuantAgent
from egoquant.models import AssetInfo, InvestorContext, MarketSnapshot, RiskClassPolicy


# ==========================================================================
# Point-in-time market data
# ==========================================================================
class HistoricalMarketProvider:
    """Agent C stand-in over a stored price table. Returns only data up to `as_of` (no look-ahead).

    Feed it synthetic paths (egoquant.demo.synthetic_market) or real prices from Agent C
    (MarketIntelligenceAgent.price_table) to backtest Agent A on either.
    """

    def __init__(self, prices: pd.DataFrame, assets: list[AssetInfo],
                 risk_free_rate_annual: float = 0.03, data_source: str = "synthetic"):
        self.prices = prices
        self.assets = assets
        self.risk_free_rate_annual = risk_free_rate_annual
        self.data_source = data_source

    def get_market_snapshot(self, asset_ids: list[str] | None, as_of: date,
                            lookback_days: int) -> MarketSnapshot:
        assets = [a for a in self.assets if asset_ids is None or a.asset_id in asset_ids]
        columns = [a.asset_id for a in assets]
        visible = self.prices.loc[: pd.Timestamp(as_of), columns].tail(lookback_days + 1)
        return MarketSnapshot(
            as_of=as_of, prices=visible.copy(), assets=assets,
            risk_free_rate_annual=self.risk_free_rate_annual, data_source=self.data_source,  # type: ignore[arg-type]
        )


# ==========================================================================
# Strategy interface
# ==========================================================================
@dataclass
class Decision:
    """A strategy's answer at one rebalance date."""

    weights: np.ndarray                    # target weights, long-only, sum to 1
    predicted_vol: float | None = None     # the strategy's own annualized volatility forecast, if any
    note: str = ""


# (price history up to and including as_of, as_of, drifted current weights or None at the start)
StrategyFn = Callable[[pd.DataFrame, date, "np.ndarray | None"], Decision]


@dataclass
class BacktestResult:
    daily_returns: dict[str, pd.Series]        # NET of trading costs
    rebalance_weights: dict[str, pd.DataFrame]
    turnover: dict[str, list[float]]           # one-way turnover per rebalance (first one excluded)
    metrics: pd.DataFrame                      # one row per strategy
    calibration: dict[str, dict[str, float]]   # volatility forecast vs realized, strategies that forecast
    notes: dict[str, list[str]] = field(default_factory=dict)


# ==========================================================================
# Metrics
# ==========================================================================
def compute_metrics(daily_returns: np.ndarray, risk_free_annual: float, periods_per_year: int) -> dict[str, float]:
    """Standard performance numbers from a daily net return series."""
    n = len(daily_returns)
    growth = float(np.prod(1.0 + daily_returns))
    ann_return = growth ** (periods_per_year / n) - 1.0
    ann_vol = float(np.std(daily_returns, ddof=1) * np.sqrt(periods_per_year))
    excess = daily_returns - risk_free_annual / periods_per_year
    sharpe = float(np.mean(excess) / np.std(daily_returns, ddof=1) * np.sqrt(periods_per_year)) if ann_vol > 0 else float("nan")
    _, cvar, _ = qc.historical_var_cvar(daily_returns, 0.95)
    return {
        "ann_return": ann_return, "ann_vol": ann_vol, "sharpe": sharpe,
        "max_drawdown": qc.max_drawdown(daily_returns), "cvar95_daily": cvar,
    }


# ==========================================================================
# Simulator
# ==========================================================================
def run_backtest(
    prices: pd.DataFrame,
    assets: list[AssetInfo],
    strategies: dict[str, StrategyFn],
    rebalance_days: int = 21,
    first_rebalance_row: int = 252,
    risk_free_annual: float = 0.0,
    periods_per_year: int = 252,
) -> BacktestResult:
    """Walk-forward simulation of every strategy on the same price table.

    Timing: a decision made at the close of row t uses rows 0..t. The new weights earn the return of
    row t+1 onward. Trading costs (one-way basis points per asset) are charged on the first return of
    the new period. The very first allocation is free (it is the starting portfolio).
    """
    asset_ids = list(prices.columns)
    by_id = {a.asset_id: a for a in assets}
    cost_rates = np.array([by_id[a].trading_cost_bps for a in asset_ids]) / 10_000.0
    equity_mask = np.array([by_id[a].asset_class == "equity" for a in asset_ids])
    returns = prices.pct_change().to_numpy()
    n_rows = len(prices)
    rebalance_rows = list(range(first_rebalance_row, n_rows - 1, rebalance_days))
    if not rebalance_rows:
        raise ValueError("not enough rows for even one rebalance after first_rebalance_row")

    daily_returns: dict[str, pd.Series] = {}
    weights_history: dict[str, pd.DataFrame] = {}
    turnover_history: dict[str, list[float]] = {}
    calibration: dict[str, dict[str, float]] = {}
    notes: dict[str, list[str]] = {}
    metric_rows: dict[str, dict[str, float]] = {}

    for name, strategy in strategies.items():
        w: np.ndarray | None = None
        net, net_dates = [], []
        targets, target_dates, turnovers, costs, predicted, realized = [], [], [], [], [], []
        strategy_notes: list[str] = []

        for k, row in enumerate(rebalance_rows):
            next_row = rebalance_rows[k + 1] if k + 1 < len(rebalance_rows) else n_rows - 1
            history = prices.iloc[: row + 1]                       # information set: rows 0..row only
            decision = strategy(history, history.index[-1].date(), None if w is None else w.copy())
            target = np.asarray(decision.weights, dtype=float)
            if abs(target.sum() - 1.0) > 1e-6 or (target < -1e-9).any():
                raise ValueError(f"{name}: weights must be long-only and sum to 1 (got sum {target.sum():.6f})")
            if decision.note:
                strategy_notes.append(f"{history.index[-1].date()}: {decision.note}")

            if w is None:
                trade_turnover, cost = 0.0, 0.0                    # starting portfolio is free
            else:
                trade_turnover = qc.turnover(target, w)
                cost = float(np.sum(np.abs(target - w) * cost_rates))
                turnovers.append(trade_turnover)
            costs.append(cost)
            w = target.copy()
            targets.append(target)
            target_dates.append(history.index[-1])

            gross_period = []
            for t in range(row + 1, next_row + 1):
                day_return = float(w @ returns[t])
                gross_period.append(day_return)
                net.append((1.0 + day_return) * (1.0 - cost) - 1.0 if t == row + 1 else day_return)
                net_dates.append(prices.index[t])
                w = w * (1.0 + returns[t]) / (1.0 + day_return)    # weights drift with returns
            if decision.predicted_vol is not None and len(gross_period) >= 5:
                predicted.append(decision.predicted_vol)
                realized.append(float(np.std(gross_period, ddof=1) * np.sqrt(periods_per_year)))

        series = pd.Series(net, index=pd.DatetimeIndex(net_dates), name=name)
        daily_returns[name] = series
        weights_history[name] = pd.DataFrame(targets, index=pd.DatetimeIndex(target_dates), columns=asset_ids)
        turnover_history[name] = turnovers
        notes[name] = strategy_notes

        metrics = compute_metrics(series.to_numpy(), risk_free_annual, periods_per_year)
        metrics["annual_turnover"] = float(np.mean(turnovers) * periods_per_year / rebalance_days) if turnovers else 0.0
        metrics["total_cost"] = float(1.0 - np.prod(1.0 - np.array(costs)))
        metrics["avg_equity_weight"] = float(np.mean([t @ equity_mask for t in targets]))
        metric_rows[name] = metrics

        if len(predicted) >= 3:
            p, r = np.array(predicted), np.array(realized)
            calibration[name] = {
                # > 1 means realized volatility was higher than forecast, < 1 lower
                "realized_over_predicted": float(np.sqrt(np.mean(r**2) / np.mean(p**2))),
                "correlation": float(np.corrcoef(p, r)[0, 1]) if np.std(p) > 0 and np.std(r) > 0 else float("nan"),
                "n_periods": float(len(p)),
            }

    return BacktestResult(daily_returns, weights_history, turnover_history,
                          pd.DataFrame(metric_rows).T, calibration, notes)


# ==========================================================================
# Strategies
# ==========================================================================
def _class_mix(policy: RiskClassPolicy) -> dict[str, float]:
    """The naive mix used by the fixed and overlay strategies: class target equity, rest in bonds."""
    return {"equity": policy.target_equity, "bond": 1.0 - policy.target_equity, "cash": 0.0}


def make_agent_strategy(agent: QuantAgent, base_context: InvestorContext, asset_ids: list[str]) -> StrategyFn:
    """Agent A as a strategy: refit on the data visible at each date, hold the last weights if infeasible.

    The agent's own market provider must serve the same price table (HistoricalMarketProvider), which
    is what enforces point-in-time data. Agent B is skipped (no behavioral evidence in the backtest).
    """
    def strategy(history: pd.DataFrame, as_of: date, current: np.ndarray | None) -> Decision:
        holdings = None if current is None else {a: float(x) for a, x in zip(asset_ids, current)}
        context = base_context.model_copy(update={"current_holdings": holdings})
        model = agent.prepare_market_model(context, as_of)
        proposal = agent.finalize_proposal(context, model, None, [])
        if proposal.status == "infeasible":
            if current is not None:
                return Decision(current, None, f"infeasible, held current weights ({proposal.message})")
            cash = model.cash_mask.astype(float)
            return Decision(cash / cash.sum() if cash.sum() > 0 else np.full(len(asset_ids), 1 / len(asset_ids)),
                            None, "infeasible at start, parked in cash")
        weights = np.array([proposal.weights[a] for a in asset_ids])
        return Decision(weights, proposal.risk.ex_ante_vol if proposal.risk else None, proposal.message)

    return strategy


def make_fixed_mix_strategy(assets: list[AssetInfo], policy: RiskClassPolicy) -> StrategyFn:
    """Naive reference: class target equity split equally across equities, the rest across bonds."""
    classes = [a.asset_class for a in assets]
    weights = qc.build_baseline_weights(classes, [False] * len(assets), _class_mix(policy), 1.0)
    return lambda history, as_of, current: Decision(weights.copy())


def make_equal_weight_strategy(n_assets: int) -> StrategyFn:
    """1/N across every asset, ignoring the risk class (a classic hard-to-beat reference)."""
    return lambda history, as_of, current: Decision(np.full(n_assets, 1.0 / n_assets))


def make_inverse_vol_strategy(assets: list[AssetInfo], policy: RiskClassPolicy, window: int = 252) -> StrategyFn:
    """Class mix as above, but inside each class weight assets by 1 / trailing volatility."""
    classes = [a.asset_class for a in assets]
    mix = _class_mix(policy)

    def strategy(history: pd.DataFrame, as_of: date, current: np.ndarray | None) -> Decision:
        vols = qc.compute_returns(history).tail(window).std().to_numpy()
        weights = np.zeros(len(assets))
        for cls, share in mix.items():
            members = [i for i, c in enumerate(classes) if c == cls]
            if members and share > 0:
                inverse = 1.0 / vols[members]
                weights[members] = share * inverse / inverse.sum()
        return Decision(weights / weights.sum())

    return strategy


def make_vol_target_overlay_strategy(assets: list[AssetInfo], policy: RiskClassPolicy,
                                     decay: float = 0.94, periods_per_year: int = 252) -> StrategyFn:
    """Legacy rule on the naive mix: w * min(1, sigma_target / sigma_ewma), the freed weight to cash."""
    classes = [a.asset_class for a in assets]
    cash_mask = np.array([c == "cash" for c in classes])
    base = qc.build_baseline_weights(classes, [False] * len(assets), _class_mix(policy), 1.0)

    def strategy(history: pd.DataFrame, as_of: date, current: np.ndarray | None) -> Decision:
        portfolio_returns = qc.compute_returns(history).tail(252).to_numpy() @ base
        sigma_hat = qc.ewma_volatility(portfolio_returns, decay, periods_per_year)
        weights, _ = qc.apply_volatility_targeting(base, sigma_hat, policy.max_vol, cash_mask)
        return Decision(weights)

    return strategy
