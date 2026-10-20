"""The goal section of the investor's report: levers, period checks, wording and the HTML page.

Run with:  pytest -v tests/system/test_goal_report.py      (SYNTHETIC prices; no internet needed)
"""

import json

import pytest

from egoquant.agents.quant import goal as gq
from egoquant.report import _chance, _in_100, _round_money, render_html
from tests.helpers import make_answers
from tests.system.test_goal_based import _analyze, _history, goal, run

RISKY_ONLY = [(0, "risky", 1.0, {"RISKY": 1.0})]
FEES = {"RISKY": 0.001, "CASH": 0.0}


def _risky(spec, n_paths=3000):
    """One candidate only, so the chosen portfolio is fixed whatever the spec."""
    return gq.analyze_goal(_history(), RISKY_ONLY, FEES, {0: 0.16}, spec, n_paths=n_paths, block_days=21, seed=7, tie=0.01)


HARD = gq.GoalSpec(start_amount=10_000, monthly_flow=50, goal_amount=16_000, horizon_years=8)


# ==========================================================================
# 1. Levers: one input changed, the same portfolio, the same simulated futures
# ==========================================================================
def test_monthly_lever_is_the_exact_chance_of_that_monthly_amount():
    base = _risky(HARD)
    assert 0.05 < base.outcomes[0].prob_goal < 0.75            # a goal the levers can help with
    monthly = [lv for lv in base.levers if lv.kind == "monthly"]
    assert monthly and all(lv.target > base.outcomes[0].prob_goal for lv in monthly)
    for lv in monthly:
        assert lv.value > HARD.monthly_flow and lv.value % gq.LEVER_ROUND == 0
        assert lv.prob_goal >= lv.target
        # Re-running the whole simulation with that monthly amount gives exactly the lever's chance (same draws).
        again = _risky(HARD.model_copy(update={"monthly_flow": lv.value}))
        assert again.outcomes[0].prob_goal == pytest.approx(lv.prob_goal)


def test_monthly_lever_is_close_to_the_smallest_amount_that_works():
    base = _risky(HARD)
    for lv in (x for x in base.levers if x.kind == "monthly"):
        lower = _risky(HARD.model_copy(update={"monthly_flow": lv.value - 2 * gq.LEVER_ROUND}))
        assert lower.outcomes[0].prob_goal < lv.target          # 20 dollars less no longer reaches the target


def test_horizon_and_goal_levers():
    base = _risky(HARD)
    p = base.outcomes[0].prob_goal
    horizons = [lv for lv in base.levers if lv.kind == "horizon"]
    assert [lv.value for lv in horizons] == [HARD.horizon_years + y for y in gq.LEVER_EXTRA_YEARS]
    assert all(lv.prob_goal >= p for lv in horizons)            # a growing portfolio: more time helps here
    goals = [lv for lv in base.levers if lv.kind == "goal"]
    assert goals and all(lv.value < HARD.goal_amount and lv.prob_goal >= lv.target for lv in goals)


def test_no_levers_when_the_goal_is_already_near_certain():
    easy = _risky(gq.GoalSpec(start_amount=10_000, monthly_flow=0, goal_amount=3_000, horizon_years=5))
    assert easy.outcomes[0].prob_goal > gq.LEVER_TARGETS[-1] + 0.01 and easy.levers == []


def test_withdrawal_lever_takes_out_less():
    spec = gq.GoalSpec(start_amount=10_000, monthly_flow=-60, goal_amount=8_000, horizon_years=6)
    base = _risky(spec)
    monthly = [lv for lv in base.levers if lv.kind == "monthly"]
    assert monthly and all(lv.value > spec.monthly_flow for lv in monthly)


def test_period_checks_split_the_history_and_std_error():
    a = _analyze(HARD)
    history = _history()
    assert len(a.period_checks) == 2
    older, newer = a.period_checks
    assert older.start == str(history.index[0].date()) and newer.end == str(history.index[-1].date())
    assert older.start < older.end and newer.start < newer.end and older.start < newer.start
    assert all(0 <= c.prob_goal <= 1 for c in a.period_checks)
    p = next(o.prob_goal for o in a.outcomes if o.menu_index == a.chosen_index)
    assert a.std_error == pytest.approx((p * (1 - p) / a.n_paths) ** 0.5)
    assert _analyze(HARD).model_dump() == a.model_dump()       # still deterministic


# ==========================================================================
# 2. Wording helpers: no false precision, never a misleading 0% or 100%
# ==========================================================================
@pytest.mark.parametrize("p, text", [(0.0, "0%"), (0.003, "less than 1%"), (0.734, "73%"), (0.996, "more than 99%"),
                                     (1.0, "100%")])
def test_chance_text(p, text):
    assert _chance(p) == text


@pytest.mark.parametrize("p, icons", [(0.0, 0), (0.004, 1), (0.734, 73), (0.996, 99), (1.0, 100)])
def test_icons(p, icons):
    assert _in_100(p) == icons


