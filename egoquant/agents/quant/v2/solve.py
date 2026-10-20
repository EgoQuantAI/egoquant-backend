"""Portfolio method v2: the constrained solve used by every v2 candidate and by the no-goal final portfolio.

It is QuantAgent._solve_with_fallback plus the v2 limits:
  * per-fund cap (RiskClassPolicy.v2_max_fund_weight, 30% for C3 to C5): applied by replacing the investor's max_asset_weight
  * group caps: no group of near-duplicate funds above 25% of the portfolio, and none above 50% of the stock sleeve
    (only for the classes that have a per-fund cap, i.e. C3 to C5)
The group caps are SOFT: if they make a level infeasible they are dropped and the result says so (never an error).
Everything else (class caps, volatility, stress loss, cash need, Agent B's limits) is unchanged and stays hard.
"""

from __future__ import annotations

import numpy as np

from egoquant.agents.quant import core as qc
from egoquant.models import Constraint

GROUP_CAP_PORTFOLIO = 0.25       # PROVISIONAL (docs/PORTFOLIO_V2.md section 3)
GROUP_CAP_STOCK_SLEEVE = 0.50    # PROVISIONAL
SATELLITE_CAP = 0.35             # PROVISIONAL: sector funds and single stocks together, as a share of the stock sleeve
SATELLITE_CAP_WITH_INTEREST = 0.50   # when the investor chose a sector or stocks (their interest tilt needs room)


def v2_constraints(agent, context, model) -> list[Constraint]:
    """The base constraints with the v2 per-fund cap in place of the investor's 40% (never looser than the investor's own)."""
    return cap_constraints(agent, context, agent._base_constraints(context, model))


def cap_constraints(agent, context, constraints: list[Constraint]) -> list[Constraint]:
    cap = agent._policy(context).v2_max_fund_weight
    if cap is None:
        return constraints
    out = []
    for c in constraints:
        if c.type == "max_asset_weight" and not c.asset_id and cap < c.value:
            c = c.model_copy(update={"value": cap, "source": "risk_class"})
        out.append(c)
    return out


def uses_group_caps(agent, context) -> bool:
    return agent._policy(context).v2_max_fund_weight is not None


def is_satellite(asset) -> bool:
    """A sector fund, an industry fund or a single stock: a deliberate bet, not the broad market."""
    return asset.asset_class == "equity" and (asset.sector is not None or asset.product_type == "stock")


def satellite_cap(model, context) -> float:
    chosen = set(context.interest_assets)
    wants = any(is_satellite(a) for a in model.assets if a.asset_id in chosen)
    return SATELLITE_CAP_WITH_INTEREST if wants else SATELLITE_CAP


def group_rows(model, state, level_mask: np.ndarray, context=None) -> list[tuple[str, np.ndarray, float]]:
    """Linear rows (label, a, b) meaning a . w <= b for every near-duplicate group.
    `level_mask` is the mask whose weight is fixed at the level (stocks, or bonds on the bond axis)."""
    rows = []
    classes = np.array(model.asset_classes)
    for k, mask in enumerate(state.group_masks(model.asset_ids)):
        # Groups of near-identical funds are limited where the overlap hides concentration: stocks, crypto and commodities.
        # Bond and cash funds of one kind (IEF, AGG, ...) are one exposure by nature; they only meet the per-fund cap.
        if set(classes[mask]) <= {"bond", "cash"}:
            continue
        m = mask.astype(float)
        rows.append((f"v2:group[{k}]<=portfolio", m, GROUP_CAP_PORTFOLIO))
        sleeve = m * level_mask.astype(float)
        if sleeve.any():                                          # share of the stock sleeve: (group stocks - 0.5 * stocks) . w <= 0
            rows.append((f"v2:group[{k}]<=stock sleeve", sleeve - GROUP_CAP_STOCK_SLEEVE * level_mask.astype(float), 0.0))
    if context is not None:
        sat = np.array([is_satellite(a) for a in model.assets], dtype=float)
        stocks = level_mask.astype(float)
        if sat.any() and stocks.any():                       # satellites <= cap * stock sleeve:  (sat - cap * stocks) . w <= 0
            rows.append(("v2:satellites<=stock sleeve", sat * stocks - satellite_cap(model, context) * stocks, 0.0))
    return rows


def solve_level(agent, model, context, constraints: list[Constraint], level: float, *, level_mask: np.ndarray | None = None,
                **options):
    """-> (SolveResult, relaxed soft constraints, group_caps_dropped). `options` go to qc.solve_portfolio
    (target_weights, expected_returns, risk_aversion, ridge, cov ...)."""
    state = getattr(model, "v2", None)
    mask = model.equity_mask if level_mask is None else level_mask
    want_groups = state is not None and uses_group_caps(agent, context)
    soft = [c for c in constraints if not c.hard]
    active_sets = [(constraints, [])]
    if soft:
        active_sets.append(([c for c in constraints if c.hard], [c.label for c in soft]))
    result = qc.SolveResult(None, "infeasible", [], "no attempt made")
    for with_groups in ((True, False) if want_groups else (False,)):
        for active, relaxed in active_sets:
            inputs = agent._solver_inputs(model, context, active, False)
            if with_groups:
                inputs["linear_rows"] = list(inputs["linear_rows"]) + group_rows(model, state, mask, context)
            inputs["equity_mask"] = mask.astype(float)
            inputs.update(options)
            result = qc.solve_portfolio(equity_target=level, **inputs)
            if result.weights is not None:
                return result, relaxed, want_groups and not with_groups
    return result, [], False
