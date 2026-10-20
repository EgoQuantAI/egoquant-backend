"""An investor who did not choose stocks gets none (the core world-stock fund is not forced in); with stocks they do.

SYNTHETIC prices; no network.
"""

from datetime import date

import pytest

from egoquant.agents.market_intel import SyntheticPriceSource
from egoquant.demo.goal_walkthrough import default_goal
from egoquant.demo.personas import PERSONAS, behavioral_agent_for
from egoquant.pipeline import run_pipeline
from egoquant.preferences import PreferenceAnswers, load_preference_bank

AS_OF = date(2026, 10, 16)
PERSONA = "crypto_trader"     # C5: up to 90% in stocks


def _run(choices, goal):
    p = PERSONAS[PERSONA]
    bank = load_preference_bank()
    return run_pipeline(p.answers(), investor_id=p.investor_id, interest_areas=None,
                        preferences=PreferenceAnswers(preferences_version=bank.preferences_version, choices=choices),
                        goal_type=p.goal_type, horizon_years=p.horizon_years, as_of=AS_OF,
                        trading_platforms=p.trading_platforms, behavioral_agent=behavioral_agent_for(p),
                        price_source=SyntheticPriceSource(), goal=default_goal(PERSONA) if goal else None,
                        min_cash_buffer=p.min_cash_buffer, liquidity_need=p.liquidity_need)


def _stocks(run):
    inst = run.agent_c.instruments
    return sum(w for t, w in run.result.final.weights.items() if inst[t].asset_class == "equity")


@pytest.mark.parametrize("goal", [False, True])
def test_no_stocks_when_none_were_chosen(goal):
    run = _run({"P1": ["bonds", "gold"]}, goal)
    assert run.approved
    assert _stocks(run) == pytest.approx(0.0, abs=1e-6)
    assert run.context.suitability_max_equity == 0.0
    assert any("did not choose stocks" in b for b in run.report.banners)


def test_stocks_when_chosen():
    run = _run({"P1": ["stocks", "bonds"]}, goal=False)
    assert run.approved and _stocks(run) > 0.05
    assert not any("did not choose stocks" in b for b in run.report.banners)
