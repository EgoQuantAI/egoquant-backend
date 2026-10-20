"""Portfolio method v2, stage 4: choose among the simulated candidates (docs/PORTFOLIO_V2.md section 2.3).

Rule (Savas, 19 Oct): among the candidates whose simulated chance of reaching the goal is within
    tau = max(1 point, min(3 points, 10% of the best chance))
of the best chance, take the one with the lowest worst-10% drawdown, i.e. the 90th percentile of the per-path maximum
drawdown (PortfolioOutcome.drawdown_p90, already tracked in the streamed month loop of goal.py). Ties: larger effective N,
then lower volatility, then lower menu index. Where at least one contender meets the diversification floor (effective N),
only the contenders that meet it are considered; otherwise all are (and the report says the floor was not met).
Pure functions; no randomness.
"""

from __future__ import annotations

MAX_HOLDINGS = 25              # PROVISIONAL: more positions than this is not a practical portfolio (equal weight over 100 funds)
MIN_POSITION = 0.005           # a weight above this counts as a holding
DRAWDOWN_RESOLUTION = 0.01     # PROVISIONAL: drawdowns closer than one point are a tie (Monte Carlo noise), so diversification decides
TOLERANCE_MIN = 0.01
TOLERANCE_MAX = 0.03
TOLERANCE_SHARE = 0.10


def tolerance(best: float) -> float:
    return max(TOLERANCE_MIN, min(TOLERANCE_MAX, TOLERANCE_SHARE * best))


def select_goal(outcomes, vols: dict[int, float], effective_n: dict[int, float], floor: float | None = None,
                n_holdings: dict[int, int] | None = None):
    """-> (outcomes best first, reason text, info dict). `outcomes` are goal.PortfolioOutcome objects."""
    best = max(o.prob_goal for o in outcomes)
    tau = tolerance(best)
    contenders = [o for o in outcomes if o.prob_goal >= best - tau - 1e-12]
    practical = [o for o in contenders if (n_holdings or {}).get(o.menu_index, 0) <= MAX_HOLDINGS] or contenders
    floor_met = floor is None or any(effective_n.get(o.menu_index, 0.0) >= floor for o in practical)
    pool = [o for o in practical if floor is None or effective_n.get(o.menu_index, 0.0) >= floor] if floor_met else practical
    def order(o):
        return (round(o.drawdown_p90 / DRAWDOWN_RESOLUTION), -effective_n.get(o.menu_index, 0.0), vols.get(o.menu_index, 0.0),
                o.menu_index)

    pool.sort(key=order)
    chosen = pool[0]
    others = [o for o in contenders if o is not chosen]
    others.sort(key=order)
    rest = [o for o in outcomes if o not in contenders]
    rest.sort(key=lambda o: (-o.prob_goal, o.drawdown_p90, o.menu_index))
    ranked = [chosen] + others + rest
    reason = (f"Best chance of reaching the goal among the approved portfolios: {best:.1%}. {len(contenders)} portfolio"
              f"{'s' if len(contenders) != 1 else ''} had a chance within {tau * 100:.1f} point{'s' if tau * 100 != 1 else ''} of it; "
              f"of those, this one ({chosen.prob_goal:.1%}) had the smallest fall from a peak in the worst 10% of simulated futures "
              f"({chosen.drawdown_p90:.1%}).")
    info = {"v2_tolerance_points": float(tau * 100), "v2_n_contenders": float(len(contenders)),
            "v2_worst10_drawdown": float(chosen.drawdown_p90), "v2_floor_met": float(1.0 if floor_met else 0.0)}
    return ranked, reason, info
