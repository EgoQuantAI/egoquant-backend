"""Number grounding (advisor/grounding.py) and the redaction gateway (advisor/redaction.py): unit tests."""

from egoquant.advisor.grounding import allowed_numbers, claims, ungrounded
from egoquant.advisor.redaction import Redactor

FACTS = {"goal": {"chance_as_shown": "73%", "prob_goal": 0.7258, "goal_amount": 218000, "median": 282779.6},
         "holdings": [{"ticker": "VTI", "weight": 0.6100}], "equity": 0.6578}
TOOL = [{"status": "ok", "baseline_goal_frequency": 73, "scenario_goal_frequency": 87,
         "scenario_outcome": {"median_final": 301234.5}}]


def test_claims_are_found_in_every_form():
    kinds = [(c.kind, c.value) for c in claims("73 in 100, 87 out of 100, 65.8%, $283k, 1,234 USD, 2.5 million "
                                               "dollars, 2x and 3 times")]
    assert ("in100", 73) in kinds and ("in100", 87) in kinds and ("percent", 65.8) in kinds
    assert ("money", 283_000) in kinds and ("money", 1234) in kinds and ("money", 2_500_000) in kinds
    assert ("multiplier", 2) in kinds and ("multiplier", 3) in kinds


def test_an_invented_frequency_is_blocked_and_tool_quotes_pass():
    allowed = allowed_numbers(FACTS, TOOL, user_text=["what if I add $200 a month?"])
    assert [c.text for c in ungrounded("about 75 in 100 futures", allowed)] == ["75 in 100"]
    assert ungrounded("Now about 73 in 100; with $200 more about 87 in 100. Median $301k, stocks 65.8% "
                      "(66%), goal 218,000 USD.", allowed) == []


def test_a_weight_never_supports_a_chance():
    allowed = allowed_numbers(FACTS)
    assert ungrounded("61% in VTI", allowed) == []                  # a share quoted as a percentage: fine
    assert [c.text for c in ungrounded("61 in 100 futures", allowed)] == ["61 in 100"]


def test_numbers_the_user_wrote_are_allowed():
    allowed = allowed_numbers(FACTS, user_text=["What if the goal were $500,000 and 2x the contributions?"])
    assert ungrounded("A goal of $500,000 with 2x the contributions.", allowed) == []
    assert ungrounded("$510,000", allowed)                           # 2% off is not rounding


def test_redaction_replaces_and_restores_identifiers_but_not_numbers():
    r = Redactor(names=["jane_d"])
    text = ("I'm jane_d (Jane_D), account U1234567 / DU7654321, jane@ex.com, +1 555 123 4567, 555-123-4567, "
            "0532 123 45 67. On 2026-10-16 I had $1,234,567 and 20261016 is a date.")
    out = r.redact(text)
    for raw in ("jane_d", "Jane_D", "U1234567", "DU7654321", "jane@ex.com", "555 123 4567", "555-123-4567",
                "0532 123 45 67"):
        assert raw not in out
    for kept in ("2026-10-16", "$1,234,567", "20261016"):
        assert kept in out
    assert r.restore(out) == text
    assert r.summary() == {"account": 2, "email": 1, "name": 1, "phone": 3}


def test_redaction_counts_distinct_values_across_calls():
    r = Redactor()
    params = {"instructions": "facts", "input": [{"role": "user", "content": "mail a@b.co"}]}
    first, second = r.redact_params(params), r.redact_params(params)
    assert first["input"][0]["content"] == second["input"][0]["content"] == "mail [EMAIL_1]"
    assert params["input"][0]["content"] == "mail a@b.co"               # the caller's copy is untouched
    assert r.summary() == {"email": 1}
