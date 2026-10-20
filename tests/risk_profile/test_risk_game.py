"""The Risk Game inside the risk category (docs/RISK_GAME_BLEND.md): the table of the contract, the rules as properties, the
JSON (the game fields exist only when a game was played), the kill switches, the explanation. Run with:
pytest -v tests/risk_profile/test_risk_game.py
"""

import json
import random

import pytest
from pydantic import BaseModel, ValidationError

from egoquant.risk_profile import GameInput, RiskProfile, build_risk_profile, explain
from egoquant.risk_profile import scoring
from egoquant.risk_profile.models import GAME_FIELDS, RiskProfileConfig
from tests.helpers import ASSESSED_AT, BANK, CONFIG, make_answers

GL_FOR_LEVEL = {1: 13, 2: 19, 3: 23, 4: 29, 5: 33}                    # a risk-attitude score inside each band
LOSS_FOR_CAP = {5: 0, 4: 10_000, 3: 8_000, 2: 4_000, 1: 2_000}         # money NOT needed back of 50,000: loss capacity 100/20/16/8/4%
NEEDED_FOR_CAP = {cap: 50_000 - lost if cap < 5 else 0 for cap, lost in
                  ((5, 0), (4, 10_000), (3, 8_000), (2, 4_000), (1, 2_000))}
GAME_FOR_LEVEL = {1: 10, 2: 30, 3: 50, 4: 70, 5: 90}                   # a game score inside each band
PRE_GAME_KEYS = {"tolerance_score", "tolerance_level", "capacity_level", "capacity_binding_question", "loss_capacity",
                 "knowledge_score", "product_access", "no_loss_gate", "c_level", "lowest_category", "binding_dimension",
                 "max_r_level", "suitability_max_equity", "flags", "questionnaire_version", "assessed_at", "valid_until",
                 "answers_hash"}


def answers_for(t=3, cap=5, **kw):
    needed = {5: 0, 4: 40_000, 3: 42_000, 2: 46_000, 1: 48_000}[cap]
    return make_answers(gl_score=GL_FOR_LEVEL[t], invested=50_000, needed=needed, **kw)


def game_of(level, loss=None):
    return GameInput(tolerance=GAME_FOR_LEVEL[level], loss_acceptance=loss)


def build(t=3, cap=5, game_level=None, **kw):
    options = {k: kw.pop(k) for k in ("game_weight", "may_raise") if k in kw}
    answers = answers_for(t, cap, **kw)
    game = None if game_level is None else game_of(game_level)
    return build_risk_profile(answers, BANK, CONFIG, assessed_at=ASSESSED_AT, game=game, **options)


# ==========================================================================
# 1. The table of the contract (section 2)
# ==========================================================================
@pytest.mark.parametrize("t, game, cap, blend, expected", [
    (4, 2, 5, 3.33, "C3"),            # a cautious game lowers
    (3, 5, 5, 3.67, "C4"),            # a bold game raises
    (5, 1, 5, 3.67, "C4"),
    (5, 5, 3, 5.00, "C3"),            # the cap wins
    (2, 5, 5, 3.00, "C3"),
    (1, 5, 5, 2.33, "C2"),
])
def test_the_rows_of_the_contract_table(t, game, cap, blend, expected):
    p = build(t, cap, game)
    assert p.c_level == expected and p.game_played and p.game_level == game and p.tolerance_level == t
    assert p.blended_level == round(blend) and p.capacity_level == cap


@pytest.mark.parametrize("t, game, expected", [(3, 5, "C3"), (2, 5, "C2"), (1, 5, "C1"), (4, 2, "C3"), (5, 1, "C4"), (3, 3, "C3")])
def test_with_may_raise_off_the_game_can_only_lower_the_tolerance_part(t, game, expected):
    assert build(t, 5, game, may_raise=False).c_level == expected


def test_not_played_is_the_questionnaire_alone_and_the_gate_wins_over_everything():
    assert build(3, 5).c_level == "C3" and not build(3, 5).game_played
    gated = build(5, 5, 5, D1="no")
    assert gated.c_level == "C1" and gated.binding_dimension == "gate" and gated.questionnaire_class == "C1"
    assert gated.game_played and gated.blended_level == 5                       # the game is recorded, the gate decides
    assert build(5, 5, 1, D1="no").c_level == "C1"


