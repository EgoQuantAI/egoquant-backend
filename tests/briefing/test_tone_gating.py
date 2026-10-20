"""Neutral gating: tone = 0 when P(neutral) is the largest probability, else P(pos) - P(neg)."""

import random

import pytest

from egoquant.briefing.evaluation import label_from_probabilities
from egoquant.briefing.loader import FIXTURE_SCORES, load_config, read_json_cache
from egoquant.briefing.models import ToneScore
from egoquant.briefing.tone import CachedToneScorer, make_score, tone_value
from tests.briefing.common import item

CFG = load_config()


def test_config_switch_is_on_by_default():
    assert CFG.tone.neutral_gating is True


@pytest.mark.parametrize("pos, neg, neu, gated, raw", [
    (0.018, 0.044, 0.938, 0.0, -0.026),      # neutral is the largest -> 0 (raw would be -0.026)
    (0.387, 0.583, 0.030, -0.196, -0.196),   # negative is the largest -> unchanged
    (0.850, 0.050, 0.100, 0.800, 0.800),     # positive is the largest -> unchanged
    (0.400, 0.150, 0.450, 0.0, 0.250),       # neutral largest even though pos > neg
])
def test_gating(pos, neg, neu, gated, raw):
    assert tone_value(pos, neg, neu, neutral_gating=True) == pytest.approx(gated)
    assert tone_value(pos, neg, neu, neutral_gating=False) == pytest.approx(raw)
    s = make_score(pos, neg, neu, "m", neutral_gating=True)
    assert (s.positive, s.negative, s.neutral) == (pos, neg, neu)            # raw probabilities kept


def test_gating_matches_the_evaluated_label():
    rng = random.Random(1)
    for _ in range(2000):
        a, b, c = (rng.random() for _ in range(3))
        total = a + b + c
        s = make_score(a / total, b / total, c / total, "m", neutral_gating=True)
        assert (s.tone == 0.0) == (label_from_probabilities(s) == "neutral" or s.positive == s.negative)


def test_cached_scores_are_gated_on_read_and_switch_off_gives_raw():
    it = item("Exchange publishes its holiday schedule")
    raw = {f"{it.id}|m": ToneScore(positive=0.018, negative=0.044, neutral=0.938, tone=-0.026, model="m").model_dump()}
    on = CachedToneScorer(None, "m", precomputed=raw, neutral_gating=True).score_items([it])[0]
    off = CachedToneScorer(None, "m", precomputed=raw, neutral_gating=False).score_items([it])[0]
    assert on.tone == 0.0 and off.tone == pytest.approx(-0.026) and on.neutral == off.neutral == 0.938


def test_fixture_tones_follow_the_gating_rule():
    data = read_json_cache(FIXTURE_SCORES)
    assert len(data) == 68
    zeros = 0
    for v in data.values():
        assert v["tone"] == pytest.approx(tone_value(v["positive"], v["negative"], v["neutral"], True))
        assert v["positive"] + v["negative"] + v["neutral"] == pytest.approx(1.0, abs=1e-3)
        zeros += v["tone"] == 0.0
    assert zeros == 9
