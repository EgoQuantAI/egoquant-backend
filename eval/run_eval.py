"""Tone methods vs hand-labeled headlines: accuracy, macro-F1 and how many headlines each method scored.

    python eval/run_eval.py                                  FinBERT + Loughran-McDonald (both free, local)
    python eval/run_eval.py --lm-dictionary path/to/LM.csv   path to the LM Master Dictionary you downloaded
    python eval/run_eval.py --with-llm                       ALSO the LLM tone (OpenAI; opt-in, capped, cached)

Writes eval/results.md and eval/results.csv. FinBERT needs `pip install -e ".[nlp]"`. Never evaluate on
Financial PhraseBank: FinBERT was fine-tuned on it.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egoquant.briefing.advice_filter import AdviceFilter  # noqa: E402
from egoquant.briefing.evaluation import (evaluate, label_from_probabilities, label_from_tone, load_labeled,  # noqa: E402
                                          results_markdown, results_rows, write_csv)
from egoquant.briefing.fetch import item_id  # noqa: E402
from egoquant.briefing.loader import briefing_cache_dir, load_config  # noqa: E402
from egoquant.briefing.models import NewsItem  # noqa: E402
from egoquant.briefing.summarize import OpenAIBackend, Summarizer  # noqa: E402
from egoquant.briefing.tone import FinBertScorer, LMDictionaryScorer  # noqa: E402


def finbert_predictor(config):
    scorer = FinBertScorer(config.tone.model, config.truncation["finbert_max_tokens"])
    if not scorer.available():
        return None
    return lambda texts: [label_from_probabilities(s) if s else None for s in scorer.score(texts)]


def lm_predictor(path: str | None):
    if not path or not Path(path).exists():
        return None
    scorer = LMDictionaryScorer.from_csv(path)
    return lambda texts: [label_from_tone(s.tone) for s in scorer.score(texts)]


def llm_predictor(config, summarizer: Summarizer):
    """LLM tone from the SAME call that writes the summary (cached; one call per headline at most)."""
    now = datetime(2000, 1, 1, tzinfo=timezone.utc)

    def run(texts):
        out = []
        for t in texts:
            item = NewsItem(id=item_id("eval", t), title=t, url="https://eval.invalid/" + item_id("eval", t),
                            published_at=now, source_name="eval", source_type="outlet", fetched_at=now,
                            data_source="fixture")
            _, _, llm_tone, _ = summarizer.summarize(item)
            out.append(None if llm_tone is None else label_from_tone(llm_tone, config.eval.llm_neutral_band))
        summarizer.flush()
        return out
    return run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--labeled", default=str(ROOT / "eval" / "headlines_labeled.csv"))
    ap.add_argument("--lm-dictionary", default=os.environ.get("LM_DICTIONARY_PATH"))
    ap.add_argument("--with-llm", action="store_true", help="also score with the LLM (OpenAI, costs money)")
    ap.add_argument("--yes", action="store_true", help="allow more LLM calls than max_llm_calls_per_run")
    ap.add_argument("--out-dir", default=str(ROOT / "eval"))
    args = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    config = load_config()
    rows = load_labeled(args.labeled)
    examples = sum(r.labeler == "example" for r in rows)
    predictors, notes = {}, []
    fb = finbert_predictor(config)
    if fb:
        predictors["FinBERT (ProsusAI/finbert, local)"] = fb
    else:
        notes.append("FinBERT skipped: not installed (pip install -e '.[nlp]').")
    lm = lm_predictor(args.lm_dictionary)
    if lm:
        predictors["Loughran-McDonald dictionary"] = lm
    else:
        notes.append("Loughran-McDonald skipped: pass --lm-dictionary or set LM_DICTIONARY_PATH.")
    if args.with_llm:
        if not OpenAIBackend.key_present():
            print("--with-llm needs OPENAI_API_KEY", file=sys.stderr)
            return 2
        backend = OpenAIBackend(config)
        summarizer = Summarizer(config, backend, AdviceFilter(config.forbidden_patterns),
                                cache_path=briefing_cache_dir() / "eval_llm_cache.json")
        pending = sum(f"{item_id('eval', r.headline)}|{summarizer.model}|{config.llm.prompt_version}"
                      not in summarizer.cache for r in rows)
        cap = config.llm.max_llm_calls_per_run
        print(f"LLM tone: {pending} OpenAI calls needed ({len(rows) - pending} cached); cap {cap} per run.")
        if pending > cap and not args.yes:
            print("Stopping: more calls than the cap. Re-run with --yes to allow it.", file=sys.stderr)
            return 3
        if args.yes:
            config.llm.max_llm_calls_per_run = max(cap, pending)
            summarizer.config = config
        predictors[f"LLM tone ({summarizer.model}, evaluation only)"] = llm_predictor(config, summarizer)
    if not predictors:
        print("No method available. " + " ".join(notes), file=sys.stderr)
        return 1
    results = evaluate(rows, predictors)
    if args.with_llm:
        cost = backend.input_tokens / 1e6 * 0.40 + backend.output_tokens / 1e6 * 1.60   # gpt-4.1-mini list prices
        print(f"LLM calls this run: {summarizer.calls}; cache hits: {summarizer.cache_hits}; tokens in/out: "
              f"{backend.input_tokens}/{backend.output_tokens}; cost about ${cost:.4f} at list price")
    note = " ".join(notes)
    if examples:
        note += f" WARNING: {examples} of {len(rows)} rows are the example rows, not real labels."
    md = results_markdown(results, note.strip())
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.md").write_text(md, encoding="utf-8")
    write_csv(out / "results.csv", results_rows(results), ["method", "n_total", "n_scored", "accuracy", "macro_f1"])
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