# ==========================================================================
# 2. The definitions
# ==========================================================================
@pytest.mark.parametrize("score, level", [(0, 1), (19, 1), (19.99, 1), (20, 2), (39, 2), (40, 3), (59.99, 3), (60, 4), (79, 4),
                                          (80, 5), (100, 5)])
def test_game_levels_are_equal_bands_of_twenty(score, level):
    assert scoring.game_level(score) == level


def test_the_game_score_is_the_mean_of_the_two_scores_or_the_tolerance_alone():
    assert scoring.game_score(GameInput(tolerance=80, loss_acceptance=40)) == 60.0
    assert scoring.game_score(GameInput(tolerance=80)) == 80.0
    assert scoring.game_score(GameInput(tolerance=0, loss_acceptance=0)) == 0.0
    p = build_risk_profile(answers_for(3), BANK, CONFIG, assessed_at=ASSESSED_AT, game=GameInput(tolerance=100, loss_acceptance=20))
    assert (p.game_score, p.game_level) == (60.0, 4)


def test_equal_levels_give_that_level_and_the_blend_rounds_to_the_nearest_class():
    for level in range(1, 6):
        assert scoring.blended_level(level, level, 1 / 3) == level
    assert scoring.blended_level(4, 2, 1 / 3) == 3 and scoring.blended_level(3, 5, 1 / 3) == 4          # 3.33, 3.67
    assert scoring.blended_level(1, 5, 1 / 3) == 2 and scoring.blended_level(5, 1, 1 / 3) == 4          # 2.33, 3.67
    assert scoring.blended_level(2, 3, 0.5) == 3 and scoring.blended_level(3, 2, 0.5) == 3              # a half rounds up
    assert scoring.blended_level(1, 1, 0.5) == 1 and scoring.blended_level(5, 5, 0.5) == 5


def test_game_input_is_a_strict_model():
    assert GameInput(tolerance=0).loss_acceptance is None and GameInput(tolerance=100, loss_acceptance=0).tolerance == 100
    for bad in (dict(tolerance=-1), dict(tolerance=101), dict(tolerance=50, loss_acceptance=101), dict(tolerance=50, loss_acceptance=-1),
                dict(tolerance=50, extra=1), dict(), dict(tolerance="x")):
        with pytest.raises(ValidationError):
            GameInput(**bad)


# ==========================================================================
# 3. The JSON: exactly today's without a game
# ==========================================================================
def test_without_a_game_the_profile_is_exactly_what_it_was_before_the_game_existed():
    p = build(3, 5)
    assert set(json.loads(p.model_dump_json())) == PRE_GAME_KEYS
    assert set(p.model_dump()) == PRE_GAME_KEYS and set(p.model_dump(mode="json")) == PRE_GAME_KEYS
    assert p == build_risk_profile(answers_for(3), BANK, CONFIG, assessed_at=ASSESSED_AT, game=None)
    assert p.answers_hash == build_risk_profile(answers_for(3), BANK, CONFIG, assessed_at=ASSESSED_AT).answers_hash
    assert (p.game_played, p.game_score, p.game_level, p.blended_level, p.questionnaire_class) == (False, None, None, None, None)


def test_with_a_game_the_five_fields_are_there_and_only_those():
    d = json.loads(build(4, 5, 2).model_dump_json())
    assert set(d) == PRE_GAME_KEYS | set(GAME_FIELDS)
    assert (d["game_played"], d["game_score"], d["game_level"], d["blended_level"], d["questionnaire_class"]) == (True, 30.0, 2, 3, "C4")
    assert "capacity_average" not in d


def test_a_weight_of_zero_is_the_kill_switch_and_gives_the_profile_without_a_game():
    for level in range(1, 6):
        off = build(3, 5, level, game_weight=0)
        assert off == build(3, 5) and off.model_dump_json() == build(3, 5).model_dump_json()
        assert off.answers_hash == build(3, 5).answers_hash and off.binding_dimension != "game"


class Wrapper(BaseModel):
    profile: RiskProfile
    other: int = 1


