"""The app's Crash Test -> Agent B: a hypothetical capitulation point when there is no trading history.

Tighten-only, never hard, at most one class below the questionnaire class (end to end with the real Agent A).
"""

import pytest
from pydantic import ValidationError

from egoquant.agents.behavioral.models import BehaviorConfig, CrashTestResult, LossAcceptance
from egoquant.demo.synthetic_investors import build_synthetic_data, eckel_grossman
from egoquant.models import DEFAULT_RISK_CLASS_POLICIES
from tests.agent_b.test_behavioral import SCENARIO, feature, run_pair, with_data

PLAYED = "2026-09-20T10:00:00"                     # before AS_OF (2026-09-30)
LIMIT = {k: p.max_stress_loss for k, p in DEFAULT_RISK_CLASS_POLICIES.items()}   # C3 0.20, C4 0.35


@pytest.fixture(scope="module")
def synthetic():
    return build_synthetic_data(seed=11)


def ct(sold_at=None, fraction=0.0, deepest=0.35, played=PLAYED) -> CrashTestResult:
    return CrashTestResult(sold_at_drawdown=sold_at, sold_fraction=fraction, max_drawdown_seen=deepest,
                           timestamp=played)


def newcomer(synthetic, crash, **kw):
    """The C4 newcomer (no trading logs) with only a crash test: no Eckel-Grossman game, no loss acceptance."""
    return run_pair(with_data(synthetic["newcomer"], game_result=None, crash_test=crash), "newcomer", **kw)


def loss_limit(report):
    return next((c for c in report.constraints if c.type == "max_stress_loss"), None)


def test_selling_in_the_fall_sets_a_soft_limit_at_that_loss(synthetic):
    result, report = newcomer(synthetic, ct(sold_at=0.25, fraction=1.0))         # C4 limit 35% -> sold at 25%
    limit = loss_limit(report)
    assert limit is not None and not limit.hard and limit.confidence == pytest.approx(0.55)
    assert limit.value == pytest.approx(0.25) and limit.evidence_ref.endswith(":crash_test")
    assert report.crash_test_threshold == pytest.approx(0.25) and report.threshold_source == "game"
    assert report.capitulation_threshold is None                                 # real-only field stays empty
    assert feature(report, "crash_test_sell_drawdown").value == pytest.approx(0.25)
    assert "crash_test_sold_below_class_limit" in report.flags
    assert any("the loss at which they sold" in f for f in report.advisor_facts)
    assert -result.final.stress[SCENARIO] <= limit.value + 1e-6 and result.final.relaxed_constraints == []


def test_an_early_sale_is_capped_one_class_down(synthetic):
    _, report = newcomer(synthetic, ct(sold_at=0.05, fraction=1.0))              # would be 5%; cap = C3's 20%
    assert loss_limit(report).value == pytest.approx(LIMIT["C3"])
    assert "game_limit_capped_one_class" in report.flags


def test_selling_after_the_class_limit_changes_nothing(synthetic):
    _, report = newcomer(synthetic, ct(sold_at=0.36, fraction=1.0, deepest=0.36))   # C4 already caps at 35%
    assert report.constraints == [] and "crash_test_sold_above_class_limit" in report.flags


@pytest.mark.parametrize("crash, flag", [(ct(), "crash_test_held_through"),
                                         (ct(sold_at=0.2, fraction=0.2), "crash_test_small_sale")])
def test_holding_on_or_a_small_sale_never_sets_or_loosens_a_limit(synthetic, crash, flag):
    result, report = newcomer(synthetic, crash)
    assert report.constraints == [] and flag in report.flags and report.status == "ok"
    assert result.final.equity_weight <= DEFAULT_RISK_CLASS_POLICIES["C4"].max_equity + 1e-6


def test_the_most_cautious_game_signal_wins(synthetic):
    data = with_data(synthetic["newcomer"], game_result=eckel_grossman(3),     # game level 3: only 1 below, no act
                     loss_acceptance=LossAcceptance(score=10, rounds_after_loss=3, timestamp=PLAYED),
                     crash_test=ct(sold_at=0.25, fraction=1.0))                  # LA level 1 -> capped C3 (20%)
    _, report = run_pair(data, "newcomer")
    limit = loss_limit(report)
    assert limit.value == pytest.approx(LIMIT["C3"]) and limit.confidence == pytest.approx(0.60)
    assert limit.evidence_ref.endswith(":loss_acceptance+crash_test")


def test_real_history_outranks_the_crash_test(synthetic):
    data = with_data(synthetic["steady"], crash_test=ct(sold_at=0.10, fraction=1.0))   # held through real crashes
    _, report = run_pair(data, "steady")
    assert report.constraints == [] and "crash_test_overridden_by_trading_history" in report.flags


def test_played_after_as_of_is_ignored_unless_live_advice_allows_it(synthetic):
    later = ct(sold_at=0.25, fraction=1.0, played="2026-10-01T09:00:00")
    _, strict = newcomer(synthetic, later)
    assert "crash_test_after_as_of_ignored" in strict.flags and strict.constraints == []
    _, live = newcomer(synthetic, later, config=BehaviorConfig(accept_game_after_as_of=True))
    assert loss_limit(live).value == pytest.approx(0.25)


def test_inconsistent_results_are_rejected():
    with pytest.raises(ValidationError):
        CrashTestResult(sold_fraction=0.5, max_drawdown_seen=0.3, timestamp=PLAYED)            # sold, but when?
    with pytest.raises(ValidationError):
        CrashTestResult(sold_at_drawdown=0.4, sold_fraction=1, max_drawdown_seen=0.3, timestamp=PLAYED)
