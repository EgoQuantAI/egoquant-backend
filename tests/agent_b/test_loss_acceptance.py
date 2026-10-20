"""Loss acceptance from the app's Risk Game -> Agent B, as a TIGHTEN-ONLY signal (end to end with the real Agent A)."""

import pytest
from pydantic import ValidationError

from egoquant.agents.behavioral.models import BehaviorConfig, LossAcceptance
from egoquant.demo.synthetic_investors import build_synthetic_data, eckel_grossman
from egoquant.models import DEFAULT_RISK_CLASS_POLICIES
from tests.agent_b.test_behavioral import SCENARIO, feature, run_pair, with_data

PLAYED = "2026-09-20T10:00:00"                     # before AS_OF (2026-09-30)


@pytest.fixture(scope="module")
def synthetic():
    return build_synthetic_data(seed=11)


def la(score: float, rounds: int | None = 3, played: str = PLAYED) -> LossAcceptance:
    return LossAcceptance(score=score, rounds_after_loss=rounds, timestamp=played)


def newcomer(synthetic, score=None, game=None, **kw):
    """The C4 newcomer: no trading logs. By default no Eckel-Grossman game either, only loss acceptance."""
    data = with_data(synthetic["newcomer"], game_result=game,
                     loss_acceptance=la(score, **kw) if score is not None else None)
    return run_pair(data, "newcomer")


def loss_limit(report):
    return next((c for c in report.constraints if c.type == "max_stress_loss"), None)


@pytest.mark.parametrize("score, level, limit_class", [(10, 1, "C3"), (30, 2, "C3")])   # capped one class down
def test_retreating_after_losses_sets_a_soft_class_limit(synthetic, score, level, limit_class):
    result, report = newcomer(synthetic, score)                              # C4: level 1 or 2 is 2+ below
    limit = loss_limit(report)
    assert limit is not None and not limit.hard and limit.confidence == pytest.approx(0.55)
    assert limit.value == pytest.approx(DEFAULT_RISK_CLASS_POLICIES[limit_class].max_stress_loss)
    assert limit.evidence_ref.endswith(":loss_acceptance") and limit.source == "behavioral"
    assert report.loss_acceptance_level == level and report.threshold_source == "game"
    assert "loss_acceptance_more_cautious_than_class" in report.flags
    assert feature(report, "loss_acceptance_level").value == level
    assert any("safer bets after losing rounds" in f for f in report.advisor_facts)
    assert -result.final.stress[SCENARIO] <= limit.value + 1e-6 and result.final.relaxed_constraints == []


def test_one_level_below_the_class_is_only_a_flag(synthetic):
    _, report = newcomer(synthetic, 45)                                      # level 3 vs C4
    assert report.constraints == [] and "loss_acceptance_slightly_more_cautious" in report.flags


@pytest.mark.parametrize("score", [50, 70, 100])
def test_neutral_or_bold_scores_never_set_or_loosen_a_limit(synthetic, score):
    result, report = newcomer(synthetic, score)
    assert report.constraints == []
    assert result.final.equity_weight <= DEFAULT_RISK_CLASS_POLICIES["C4"].max_equity + 1e-6


def test_the_apps_default_50_never_tightens_even_a_c5(synthetic):
    """The app reports 50 when no losing round was played. Even for C5 (level 3 is 2 below) it must not act."""
    data = with_data(synthetic["newcomer"], game_result=None, loss_acceptance=la(50, rounds=None))
    _, report = run_pair(data, "newcomer", risk_class="C5")
    assert report.risk_class == "C5" and report.loss_acceptance_level == 3
    assert report.constraints == []
    _, lower = run_pair(with_data(synthetic["newcomer"], game_result=None, loss_acceptance=la(49)), "newcomer",
                        risk_class="C5")
    assert loss_limit(lower) is not None                                     # just below neutral does act on C5


def test_a_score_without_losing_rounds_is_ignored(synthetic):
    _, report = newcomer(synthetic, 0, rounds=0)
    assert report.constraints == [] and report.loss_acceptance_level is None
    assert "loss_acceptance_without_losing_rounds" in report.flags and report.status == "insufficient_data"


def test_game_and_loss_acceptance_together_take_the_more_cautious_level(synthetic):
    _, report = newcomer(synthetic, 10, game=eckel_grossman(2))              # game level 2, loss acceptance level 1
    limit = loss_limit(report)
    assert limit.value == pytest.approx(DEFAULT_RISK_CLASS_POLICIES["C3"].max_stress_loss)   # C1, capped to C3
    assert not limit.hard and limit.confidence == pytest.approx(0.60)
    assert limit.evidence_ref.endswith(":game+loss_acceptance")
    assert report.game_level == 2 and report.loss_acceptance_level == 1


def test_a_neutral_score_leaves_the_game_rule_unchanged(synthetic):
    _, alone = newcomer(synthetic, game=eckel_grossman(1))
    _, both = newcomer(synthetic, 60, game=eckel_grossman(1))
    assert [(c.value, c.confidence, c.hard) for c in both.constraints] == \
           [(c.value, c.confidence, c.hard) for c in alone.constraints]
    assert loss_limit(both).evidence_ref.endswith(":game")


def test_real_history_outranks_loss_acceptance(synthetic):
    data = with_data(synthetic["steady"], loss_acceptance=la(0))             # held through crashes
    _, report = run_pair(data, "steady")
    assert report.constraints == [] and "loss_acceptance_overridden_by_trading_history" in report.flags


def test_played_after_as_of_is_ignored_unless_live_advice_allows_it(synthetic):
    data = with_data(synthetic["newcomer"], game_result=None, loss_acceptance=la(10, played="2026-10-01T09:00:00"))
    _, strict = run_pair(data, "newcomer")
    assert "loss_acceptance_after_as_of_ignored" in strict.flags and strict.constraints == []
    _, live = run_pair(data, "newcomer", BehaviorConfig(accept_game_after_as_of=True))
    assert loss_limit(live) is not None


@pytest.mark.parametrize("score", range(0, 101, 10))
def test_only_ever_tightens(synthetic, score):
    """Whatever the score: no limit above the class's own, and never a hard one."""
    _, report = newcomer(synthetic, score)
    for c in report.constraints:
        assert not c.hard and c.value <= DEFAULT_RISK_CLASS_POLICIES["C4"].max_stress_loss + 1e-9


def test_loss_acceptance_validation():
    with pytest.raises(ValidationError):
        LossAcceptance(score=101, timestamp=PLAYED)
    with pytest.raises(ValidationError):
        LossAcceptance(score=10, timestamp="yesterday")
    with pytest.raises(ValidationError):
        BehaviorConfig(loss_acceptance_bands=[(50.0, 1), (40.0, 2)])
    with pytest.raises(ValidationError):
        BehaviorConfig(loss_acceptance_bands=[(50.0, 1), (90.0, 2)])            # must reach 100
    cfg = BehaviorConfig()
    assert [cfg.loss_acceptance_level(s) for s in (0, 20, 21, 50, 80, 100)] == [1, 1, 2, 3, 4, 5]