def test_the_serializer_also_works_when_the_profile_is_nested_and_it_round_trips():
    with_game, without = Wrapper(profile=build(4, 5, 2)), Wrapper(profile=build(4, 5))
    assert "game_level" in json.loads(with_game.model_dump_json())["profile"]
    assert "game_level" not in json.loads(without.model_dump_json())["profile"]
    assert RiskProfile.model_validate_json(build(4, 5, 2).model_dump_json()) == build(4, 5, 2).model_copy(update={"capacity_average": None})
    assert RiskProfile.model_validate_json(build(4, 5).model_dump_json()).game_played is False
    assert len([m for m in RiskProfile.__pydantic_decorators__.model_serializers]) == 1                  # one serializer only


# ==========================================================================
# 4. Binding dimension, answers hash, validation
# ==========================================================================
def test_the_binding_dimension_says_game_only_when_the_game_moved_the_category():
    assert build(4, 5, 2).binding_dimension == "game"                    # lowered
    assert build(3, 5, 5).binding_dimension == "game"                    # raised
    assert build(5, 3, 5).binding_dimension == "capacity"                # the cap wins, the game changes nothing
    assert build(3, 5, 3).binding_dimension == "tolerance"               # the game agrees with the questionnaire
    assert build(5, 5, 1).binding_dimension == "game"                    # lowered (blend 3.67 -> C4 instead of C5)
    assert build(5, 3, 1).binding_dimension == "capacity"                # lowered, but the cap is lower still: the cap decides
    assert build(3, 5, 5, may_raise=False).binding_dimension == "tolerance"      # the raise is switched off
    assert build(3, 2).binding_dimension == "capacity" and build(3, 5).binding_dimension == "tolerance"      # as before


def test_the_answers_hash_changes_with_the_game_and_not_without_one():
    plain = build(3, 5).answers_hash
    assert build(3, 5, 4).answers_hash != plain and build(3, 5, 4).answers_hash != build(3, 5, 5).answers_hash
    assert build(3, 5, 4).answers_hash == build(3, 5, 4).answers_hash
    loss = build_risk_profile(answers_for(3), BANK, CONFIG, assessed_at=ASSESSED_AT, game=GameInput(tolerance=70, loss_acceptance=30))
    assert loss.answers_hash not in (plain, build(3, 5, 4).answers_hash)


def test_the_weight_is_validated_in_the_call_and_in_the_config():
    for bad in (-0.01, 0.51, 1.0):
        with pytest.raises(ValueError, match="between 0 and 0.5"):
            build(3, 5, 3, game_weight=bad)
    assert build(3, 5, 5, game_weight=0.5).blended_level == 4                              # (3 + 5) / 2
    data = CONFIG.model_dump()
    for bad in (0.6, -0.1):
        with pytest.raises(ValidationError):
            RiskProfileConfig.model_validate({**data, "game": {"weight": bad, "may_raise": True}})
    assert CONFIG.game.weight == pytest.approx(1 / 3) and CONFIG.game.may_raise is True
    off = RiskProfileConfig.model_validate({**data, "game": {"weight": 1 / 3, "may_raise": False}})
    assert build_risk_profile(answers_for(3), BANK, off, assessed_at=ASSESSED_AT, game=game_of(5)).c_level == "C3"      # config switch
    assert build_risk_profile(answers_for(3), BANK, off, assessed_at=ASSESSED_AT, game=game_of(5), may_raise=True).c_level == "C4"


# ==========================================================================
# 5. Properties
# ==========================================================================
def random_case(rng):
    t, cap, level = rng.randint(1, 5), rng.randint(1, 5), rng.randint(1, 5)
    loss = rng.choice([None, 0, 25, 50, 75, 100])
    tolerance = rng.randint(0, 100)
    gate = rng.random() < 0.1
    return t, cap, GameInput(tolerance=tolerance, loss_acceptance=loss), gate, level


def case_profile(t, cap, game, gate, **options):
    answers = answers_for(t, cap, **({"D1": "no"} if gate else {}))
    return build_risk_profile(answers, BANK, CONFIG, assessed_at=ASSESSED_AT, game=game, **options)


