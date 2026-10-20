"""Portfolio method v2 for the path WITHOUT a goal (QuantAgent.finalize_proposal): the investor's class target stock share is
kept, but the portfolio is no longer the single minimum-variance solution. At the first feasible level (the class target, stepped
down only if the limits require it) every v2 style is solved and the portfolio with the largest effective number of holdings
wins (ties: lower volatility). Agent B's limits are part of `constraints`, so they bind exactly as before.
"""

from __future__ import annotations

import numpy as np

from egoquant.agents.quant import core as qc
from egoquant.agents.quant.v2 import candidates as v2cand
from egoquant.agents.quant.v2 import selection as v2sel


def solve_final(agent, model, context, constraints, start_level: float):
    """-> (SolveResult | None, relaxed list, used level). Same contract as QuantAgent._search_equity_level.
    SolveResult.message carries 'v2:<style>:<n_draws>' for the hook to read."""
    cfg = agent.config
    step = cfg.equity_step_down
    n_steps = int(np.floor(start_level / step + 1e-9))
    levels = [max(start_level - k * step, 0.0) for k in range(n_steps + 1)]
    if levels[-1] > 1e-9:
        levels.append(0.0)
    axis = v2cand.Axis(agent, context, model, constraints, force_stocks=True)
    floor = v2cand.EFFECTIVE_N_FLOOR if agent._policy(context).v2_max_fund_weight is not None else 0.0
    mu = v2cand.prior_returns(agent, model, context)
    for level in levels:
        best = None
        for style, result, relaxed, dropped, note in v2cand.solutions_at_level(agent, model, context, constraints, axis,
                                                                              level, mu):
            if result is None or result.weights is None:
                continue
            w = result.weights
            utility = float(mu @ w - 0.5 * v2cand.RISK_AVERSION * (w @ model.cov_annual @ w))     # Black-Litterman utility
            ok_n = v2cand.effective_n(w) >= floor
            ok_h = int(np.sum(w > v2sel.MIN_POSITION)) <= v2sel.MAX_HOLDINGS
            key = (0 if (ok_n and ok_h) else 1 if ok_h else 2, -utility, style)    # diversified and practical first, then best utility
            if best is None or key < best[0]:
                best = (key, style, result, relaxed)
        if best is not None:
            _, style, result, relaxed = best
            tagged = qc.SolveResult(result.weights, result.status, result.binding, f"v2:{style}:{v2cand.N_RESAMPLE}")
            return tagged, relaxed, level
    return None, [], start_level
