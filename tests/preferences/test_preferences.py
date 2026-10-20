"""Investment-preference form: what each risk class is offered, answer validation, Agent C input.

Run with:  pytest -v tests/preferences      (no network)
"""

import pytest

from egoquant.agents.market_intel import DEFAULT_CATALOG
from egoquant.preferences import AREA_PRIORITY, PreferenceAnswers, build_form, load_preference_bank, to_interest_fields
from egoquant.risk_profile import build_risk_profile
from tests.helpers import ASSESSED_AT, BANK, CONFIG, make_answers

PREF_BANK = load_preference_bank()
LEVELS = {"C1": dict(gl_score=15), "C2": dict(gl_score=20), "C3": dict(gl_score=25), "C4": dict(gl_score=30),
          "C5": dict(gl_score=40)}


def form_for(c_level="C5", platforms=("IBKR", "Binance"), **answers):
    profile = build_risk_profile(make_answers(**{**LEVELS[c_level], **answers}), BANK, CONFIG,
                                 assessed_at=ASSESSED_AT)
    assert profile.c_level == c_level
    return build_form(PREF_BANK, profile, CONFIG, list(platforms))


def offered(form, qid="P1") -> set[str]:
    return {o.id for o in form.question(qid).options if o.available}


def answers(**choices) -> PreferenceAnswers:
    return PreferenceAnswers(preferences_version=PREF_BANK.preferences_version, choices=choices)


# ==========================================================================
# 1. What each class is offered
# ==========================================================================
def test_each_class_is_offered_only_what_it_may_hold():
    assert offered(form_for("C1")) == {"cash"}
    assert offered(form_for("C2")) == {"bonds", "cash"}
    assert offered(form_for("C3")) == {"stocks", "bonds", "cash", "gold"}
    assert offered(form_for("C4")) == {"stocks", "bonds", "cash", "gold", "real_estate", "precious_metals", "commodities"}
    assert offered(form_for("C5")) == {o.id for o in PREF_BANK.get("P1").options}


def test_reasons_keep_the_names_capitalised_in_the_middle_of_a_sentence():
    reasons = {o.id: o.reason for o in form_for("C2").question("P1").options if not o.available}
    assert reasons["crypto"].endswith("does not include Bitcoin and Ether")           # a name keeps its capitals
    assert reasons["gold"].endswith("does not include gold")                           # a common noun does not
    assert "risk category C2" in reasons["gold"]


def test_every_unavailable_option_says_why():
    for c in LEVELS:
        for q in form_for(c).questions:
            for o in q.options:
                assert o.available or (o.reason and len(o.reason) > 10), (c, q.id, o.id)
    reason = next(o for o in form_for("C3").question("P1").options if o.id == "real_estate").reason
    assert "R4" in reason and "R3" in reason


def test_individual_stocks_and_industries_need_r4():
    assert offered(form_for("C3"), "P4") == {"funds"} and offered(form_for("C3"), "P2") == {"no_preference"}
    assert offered(form_for("C4"), "P4") == {"funds", "individual", "mix"}
    assert "technology" in offered(form_for("C4"), "P2")


def test_crypto_needs_product_knowledge_even_in_c5():
    form = form_for("C5", K1="dk", K2="dk", K3="dk", K5="none", K4=["none"])
    crypto = next(o for o in form.question("P1").options if o.id == "crypto")
    assert not crypto.available and "knowledge" in crypto.reason
    assert "gold" in offered(form)


def test_platform_is_the_last_reason_and_only_when_suitability_allows():
    form = form_for("C5", platforms=("Fidelity",))
    altcoins = next(o for o in form.question("P1").options if o.id == "altcoins")
    assert not altcoins.available and "platform" in altcoins.reason        # allowed, but not on Fidelity
    assert "crypto" in offered(form)                                        # IBIT is US-listed
    c1 = next(o for o in form_for("C1", platforms=("Fidelity",)).question("P1").options if o.id == "altcoins")
    assert "C1" in c1.reason                                                # suitability reason first


def test_avoiding_an_industry_is_always_possible():
    assert offered(form_for("C1"), "P5") == {o.id for o in PREF_BANK.get("P5").options}


def test_every_option_maps_to_catalog_areas_that_exist():
    catalog_areas = {a for i in DEFAULT_CATALOG for a in i.areas}
    for q in PREF_BANK.questions:
        for o in q.options:
            assert set(o.areas) <= catalog_areas, (q.id, o.id)
            assert all(a in AREA_PRIORITY for a in o.areas), (q.id, o.id)


# ==========================================================================
# 2. Answers -> Agent C
# ==========================================================================
def test_answers_become_prioritised_interests():
    fields = to_interest_fields(answers(P1=["crypto", "gold", "stocks", "bonds"], P2=["healthcare", "technology"],
                                        P4=["mix"], P5=["energy"], P6=["government_bonds"], P7=["individual"]),
                                form_for("C5"), PREF_BANK)
    assert fields["interest_areas"] == ["stocks", "bonds", "technology", "healthcare", "government_bonds", "gold",
                                        "crypto"]
    assert fields["instrument_style"] == "mix" and fields["avoid_sectors"] == ["energy"]
    assert fields["wants_individual_bonds"] is True


