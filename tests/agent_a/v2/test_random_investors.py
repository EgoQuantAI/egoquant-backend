"""Portfolio method v2, acceptance on random investors (docs/PORTFOLIO_V2.md section 5): seeded random questionnaires,
interests, platforms, horizons and goals go through the WHOLE pipeline (questionnaire -> Agent C -> Agent A -> Agent B ->
Risk Engine -> report) on SYNTHETIC prices, once under v1 and twice under v2. For every investor:

  * the Risk Engine approves under v2 whatever it approved under v1;
  * no hard limit is violated (Agent A's own list is empty, and the weights are checked again here, independently:
    non-negative, sum 1, per-holding cap, 10% per single stock, 30% per fund for C3 to C5);
  * the same input twice gives byte-identical output (proposal and review).

Investors the pipeline itself refuses under v1 (for example an answer set that cannot be scored) are skipped, counted, and
must stay a small minority. The default run is 12 investors (a few minutes: v2 prices ~260 products); the acceptance run is
`V2_RANDOM_INVESTORS=200 pytest tests/agent_a/v2/test_random_investors.py` (about an hour; result in overnight/damla.md).
No network, no LLM.
"""

from __future__ import annotations

import contextlib
import os
from datetime import date

import numpy as np
import pytest

from egoquant.agents.market_intel import SyntheticPriceSource
from egoquant.agents.market_intel.catalog import DEFAULT_CATALOG
from egoquant.agents.market_intel.platforms import PLATFORMS
from egoquant.agents.quant.goal import GoalSpec
from egoquant.models import AgentConfig
from egoquant.pipeline import run_pipeline
from egoquant.risk_profile.models import Answers
from tests.helpers import BANK

N_INVESTORS = int(os.environ.get("V2_RANDOM_INVESTORS", "12"))
FLAG = "EGOQUANT_PORTFOLIO_METHOD"
AS_OF = date(2026, 9, 30)
SKIP_SHARE = 0.25                      # at most this share of the random investors may be refused by the pipeline itself
AREAS = sorted({a for i in DEFAULT_CATALOG for a in i.areas})
KIND = {i.ticker: i for i in DEFAULT_CATALOG}


def random_investor(seed: int) -> dict:
    """Keyword arguments for run_pipeline, drawn from a seeded generator (the same seed is the same investor)."""
    rng = np.random.default_rng(seed)
    choices: dict[str, str] = {}
    for q in BANK.questions:
        if q.id in ("B3_invested", "B3_needed", "K4"):
            continue
        options = [o.id for o in q.options if o.id != "refuse"]
        choices[q.id] = str(rng.choice(options))
    choices["D1"] = "yes"
    k4 = [o.id for o in BANK.get("K4").options]
    picked = [str(x) for x in rng.choice(k4, size=int(rng.integers(1, 4)), replace=False)]
    if "none" in picked:                                       # "none of these" cannot be combined with other options
        picked = ["none"]
    invested = float(np.exp(rng.uniform(np.log(2_000), np.log(2_000_000))))
    needed = float(invested * rng.choice([0.0, 0.0, 0.1, 0.3, 0.6]))
    answers = Answers(questionnaire_version=BANK.questionnaire_version, choices=choices, multi_choices={"K4": picked},
                      amount_invested=round(invested, 2), amount_needed=round(needed, 2))
    horizon = float(rng.choice([2, 5, 8, 10, 15, 20, 30]))
    kwargs = dict(answers=answers, investor_id=f"rand{seed}", goal_type=str(choices["B1"]), horizon_years=horizon,
                  as_of=AS_OF, interest_areas=[str(a) for a in rng.choice(AREAS, size=int(rng.integers(1, 6)), replace=False)],
                  trading_platforms=[str(p) for p in rng.choice(list(PLATFORMS), size=int(rng.integers(0, 3)), replace=False)],
                  min_cash_buffer=float(rng.choice([0.0, 0.02, 0.05, 0.10])),
                  liquidity_need=float(rng.choice([0.0, 0.0, 0.05, 0.15])),
                  agent_config=AgentConfig(goal_paths=1_000, run_sensitivity=False))
    if rng.random() < 0.34:                                   # a third of the investors have a goal
        kwargs["goal"] = GoalSpec(start_amount=answers.amount_invested, monthly_flow=float(rng.choice([0.0, 100.0, 500.0])),
                                  goal_amount=round(answers.amount_invested * float(rng.uniform(1.2, 3.0)), 2),
                                  horizon_years=horizon)
    return kwargs


@contextlib.contextmanager
def method(value: str):
    old = os.environ.get(FLAG)
    os.environ[FLAG] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(FLAG, None)
        else:
            os.environ[FLAG] = old


def run(kwargs: dict, value: str):
    with method(value):
        return run_pipeline(price_source=SyntheticPriceSource(), **kwargs)


def check_hard_limits(out, kwargs) -> None:
    """The limits again, independently of Agent A and of the Risk Engine's own arithmetic."""
    final = out.result.final
    if final.status == "infeasible":
        return
    assert not final.violated_constraints, (kwargs["investor_id"], final.violated_constraints)
    weights = final.weights
    assert min(weights.values()) >= -1e-9 and abs(sum(weights.values()) - 1.0) < 1e-6
    assert max(weights.values()) <= out.context.max_asset_weight + 1e-6
    for ticker, w in weights.items():
        inst = KIND.get(ticker)
        if inst is not None and inst.max_weight is not None:
            assert w <= inst.max_weight + 1e-6, (ticker, w)                            # single stocks: 10%
    if out.profile.c_level in ("C3", "C4", "C5") and final.method.startswith("v2"):
        non_cash = [w for t, w in weights.items() if KIND.get(t) is None or KIND[t].asset_class != "cash"]
        assert max(non_cash, default=0.0) <= 0.30 + 1e-6                               # v2: one fund at most 30%
    for ticker, w in weights.items():
        if w > 1e-9:
            inst = KIND.get(ticker)
            assert inst is None or inst.r_level <= out.profile.max_r_level, (ticker, inst.r_level)


@pytest.fixture(scope="module")
def results():
    return []


@pytest.mark.parametrize("seed", range(N_INVESTORS))
def test_random_investor_under_v2(seed, results):
    kwargs = random_investor(seed)
    try:
        v1 = run(kwargs, "v1")
    except Exception as error:                                                          # refused under v1 as well
        results.append(("skipped", seed, type(error).__name__))
        pytest.skip(f"the pipeline itself refuses this investor under v1: {type(error).__name__}: {str(error)[:80]}")
    v2a = run(kwargs, "v2")
    v2b = run(kwargs, "v2")
    results.append(("run", seed, v1.approved, v2a.approved))

    if v1.approved:
        assert v2a.approved, (seed, v2a.review.violations)                               # v2 never loses an approval
    check_hard_limits(v2a, kwargs)
    assert v2a.result.final.model_dump_json() == v2b.result.final.model_dump_json()   # the same input, the same portfolio
    assert v2a.review.model_dump_json() == v2b.review.model_dump_json()
    assert v1.result.final.method == "min_variance" or v1.result.final.method.startswith("goal_based")   # v1 is v1
    assert not v1.result.final.engine_version.endswith("+pv2")


def test_few_random_investors_were_refused_by_the_pipeline(results):
    ran = [r for r in results if r[0] == "run"]
    skipped = [r for r in results if r[0] == "skipped"]
    assert len(ran) >= (1 - SKIP_SHARE) * N_INVESTORS, (len(ran), skipped)
