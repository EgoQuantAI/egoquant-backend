"""The Risk Game sits inside the risk category (docs/RISK_GAME_BLEND.md), so Agent B must not use the game's two scores a second
time as a tighter limit: BehaviorConfig.game_in_profile ignores `game_result` and `loss_acceptance`; the Crash Test and the trade
log are used exactly as before. End to end with the real Agent A, like tests/agent_b/test_loss_acceptance.py.
"""

import pytest

from egoquant.agents.behavioral.models import BehaviorConfig, CrashTestResult, LossAcceptance
from egoquant.demo.synthetic_investors import build_synthetic_data, eckel_grossman
from egoquant.models import DEFAULT_RISK_CLASS_POLICIES
from tests.agent_b.test_behavioral import run_pair, with_data

PLAYED = "2026-09-20T10:00:00"                          # before AS_OF (2026-09-30)
IN_PROFILE = BehaviorConfig(game_in_profile=True)


@pytest.fixture(scope="module")
def synthetic():
    return build_synthetic_data(seed=11)


def loss_acceptance(score=10):
    return LossAcceptance(score=score, rounds_after_loss=3, timestamp=PLAYED)


def crash(sold_at=0.25):
    return CrashTestResult(sold_at_drawdown=sold_at, sold_fraction=1.0, max_drawdown_seen=0.30, timestamp=PLAYED)


def loss_limit(report):
    return next((c for c in report.constraints if c.type == "max_stress_loss"), None)


def evidence_kinds(report):
    return {e.kind for e in report.evidence}


def test_the_default_is_off_and_an_off_flag_leaves_every_hash_where_it_was(synthetic):
    assert BehaviorConfig().game_in_profile is False
    assert "game_in_profile" not in BehaviorConfig().model_dump_json() and "game_in_profile" not in BehaviorConfig().model_dump()
    assert '"game_in_profile":true' in IN_PROFILE.model_dump_json().replace(" ", "")
    assert BehaviorConfig(game_in_profile=False).model_dump_json() == BehaviorConfig().model_dump_json()
    # so the report's input hash (which includes the config) and its evidence references are what they always were
    data = {"panic_seller": synthetic["panic_seller"]}
    _, default = run_pair(data, "panic_seller")
    _, explicit = run_pair(data, "panic_seller", BehaviorConfig(game_in_profile=False))
    assert default.input_hash == explicit.input_hash and default.constraints == explicit.constraints
    _, switched = run_pair(data, "panic_seller", IN_PROFILE)
    assert switched.input_hash != default.input_hash                       # on: the report says it used another configuration


def test_a_cautious_game_is_not_counted_twice_when_it_is_part_of_the_category(synthetic):
    """A C4 newcomer who played very cautiously: before, the game ALSO set a soft class limit; now the category carries it."""
    data = with_data(synthetic["newcomer"], game_result=eckel_grossman(1), loss_acceptance=loss_acceptance(10))
    _, before = run_pair(data, "newcomer")
    assert loss_limit(before) is not None and {"game", "loss_acceptance"} <= evidence_kinds(before)             # the old behaviour
    result, after = run_pair(data, "newcomer", IN_PROFILE)
    assert after.constraints == [] and loss_limit(after) is None
    assert not evidence_kinds(after) & {"game", "loss_acceptance"}
    assert after.game_level is None and after.loss_acceptance_level is None and after.threshold_source is None
    assert "game_part_of_category" in after.flags
    assert any("already part of the risk category" in f and "not counted a second time" in f for f in after.advisor_facts)
    assert result.final.relaxed_constraints == [] and result.final.equity_weight <= DEFAULT_RISK_CLASS_POLICIES["C4"].max_equity + 1e-6


@pytest.mark.parametrize("game, la", [(eckel_grossman(1), None), (None, loss_acceptance(10)), (eckel_grossman(2), loss_acceptance(30))])
def test_either_score_alone_or_both_are_ignored(synthetic, game, la):
    _, report = run_pair(with_data(synthetic["newcomer"], game_result=game, loss_acceptance=la), "newcomer", IN_PROFILE)
    assert report.constraints == [] and not evidence_kinds(report) & {"game", "loss_acceptance"}
    assert "game_part_of_category" in report.flags and report.status == "insufficient_data"       # nothing else to go on


def test_the_crash_test_still_counts_and_is_the_only_game_evidence_left(synthetic):
    data = with_data(synthetic["newcomer"], game_result=eckel_grossman(1), loss_acceptance=loss_acceptance(10),
                     crash_test=crash(0.25))
    _, report = run_pair(data, "newcomer", IN_PROFILE)
    limit = loss_limit(report)
    assert limit is not None and limit.value == pytest.approx(0.25) and not limit.hard
    assert limit.evidence_ref.endswith(":crash_test") and "loss_acceptance" not in limit.evidence_ref
    assert evidence_kinds(report) == {"crash_test"} and report.status == "ok"
    assert "game_part_of_category" in report.flags


def test_the_trade_log_is_used_exactly_as_before(synthetic):
    """Real trading evidence outranks the game and is unaffected by the flag: the same hard limit with and without it."""
    data = with_data(synthetic["panic_seller"], game_result=eckel_grossman(1), loss_acceptance=loss_acceptance(10))
    _, off = run_pair(data, "panic_seller")
    _, on = run_pair(data, "panic_seller", IN_PROFILE)
    limit_off, limit_on = loss_limit(off), loss_limit(on)
    assert limit_off is not None and limit_on is not None and limit_on.hard
    assert limit_on.value == pytest.approx(limit_off.value) and limit_on.confidence == limit_off.confidence
    assert limit_on.evidence_ref.startswith("panic_seller:") and limit_on.evidence_ref.endswith(":capitulation")
    assert on.capitulation_threshold == pytest.approx(off.capitulation_threshold) and on.threshold_source == "real"
    assert "game_part_of_category" in on.flags and not evidence_kinds(on) & {"game", "loss_acceptance"}


def test_without_any_game_data_the_flag_changes_nothing(synthetic):
    for investor in ("steady", "panic_seller", "newcomer"):
        data = {investor: synthetic[investor].model_copy(update={"game_result": None, "loss_acceptance": None})}
        _, off = run_pair(data, investor)
        _, on = run_pair(data, investor, IN_PROFILE)
        assert [(c.type, c.value, c.hard, c.confidence) for c in on.constraints] == [
            (c.type, c.value, c.hard, c.confidence) for c in off.constraints]
        assert on.advisor_facts == off.advisor_facts and on.flags == off.flags
        assert "game_part_of_category" not in on.flags


def test_a_game_played_after_the_decision_date_is_still_ignored_the_old_way_first(synthetic):
    """The date rule runs before the flag: a result from the future is dropped with its own flag, not with the new one."""
    late = "2026-10-05T10:00:00"
    data = with_data(synthetic["newcomer"], game_result=eckel_grossman(1, played=late), loss_acceptance=None)
    _, report = run_pair(data, "newcomer", IN_PROFILE)
    assert "game_after_as_of_ignored" in report.flags and "game_part_of_category" not in report.flags
