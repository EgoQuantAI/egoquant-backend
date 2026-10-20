"""goal.stream_wealth: the goal simulation without wealth matrices gives exactly the matrix version's numbers."""

from __future__ import annotations

import numpy as np
import pytest

from egoquant.agents.quant import goal as gq


@pytest.fixture(scope="module")
def setup():
    rng = np.random.default_rng(3)
    blocks = 1.0 + rng.normal(0.004, 0.06, size=(400, 3))
    blocks[17] = 0.02                                             # a crash month: paths can run out of money
    draws = rng.integers(0, len(blocks), size=(3_000, 120))
    extra = rng.integers(0, len(blocks), size=(3_000, 36))
    return blocks, np.array([0.5, 0.3, 0.2]), np.array([0.001, 0.003, 0.0]), draws, extra


@pytest.mark.parametrize("flow", [500.0, 0.0, -900.0])           # contributions, none, withdrawals
def test_stream_equals_the_matrix_simulation(setup, flow):
    blocks, w, fees, draws, extra = setup
    spec = gq.GoalSpec(start_amount=50_000, monthly_flow=flow, goal_amount=120_000, horizon_years=10)
    wealth = gq.simulate_wealth(blocks, w, fees, spec, draws)
    rows = np.array([0, 7, 2_999])
    s = gq.stream_wealth(blocks, w, fees, spec, draws, keep_months=[0, 12, 120], keep_rows=rows)
    assert np.array_equal(s.final, wealth[:, -1])
    assert np.array_equal(s.depleted, (wealth[:, 1:] <= 0).any(axis=1))
    assert np.array_equal(s.drawdowns, gq.max_drawdowns(wealth))
    assert all(np.array_equal(s.columns[m], wealth[:, m]) for m in (0, 12, 120))
    assert np.array_equal(s.rows, wealth[rows])
    if flow < 0:
        assert s.depleted.any()                                   # the withdrawal case does run out


def test_extra_months_continue_the_paths_like_hstack(setup):
    blocks, w, fees, draws, extra = setup
    spec = gq.GoalSpec(start_amount=50_000, monthly_flow=300, goal_amount=120_000, horizon_years=13)
    wealth = gq.simulate_wealth(blocks, w, fees, spec, np.hstack([draws, extra]))
    s = gq.stream_wealth(blocks, w, fees, spec, draws, extra)
    assert np.array_equal(s.final, wealth[:, -1]) and np.array_equal(s.drawdowns, gq.max_drawdowns(wealth))
