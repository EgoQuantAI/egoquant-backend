"""Tone scoring. The OFFICIAL tone is FinBERT (ProsusAI/finbert) running locally: free, no API.

    tone = P(positive) - P(negative)    in [-1, 1]; a measurement of wording, not a forecast.
    With tone.neutral_gating (default): tone = 0 when P(neutral) is the largest of the three, i.e. exactly
    when the evaluation's FinBERT label (top_class) is "neutral". The raw probabilities are always kept.

Scorers share one interface so tests inject a deterministic fake. FinBERT is an optional dependency
(pip install -e ".[nlp]"); when it is missing or fails, tone is "unavailable" rather than a crash, and
the LLM's tone is never substituted for it.

Also here: the Loughran-McDonald dictionary baseline used by eval/run_eval.py.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Protocol

from egoquant.briefing.loader import read_json_cache, write_json_cache
from egoquant.briefing.models import NewsItem, ToneScore


class ToneScorer(Protocol):
    model_name: str

    def score(self, texts: list[str]) -> list[ToneScore | None]: ...


def item_text(item: NewsItem) -> str:
    return f"{item.title}. {item.summary_text}".strip()


def top_class(positive: float, negative: float, neutral: float) -> str:
    """The class with the highest probability (ties: positive, then negative, then neutral).
    The single rule shared by neutral gating and the evaluation's FinBERT label."""
    return max(("positive", positive), ("negative", negative), ("neutral", neutral), key=lambda kv: kv[1])[0]


def tone_value(positive: float, negative: float, neutral: float, neutral_gating: bool = False) -> float:
    if neutral_gating and top_class(positive, negative, neutral) == "neutral":
        return 0.0
    return max(-1.0, min(1.0, positive - negative))


def make_score(positive: float, negative: float, neutral: float, model: str,
               neutral_gating: bool = False) -> ToneScore:
    return ToneScore(positive=positive, negative=negative, neutral=neutral,
                     tone=tone_value(positive, negative, neutral, neutral_gating), model=model)


def apply_gating(score: ToneScore, neutral_gating: bool) -> ToneScore:
    """Recompute the tone from the stored probabilities (works for any cached or precomputed score)."""
    return score.model_copy(update={"tone": tone_value(score.positive, score.negative, score.neutral, neutral_gating)})


# --------------------------------------------------------------------------
# FinBERT (local, lazy)
# --------------------------------------------------------------------------
class FinBertScorer:
    """Loads the model on first use, once. Never downloads during tests (tests use a fake scorer)."""

    def __init__(self, model_name: str = "ProsusAI/finbert", max_tokens: int = 512, batch_size: int = 16):
        self.model_name, self.max_tokens, self.batch_size = model_name, max_tokens, batch_size
        self._pipe = None
        self._failed: str | None = None

    def available(self) -> bool:
        return self._load() is not None

    def _load(self):
        if self._pipe is not None or self._failed:
            return self._pipe
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
            tok = AutoTokenizer.from_pretrained(self.model_name)
            model = AutoModelForSequenceClassification.from_pretrained(self.model_name)
            model.eval()
            self._pipe = (tok, model, torch)
        except Exception as exc:  # not installed, no model files, no network for the first download
            self._failed = f"{type(exc).__name__}: {exc}"[:200]
        return self._pipe

    def score(self, texts: list[str]) -> list[ToneScore | None]:
        loaded = self._load()
        if loaded is None:
            return [None] * len(texts)
        tok, model, torch = loaded
        labels = {i: str(l).lower() for i, l in model.config.id2label.items()}
        out: list[ToneScore | None] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start:start + self.batch_size]
            enc = tok(batch, padding=True, truncation=True, max_length=self.max_tokens, return_tensors="pt")
            with torch.no_grad():
                probs = torch.softmax(model(**enc).logits, dim=-1).tolist()
            for row in probs:
                p = {labels[i]: float(v) for i, v in enumerate(row)}
                out.append(make_score(p.get("positive", 0.0), p.get("negative", 0.0), p.get("neutral", 0.0),
                                      self.model_name))
        return out


# --------------------------------------------------------------------------
# Cache layer: item id + model name -> score. Also serves the precomputed fixture scores.
# --------------------------------------------------------------------------
class CachedToneScorer:
    """neutral_gating: the tone of every returned score is recomputed from its probabilities with this rule,
    so cached and precomputed scores always follow the current config."""

    def __init__(self, inner: ToneScorer | None, model_name: str, cache_path: Path | None = None,
                 precomputed: dict[str, dict] | None = None, neutral_gating: bool = False):
        self.inner, self.model_name, self.cache_path = inner, model_name, cache_path
        self.neutral_gating = neutral_gating
        self.cache: dict[str, dict] = dict(precomputed or {})
        if cache_path is not None:
            self.cache.update(read_json_cache(cache_path))
        self.misses = 0

    def key(self, item: NewsItem) -> str:
        return f"{item.id}|{self.model_name}"

    def score_items(self, items: list[NewsItem]) -> list[ToneScore | None]:
        missing = [it for it in items if self.key(it) not in self.cache]
        if missing and self.inner is not None:
            scores = self.inner.score([item_text(it) for it in missing])
            for it, s in zip(missing, scores):
                if s is not None:
                    self.cache[self.key(it)] = s.model_dump()
            if self.cache_path is not None:
                write_json_cache(self.cache_path, self.cache)
        result = []
        for it in items:
            raw = self.cache.get(self.key(it))
            result.append(apply_gating(ToneScore.model_validate(raw), self.neutral_gating) if raw else None)
        self.misses = sum(r is None for r in result)
        return result


# --------------------------------------------------------------------------
# Loughran-McDonald dictionary baseline (evaluation only)
# --------------------------------------------------------------------------
_WORD = re.compile(r"[A-Za-z]+")


class LMDictionaryScorer:
    """tone = (pos - neg) / (pos + neg); 0 (neutral) when no dictionary word appears.

    The Loughran-McDonald Master Dictionary is not redistributed with this code (see the README for its
    terms). Load it from the CSV you download (columns Word, Positive, Negative; a non-zero value in
    Positive/Negative means the word is in that list) or pass word sets directly (tests).
    """

    model_name = "loughran-mcdonald"

    def __init__(self, positive: set[str], negative: set[str]):
        self.positive = {w.upper() for w in positive}
        self.negative = {w.upper() for w in negative}

    @classmethod
    def from_csv(cls, path: str | Path) -> "LMDictionaryScorer":
        pos, neg = set(), set()
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                word = (row.get("Word") or "").strip().upper()
                if not word:
                    continue
                if str(row.get("Positive", "0")).strip() not in ("", "0"):
                    pos.add(word)
                if str(row.get("Negative", "0")).strip() not in ("", "0"):
                    neg.add(word)
        if not pos or not neg:
            raise ValueError(f"{path}: no Positive/Negative words found (expected the LM Master Dictionary CSV)")
        return cls(pos, neg)

    def score(self, texts: list[str]) -> list[ToneScore | None]:
        out = []
        for t in texts:
            words = [w.upper() for w in _WORD.findall(t)]
            p = sum(w in self.positive for w in words)
            n = sum(w in self.negative for w in words)
            tone = (p - n) / (p + n) if (p + n) else 0.0
            pos, neg = max(tone, 0.0), max(-tone, 0.0)
            out.append(ToneScore(positive=pos, negative=neg, neutral=1.0 - abs(tone), tone=tone, model=self.model_name))
        return out
