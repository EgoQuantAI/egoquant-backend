"""Tests for the risk questionnaire module. Run with:  pytest -v tests/risk_profile

Layout:  1) tolerance (Grable & Lytton)  2) capacity  3) knowledge  4) gate and combining
         5) consistency check  6) adapter  7) audit log, explanations, determinism, config
"""

from datetime import datetime, timezone

import pytest

from egoquant.models import AgentConfig
from egoquant.risk_profile import (
    audit_records,
    build_risk_profile,
    explain,
    to_agent_c_limits,
    to_constraints,
)
from egoquant.risk_profile import scoring
from egoquant.scenarios import DEFAULT_STRESS_SCENARIOS
from tests.helpers import ASSESSED_AT, BANK, CONFIG, GL_IDS, gl_option, make_answers


def profile(**kwargs):
    return build_risk_profile(make_answers(**kwargs), BANK, CONFIG, assessed_at=ASSESSED_AT)


# ==========================================================================
# 1. Tolerance
# ==========================================================================
def test_minimum_score_is_13():
    answers = make_answers(gl_overrides={q: gl_option(q, "min") for q in GL_IDS})
    assert scoring.tolerance_score(answers, BANK) == 13
    assert build_risk_profile(answers, BANK, CONFIG, assessed_at=ASSESSED_AT).tolerance_level == 1


def test_maximum_score_is_47():
    answers = make_answers(gl_overrides={q: gl_option(q, "max") for q in GL_IDS})
    assert scoring.tolerance_score(answers, BANK) == 47
    assert build_risk_profile(answers, BANK, CONFIG, assessed_at=ASSESSED_AT).tolerance_level == 5


@pytest.mark.parametrize("score, level", [
    (18, 1), (19, 2), (22, 2), (23, 3), (28, 3), (29, 4), (32, 4), (33, 5),
])
def test_band_boundaries(score, level):
    p = profile(gl_score=score)
    assert p.tolerance_score == score
    assert p.tolerance_level == level


def test_thirteen_items_verbatim_source_in_bank():
    assert [q.id for q in BANK.section("GL")] == GL_IDS
    assert BANK.get("GL13").option("c").text == "Three month’s salary"   # original spelling kept


# ==========================================================================
# 2. Capacity
# ==========================================================================
def test_capacity_answers_are_averaged_not_capped_by_the_weakest():
    """B2, B4, B5 and B6 give points; the average decides the level (nearest level); B3 is a separate hard cap."""
    assert profile(B2="gt5", B4="gt6", B5="lt25", B6="stable").capacity_level == 5            # (5+5+5+5)/4
    p = profile(B2="gt5", B4="lt3", B5="p25_50", B6="stable")                                 # (5+2+4+5)/4 = 4.0
    assert (p.capacity_level, p.capacity_average, p.capacity_binding_question) == (4, 4.0, "B4")   # before: capped at 2
    assert profile(B5="gt75").capacity_level == 4                                             # (5+5+1+5)/4 = 4.0
    assert profile(B2="lt1", B4="lt3", B5="gt75", B6="unstable").capacity_level == 2          # (1+2+1+4)/4 = 2.0


def test_an_irregular_income_costs_one_point_and_does_not_cap_a_wealthy_investor():
    """The reason for the change: 'irregular or unstable income' used to cap everybody at C3."""
    rich = dict(gl_score=40, B2="gt5", B4="gt6", B5="lt25")                  # long horizon, big savings, small share
    for income in ("stable", "unstable", "none"):
        p = profile(B6=income, **rich)
        assert p.capacity_level == 5 and p.c_level == "C5", income                  # (5+5+5+{5,4,3})/4 >= 4.5
    assert profile(B6="unstable", **rich).capacity_average == pytest.approx(4.75)
    assert profile(B6="stable", **rich).capacity_average - profile(B6="unstable", **rich).capacity_average == pytest.approx(0.25)
    # in a modest situation the irregular income is the answer that tips the average between two levels
    modest = dict(B2="y3_5", B4="m3_6", B5="p25_50")                                  # 4, 4, 4
    assert profile(B6="stable", **modest).capacity_level == 4 and profile(B6="unstable", **modest).capacity_level == 4
    assert profile(B6="unstable", B2="y1_3", B4="m3_6", B5="p25_50").capacity_level == 3      # average 3.75 = 4, but 1-3 years caps at 3
    assert profile(B6="none", B2="y1_3", B4="m3_6", B5="p50_75").capacity_level == 3          # (3+4+3+3)/4 = 3.25