def test_properties_over_random_investors_and_games():
    rng = random.Random(20261020)
    for _ in range(400):
        t, cap, game, gate, _ = random_case(rng)
        p = case_profile(t, cap, game, gate)
        rank = int(p.c_level[1:])
        # never above the capacity level, the gate wins, and the same inputs give the same profile
        assert rank <= p.capacity_level
        assert (rank == 1) if gate else rank == min(cap, p.blended_level)
        assert p == case_profile(t, cap, game, gate) and p.model_dump_json() == case_profile(t, cap, game, gate).model_dump_json()
        # the switches
        off = int(case_profile(t, cap, game, gate, may_raise=False).c_level[1:])
        assert off <= rank and off <= int(case_profile(t, cap, None, gate).c_level[1:])                # only lowers
        assert case_profile(t, cap, game, gate, game_weight=0) == case_profile(t, cap, None, gate)    # weight 0 = no game
        # the category without the game is what the questionnaire alone gives
        assert p.questionnaire_class == case_profile(t, cap, None, gate).c_level
        assert p.blended_level == scoring.blended_level(t, p.game_level, CONFIG.game.weight)
        # a game that agrees with the questionnaire changes nothing
        agreeing = GameInput(tolerance=GAME_FOR_LEVEL[t])
        assert case_profile(t, cap, agreeing, gate).c_level == case_profile(t, cap, None, gate).c_level


def test_the_category_never_falls_when_the_game_level_rises_and_never_leaves_the_questionnaire_by_more_than_one_class():
    for t in range(1, 6):
        for cap in range(1, 6):
            ranks = [int(build(t, cap, level).c_level[1:]) for level in range(1, 6)]
            assert ranks == sorted(ranks), (t, cap, ranks)                                      # monotone in the game level
            questionnaire = min(t, cap)
            assert all(abs(r - questionnaire) <= 2 for r in ranks)                              # one third of the distance, rounded
            assert all(r <= cap for r in ranks)


def test_the_game_moves_the_tolerance_part_by_at_most_one_third_of_the_distance():
    for t in range(1, 6):
        for level in range(1, 6):
            blended = scoring.blended_level(t, level, 1 / 3)
            assert abs(blended - t) <= (abs(level - t) / 3) + 0.5 + 1e-9


# ==========================================================================
# 6. The explanation
# ==========================================================================
def lines_for(t, cap, game_level, **kw):
    answers = answers_for(t, cap)
    return explain(build_risk_profile(answers, BANK, CONFIG, assessed_at=ASSESSED_AT, game=game_of(game_level), **kw), answers, BANK, CONFIG)


def test_the_explanation_says_what_the_game_did():
    lowered = lines_for(4, 5, 2)
    assert any("The Risk Game you played came to level 2 of 5 (score 30 out of 100). Together with your questionnaire "
               "(two thirds / one third) that is attitude level 3. Your financial situation sets a cap at level 5. "
               "Your category is the lower of the two: C3." in s for s in lowered)
    assert any("The game moved your category: your questionnaire answers alone would give C4" in s and "more careful" in s
               and s.endswith("so it is C3.") for s in lowered)
    raised = lines_for(3, 5, 5)
    assert any("that is attitude level 4" in s and "Your category is the lower of the two: C4" in s for s in raised)
    assert any("would give C3" in s and "bolder attitude, so it is C4." in s for s in raised)
    capped = lines_for(5, 3, 5)                                                                  # the cap wins
    assert any("sets a cap at level 3. Your category is the lower of the two: C3." in s for s in capped)
    assert any("limited to C3" in s and "attitude to risk is at level 5" in s for s in capped)
    assert not any("The game moved your category" in s for s in capped)
    same = lines_for(3, 5, 3)
    assert any("The game did not change your category" in s and "C3" in s for s in same)


def test_the_gate_and_not_played_keep_their_text():
    answers = answers_for(4, 5, D1="no")
    gated = explain(build_risk_profile(answers, BANK, CONFIG, assessed_at=ASSESSED_AT, game=game_of(5)), answers, BANK, CONFIG)
    plain = explain(build_risk_profile(answers, BANK, CONFIG, assessed_at=ASSESSED_AT), answers, BANK, CONFIG)
    assert gated == plain and not any("Risk Game" in s for s in gated)


def test_without_a_game_the_explanation_is_what_it_was():
    answers = answers_for(3, 5)
    lines = explain(build_risk_profile(answers, BANK, CONFIG, assessed_at=ASSESSED_AT), answers, BANK, CONFIG)
    assert not any("Risk Game" in s for s in lines)
    assert any("Your financial situation allows level 5, so your attitude level (3) sets your risk category: C3." in s for s in lines)
