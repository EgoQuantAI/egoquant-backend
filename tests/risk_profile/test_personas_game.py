"""Personas and the Risk Game: nobody plays by default (so no persona, class or demo number moves); a persona can carry a game,
and the category then follows docs/RISK_GAME_BLEND.md. Run with: pytest -v tests/risk_profile/test_personas_game.py
"""

import dataclasses

import pytest

from egoquant.demo.personas import PERSONAS, interest_fields
from egoquant.risk_profile import GameInput, build_risk_profile, load_all
from tests.helpers import ASSESSED_AT

BANK, CONFIG = load_all()


def profile(persona, game="persona", **kw):
    return build_risk_profile(persona.answers(), BANK, CONFIG, assessed_at=ASSESSED_AT,
                              game=persona.game() if game == "persona" else game, **kw)


def test_no_persona_plays_by_default_and_every_class_is_what_it_was():
    for persona in PERSONAS.values():
        assert persona.risk_game is None and persona.game() is None
        p = profile(persona)
        assert p.c_level == persona.expected_class and not p.game_played


def test_a_persona_can_carry_a_game_and_the_category_follows_the_blend():
    saver = PERSONAS["cautious_retiree"]                      # C2: attitude level 2, capacity level 3 (15% loss capacity)
    for score, expected in ((10, "C2"), (50, "C2"), (90, "C3")):          # blend 1.67, 2.33, 3.00
        persona = dataclasses.replace(saver, risk_game={"tolerance": score})
        assert persona.game() == GameInput(tolerance=score)
        assert profile(persona).c_level == expected, score
    assert profile(dataclasses.replace(saver, risk_game={"tolerance": 100, "loss_acceptance": 100})).capacity_level == 3   # never above
    beginner = PERSONAS["eager_beginner"]                     # C5, capacity 5: a cautious game lowers
    assert profile(dataclasses.replace(beginner, risk_game={"tolerance": 30, "loss_acceptance": 30})).c_level == "C4"
    balanced = PERSONAS["balanced_saver"]                     # C3: a balanced game changes nothing
    assert profile(dataclasses.replace(balanced, risk_game={"tolerance": 50, "loss_acceptance": 50})).c_level == "C3"


def test_the_gate_persona_stays_c1_whatever_it_plays():
    gate = PERSONAS["no_loss_saver"]
    for score in (0, 50, 100):
        p = profile(dataclasses.replace(gate, risk_game={"tolerance": score}))
        assert p.c_level == "C1" and p.binding_dimension == "gate"


def test_the_preference_form_follows_the_category_with_the_game():
    """interest_fields() builds the form for the persona's category, game included; without a game it is as before."""
    persona = PERSONAS["eager_beginner"]
    without = interest_fields(persona)
    assert interest_fields(dataclasses.replace(persona, risk_game=None)) == without
    assert interest_fields(dataclasses.replace(persona, risk_game={"tolerance": 30, "loss_acceptance": 30}))["interest_areas"]


def test_a_game_with_extra_or_out_of_range_values_is_refused():
    from pydantic import ValidationError
    for bad in ({"tolerance": 101}, {"tolerance": 50, "other": 1}, {}):
        with pytest.raises(ValidationError):
            dataclasses.replace(PERSONAS["balanced_saver"], risk_game=bad).game()