@pytest.mark.parametrize("average_points, level", [
    ({"B2": 1, "B4": 2}, 2), ({"B2": 1, "B4": 1}, 1), ({"B2": 2, "B4": 2}, 2), ({"B2": 2, "B4": 3}, 3),      # 1.5 -> 2, 2.5 -> 3
    ({"B2": 3, "B4": 4}, 4), ({"B2": 4, "B4": 4}, 4), ({"B2": 4, "B4": 5}, 5), ({"B2": 5, "B4": 5}, 5),      # 3.5 -> 4, 4.5 -> 5
    ({"B2": 3, "B4": 3}, 3),
])
def test_the_average_is_rounded_to_the_nearest_level_with_the_half_going_up(average_points, level):
    assert scoring.aggregate_level(average_points, CONFIG)[1] == level


def test_the_limiting_question_is_the_hard_cap_or_the_weakest_averaged_answer():
    assert profile(invested=50_000, needed=48_000).capacity_binding_question == "B3"           # 4% loss capacity -> 1
    assert profile(B5="gt75").capacity_binding_question == "B5"                                # 1 point, the weakest
    assert profile(B2="lt1", B5="gt75").capacity_binding_question == "B2"                      # tie on points: B2 first
    p = profile(B4="lt3", invested=50_000, needed=40_000)                                      # B3 = 20% -> 4, average 4.25 -> 4
    assert p.capacity_level == 4 and p.capacity_binding_question == "B3"                       # the hard cap on a tie


def test_a_plan_that_needs_its_money_back_is_never_averaged_into_a_risky_category():
    p = profile(gl_score=40, invested=50_000, needed=48_000)                                   # loss capacity 4%, all else perfect
    assert p.capacity_average == 5.0 and p.capacity_level == 1 and p.c_level == "C1" and p.binding_dimension == "capacity"


def test_the_questions_that_are_hard_caps_and_averaged_are_configured_and_checked():
    base = CONFIG.model_dump()

    def config_with(**changes):
        data = {**base, "capacity": {**base["capacity"], **changes}}
        return type(CONFIG).model_validate(data)

    # make the horizon a hard cap too: under a year then limits the capacity on its own
    strict = config_with(hard_caps=["B2", "B3"], option_caps={"B2": {"lt1": 2, "y1_3": 3, "y3_5": 4, "gt5": 5}}, answer_caps={},
                         aggregate={**base["capacity"]["aggregate"], "questions": ["B4", "B5", "B6"]})
    answers = make_answers(B2="lt1")
    assert build_risk_profile(answers, BANK, strict, assessed_at=ASSESSED_AT).capacity_level == 2
    assert build_risk_profile(answers, BANK, CONFIG, assessed_at=ASSESSED_AT).capacity_level == 2       # answer cap: under a year
    pure = config_with(answer_caps={})
    assert build_risk_profile(answers, BANK, pure, assessed_at=ASSESSED_AT).capacity_level == 4          # pure average: (1+5+5+5)/4
    with pytest.raises(ValueError, match="either a hard cap or averaged"):
        config_with(hard_caps=["B2", "B3"])                                                    # B2 in both lists
    with pytest.raises(ValueError, match="either a hard cap or averaged"):
        config_with(hard_caps=[])                                                              # B3 in neither
    with pytest.raises(ValueError, match="no option_caps"):
        config_with(hard_caps=["B2", "B3"], answer_caps={}, aggregate={**base["capacity"]["aggregate"], "questions": ["B4", "B5", "B6"]})