@pytest.mark.parametrize("x, rounded", [(282_779, 283_000), (1_234_567, 1_230_000), (98_512, 98_500), (987, 990)])
def test_money_is_rounded_to_three_figures(x, rounded):
    assert _round_money(x) == rounded


# ==========================================================================
# 3. The report: structured section and HTML page
# ==========================================================================
@pytest.fixture(scope="module")
def goal_run():
    answers = make_answers(gl_score=30)
    return run(answers, goal=goal(answers, multiple=1.6, monthly=100.0, years=12.0))


def test_report_carries_the_goal_section(goal_run):
    g, gr = goal_run.goal_analysis, goal_run.report.goal
    chosen = next(o for o in g.outcomes if o.menu_index == g.chosen_index)
    assert gr is not None and gr.prob_goal == chosen.prob_goal
    assert gr.in_100 == _in_100(chosen.prob_goal) and gr.chance == _chance(chosen.prob_goal)
    assert f"{gr.in_100} of every 100 simulated futures" in gr.headline or chosen.prob_goal in (0.0, 1.0)
    assert [r.label for r in gr.outcomes] == ["1 in 10 futures ends below", "Half end above", "1 in 10 ends above"]
    assert gr.severity[0].label == "If it goes badly" and len(gr.method) >= 5 and len(gr.choice) == 3
    assert len(gr.levers) == len(g.levers) and len(gr.shortlist) == len(g.outcomes)
    assert sum(r.chosen for r in gr.shortlist) == 1
    assert "synthetic (invented)" in gr.method[0].text and "includes" not in gr.method[0].text
    assert any("hypothetical" in d and "not guarantees" in d for d in gr.disclosures)
    assert any("vary with each use and over time" in d for d in gr.disclosures)
    json.dumps(goal_run.report.model_dump(mode="json"))             # the API sends it as JSON


def test_html_report_shows_the_goal_section(goal_run):
    page = render_html(goal_run.report)
    for heading in ("Your goal:", "Where you could end up", "How bad could a miss be?", "How we calculated this",
                    "How this portfolio was chosen", "About these numbers"):
        assert heading in page
    if goal_run.report.goal.levers:
        assert "What would change your odds" in page
    assert page.count('class="ico-y"') + page.count('class="ico-n"') == 100
    assert page.count('class="ico-y"') == goal_run.report.goal.in_100
    assert page.index("Your goal:") < page.index("What you would hold")   # right after the summary
    assert "simulated chance of reaching" in page


def test_withdrawal_plan_reports_money_running_out():
    answers = make_answers(gl_score=30)
    out = run(answers, goal=goal(answers, multiple=0.5, monthly=-answers.amount_invested / 60, years=8.0))
    labels = [r.label for r in out.report.goal.severity]
    assert "Money runs out" in labels


def test_no_goal_no_goal_section():
    out = run(make_answers(gl_score=30), goal=None)
    assert out.report.goal is None and "Your goal:" not in render_html(out.report)


# ==========================================================================
# 4. The Advisor chat gets the same goal wording and levers the page shows
# ==========================================================================
def test_advisor_facts_carry_the_levers_and_uncertainty(goal_run, monkeypatch):
    from egoquant.advisor import build_portfolio_facts, suggested_questions
    from egoquant.advisor.context import MAX_TOKEN_LENGTH, open_context, seal_context
    from egoquant_routes import _goal_summary

    out = {"approved": True, "report": goal_run.report.model_dump(mode="json"), "goal": _goal_summary(goal_run)}
    facts = build_portfolio_facts(json.loads(json.dumps(out, default=str)))["goal"]
    gr = goal_run.report.goal
    assert facts["chance_as_shown"] == gr.chance and facts["headline"] == gr.headline
    assert [(lv["change"], lv["chance"]) for lv in facts["levers_what_would_change_the_odds"]] == \
        [(lv.change, lv.chance) for lv in gr.levers][:8]
    assert len(facts["uncertainty"]["by_history_half"]) == 2 and facts["uncertainty"]["precision_note"]
    assert [x["label"] for x in facts["outcomes_by_frequency"]] == [x.label for x in gr.outcomes]
    portfolio = {"goal": facts, "holdings": []}
    if gr.levers:
        assert "what could I change to improve my chances?" in suggested_questions(None, portfolio)[0]
    monkeypatch.setenv("EGOQUANT_CONTEXT_SECRET", "test-secret")
    token = seal_context({"goal": facts})
    assert len(token) < MAX_TOKEN_LENGTH and open_context(token)["goal"] == json.loads(json.dumps(facts))


def test_advisor_goal_facts_without_the_report_section():
    from egoquant.advisor.context import _goal_facts
    facts = _goal_facts({"levers": [{"kind": "monthly", "value": 470.0, "prob_goal": 0.9012, "target": 0.9}],
                         "period_checks": [], "std_error": 0.004})
    assert facts["levers_what_would_change_the_odds"] == [{"kind": "monthly", "value": 470.0, "prob_goal": 0.9012}]
    assert "chance_as_shown" not in facts
