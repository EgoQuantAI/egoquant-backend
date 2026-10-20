"""Fund-aware news tone: for an investor who HOLDS a fund, is this headline good, bad or neutral news?

This is Agent D's primary tone (config tone.method = "llm_fund_aware"). FinBERT stays as the automatic fallback.
Evaluation (eval/results.md): on 120 headlines labeled by the team for the matched fund, this prompt matched the
human label 85% of the time; FinBERT, which only reads the wording, 53%.

Rules:
  * The prompt is EXACTLY the evaluated one: prompts/fund_aware_tone.txt, byte-identical to the file committed
    in 1adb563 (EVALUATED_PROMPT_SHA256; a test checks it). Never edit it; a new prompt needs a new evaluation.
  * Model gpt-4.1-mini (the evaluated model), temperature 0, store=False.
  * Input: only the headline (untrusted: angle brackets replaced so it cannot close the <headline> tag) and ONE
    fund (ticker + plain-English name). Never investor data, weights or ids.
  * Cache: one answer per (headline id, ticker, prompt hash, model), shared across ALL investors, so no request
    and no cache entry reveals whose portfolio a fund is in. A cached pair is never sent again.
  * Strict parsing: exactly positive / negative / neutral, otherwise "unscored" -> FinBERT for that pair.
  * No new calls (FinBERT fallback for uncached pairs) when: llm.enabled is false, no OPENAI_API_KEY, the per-run
    cap tone.llm_max_calls_per_run is reached, or an API/network error happened (no further calls that run).
    Cached answers are still used: they cost nothing and send nothing.
  * The LLM label only feeds the tone aggregates and the alert that OFFERS a risk-profile re-check. It never sets
    a score, weight, limit or approval.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from egoquant.agents.market_intel import DEFAULT_CATALOG
from egoquant.briefing.loader import BriefingConfig, read_json_cache, write_json_cache
from egoquant.briefing.models import NewsItem

logger = logging.getLogger(__name__)

LABELS = ("positive", "negative", "neutral")
PROMPT_PATH = Path(__file__).parent / "prompts" / "fund_aware_tone.txt"
EVALUATED_PROMPT_SHA256 = "ebb752b547507ffcf8edd43fa8eac0bb4dd9990249355a86bbe2bb50ea5c8544"   # commit 1adb563

# Plain-English description per fund, as shown to the labelers and to the model in the evaluation. Any other
# catalog ticker (Agent C's catalog also lists single stocks) falls back to its catalog name.
FUND_PLAIN_NAMES = {
    "BIL": "1-3 month US Treasury bills (cash-like)",
    "AGG": "US investment-grade bond market",
    "SHY": "short-term (1-3 year) US Treasury bonds",
    "VT": "global stock market (all countries)",
    "VTI": "whole US stock market",
    "VXUS": "international stocks outside the US",
    "VWO": "emerging-market stocks",
    "QQQ": "Nasdaq-100 (large US tech companies)",
    "VB": "US small-company stocks",
    "SCHD": "US dividend-paying stocks",
    "VNQ": "US real estate (REITs)",
    "IEF": "7-10 year US Treasury bonds",
    "TLT": "long-term US Treasury bonds",
    "LQD": "US investment-grade corporate bonds",
    "TIP": "US inflation-protected Treasury bonds (TIPS)",
    "BNDX": "international bonds (currency-hedged)",
    "IAU": "gold",
    "SLV": "silver",
    "PDBC": "broad commodities (oil, metals, crops)",
    "IBIT": "bitcoin fund (ETF)",
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ether (Ethereum)",
}


def fund_text(tickers: str) -> str:
    """'TLT - long-term US Treasury bonds'; several tickers (space-separated) joined with '; '."""
    names = {i.ticker: i.name for i in DEFAULT_CATALOG}
    return "; ".join(f"{t} - {FUND_PLAIN_NAMES.get(t, names.get(t, t))}" for t in tickers.split())


# --------------------------------------------------------------------------
# Prompt, sanitising, parsing (shared with eval/run_fund_aware_llm.py)
# --------------------------------------------------------------------------
def prompt_sha256(path: Path = PROMPT_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_prompt(path: Path = PROMPT_PATH) -> tuple[str, str, str]:
    """(system, user template, 12-character prompt hash)."""
    text = path.read_text(encoding="utf-8")
    system = text.split("=== SYSTEM ===", 1)[1].split("=== USER ===", 1)[0].strip()
    user = text.split("=== USER ===", 1)[1].strip()
    if "{funds}" not in user or "{headline}" not in user:
        raise ValueError(f"{path}: the user template needs {{funds}} and {{headline}}")
    return system, user, hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def safe_headline(headline: str) -> str:
    """Untrusted text: it must not be able to close or open the <headline> tag."""
    return headline.replace("<", "(").replace(">", ")")


def user_message(template: str, headline: str, tickers: str) -> str:
    return template.replace("{funds}", fund_text(tickers)).replace("{headline}", safe_headline(headline))


def parse_answer(text: str | None) -> str | None:
    """The label, or None (unscored) unless the answer is exactly one of the three words (case and surrounding
    whitespace ignored; anything else, including punctuation, is unscored)."""
    word = (text or "").strip().lower()
    return word if word in LABELS else None


# --------------------------------------------------------------------------
# Labeler
# --------------------------------------------------------------------------
class FundToneLabeler:
    """Labels (headline, ticker) pairs. Cache keys hold no investor information.

    client: an OpenAI-like client (tests inject a fake); None = create one lazily when a call is allowed.
    allow_calls: False = cache only (e.g. the web request path, tests, the offline demo).
    """

    def __init__(self, config: BriefingConfig, *, client: Any | None = None, cache_path: Path | None = None,
                 precomputed: dict[str, dict] | None = None, allow_calls: bool = True,
                 env: dict[str, str] | None = None):
        self.config, self._client, self.cache_path = config, client, cache_path
        self.allow_calls = allow_calls
        self.env = env if env is not None else os.environ
        self.model = config.tone.llm_model
        self.system, self.template, self.prompt_hash = load_prompt()
        self.cache: dict[str, dict] = dict(precomputed or {})
        if cache_path is not None:
            self.cache.update(read_json_cache(cache_path))
        self.calls = self.cache_hits = self.unscored = self.errors = 0
        self.input_tokens = self.output_tokens = 0
        self.fallback_reasons: set[str] = set()
        self._broken = False
        self._dirty = False

    def key(self, item_id: str, ticker: str) -> str:
        return f"{item_id}|{ticker}|{self.prompt_hash}|{self.model}"

    def request(self, headline: str, ticker: str) -> dict[str, Any]:
        """The exact request for one (headline, fund) pair: the headline and one fund, nothing else."""
        return dict(model=self.model, instructions=self.system, input=user_message(self.template, headline, ticker),
                    temperature=0, max_output_tokens=16, store=False)

    def store(self, item_id: str, ticker: str, answer: str) -> None:
        """Record an answer obtained outside label() (e.g. the web path's parallel calls)."""
        self.cache[self.key(item_id, ticker)] = {"answer": answer}
        self._dirty = True

    def _can_call(self) -> bool:
        if not self.allow_calls:
            self.fallback_reasons.add("cache only")
            return False
        if not self.config.llm.enabled:
            self.fallback_reasons.add("llm.enabled is false")
            return False
        if self._client is None and not self.env.get("OPENAI_API_KEY", "").strip():
            self.fallback_reasons.add("no OPENAI_API_KEY")
            return False
        if self._broken:
            return False
        if self.calls >= self.config.tone.llm_max_calls_per_run:
            self.fallback_reasons.add(f"call cap reached ({self.config.tone.llm_max_calls_per_run} per run)")
            return False
        return True

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI                       # optional dependency (pip install -e ".[briefing]")
            self._client = OpenAI(timeout=self.config.llm.timeout_seconds, max_retries=1)
        return self._client

    def label(self, item: NewsItem, ticker: str) -> str | None:
        """positive / negative / neutral, or None (not cached and no call possible, error, or unscored)."""
        key = self.key(item.id, ticker)
        if key in self.cache:
            self.cache_hits += 1
            answer = parse_answer(self.cache[key].get("answer"))
            if answer is None:
                self.unscored += 1
            return answer
        if not self._can_call():
            return None
        self.calls += 1
        try:
            resp = self._get_client().responses.create(**self.request(item.title, ticker))
        except Exception as exc:                            # network, auth, rate limit, timeout
            self.errors += 1
            self._broken = True
            self.fallback_reasons.add(f"API error ({type(exc).__name__})")
            logger.warning("fund-aware tone call failed (%s); FinBERT for the rest of this run", type(exc).__name__)
            return None
        usage = getattr(resp, "usage", None)
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        text = getattr(resp, "output_text", "") or ""
        self.cache[key] = {"answer": text}
        self._dirty = True
        answer = parse_answer(text)
        if answer is None:
            self.unscored += 1
        return answer

    def flush(self) -> None:
        if self._dirty and self.cache_path is not None:
            write_json_cache(self.cache_path, self.cache)
            self._dirty = False