def test_capacity_tie_between_equal_answers_reports_the_first_question_in_order():
    p = profile(B2="lt1", B4="gt6", B5="lt25", B6="stable")                                    # only B2 is weak
    assert p.capacity_binding_question == "B2"


def test_loss_capacity_can_bind():
    p = profile(invested=50_000, needed=48_000)                 # 4% -> cap 1
    assert p.capacity_level == 1
    assert p.capacity_binding_question == "B3"


def test_loss_capacity_values():
    assert scoring.loss_capacity(50_000, 0) == 1.0
    assert scoring.loss_capacity(50_000, 50_000) == 0.0
    assert scoring.loss_capacity(50_000, 40_000) == pytest.approx(0.2)


def test_needed_larger_than_invested_is_rejected():
    with pytest.raises(ValueError, match=r"amount needed back by the goal date \(60,000\) cannot be larger "
                                         r"than the amount invested \(50,000\)"):
        make_answers(invested=50_000, needed=60_000)
    with pytest.raises(ValueError, match="cannot be larger"):
        scoring.loss_capacity(50_000, 80_000)
    assert make_answers(invested=50_000, needed=50_000).amount_needed == 50_000   # equal is allowed


def test_loss_capacity_rejects_non_positive_invested():
    with pytest.raises(ValueError):
        scoring.loss_capacity(0, 0)
    with pytest.raises(ValueError):                             # also rejected at the model level
        make_answers(invested=0)


@pytest.mark.parametrize("value, cap", [
    (0.0, 1), (0.0499, 1), (0.05, 2), (0.10, 3), (0.20, 4), (0.3499, 4), (0.35, 5), (1.0, 5),
])
def test_loss_capacity_cap_boundaries(value, cap):
    assert scoring.loss_capacity_cap(value, CONFIG) == cap


# ==========================================================================
# 3. Knowledge
# ==========================================================================
def test_knowledge_never_changes_c_level():
    full = profile(gl_score=30)
    blocked = profile(gl_score=30, K1="dk", K2="more", K3="true", K4=["none"], K5="none")
    assert full.knowledge_score == 3 and blocked.knowledge_score == 0
    assert full.product_access == "full" and blocked.product_access == "blocked_complex"
    assert full.c_level == blocked.c_level
    assert full.suitability_max_equity == blocked.suitability_max_equity


@pytest.mark.parametrize("k_answers, experienced, access", [
    ({"K1": "more", "K2": "less", "K3": "false"}, True, "full"),
    ({"K1": "more", "K2": "less", "K3": "false"}, False, "warning"),   # 3 correct, no experience
    ({"K1": "more", "K2": "less", "K3": "dk"}, True, "warning"),
    ({"K1": "more", "K2": "same", "K3": "refuse"}, True, "blocked_complex"),
    ({"K1": "dk", "K2": "dk", "K3": "dk"}, True, "blocked_complex"),
])
def test_product_access_rules(k_answers, experienced, access):
    experience = {"K4": ["stocks"], "K5": "y1_3"} if experienced else {"K4": ["deposits"], "K5": "none"}
    assert profile(**k_answers, **experience).product_access == access


# ==========================================================================
# 4. Gate and combining
# ==========================================================================
def test_gate_overrides_to_c1_lowest_category():
    p = profile(gl_score=47, D1="no")
    assert p.c_level == "C1"
    assert p.lowest_category is True
    assert p.no_loss_gate is True
    assert p.binding_dimension == "gate"
    assert p.max_r_level == 1
    assert p.suitability_max_equity == 0.0


def test_scored_c1_is_not_lowest_category():
    p = profile(gl_score=13)
    assert p.c_level == "C1" and p.lowest_category is False


