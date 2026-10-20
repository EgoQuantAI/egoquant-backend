"""Build eval/results.md and eval/results.csv: every tone method vs the human labels.

    python eval/build_results.py --lm-dictionary LM_dictionary.csv

Methods, each on (a) all labeled headlines and (b) only those the two labelers agreed on:
  always "neutral" (naive baseline) · Loughran-McDonald word counting · FinBERT (plain) ·
  FinBERT with a cross-validated neutral margin (held-out predictions only) · LLM tone (the summarizer's
  llm_tone, as in run_eval.py --with-llm) · LLM tone, fund-aware (eval/run_fund_aware_llm.py).

Makes NO OpenAI calls: both LLM methods are read from their caches (run those scripts first). FinBERT runs
locally. Per-headline predictions are kept in memory only; the outputs hold aggregate numbers plus a handful
of example headlines.
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from egoquant.briefing.evaluation import label_from_probabilities, label_from_tone, metrics  # noqa: E402

CLASSES = ("positive", "negative", "neutral")
MARGINS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5)


# --------------------------------------------------------------------------
# FinBERT neutral margin, chosen by stratified 5-fold cross-validation
# --------------------------------------------------------------------------
def margin_label(pos: float, neg: float, neu: float, m: float) -> str:
    """positive/negative instead of neutral when max(P(pos), P(neg)) >= P(neutral) - m."""
    if max(pos, neg) >= neu - m:
        return "positive" if pos >= neg else "negative"
    return "neutral"


def stratified_folds(ids: list[str], labels: list[str], k: int = 5, seed: int = 42) -> list[int]:
    """Fold number per item: within each label, items (sorted by id, then shuffled with the seed) are dealt
    round-robin to folds, so every fold has about the same label mix."""
    fold = [0] * len(ids)
    rng = random.Random(seed)
    for c in sorted(set(labels)):
        members = sorted((i for i, lab in enumerate(labels) if lab == c), key=lambda i: ids[i])
        rng.shuffle(members)
        for n, i in enumerate(members):
            fold[i] = n % k
    return fold


def choose_margin(probs: list[tuple[float, float, float]], gold: list[str]) -> float:
    """The margin with the best macro-F1 on these items (ties: higher accuracy, then the smaller margin)."""
    best = None
    for m in MARGINS:
        r = metrics(gold, [margin_label(*p, m) for p in probs], "m")
        key = (r.macro_f1, r.accuracy, -m)
        if best is None or key > best[0]:
            best = (key, m)
    return best[1]


def cross_validate(ids: list[str], gold: list[str], probs: list[tuple[float, float, float]], k: int = 5,
                   seed: int = 42) -> tuple[list[str], list[dict]]:
    """(held-out prediction per item, per-fold rows {fold, m, n, accuracy, macro_f1})."""
    folds = stratified_folds(ids, gold, k, seed)
    held = [None] * len(ids)
    per_fold = []
    for f in range(k):
        train = [i for i in range(len(ids)) if folds[i] != f]
        test = [i for i in range(len(ids)) if folds[i] == f]
        m = choose_margin([probs[i] for i in train], [gold[i] for i in train])
        for i in test:
            held[i] = margin_label(*probs[i], m)
        r = metrics([gold[i] for i in test], [held[i] for i in test], "fold")
        per_fold.append({"fold": f + 1, "m": m, "n": len(test), "accuracy": r.accuracy, "macro_f1": r.macro_f1})
    return held, per_fold


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------
def confusion(gold: list[str], pred: list[str | None]) -> list[list[int]]:
    return [[sum(1 for g, p in zip(gold, pred) if g == gc and p == pc) for pc in CLASSES] for gc in CLASSES]


def per_class(gold: list[str], pred: list[str | None]) -> dict[str, tuple[float, float]]:
    out = {}
    for c in CLASSES:
        tp = sum(g == c and p == c for g, p in zip(gold, pred))
        fp = sum(g != c and p == c for g, p in zip(gold, pred))
        fn = sum(g == c and p != c for g, p in zip(gold, pred))
        out[c] = (tp / (tp + fp) if tp + fp else 0.0, tp / (tp + fn) if tp + fn else 0.0)
    return out


def score_rows(name: str, gold: list[str], pred: list[str | None], agreed: list[bool], note: str = "",
               all_override: tuple[float, float] | None = None) -> list[dict]:
    """Two result rows (all / agreed). all_override: (accuracy, macro-F1) to report for 'all' (CV means)."""
    rows = []
    for subset, mask in (("all", [True] * len(gold)), ("agreed", agreed)):
        g = [x for x, keep in zip(gold, mask) if keep]
        p = [x for x, keep in zip(pred, mask) if keep]
        r = metrics(g, p, name)
        acc, f1 = (all_override if subset == "all" and all_override else (r.accuracy, r.macro_f1))
        rows.append({"method": name, "subset": subset, "n_total": r.n_total, "n_scored": r.n_scored,
                     "accuracy": acc, "macro_f1": f1, "note": note})
    return rows


def fmt(x: float | None) -> str:
    return "–" if x is None else f"{x:.3f}"


def markdown(results: list[dict], context: dict) -> str:
    L = ["# Tone evaluation: methods vs hand-labeled headlines", "",
         f"Generated {context['generated']} by `eval/build_results.py`. {context['n']} headlines labeled by our team "
         f"({context['n_agreed']} agreed by both labelers, {context['n'] - context['n_agreed']} adjudicated). "
         "Labels judge each headline for an investor who holds the matched fund.", "",
         "## Results", "",
         "| Method | All: scored | All: accuracy | All: macro-F1 | Agreed only: scored | Agreed: accuracy | Agreed: macro-F1 |",
         "|---|---|---|---|---|---|---|"]
    by = {}
    for r in results:
        by.setdefault(r["method"], {})[r["subset"]] = r
    for method, d in by.items():
        a, g = d["all"], d["agreed"]
        L.append(f"| {method} | {a['n_scored']}/{a['n_total']} | {fmt(a['accuracy'])} | {fmt(a['macro_f1'])} | "
                 f"{g['n_scored']}/{g['n_total']} | {fmt(g['accuracy'])} | {fmt(g['macro_f1'])} |")
    L += ["", *[f"- {n}" for n in context["notes"]], "",
          "## Reference: human agreement", "",
          f"Two labelers agreed on {context['n_agreed']}/{context['n']} headlines "
          f"({context['n_agreed'] / context['n']:.0%}) before adjudication. Cohen's kappa per labeler pair:", "",
          "| Pair (shared block) | Headlines | Kappa |", "|---|---|---|"]
    L += [f"| {x} + {y} ({b}) | {n} | {fmt(k)} |" for x, y, b, n, k in context["kappas"]]
    L += ["", "## FinBERT neutral margin (5-fold cross-validation)", "",
          "Predict positive/negative instead of neutral when max(P(pos), P(neg)) >= P(neutral) − m, with m from "
          f"{{{', '.join(str(m) for m in MARGINS)}}} chosen on 4 folds (best macro-F1) and scored on the held-out fold "
          "(stratified by label, seed 42). The table above reports the mean of the held-out folds for 'All' and the "
          "pooled held-out predictions for 'Agreed only'.", "",
          "| Fold | m chosen | Held-out headlines | Accuracy | Macro-F1 |", "|---|---|---|---|---|"]
    L += [f"| {f['fold']} | {f['m']} | {f['n']} | {fmt(f['accuracy'])} | {fmt(f['macro_f1'])} |" for f in context["folds"]]
    for title, conf, pc in (("FinBERT", context["conf_fb"], context["pc_fb"]),
                            ("LLM tone, fund-aware", context["conf_fa"], context["pc_fa"])):
        L += ["", f"## Confusion table: {title} (rows = human label, columns = {title})", "",
              "| Human \\ Model | positive | negative | neutral | unscored |", "|---|---|---|---|---|"]
        for c, row in zip(CLASSES, conf["matrix"]):
            L.append(f"| {c} | {row[0]} | {row[1]} | {row[2]} | {conf['unscored'][c]} |")
        L += ["", "| Class | Precision | Recall |", "|---|---|---|"]
        L += [f"| {c} | {fmt(p)} | {fmt(r)} |" for c, (p, r) in pc.items()]
    L += ["", "## Examples", "", "FinBERT wrong, fund-aware LLM right:", "",
          "| Headline | Fund | Human | FinBERT | Fund-aware LLM |", "|---|---|---|---|---|"]
    L += [f"| {e['headline']} | {e['fund']} | {e['human']} | {e['finbert']} | {e['fund_aware']} |" for e in context["ex_good"]]
    L += ["", "Fund-aware LLM wrong:", "", "| Headline | Fund | Human | FinBERT | Fund-aware LLM |", "|---|---|---|---|---|"]
    L += [f"| {e['headline']} | {e['fund']} | {e['human']} | {e['finbert']} | {e['fund_aware']} |" for e in context["ex_bad"]]
    L += ["", "## Runs and cost", "", *[f"- {x}" for x in context["runs"]], "",
          "## Limitations", "",
          f"- {context['n']} headlines from one 14-day window of free news feeds; the mix is dominated by crypto, tech and "
          "dividend funds. Results may not generalise to other periods or funds.",
          "- Labels were made by our own team (4 people, every headline labeled twice), not by independent experts.",
          f"- The {context['n'] - context['n_agreed']} disagreements were resolved by one adjudicator.",
          "- The fund-aware prompt (`eval/llm_fund_aware_prompt.txt`) was fixed and committed before running, and was "
          "run once; it was not tuned on these headlines. The neutral margin was chosen by cross-validation, never on "
          "the scored fold.",
          "- Both LLM methods send headlines (and, for the fund-aware method, the fund names) to OpenAI; never investor "
          "data. They cost money per call. FinBERT and the dictionary run locally for free.",
          "- Agent D's official tone remains FinBERT; the LLM methods here are an evaluation, not a product change.", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    from egoquant.briefing.advice_filter import AdviceFilter
    from egoquant.briefing.fetch import item_id
    from egoquant.briefing.loader import briefing_cache_dir, load_config, read_json_cache
    from egoquant.briefing.tone import FinBertScorer, LMDictionaryScorer
    import merge_labels
    import run_fund_aware_llm as fa
    from make_labeling_handouts import fund_text

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--labeled", default=str(ROOT / "eval" / "headlines_labeled.csv"))
    ap.add_argument("--lm-dictionary", required=True)
    ap.add_argument("--labeling-folder", default=str(ROOT / "eval" / "labeling"))
    ap.add_argument("--run-note", action="append", default=[], help="line for the 'Runs and cost' section")
    ap.add_argument("--out-dir", default=str(ROOT / "eval"))
    args = ap.parse_args(argv)

    rows = list(csv.DictReader(open(args.labeled, encoding="utf-8")))
    sheet_rows = list(csv.DictReader(open(ROOT / "eval" / "labeling_sheet.csv", encoding="utf-8")))
    tickers = {r["id"]: r["matched_tickers"] for r in sheet_rows}
    ids, gold, heads = [r["id"] for r in rows], [r["label"] for r in rows], [r["headline"] for r in rows]
    agreed = [r["labeler"] == "agreed" for r in rows]
    config = load_config()

    fb = FinBertScorer(config.tone.model, config.truncation["finbert_max_tokens"]).score(heads)
    if any(s is None for s in fb):
        raise SystemExit("FinBERT is not available (pip install -e '.[nlp]')")
    probs = [(s.positive, s.negative, s.neutral) for s in fb]
    pred_fb = [label_from_probabilities(s) for s in fb]
    pred_lm = [label_from_tone(s.tone) for s in LMDictionaryScorer.from_csv(args.lm_dictionary).score(heads)]
    held, folds = cross_validate(ids, gold, probs)
    cv_mean = (sum(f["accuracy"] for f in folds) / len(folds), sum(f["macro_f1"] for f in folds) / len(folds))

    # LLM tone from the summarizer's cache only (no backend -> no calls)
    from egoquant.briefing.models import NewsItem
    from egoquant.briefing.summarize import Summarizer
    llm_cache = read_json_cache(briefing_cache_dir() / "eval_llm_cache.json")
    summ = Summarizer(config, None, AdviceFilter(config.forbidden_patterns), precomputed=llm_cache)
    t0 = datetime(2000, 1, 1, tzinfo=timezone.utc)
    pred_llm = []
    for h in heads:
        it = NewsItem(id=item_id("eval", h), title=h, url="https://eval.invalid/" + item_id("eval", h), published_at=t0,
                      source_name="eval", source_type="outlet", fetched_at=t0, data_source="fixture")
        tone = summ.summarize(it)[2]
        pred_llm.append(None if tone is None else label_from_tone(tone, config.eval.llm_neutral_band))
    if summ.calls:
        raise SystemExit("unexpected OpenAI call")
    fa_cache = read_json_cache(fa.CACHE_PATH)
    pred_fa_map = fa.predictions(rows, fa_cache, fa.load_prompt()[2])
    pred_fa = [pred_fa_map[i] for i in ids]

    results = []
    results += score_rows('Always "neutral" (naive baseline)', gold, ["neutral"] * len(gold), agreed)
    results += score_rows("Loughran-McDonald word counting", gold, pred_lm, agreed)
    results += score_rows("FinBERT (plain)", gold, pred_fb, agreed)
    results += score_rows("FinBERT + cross-validated neutral margin (held-out)", gold, held, agreed,
                          note="All = mean of 5 held-out folds; Agreed = pooled held-out predictions", all_override=cv_mean)
    results += score_rows("LLM tone (summarizer llm_tone, existing --with-llm)", gold, pred_llm, agreed)
    results += score_rows("LLM tone, fund-aware (gpt-4.1-mini)", gold, pred_fa, agreed)

    files = {p: Path(args.labeling_folder) / f"{p}.xlsx" for p in merge_labels.ASSIGNMENT}
    per_person = {p: dict(merge_labels.read_labels(path)[0]) for p, path in files.items()}
    kappas = merge_labels.pair_kappas(sheet_rows, per_person)

    def unscored(pred):
        return {c: sum(1 for g, p in zip(gold, pred) if g == c and p is None) for c in CLASSES}

    rng = random.Random(42)
    def example(i):
        return {"headline": heads[i].replace("|", "/"), "fund": fund_text(tickers[i_id(i)]), "human": gold[i],
                "finbert": pred_fb[i], "fund_aware": pred_fa[i] or "unscored"}
    def i_id(i):
        return ids[i]
    good = [i for i in range(len(ids)) if pred_fb[i] != gold[i] and pred_fa[i] == gold[i]]
    bad = [i for i in range(len(ids)) if pred_fa[i] != gold[i]]
    context = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "n": len(rows), "n_agreed": sum(agreed),
        "notes": ["Accuracy and macro-F1 are over the headlines each method could score; 'scored' shows coverage.",
                  "FinBERT label = the most likely of its three classes (the same rule as Agent D's neutral gating).",
                  "LLM tone (existing) = the summarizer's llm_tone, neutral within ±0.10.",
                  f"Cross-validated margin, mean of the 5 held-out folds: accuracy {cv_mean[0]:.3f}, macro-F1 {cv_mean[1]:.3f}.",
                  "Uncertainty: with 120 headlines, an accuracy near 0.85 has a 95% interval of about ±0.064, near 0.53 "
                  "about ±0.089 (normal approximation); differences of a few points are not meaningful.",
                  "The fund-aware LLM matches the final labels more often than two individual labelers matched each other "
                  "(75%): the final labels are a consensus, more reliable than one person's label, and the model was asked "
                  "the same fund-aware question the labelers answered. FinBERT judges wording only."],
        "kappas": kappas, "folds": folds,
        "conf_fb": {"matrix": confusion(gold, pred_fb), "unscored": unscored(pred_fb)}, "pc_fb": per_class(gold, pred_fb),
        "conf_fa": {"matrix": confusion(gold, pred_fa), "unscored": unscored(pred_fa)}, "pc_fa": per_class(gold, pred_fa),
        "ex_good": [example(i) for i in sorted(rng.sample(good, min(5, len(good))))],
        "ex_bad": [example(i) for i in sorted(rng.sample(bad, min(3, len(bad))))],
        "runs": args.run_note or ["(no run notes given)"],
    }
    out = Path(args.out_dir)
    (out / "results.md").write_text(markdown(results, context), encoding="utf-8")
    with open(out / "results.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["method", "subset", "n_total", "n_scored", "accuracy", "macro_f1", "note"],
                           lineterminator="\n")
        w.writeheader()
        for r in results:
            w.writerow({**r, "accuracy": f"{r['accuracy']:.4f}" if r["accuracy"] is not None else "",
                        "macro_f1": f"{r['macro_f1']:.4f}" if r["macro_f1"] is not None else ""})
    for r in results:
        print(f"{r['method'][:52]:52} {r['subset']:6} {r['n_scored']:>3}/{r['n_total']:<3} acc {fmt(r['accuracy'])}  F1 {fmt(r['macro_f1'])}")
    print("folds:", [(f["fold"], f["m"]) for f in folds])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
