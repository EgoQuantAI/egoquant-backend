"""A trader who chose crypto and may hold it gets crypto in the advice, for sure; nobody else does.

SYNTHETIC prices; no network.
"""

from datetime import date

import pytest

from egoquant.agents.market_intel import SyntheticPriceSource
from egoquant.demo.goal_walkthrough import default_goal
from egoquant.demo.personas import PERSONAS, behavioral_agent_for
from egoquant.models import AgentConfig
from egoquant.pipeline import run_pipeline
from egoquant.preferences import PreferenceAnswers, load_preference_bank

AS_OF = date(2026, 10, 16)
FLOOR = AgentConfig().interest_crypto_floor


def _run(persona, goal=False, preferences=None, config=None):
    p = PERSONAS[persona]
    return run_pipeline(p.answers(), investor_id=p.investor_id, interest_areas=None,
                        preferences=preferences or p.preference_answers(), goal_type=p.goal_type,
                        horizon_years=p.horizon_years, as_of=AS_OF, trading_platforms=p.trading_platforms,
                        behavioral_agent=behavioral_agent_for(p), price_source=SyntheticPriceSource(),
                        goal=default_goal(persona) if goal else None, agent_config=config,
                        min_cash_buffer=p.min_cash_buffer, liquidity_need=p.liquidity_need)


def _crypto(run, weights=None):
    inst = run.agent_c.instruments
    return sum(w for t, w in (weights or run.result.final.weights).items() if inst[t].asset_class == "crypto")


@pytest.mark.parametrize("goal", [False, True])
def test_a_crypto_trader_always_gets_crypto(goal):
    run = _run("crypto_trader", goal=goal)
    assert run.approved
    assert _crypto(run) >= FLOOR - 1e-6
    assert all(_crypto(run, p.weights) >= FLOOR - 1e-6 for p in run.result.menu.portfolios)   # every candidate
    assert not any("min_class_weight" in c for c in run.result.final.relaxed_constraints)    # not dropped


def test_the_floor_never_goes_above_the_class_cap():
    run = _run("crypto_trader", goal=True, config=AgentConfig(interest_crypto_floor=0.5))
    cap = AgentConfig().risk_class_policies["C5"].max_crypto
    assert run.approved and cap - 1e-6 <= _crypto(run) <= cap + 1e-6


def test_no_crypto_for_an_investor_whose_knowledge_blocks_it():
    run = _run("eager_beginner", goal=True)
    assert _crypto(run) == 0.0
    final = run.result.final
    assert not any("min_class_weight" in c for c in final.binding_constraints + final.relaxed_constraints)


def test_no_crypto_floor_when_the_trader_did_not_choose_crypto():
    bank = load_preference_bank()
    stocks_only = PreferenceAnswers(preferences_version=bank.preferences_version,
                                    choices={"P1": ["stocks", "gold"], "P2": ["technology"], "P4": ["individual"]})
    run = _run("crypto_trader", goal=True, preferences=stocks_only)
    assert _crypto(run) == 0.0
    final = run.result.final
    assert not any("min_class_weight" in c for c in final.binding_constraints + final.relaxed_constraints)