def test_tolerance_4_capacity_2_gives_c2_bound_by_capacity():
    p = profile(gl_score=30, B2="lt1", B4="lt3", B5="gt75")                                      # (1+2+1+5)/4 = 2.25 -> 2
    assert (p.tolerance_level, p.capacity_level) == (4, 2)
    assert p.c_level == "C2"
    assert p.binding_dimension == "capacity"


def test_tie_is_reported_as_tolerance():
    p = profile(gl_score=20, B2="lt1", B4="lt3", B5="gt75")     # tolerance 2, capacity 2
    assert p.c_level == "C2"
    assert p.binding_dimension == "tolerance"


def test_c_level_maps_to_config_table():
    p = profile(gl_score=30)                                    # tolerance 4, capacity 5
    assert p.c_level == "C4"
    assert p.max_r_level == 4
    assert p.suitability_max_equity == pytest.approx(0.70)


# ==========================================================================
# 5. Consistency check
# ==========================================================================
def test_consistency_flag_when_self_description_and_scenarios_disagree():
    bold_self_timid_acts = {q: gl_option(q, "min") for q in GL_IDS} | {"GL1": "a"}
    p = profile(gl_overrides=bold_self_timid_acts)
    assert "review_suggested" in p.flags


def test_no_consistency_flag_when_answers_agree():
    assert "review_suggested" not in profile(gl_overrides={q: gl_option(q, "max") for q in GL_IDS}).flags


def test_gain_loss_pair_is_excluded_from_consistency():
    agree = {q: gl_option(q, "max") for q in GL_IDS}
    base = scoring.consistency_gap(make_answers(gl_overrides=agree), BANK, CONFIG)
    flipped = scoring.consistency_gap(make_answers(gl_overrides=agree | {"GL9": "a", "GL10": "a"}), BANK, CONFIG)
    assert base == flipped
    assert {"GL9", "GL10"} <= set(CONFIG.tolerance.consistency.excluded_items)


def test_consistency_flag_never_changes_score():
    bold_self_timid_acts = {q: gl_option(q, "min") for q in GL_IDS} | {"GL1": "a"}
    p = profile(gl_overrides=bold_self_timid_acts)
    assert p.tolerance_score == 16 and p.tolerance_level == 1


# ==========================================================================
# 6. Adapter
# ==========================================================================
def test_adapter_emits_exactly_two_suitability_constraints():
    p = profile(gl_score=25, invested=50_000, needed=40_000)    # C3, loss capacity 0.2
    constraints = to_constraints(p, CONFIG)
    assert len(constraints) == 2
    by_type = {c.type: c for c in constraints}
    assert set(by_type) == {"max_equity", "max_stress_loss"}
    assert by_type["max_equity"].value == pytest.approx(0.40)
    assert by_type["max_stress_loss"].value == pytest.approx(0.20)
    for c in constraints:
        assert c.source == "suitability"
        assert c.hard is True
        assert c.evidence_ref == p.questionnaire_version


def test_adapter_stress_scenario_exists_in_agent_a():
    assert CONFIG.adapter.stress_scenario == AgentConfig().capacity_scenario
    assert CONFIG.adapter.stress_scenario in {s.name for s in DEFAULT_STRESS_SCENARIOS}


def test_adapter_gate_sets_zero_stress_loss():
    p = profile(D1="no", needed=0)                              # loss capacity 1.0, but no loss accepted
    by_type = {c.type: c for c in to_constraints(p, CONFIG)}
    assert by_type["max_stress_loss"].value == CONFIG.gate.max_stress_loss == 0.0
    assert by_type["max_equity"].value == 0.0


def test_constraints_carry_no_score_or_c_level():
    dumped = str([c.model_dump() for c in to_constraints(profile(gl_score=30), CONFIG)])
    assert "C4" not in dumped and "tolerance" not in dumped