def test_defaults_are_funds_and_no_exclusions():
    fields = to_interest_fields(answers(P1=["stocks"]), form_for("C3"), PREF_BANK)
    assert fields == {"interest_areas": ["stocks"], "instrument_style": "funds", "avoid_sectors": [],
                      "wants_individual_bonds": False}


@pytest.mark.parametrize("bad, message", [
    (dict(P1=["crypto"]), "not available"),                      # not offered to C3
    (dict(P1=["bonds"], P2=["technology"]), "only applies"),     # industries only with stocks
    (dict(P1=[]), "not answered"),
    (dict(P1=["stocks"], P4=["funds", "mix"]), "choose one"),
    (dict(P1=["stocks"], P3=["us_stocks", "no_preference"]), "cannot be combined"),
    (dict(P1=["stocks", "stocks"]), "twice"),
    (dict(P1=["stocks"], P9=["x"]), "unknown questions"),
    (dict(P1=["shares"]), "unknown option"),
])
def test_invalid_answers_are_rejected(bad, message):
    with pytest.raises(ValueError, match=message):
        to_interest_fields(answers(**bad), form_for("C3"), PREF_BANK)


def test_an_industry_cannot_be_wanted_and_avoided():
    with pytest.raises(ValueError, match="avoid"):
        to_interest_fields(answers(P1=["stocks"], P2=["energy"], P5=["energy"]), form_for("C5"), PREF_BANK)


def test_version_mismatch_is_rejected():
    with pytest.raises(ValueError, match="preferences"):
        to_interest_fields(PreferenceAnswers(preferences_version="old", choices={"P1": ["cash"]}), form_for("C1"),
                           PREF_BANK)


def test_form_is_json_for_the_frontend():
    payload = form_for("C4").model_dump(mode="json")
    assert payload["c_level"] == "C4" and payload["questions"][0]["id"] == "P1"
    assert all({"id", "text", "available", "reason"} <= set(o) for o in payload["questions"][0]["options"])


def test_individual_stocks_without_an_industry_spread_over_the_ones_not_avoided():
    fields = to_interest_fields(answers(P1=["stocks"], P2=["no_preference"], P4=["individual"], P5=["energy", "utilities"]),
                                form_for("C5"), PREF_BANK)
    assert fields["interest_areas"][0] == "stocks"
    assert "technology" in fields["interest_areas"] and not {"energy", "utilities"} & set(fields["interest_areas"])
    picked = to_interest_fields(answers(P1=["stocks"], P2=["technology"], P4=["individual"]), form_for("C5"), PREF_BANK)
    assert picked["interest_areas"] == ["stocks", "technology"]               # an explicit pick is not widened


def test_real_estate_as_an_asset_class_does_not_stop_the_industry_spread():
    fields = to_interest_fields(answers(P1=["stocks", "real_estate"], P2=["no_preference"], P4=["mix"]),
                                form_for("C5"), PREF_BANK)
    assert {"technology", "healthcare", "real_estate"} <= set(fields["interest_areas"])


# ==========================================================================
# 4. Sentences a person reads (twelve-user QA, 20 Oct): plain words, no question numbers
# ==========================================================================
def test_an_option_that_is_too_risky_is_explained_in_words_with_the_levels_kept():
    reason = next(o for o in form_for("C2").question("P3").options if o.id == "us_stocks").reason
    assert reason.startswith("riskier than your category C2 allows")
    assert "R3" in reason and "R2" in reason                                       # the technical reader still finds the levels
    assert "rated R3 or higher; your risk category" not in reason                 # the old, terse wording


@pytest.mark.parametrize("bad, must_say, must_not_say", [
    (dict(P1=["bonds"], P4=["funds"]), ['"How would you like to hold stocks?" only applies if you chose', "Stocks"], ["P4", "P1="]),
    (dict(P1=["stocks"], P2=["energy"], P5=["energy"]), ["Energy", "both as an industry you are interested in and as one to avoid"],
     ["P2", "P5", "['energy']"]),
    (dict(P1=["crypto"]), ['"Bitcoin and Ether" is not available to you', "Which would you like us to consider?"], ["P1:", "'crypto'"]),
    (dict(P1=["stocks"], P3=["us_stocks", "no_preference"]), ['"No preference (any market)" cannot be combined'], ["P3"]),
    (dict(P1=["stocks"], P4=["funds", "mix"]), ["choose one option", "How would you like to hold stocks?"], ["P4"]),
    (dict(P1=[]), ["is not answered", "Which would you like us to consider?"], ["P1 "]),
])
def test_answer_errors_name_the_question_as_the_person_saw_it(bad, must_say, must_not_say):
    with pytest.raises(ValueError) as error:
        to_interest_fields(answers(**bad), form_for("C3" if "crypto" in str(bad) or "bonds" in str(bad) else "C5"), PREF_BANK)
    message = str(error.value)
    assert all(part in message for part in must_say), message
    assert not any(part in message for part in must_not_say), message
