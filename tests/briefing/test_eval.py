"""Evaluation helpers and eval/run_eval.py on the example rows, with fake scorers (offline)."""

import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest

from egoquant.briefing.evaluation import (evaluate, label_from_probabilities, label_from_tone, load_labeled, metrics,
                                          sample_for_labeling)
from egoquant.briefing.tone import LMDictionaryScorer, make_score
from tests.briefing.common import item

ROOT = Path(__file__).resolve().parents[2]
LABELED = ROOT / "tests" / "briefing" / "data" / "headlines_labeled_example.csv"   # 10 example rows, test only


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "eval" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_metrics_accuracy_and_macro_f1():
    r = metrics(["positive", "negative", "neutral", "neutral"], ["positive", "neutral", "neutral", None], "m")
    assert (r.n_total, r.n_scored) == (4, 3)
    assert r.accuracy == pytest.approx(2 / 3)
    assert r.macro_f1 == pytest.approx((1.0 + 0.0 + 2 / 3) / 3)


def test_labels_from_scores():
    assert label_from_probabilities(make_score(0.1, 0.7, 0.2, "m")) == "negative"
    assert label_from_tone(0.05, 0.1) == "neutral" and label_from_tone(-0.2) == "negative"


def test_lm_dictionary_baseline():
    lm = LMDictionaryScorer({"gain", "strong"}, {"loss", "weak", "decline"})
    t = [s.tone for s in lm.score(["Strong gain", "Weak quarter, loss widens", "Strong gain despite loss", "Flat day"])]
    assert t == [1.0, -1.0, pytest.approx(1 / 3), 0.0]


def test_example_rows_are_marked():
    rows = load_labeled(LABELED)
    assert len(rows) == 10 and all(r.labeler == "example" and r.headline.startswith("EXAMPLE ROW") for r in rows)


def test_sample_for_labeling_is_reproducible_and_unscored():
    items = [(item(f"Headline {i} about REITs", i * 0.1), ["VNQ"]) for i in range(30)]
    a = sample_for_labeling(items, 10, seed=42)
    b = sample_for_labeling(list(reversed(items)), 10, seed=42)
    assert a == b and len(a) == 10
    assert all(r["label_1"] == r["label_2"] == r["final_label"] == "" for r in a)


def test_run_eval_on_example_rows_with_fake_scorer(tmp_path, monkeypatch):
    mod = load_script("run_eval")
    fake = lambda texts: ["negative" if any(w in t.lower() for w in ("cut", "slide", "fall", "outflows")) else
                          "positive" if any(w in t.lower() for w in ("record", "climbs", "growth")) else "neutral"
                          for t in texts]
    monkeypatch.setattr(mod, "finbert_predictor", lambda config: fake)
    monkeypatch.setattr(mod, "lm_predictor", lambda path: None)
    assert mod.main(["--labeled", str(LABELED), "--out-dir", str(tmp_path)]) == 0
    md = (tmp_path / "results.md").read_text()
    assert "FinBERT" in md and "10 / 10" in md and "example rows" in md
    assert (tmp_path / "results.csv").read_text().startswith("method,n_total,n_scored,accuracy,macro_f1")


def test_run_eval_llm_is_opt_in_and_needs_a_key(tmp_path, monkeypatch):
    mod = load_script("run_eval")
    monkeypatch.setattr(mod, "finbert_predictor", lambda config: None)
    monkeypatch.setattr(mod, "lm_predictor", lambda path: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(mod, "ROOT", tmp_path)                    # no .env is loaded
    assert mod.main(["--labeled", str(LABELED), "--out-dir", str(tmp_path), "--with-llm"]) == 2
    assert mod.main(["--labeled", str(LABELED), "--out-dir", str(tmp_path)]) == 1   # nothing free available