def test_agent_c_limits():
    assert to_agent_c_limits(profile(gl_score=30), CONFIG) == {"max_r_level": 4, "blocked_product_types": []}
    blocked = profile(gl_score=30, K1="dk", K2="dk", K3="dk")
    assert to_agent_c_limits(blocked, CONFIG)["blocked_product_types"] == ["crypto_etp", "crypto_spot"]


# ==========================================================================
# 7. Audit log, explanations, determinism, validation
# ==========================================================================
def test_audit_log_one_record_per_answer():
    answers = make_answers(K4=["bonds", "stocks"])
    ts = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    records = audit_records(answers, BANK, ts)
    n_single = sum(q.type == "single" for q in BANK.questions)
    assert len(records) == n_single + 2 + 2                     # + two K4 options + two B3 amounts
    assert all(r.questionnaire_version == BANK.questionnaire_version and r.timestamp == ts for r in records)
    gl1 = next(r for r in records if r.question_id == "GL1")
    assert gl1.option_id == answers.choices["GL1"]
    invested = next(r for r in records if r.question_id == "B3_invested")
    assert invested.option_id is None and invested.value == 50_000


def test_explanations_for_capacity_binding_and_crash():
    # an averaged answer limits the category: the text says the answers count together and which one holds the average back most
    answers = make_answers(gl_score=40, B4="lt3", invested=50_000)
    lines = explain(build_risk_profile(answers, BANK, CONFIG, assessed_at=ASSESSED_AT), answers, BANK, CONFIG)
    assert any("level 5" in s and "taken together" in s and "held back most by your emergency savings (under 3 months)" in s
               and "C4" in s for s in lines)
    assert not any("weighs the least" in s for s in lines)               # it read as "unimportant"
    # the hard cap (money needed back) keeps its own sentence
    needed = make_answers(gl_score=40, invested=50_000, needed=48_000)
    lines = explain(build_risk_profile(needed, BANK, CONFIG, assessed_at=ASSESSED_AT), needed, BANK, CONFIG)
    assert any("you can lose at most 4% of it" in s and "C1" in s and "taken together" not in s for s in lines)
    # nothing binds: capacity at least as high as the attitude
    free = make_answers(gl_score=30, B4="lt3", invested=50_000)
    lines = explain(build_risk_profile(free, BANK, CONFIG, assessed_at=ASSESSED_AT), free, BANK, CONFIG)
    assert any("Your financial situation allows level 4" in s and "C4" in s for s in lines)
    c3 = make_answers(gl_score=25, invested=50_000)
    lines = explain(build_risk_profile(c3, BANK, CONFIG, assessed_at=ASSESSED_AT), c3, BANK, CONFIG)
    assert any("C3 portfolio of your 50,000 could fall to about 40,000" in s for s in lines)


def test_same_answers_give_identical_profile():
    answers = make_answers(gl_score=27, B5="p50_75", K2="dk")
    first = build_risk_profile(answers, BANK, CONFIG, assessed_at=ASSESSED_AT)
    second = build_risk_profile(answers.model_copy(deep=True), BANK, CONFIG, assessed_at=ASSESSED_AT)
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()


def test_missing_or_unknown_answers_are_rejected():
    answers = make_answers()
    del answers.choices["B4"]
    with pytest.raises(ValueError, match="B4"):
        build_risk_profile(answers, BANK, CONFIG, assessed_at=ASSESSED_AT)
    with pytest.raises(ValueError, match="unknown option"):
        build_risk_profile(make_answers(B2="forever"), BANK, CONFIG, assessed_at=ASSESSED_AT)
    with pytest.raises(ValueError, match="none"):
        build_risk_profile(make_answers(K4=["none", "stocks"]), BANK, CONFIG, assessed_at=ASSESSED_AT)


def test_version_mismatch_is_rejected():
    answers = make_answers().model_copy(update={"questionnaire_version": "old"})
    with pytest.raises(ValueError, match="version"):
        build_risk_profile(answers, BANK, CONFIG, assessed_at=ASSESSED_AT)


