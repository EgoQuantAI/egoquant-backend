"""Tests for the walk-forward backtest harness. Run with:  pytest -v tests/agent_a/test_backtest.py

All price data here is SYNTHETIC. The tests check the MECHANICS (no look-ahead, cost accounting,
timing, bookkeeping), not whether any strategy is good.
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from egoquant.agents.behavioral import NoBehavioralEvidence
from egoquant.agents.quant import QuantAgent
from egoquant.agents.quant.backtest import (
    Decision,
    HistoricalMarketProvider,
    compute_metrics,
    make_agent_strategy,
    make_equal_weight_strategy,
    make_fixed_mix_strategy,
    make_inverse_vol_strategy,
    make_vol_target_overlay_strategy,
    run_backtest,
)
from egoquant.demo.synthetic_market import make_synthetic_history, synthetic_asset_infos
from egoquant.models import AgentConfig, AssetInfo, InvestorContext

END = date(2026, 9, 30)


def two_asset_setup(n_rows: int = 400, flat: bool = True):
    """Two assets with different trading costs; flat prices make the arithmetic exact."""
    dates = pd.bdate_range(end=pd.Timestamp(END), periods=n_rows)
    rng = np.random.default_rng(1)
    if flat:
        prices = pd.DataFrame(100.0, index=dates, columns=["A", "B"])
    else:
        prices = pd.DataFrame(100.0 * np.cumprod(1 + rng.normal(0.0003, 0.01, (n_rows, 2)), axis=0),
                              index=dates, columns=["A", "B"])
    assets = [AssetInfo(asset_id="A", asset_class="equity", trading_cost_bps=10.0),
              AssetInfo(asset_id="B", asset_class="bond", trading_cost_bps=20.0)]
    return prices, assets


# ---- provider: point-in-time data -----------------------------------------
def test_provider_never_returns_rows_after_as_of():
    prices = make_synthetic_history(seed=3, n_days=600, end_date=END)
    provider = HistoricalMarketProvider(prices, synthetic_asset_infos())
    as_of = prices.index[400].date()
    snapshot = provider.get_market_snapshot(None, as_of, lookback_days=504)
    assert snapshot.prices.index.max().date() == as_of
    assert snapshot.prices.index.max() < prices.index[401]
    assert len(snapshot.prices) == 401                        # only 401 rows exist up to that date


def test_provider_respects_lookback_and_asset_subset():
    prices = make_synthetic_history(seed=3, n_days=600, end_date=END)
    provider = HistoricalMarketProvider(prices, synthetic_asset_infos())
    snapshot = provider.get_market_snapshot(["EQ_GLOBAL", "CASH_MM"], END, lookback_days=100)
    assert len(snapshot.prices) == 101 and list(snapshot.prices.columns) == ["EQ_GLOBAL", "CASH_MM"]
    assert [a.asset_id for a in snapshot.assets] == ["EQ_GLOBAL", "CASH_MM"]


def test_synthetic_history_is_reproducible_and_crisis_raises_volatility():
    first = make_synthetic_history(seed=9, n_days=500, end_date=END, crisis_start=400, crisis_len=50)
    second = make_synthetic_history(seed=9, n_days=500, end_date=END, crisis_start=400, crisis_len=50)
    assert first.equals(second)
    returns = first.pct_change()
    assert returns["EQ_GLOBAL"].iloc[400:450].std() > 1.8 * returns["EQ_GLOBAL"].iloc[100:400].std()


# ---- simulator mechanics ---------------------------------------------------
def test_strategy_sees_only_data_up_to_the_rebalance_date():
    prices, assets = two_asset_setup()
    seen = []

    def spy(history, as_of, current):
        seen.append((len(history), history.index[-1].date(), as_of))
        return Decision(np.array([0.5, 0.5]))

    run_backtest(prices, assets, {"spy": spy}, rebalance_days=21, first_rebalance_row=252)
    assert [length for length, _, _ in seen] == [253 + 21 * k for k in range(len(seen))]
    assert all(last == as_of == prices.index[length - 1].date() for length, last, as_of in seen)


def test_first_return_earned_is_the_day_after_the_first_decision():
    prices, assets = two_asset_setup()
    result = run_backtest(prices, assets, {"s": lambda h, d, c: Decision(np.array([0.5, 0.5]))},
                          rebalance_days=21, first_rebalance_row=252)
    series = result.daily_returns["s"]
    assert series.index[0] == prices.index[253] and series.index[-1] == prices.index[-1]


def test_buy_and_hold_growth_matches_the_hand_calculation_for_the_first_period():
    prices, assets = two_asset_setup(flat=False)
    result = run_backtest(prices, assets, {"s": lambda h, d, c: Decision(np.array([0.7, 0.3]))},
                          rebalance_days=21, first_rebalance_row=252)
    first_period = result.daily_returns["s"].iloc[:21]          # the starting allocation is free of cost
    expected = 0.7 * prices.iloc[273]["A"] / prices.iloc[252]["A"] + 0.3 * prices.iloc[273]["B"] / prices.iloc[252]["B"]
    assert np.prod(1 + first_period.to_numpy()) == pytest.approx(expected, rel=1e-10)


def test_trading_costs_are_charged_on_the_weights_that_change():
    prices, assets = two_asset_setup(flat=True)                 # zero returns: only costs move the portfolio
    flip = {"n": 0}

    def flipper(history, as_of, current):
        flip["n"] += 1
        return Decision(np.array([1.0, 0.0]) if flip["n"] % 2 == 1 else np.array([0.0, 1.0]))

    result = run_backtest(prices, assets, {"flip": flipper}, rebalance_days=21, first_rebalance_row=252)
    assert all(t == pytest.approx(1.0) for t in result.turnover["flip"])          # 100% one-way each time
    series = result.daily_returns["flip"]
    first_day_of_period_2 = series.iloc[21]
    assert first_day_of_period_2 == pytest.approx(-(0.0010 + 0.0020))             # sell 100% A (10bp) + buy 100% B (20bp)
    assert series.iloc[0] == 0.0                                                  # starting allocation is free
    assert result.metrics.loc["flip", "total_cost"] > 0
    assert result.metrics.loc["flip", "annual_turnover"] == pytest.approx(252 / 21)


def test_weights_must_be_long_only_and_sum_to_one():
    prices, assets = two_asset_setup()
    with pytest.raises(ValueError):
        run_backtest(prices, assets, {"bad": lambda h, d, c: Decision(np.array([0.6, 0.3]))})
    with pytest.raises(ValueError):
        run_backtest(prices, assets, {"short": lambda h, d, c: Decision(np.array([1.5, -0.5]))})


def test_too_little_history_raises():
    prices, assets = two_asset_setup(n_rows=200)
    with pytest.raises(ValueError):
        run_backtest(prices, assets, {"s": lambda h, d, c: Decision(np.array([0.5, 0.5]))}, first_rebalance_row=252)


def test_weights_drift_between_rebalances_and_current_weights_are_passed_in():
    prices, assets = two_asset_setup(flat=False)
    received = []

    def watcher(history, as_of, current):
        received.append(None if current is None else current.copy())
        return Decision(np.array([0.5, 0.5]))

    run_backtest(prices, assets, {"w": watcher}, rebalance_days=21, first_rebalance_row=252)
    assert received[0] is None
    assert all(abs(c.sum() - 1.0) < 1e-12 for c in received[1:])
    assert not np.allclose(received[1], [0.5, 0.5])                               # drifted away from target


def test_metrics_on_known_series():
    constant = np.full(252, 0.0004)
    metrics = compute_metrics(constant, 0.0, 252)
    assert metrics["ann_return"] == pytest.approx(1.0004**252 - 1)
    assert metrics["ann_vol"] == pytest.approx(0.0, abs=1e-12) and metrics["max_drawdown"] == 0.0
    path = np.array([0.10, -0.20, 0.05, 0.05])
    assert compute_metrics(path, 0.0, 252)["max_drawdown"] == pytest.approx(0.20)


# ---- comparison strategies -------------------------------------------------
def test_simple_strategies_return_valid_weights():
    prices = make_synthetic_history(seed=2, n_days=400, end_date=END)
    assets = synthetic_asset_infos()
    policy = AgentConfig().risk_class_policies["C3"]
    for strategy in [make_equal_weight_strategy(6), make_fixed_mix_strategy(assets, policy),
                     make_inverse_vol_strategy(assets, policy), make_vol_target_overlay_strategy(assets, policy)]:
        weights = strategy(prices, END, None).weights
        assert weights.sum() == pytest.approx(1.0) and (weights >= 0).all()


def test_fixed_mix_and_inverse_vol_share_the_class_equity_weight():
    prices = make_synthetic_history(seed=2, n_days=400, end_date=END)
    assets = synthetic_asset_infos()
    policy = AgentConfig().risk_class_policies["C3"]
    equity = np.array([a.asset_class == "equity" for a in assets])
    assert make_fixed_mix_strategy(assets, policy)(prices, END, None).weights[equity].sum() == pytest.approx(0.40)
    inverse = make_inverse_vol_strategy(assets, policy)(prices, END, None).weights
    assert inverse[equity].sum() == pytest.approx(0.40)
    assert inverse[0] > inverse[1]                          # EQ_GLOBAL (16% vol) beats EQ_EMERGING (22% vol)


def test_vol_target_overlay_derisks_only_when_volatility_is_high():
    assets = synthetic_asset_infos()
    policy = AgentConfig().risk_class_policies["C4"]
    equity = np.array([a.asset_class == "equity" for a in assets])
    strategy = make_vol_target_overlay_strategy(assets, policy)
    calm = make_synthetic_history(seed=4, n_days=400, end_date=END)
    stressed = make_synthetic_history(seed=4, n_days=400, end_date=END, crisis_start=370, crisis_len=30)
    assert strategy(stressed, END, None).weights[equity].sum() < strategy(calm, END, None).weights[equity].sum()


# ---- Agent A inside the simulator -----------------------------------------
def test_agent_strategy_runs_walk_forward_and_respects_class_limits():
    prices = make_synthetic_history(seed=6, n_days=440, end_date=END, crisis_start=400, crisis_len=30)
    assets = synthetic_asset_infos()
    provider = HistoricalMarketProvider(prices, assets)
    agent = QuantAgent(provider, NoBehavioralEvidence(), AgentConfig(run_sensitivity=False))
    context = InvestorContext(investor_id="bt", risk_class="C3", goal_type="growth", horizon_years=10,
                              min_cash_buffer=0.05, max_acceptable_loss=0.30)
    ids = list(prices.columns)
    result = run_backtest(prices, assets, {"agent": make_agent_strategy(agent, context, ids)},
                          rebalance_days=63, first_rebalance_row=252, risk_free_annual=0.03)
    weights = result.rebalance_weights["agent"]
    equity = np.array([a.asset_class == "equity" for a in assets])
    assert len(weights) == 3 and np.allclose(weights.sum(axis=1), 1.0) and (weights.to_numpy() >= 0).all()
    assert (weights.to_numpy()[:, equity].sum(axis=1) <= 0.50 + 1e-6).all()             # C3 cap holds every time
    assert np.isfinite(result.metrics.loc["agent"].to_numpy()).all()
    assert "agent" in result.calibration and result.calibration["agent"]["n_periods"] == 3


def test_agent_never_sees_prices_after_the_decision_date():
    prices = make_synthetic_history(seed=6, n_days=330, end_date=END)
    assets = synthetic_asset_infos()
    provider = HistoricalMarketProvider(prices, assets)
    agent = QuantAgent(provider, NoBehavioralEvidence(), AgentConfig(run_sensitivity=False))
    context = InvestorContext(investor_id="bt", risk_class="C2", goal_type="growth", horizon_years=5)
    as_of = prices.index[260].date()
    model = agent.prepare_market_model(context, as_of)
    assert model.data_quality.last_date == as_of
    assert model.returns_all.index.max().date() == as_of
