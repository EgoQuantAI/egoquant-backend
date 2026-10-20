"""Plain-language pass of 20 Oct (Bartu's twelve-user reading): the investor-facing sentences the engine builds.

Nothing here changes a number: the Risk Engine keeps its exact details; the investor sees them once, without internal labels."""

from types import SimpleNamespace

import pytest

from egoquant.models import Constraint
from egoquant.report import _goal_behaviour_reasons, _lower_by, _pct
from egoquant.risk_engine import plain_violations


def test_a_failed_check_reads_as_a_sentence_without_internal_labels_and_once():
    raw = ["equity weight 85.61% vs limit 70.00% (suitability:max_equity)",
           "equity weight 85.61% vs limit 70.00% (risk_class:max_equity)",
           "VT (15.0%) is R3; investor maximum is R2",
           "commodity weight 12.50% vs limit 10.00% (risk_class:max_class_weight[commodity])",
           'hypothetical loss 24.50% in Severe market fall vs limit 20.00% (risk_class:max_stress_loss[x])',
           "TSLA weight 12.00% vs instrument cap 10.00%",
           "weights sum to 0.900000"]
    plain = plain_violations(raw)
    assert plain == ["your share in stocks would be 85.6%, above your limit of 70%",
                     "VT is a higher-risk product (risk level R3); your category allows products up to R2",
                     "your share in gold and commodities would be 12.5%, above your limit of 10%",
                     'in the "Severe market fall" scenario the portfolio could lose 24.5%, more than your limit of 20%',
                     "TSLA would be 12% of the portfolio, above the 10% allowed in one product of this kind",
                     "weights sum to 0.900000"]                          # an unknown detail is passed on unchanged
    assert not any("(" in p and ":" in p for p in plain[:5])


@pytest.mark.parametrize("value,text", [(0.4, "40%"), (0.125, "12.5%"), (0.1, "10%"), (0.405, "40.5%"), (0.0, "0%")])
def test_percentages_have_one_decimal_only_when_they_need_it(value, text):
    assert _pct(value) == text


def test_the_diversification_ratio_is_said_as_a_percentage():
    assert _lower_by(1.06) == "6%" and _lower_by(2.0) == "50%" and _lower_by(1.0) == "0%" and _lower_by(0.9) == "0%"


def screening_with(loss, source="behavioral", confidence=0.6):
    c = Constraint(type="max_stress_loss", value=loss, source=source, hard=False, confidence=confidence,
                   scenario="severe_market_fall")
    return SimpleNamespace(constraints=[c])


POLICY = SimpleNamespace(max_stress_loss=0.35, target_equity=0.70)
FINAL = SimpleNamespace(ignored_constraints=[])


def test_the_goal_path_says_when_the_investors_own_history_held_the_stocks_back():
    out = _goal_behaviour_reasons(screening_with(0.10), POLICY, {"equity": 0.22}, FINAL)
    assert len(out) == 1 and out[0].kind == "behavioral"
    assert "loss of at most 10%" in out[0].text and "(confidence 60%)" in out[0].text and "35%" in out[0].text
    assert "holds 22% in stocks, below the 70% your category aims for" in out[0].text


def test_no_behavioural_sentence_when_the_history_is_not_stricter_than_the_category():
    assert _goal_behaviour_reasons(screening_with(0.35), POLICY, {"equity": 0.7}, FINAL) == []
    assert _goal_behaviour_reasons(screening_with(0.40), POLICY, {"equity": 0.7}, FINAL) == []
    assert _goal_behaviour_reasons(SimpleNamespace(constraints=[]), POLICY, {"equity": 0.7}, FINAL) == []
    ignored = SimpleNamespace(ignored_constraints=["behavioral:max_stress_loss[severe_market_fall]"])
    assert _goal_behaviour_reasons(screening_with(0.10), POLICY, {"equity": 0.2}, ignored) == []


def test_a_real_report_uses_one_word_for_the_category_and_says_the_most_important_limit_once():
    from tests.helpers import make_answers
    from tests.system.test_pipeline import run
    out = run(make_answers(gl_score=25))
    texts = [r.text for r in out.report.reasons]
    assert texts[0].startswith("Your answers placed you in category")
    assert sum(t.startswith("The most important limit was") for t in texts) <= 1
    joined = " ".join(texts)
    for stale in ("your class", "Your class", "That class", "class cap", "suitability cap", "Ledoit-Wolf shrinkage"):
        assert stale not in joined + " ".join(out.report.disclosures), stale