def test_agent_a_class_table_matches_questionnaire():
    from egoquant.models import DEFAULT_RISK_CLASS_POLICIES
    for c, level in CONFIG.c_levels.items():
        policy = DEFAULT_RISK_CLASS_POLICIES[c]
        assert policy.max_equity == pytest.approx(level.max_equity)
        assert policy.max_stress_loss == pytest.approx(level.illustrative_crash_loss)
        assert policy.max_commodity == pytest.approx(level.max_commodity)
        assert policy.max_crypto == pytest.approx(level.max_crypto)


def test_class_caps_only_allow_products_the_r_level_allows():
    """A class may only have a commodity / crypto cap if its R-level admits some product of that class."""
    from egoquant.agents.market_intel import DEFAULT_CATALOG
    lowest_r = {cls: min(i.r_level for i in DEFAULT_CATALOG if i.asset_class == cls) for cls in ("commodity", "crypto")}
    for c, level in CONFIG.c_levels.items():
        for cls, cap in (("commodity", level.max_commodity), ("crypto", level.max_crypto)):
            assert cap == 0.0 or level.max_r_level >= lowest_r[cls], (c, cls)


# --------------------------------------------------------------------------
# A short horizon is not made safe by big savings (twelve-user check, 20 Oct): an answer cap on the horizon only
# --------------------------------------------------------------------------
@pytest.mark.parametrize("horizon, level", [("lt1", 2), ("y1_3", 3), ("y3_5", 5), ("gt5", 5)])
def test_a_short_horizon_limits_the_capacity_even_when_everything_else_is_strong(horizon, level):
    p = profile(gl_score=40, B2=horizon)                                    # savings over 6 months, small share, stable income
    assert p.capacity_level == level and p.c_level == f"C{level}"
    if level < 5:
        assert p.capacity_binding_question == "B2" and p.binding_dimension == "capacity"


def test_the_horizon_cap_does_not_touch_the_wealthy_irregular_income_investor_the_change_was_made_for():
    rich = dict(gl_score=40, B2="gt5", B4="gt6", B5="lt25", B6="unstable")
    assert profile(**rich).capacity_level == 5 and profile(**rich).c_level == "C5"


def test_the_horizon_cap_is_a_cap_not_extra_points_and_never_raises():
    weak = profile(gl_score=40, B2="lt1", B4="lt3", B5="gt75", B6="none")           # the average alone is already 2
    assert weak.capacity_level == 2 and weak.capacity_average == pytest.approx(1.75)
    assert profile(gl_score=40, B2="y1_3", B4="lt3", B5="gt75", B6="none").capacity_level == 2     # average (3+2+1+3)/4 = 2.25 lower


def test_the_explanation_names_the_horizon_as_the_answer_that_limits_the_category():
    answers = make_answers(gl_score=40, B2="lt1")
    lines = explain(build_risk_profile(answers, BANK, CONFIG, assessed_at=ASSESSED_AT), answers, BANK, CONFIG)
    sentence = next(s for s in lines if "limited to C2" in s)
    assert "your investment horizon (less than 1 year)" in sentence and "taken together" not in sentence
    assert "held back most" not in sentence                                          # not an averaged answer here


def test_answer_caps_are_checked_and_can_be_switched_off():
    base = CONFIG.model_dump()

    def config_with(**changes):
        return type(CONFIG).model_validate({**base, "capacity": {**base["capacity"], **changes}})

    with pytest.raises(ValueError, match="not an averaged answer question"):
        config_with(answer_caps={"B3": {"x": 1}})                                    # B3 is a hard cap
    with pytest.raises(ValueError, match="between 1 and 5"):
        config_with(answer_caps={"B2": {"lt1": 0}})
    with pytest.raises(ValueError, match="not an averaged answer question"):
        config_with(answer_caps={"K1": {"more": 3}})
    off = config_with(answer_caps={})
    assert build_risk_profile(make_answers(gl_score=40, B2="lt1"), BANK, off, assessed_at=ASSESSED_AT).capacity_level == 4
