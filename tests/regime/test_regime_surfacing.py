"""Surfacing (only with the regime sampler): the report's methodology step, the sealed chat facts and number grounding."""

from __future__ import annotations

import pytest

from egoquant.advisor.context import _goal_facts
from egoquant.advisor.grounding import allowed_numbers, ungrounded
from egoquant.report import _regime_step

RECORD = {"sampler": "regime_hmm_v1", "model_version": "regime_hmm_v1", "model_sha256": "e" * 64,
          "fit_start": "2007-06-22", "fit_end": "2026-10-16", "start_probs": [0.8312, 0.1588, 0.01],
          "regime_today": "calm", "regime_today_probability": 0.8312, "state_names": ["calm", "stressed", "crisis"],
          "long_crisis_share": 0.4217, "long_crisis_in_100": 42}


def test_the_report_shows_the_model_today_s_regime_and_the_long_crisis_frequency():
    step = _regime_step(RECORD)
    assert step.title == "Market regimes"
    assert "Hidden Markov Model" in step.text and "calm, stressed or crisis" in step.text
    assert "Market regime today: calm (probability 83%)." in step.text
    assert step.text.endswith("In about 42 of every 100 simulated futures, there is at least one crisis period "
                              "lasting 3 months or more.")


@pytest.mark.parametrize("share, words", [(1.0, "In every simulated future"), (0.0, "In none of the simulated")])
def test_never_a_misleading_100_or_0_in_100(share, words):
    assert words in _regime_step({**RECORD, "long_crisis_share": share}).text


@pytest.mark.parametrize("rec", [None, {"sampler": "block_bootstrap", "fallback_reason": "short_overlap"}])
def test_nothing_is_added_without_the_regime_sampler(rec):
    assert _regime_step(rec) is None
    facts = _goal_facts({"n_paths": 10_000, "sampler": rec} if rec else {"n_paths": 10_000})
    assert "market_regime" not in facts
    assert facts["simulation"]["method"] == "block bootstrap of past daily returns (in-sample)"


def chat_facts() -> dict:
    """Goal facts as the advisor sees them, including portfolio weights (which must never support a chance)."""
    g = {"n_paths": 10_000, "sampler": RECORD, "std_error": 0.0046,
         "chosen": {"prob_goal": 0.6849, "p10": 151_000.0, "p50": 262_000.0, "p90": 410_000.0, "equity_weight": 0.61},
         "shortlist": [{"label": "Balanced", "equity_weight": 0.61, "prob_goal": 0.6849}]}
    return {"goal": _goal_facts(g), "holdings": [{"ticker": "VTI", "weight": 0.61}, {"ticker": "AGG", "weight": 0.39}]}


def test_regime_facts_are_in_the_sealed_chat_context_as_chance_values():
    facts = chat_facts()
    mr = facts["goal"]["market_regime"]
    assert mr["regime_today"] == "calm" and mr["regime_today_probability"] == 0.8312
    assert mr["long_crisis_in_100"] == 42 and mr["long_crisis_probability"] == 0.4217
    assert "not an LLM" in mr["model"]
    assert facts["goal"]["simulation"]["method"] == "regime-aware block bootstrap of past daily returns (in-sample)"
    allowed = allowed_numbers(facts)
    assert {42.0, 0.4217, 0.8312} <= allowed.chance                 # chance-typed: they may support "X in 100"


def test_grounding_accepts_the_regime_facts():
    allowed = allowed_numbers(chat_facts())
    reply = ("The model sees today's market as calm, with a probability of 83%. In about 42 of every 100 simulated "
             "futures there is at least one crisis lasting three months or more, and stressed has a 15.9% probability.")
    assert ungrounded(reply, allowed) == []


def test_grounding_blocks_an_invented_regime_percentage_and_frequency():
    allowed = allowed_numbers(chat_facts())
    bad = ungrounded("The model gives a 27% probability of a crisis next year.", allowed)
    assert [c.text for c in bad] == ["27%"]
    # a portfolio weight of 0.61 is in the facts, but it is not a chance, so it cannot support "61 in 100"
    bad = ungrounded("About 61 in 100 simulated futures go through a long crisis.", allowed)
    assert [c.text for c in bad] == ["61 in 100"]
