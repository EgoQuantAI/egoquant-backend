"""v2 solve_level always asks qc.solve_portfolio, even when the target already meets every constraint.

A shortcut that returned the target itself once existed (949c2f3, reverted 20 Oct). It was not the solver's result: on the
balanced demo scenario the target met every limit and had distance 0, but SLSQP stopped 0.028 away from it and reported
"optimal", so the shortcut moved 3 of 12 blocks of the v2 demo pack (docs/demo_data/demo_numbers_v2.json). The committed
numbers are what the solver returns, so the solver stays the only source of the weights."""

from types import SimpleNamespace

import numpy as np

from egoquant.agents.quant import core as qc
from egoquant.agents.quant.v2 import solve as v2_solve


def test_a_target_that_meets_every_constraint_still_goes_to_the_solver(monkeypatch):
    n = 4
    target = np.array([0.4, 0.3, 0.2, 0.1])
    inputs = dict(cov=np.eye(n) * 0.04, asset_ids=[f"A{i}" for i in range(n)], upper_bounds=np.ones(n),
                  bound_labels=["cap"] * n, linear_rows=[("row", np.ones(n), 1.0)], vol_limits=[("vol", 0.5)],
                  expected_returns=None, risk_aversion=5.0, current_weights=None, trade_cost_rates=np.zeros(n),
                  turnover_penalty=0.0, turnover_cap=None)
    agent = SimpleNamespace(_solver_inputs=lambda model, context, active, flag: dict(inputs))
    model = SimpleNamespace(v2=None, equity_mask=np.array([True, True, False, False]))

    called = []
    answer = qc.SolveResult(np.array([0.41, 0.29, 0.2, 0.1]), "optimal", [])         # deliberately not the target

    def fake_solver(**kwargs):
        called.append(kwargs)
        return answer

    monkeypatch.setattr(qc, "solve_portfolio", fake_solver)
    result, relaxed, groups_dropped = v2_solve.solve_level(agent, model, None, [], 0.7, target_weights=target)

    assert len(called) == 1, "the target met every constraint, but the solver must still decide the weights"
    assert result is answer and relaxed == [] and groups_dropped is False
    assert called[0]["equity_target"] == 0.7 and np.array_equal(called[0]["target_weights"], target)
