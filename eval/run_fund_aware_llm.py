"""Fund-aware LLM tone on the hand-labeled headlines (EVALUATION ONLY; not Agent D's official tone).

    python eval/run_fund_aware_llm.py                 OpenAI calls for headlines not yet cached (cap 130)

For each headline in eval/headlines_labeled.csv, the model gets the fund(s) the headline was matched to (ticker
and plain-English name, from eval/labeling_sheet.csv) and is asked whether the headline is likely good, bad or
neutral/unclear news for an investor who HOLDS that fund. The prompt is Agent D's production prompt,
egoquant/briefing/prompts/fund_aware_tone.txt (byte-identical to eval/llm_fund_aware_prompt.txt as fixed and
committed in 1adb563 before the first call); its hash is part of the cache key, so a changed prompt would never
reuse old answers. Prompt loading, sanitising and parsing are shared with Agent D (egoquant/briefing/fund_tone.py).

Rules:
  * Model gpt-4.1-mini, temperature 0, store=False. Only the headline and the fund names are sent: no investor
    data. The headline is untrusted: angle brackets are replaced so it cannot close the <headline> tag, and the
    system prompt tells the model to ignore instructions inside it.
  * The answer must be exactly one of positive / negative / neutral (surrounding whitespace and letter case are
    ignored; anything else, including punctuation, counts as UNSCORED).
  * Answers are cached outside the repository (settings.cache_dir()/briefing/eval_fund_aware_cache.json);
    each headline is sent at most once. The run stops before calling if more than --max-calls would be needed.
Per-headline answers stay in the cache, never in the repository.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from egoquant.briefing.fund_tone import (PROMPT_PATH, fund_text, load_prompt, parse_answer,  # noqa: E402,F401
                                         safe_headline, user_message)
from egoquant.briefing.loader import briefing_cache_dir, read_json_cache, write_json_cache  # noqa: E402

MODEL = "gpt-4.1-mini"
LABELS = ("positive", "negative", "neutral")
CACHE_PATH = briefing_cache_dir() / "eval_fund_aware_cache.json"
# gpt-4.1-mini list prices in USD per 1M tokens, used only to report the cost of this run (check OpenAI's page).
PRICE_INPUT, PRICE_OUTPUT = 0.40, 1.60


def cache_key(row_id: str, prompt_hash: str) -> str:
    return f"{row_id}|{MODEL}|{prompt_hash}"


def run(rows: list[dict], tickers_by_id: dict[str, str], client, max_calls: int,
        cache: dict, prompt: tuple[str, str, str]) -> dict:
    """Ask the model for every row not yet cached. Returns usage counts. Raises before calling if over the cap."""
    system, template, phash = prompt
    pending = [r for r in rows if cache_key(r["id"], phash) not in cache]
    if len(pending) > max_calls:
        raise SystemExit(f"{len(pending)} calls needed, more than the cap of {max_calls}; nothing was sent.")
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    for r in pending:
        resp = client.responses.create(model=MODEL, instructions=system,
                                       input=user_message(template, r["headline"], tickers_by_id[r["id"]]),
                                       temperature=0, max_output_tokens=16, store=False)
        usage["calls"] += 1
        u = getattr(resp, "usage", None)
        usage["input_tokens"] += int(getattr(u, "input_tokens", 0) or 0)
        usage["output_tokens"] += int(getattr(u, "output_tokens", 0) or 0)
        cache[cache_key(r["id"], phash)] = {"answer": getattr(resp, "output_text", "") or ""}
    return usage


def predictions(rows: list[dict], cache: dict, prompt_hash: str) -> dict[str, str | None]:
    """{id: label or None} from the cache (None = unscored or not asked)."""
    return {r["id"]: parse_answer((cache.get(cache_key(r["id"], prompt_hash)) or {}).get("answer"))
            for r in rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--labeled", default=str(ROOT / "eval" / "headlines_labeled.csv"))
    ap.add_argument("--max-calls", type=int, default=130)
    args = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    rows = list(csv.DictReader(open(args.labeled, encoding="utf-8")))
    sheet = {r["id"]: r["matched_tickers"] for r in csv.DictReader(open(ROOT / "eval" / "labeling_sheet.csv",
                                                                         encoding="utf-8"))}
    prompt = load_prompt()
    cache = read_json_cache(CACHE_PATH)
    pending = sum(cache_key(r["id"], prompt[2]) not in cache for r in rows)
    print(f"prompt {PROMPT_PATH.name} (hash {prompt[2]}); {pending} calls needed, {len(rows) - pending} cached; "
          f"cap {args.max_calls}")
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    if pending:
        if not os.environ.get("OPENAI_API_KEY", "").strip():
            print("OPENAI_API_KEY is not set", file=sys.stderr)
            return 2
        from openai import OpenAI
        try:
            usage = run(rows, sheet, OpenAI(timeout=30, max_retries=1), args.max_calls, cache, prompt)
        finally:
            write_json_cache(CACHE_PATH, cache)
    preds = predictions(rows, cache, prompt[2])
    cost = usage["input_tokens"] / 1e6 * PRICE_INPUT + usage["output_tokens"] / 1e6 * PRICE_OUTPUT
    print(f"calls this run: {usage['calls']}; tokens in/out: {usage['input_tokens']}/{usage['output_tokens']}; "
          f"cost about ${cost:.4f} at list price")
    print(f"scored: {sum(v is not None for v in preds.values())} / {len(preds)}; "
          f"unscored: {sum(v is None for v in preds.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
