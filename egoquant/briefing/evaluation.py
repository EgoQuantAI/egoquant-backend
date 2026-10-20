"""Evaluation helpers for the pitch: tone methods vs hand-labeled headlines.

    eval/collect_headlines.py  -> eval/headlines_to_label.csv   (sample; humans label; nothing is scored)
    eval/headlines_labeled.csv -> eval/run_eval.py             (accuracy, macro-F1, coverage per method)

Do not evaluate FinBERT on Financial PhraseBank: it was fine-tuned on it, so that would not be a fair test.
"""

from __future__ import annotations

import csv
import random
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from egoquant.briefing.models import NewsItem, ToneScore
from egoquant.briefing.tone import top_class

LABELS = ("positive", "negative", "neutral")
TO_LABEL_COLUMNS = ["id", "headline", "source", "date", "matched_tickers", "label_1", "label_2", "final_label"]


# --------------------------------------------------------------------------
# Collect (no scoring, no labeling)
# --------------------------------------------------------------------------
def sample_for_labeling(items: list[tuple[NewsItem, list[str]]], sample_size: int, seed: int,
                        max_per_ticker: int | None = None) -> list[dict]:
    """items: (item, matched tickers) already deduplicated and matched. Fixed seed -> same sample.

    max_per_ticker: no matched ticker may appear in more than this many sampled rows (a row with several
    tickers counts for each), so one popular fund cannot dominate the labeling set.
    """
    pool = sorted(items, key=lambda x: x[0].id)                       # independent of fetch order
    random.Random(seed).shuffle(pool)
    counts: Counter[str] = Counter()
    picked = []
    for it, tickers in pool:
        if len(picked) >= sample_size:
            break
        if max_per_ticker is not None and any(counts[t] >= max_per_ticker for t in tickers):
            continue
        picked.append((it, tickers))
        counts.update(tickers)
    picked.sort(key=lambda x: (x[0].published_at, x[0].id))
    return [{"id": it.id, "headline": it.title, "source": it.source_name, "date": it.published_at.date().isoformat(),
             "matched_tickers": " ".join(t), "label_1": "", "label_2": "", "final_label": ""} for it, t in picked]


def rows_per_ticker(rows: list[dict]) -> dict[str, int]:
    """How many sampled rows mention each ticker (a row with several tickers counts for each)."""
    c = Counter(t for r in rows for t in r["matched_tickers"].split())
    return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))


def write_csv(path: Path | str, rows: list[dict], columns: list[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------
# Evaluate
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class LabeledHeadline:
    id: str
    headline: str
    label: str
    labeler: str


def load_labeled(path: Path | str) -> list[LabeledHeadline]:
    rows = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            label = (r.get("label") or "").strip().lower()
            if label not in LABELS:
                raise ValueError(f"{path}: row {r.get('id')} has label {label!r}; expected one of {LABELS}")
            rows.append(LabeledHeadline(r["id"], r["headline"], label, (r.get("labeler") or "").strip()))
    return rows


def label_from_probabilities(s: ToneScore) -> str:
    """FinBERT: the class with the highest probability (same rule as neutral gating in tone.py)."""
    return top_class(s.positive, s.negative, s.neutral)


def label_from_tone(tone: float, neutral_band: float = 0.0) -> str:
    """LM dictionary (band 0: sign of the tone) and LLM tone (band from config)."""
    if tone > neutral_band:
        return "positive"
    if tone < -neutral_band:
        return "negative"
    return "neutral"


@dataclass
class MethodResult:
    method: str
    n_total: int
    n_scored: int
    accuracy: float | None
    macro_f1: float | None


def metrics(gold: list[str], pred: list[str | None], method: str) -> MethodResult:
    pairs = [(g, p) for g, p in zip(gold, pred) if p is not None]
    if not pairs:
        return MethodResult(method, len(gold), 0, None, None)
    acc = sum(g == p for g, p in pairs) / len(pairs)
    f1s = []
    for c in LABELS:
        tp = sum(g == c and p == c for g, p in pairs)
        fp = sum(g != c and p == c for g, p in pairs)
        fn = sum(g == c and p != c for g, p in pairs)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return MethodResult(method, len(gold), len(pairs), acc, sum(f1s) / len(f1s))


def results_markdown(results: list[MethodResult], note: str = "") -> str:
    lines = ["| Method | Scored / total | Accuracy | Macro-F1 |", "|---|---|---|---|"]
    for r in results:
        acc = f"{r.accuracy:.3f}" if r.accuracy is not None else "–"
        f1 = f"{r.macro_f1:.3f}" if r.macro_f1 is not None else "–"
        lines.append(f"| {r.method} | {r.n_scored} / {r.n_total} | {acc} | {f1} |")
    return "\n".join(lines) + (f"\n\n{note}\n" if note else "\n")


def evaluate(rows: list[LabeledHeadline], predictors: dict) -> list[MethodResult]:
    """predictors: method name -> function(list of headlines) -> list of labels (None = could not score)."""
    gold = [r.label for r in rows]
    texts = [r.headline for r in rows]
    return [metrics(gold, list(fn(texts)), name) for name, fn in predictors.items()]


def results_rows(results: list[MethodResult]) -> list[dict]:
    return [{"method": r.method, "n_total": r.n_total, "n_scored": r.n_scored,
             "accuracy": "" if r.accuracy is None else f"{r.accuracy:.4f}",
             "macro_f1": "" if r.macro_f1 is None else f"{r.macro_f1:.4f}"} for r in results]
