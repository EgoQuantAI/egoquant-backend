"""Briefing jobs: the full Agent D briefing for one investor, built OUTSIDE the /portfolio request.

    POST /briefing/start {briefing_context}  ->  {job, state}
    GET  /briefing/{job}                      ->  {state, rated, total, result?}

The context is the sealed token /portfolio returned (egoquant/advisor HMAC seal, kind "briefing"): the APPROVED
weights (none when the Risk Engine rejected the proposal), interests, watchlist, max R-level, horizon and Agent C's
drawdown numbers. Nothing client-supplied is trusted, and a job never re-runs the portfolio build.

A job fetches every feed for the investor (briefing_feeds), adds the local news archive, and rates EVERY relevant
(headline, fund) pair missing from the cache (holdings first, newest first, in parallel, with retries). The ceilings
(briefing_job.max_seconds / max_pairs) are only a safety net: leftovers go to the background worker and show as
"rating in progress". Then it builds the week and the month briefing and "What changed" for both.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from egoquant.agents.market_intel import DEFAULT_CATALOG, InvestorInterests
from egoquant.agents.market_intel.agent import DrawdownAlert
from egoquant.briefing import web
from egoquant.briefing.archive import archive_items, load_archive
from egoquant.briefing.build import build_briefing, select_tickers
from egoquant.briefing.fetch import EdgarAdapter, RSSAdapter
from egoquant.briefing.loader import BriefingConfig, briefing_cache_dir, load_config, with_period
from egoquant.briefing.matching import Matcher
from egoquant.briefing.models import Briefing
from egoquant.briefing.rating import rate_pairs
from egoquant.briefing.tone import CachedToneScorer

logger = logging.getLogger(__name__)

CONTEXT_KIND = "briefing"


# --------------------------------------------------------------------------
# The sealed context
# --------------------------------------------------------------------------
def context_facts(*, interests: InvestorInterests, watchlist: list[str], weights: dict[str, float] | None,
                  horizon_years: float, drawdowns: list[DrawdownAlert], names: dict[str, str],
                  data_to: date | None, synthetic: bool, drawdown_threshold: float | None,
                  interest_tickers: list[str], report_digest: str | None = None) -> dict:
    """What a briefing job may use, sealed into /portfolio's response. weights: the APPROVED proposal's, or None."""
    dd_names = {a.ticker: names.get(a.ticker, a.ticker) for a in drawdowns}
    return {
        "kind": CONTEXT_KIND,
        "interest_areas": list(interests.interest_areas),
        "max_r_level": interests.max_r_level,
        "held_assets": list(interests.held_assets),
        "watchlist": list(watchlist),
        "weights": {t: round(w, 6) for t, w in (weights or {}).items() if w > 1e-9} if weights is not None else None,
        "horizon_years": horizon_years,
        "drawdowns": [{"ticker": a.ticker, "window_days": a.window_days,
                       "drawdown_from_peak": round(a.drawdown_from_peak, 6)} for a in drawdowns],
        "names": dd_names,
        "data_to": data_to.isoformat() if data_to else None,
        "synthetic": synthetic,
        "drawdown_threshold": drawdown_threshold,
        "interest_tickers": sorted(set(interest_tickers)),
        # fingerprint of the report's other numbers (egoquant.briefing.final): only that report may take this
        # job's final briefing
        "report_digest": report_digest,
    }


def seal_briefing_context(facts: dict) -> str:
    from egoquant.advisor.context import seal_context
    return seal_context(facts)


class BriefingContextError(ValueError):
    pass


def open_briefing_context(token: str) -> dict:
    """The facts, or BriefingContextError (bad seal, expired, wrong kind). Never trusts anything else."""
    from egoquant.advisor.context import ContextError, open_context
    try:
        facts = open_context(token)
    except ContextError as exc:
        raise BriefingContextError(str(exc)) from None
    if not isinstance(facts, dict) or facts.get("kind") != CONTEXT_KIND:
        raise BriefingContextError("not a briefing context")
    return facts


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
@dataclass
class Job:
    id: str
    created: float
    state: str = "running"                       # running | done | failed
    stage: str = "collecting news"               # collecting news | rating | building | done
    rated: int = 0
    total: int = 0
    result: dict | None = None
    error: str | None = None
    left_in_background: int = 0
    report_digest: str | None = None             # the report this job's final briefing belongs to (final.py)

    def public(self) -> dict:
        out = {"job": self.id, "state": self.state, "stage": self.stage, "rated": self.rated, "total": self.total,
               "left_in_background": self.left_in_background}
        if self.state == "done":
            out["result"] = self.result
        if self.state == "failed":
            out["error"] = "The news briefing could not be built right now; your portfolio is unaffected."
        return out


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _prune(cfg: BriefingConfig, now: float) -> None:
    ttl = cfg.briefing_job.ttl_minutes * 60
    for jid in [j for j, job in _jobs.items() if now - job.created > ttl]:
        _jobs.pop(jid, None)
    while len(_jobs) >= cfg.briefing_job.max_jobs:
        _jobs.pop(min(_jobs, key=lambda j: _jobs[j].created))


def start_job(facts: dict, *, config: BriefingConfig | None = None, env: Mapping[str, str] | None = None,
              runner: Callable[..., None] | None = None, synchronous: bool = False, **kw) -> Job:
    cfg = config or load_config()
    job = Job(id=secrets.token_urlsafe(12), created=time.monotonic(), report_digest=facts.get("report_digest"))
    with _jobs_lock:
        _prune(cfg, job.created)
        _jobs[job.id] = job
    target = runner or run_job
    if synchronous:
        target(job, facts, cfg, env if env is not None else os.environ, **kw)
    else:
        threading.Thread(target=target, args=(job, facts, cfg, env if env is not None else os.environ), kwargs=kw,
                         name=f"briefing-job-{job.id[:6]}", daemon=True).start()
    return job


def get_job(job_id: str) -> Job | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def clear_jobs() -> None:
    with _jobs_lock:
        _jobs.clear()


def _finbert_inner(config: BriefingConfig):
    """FinBERT only where it is installed AND its model is already downloaded (local Macs); never on the server."""
    try:
        from egoquant.briefing.tone import FinBertScorer
        scorer = FinBertScorer(config.tone.model, config.truncation["finbert_max_tokens"])
        if not scorer.available():
            return None
        from huggingface_hub import try_to_load_from_cache
        if try_to_load_from_cache(config.tone.model, "config.json") in (None,):
            return None
        return scorer
    except Exception:
        return None


def run_job(job: Job, facts: dict, cfg: BriefingConfig, env: Mapping[str, str], *,
            get: web.Getter | None = None, tone_client_factory: Callable[[float], Any] | None = None,
            now: Callable[[], datetime] | None = None, clock: Callable[[], float] = time.monotonic,
            finbert: bool = True) -> None:
    try:
        _run(job, facts, cfg, env, get, tone_client_factory, now, clock, finbert)
        job.stage, job.state = "done", "done"
    except Exception:
        logger.exception("Briefing job failed")
        job.state = "failed"


def _drawdowns(facts: dict) -> list[DrawdownAlert]:
    return [DrawdownAlert(ticker=d["ticker"], window_days=d["window_days"],
                          drawdown_from_peak=d["drawdown_from_peak"], message="")
            for d in facts.get("drawdowns") or []]


def _offer(facts: dict, briefing: Briefing | None) -> bool:
    """The re-check offer: a holding's tone fell by the threshold, or a holding above 5% fell 10% from its high."""
    from egoquant.report import drawdown_offered, recheck_offered
    return recheck_offered(briefing) or drawdown_offered(_drawdowns(facts), facts.get("weights") or {},
                                                        facts.get("drawdown_threshold"))


def _what_changed(facts: dict, briefing: Briefing | None) -> list[str]:
    from egoquant.report import what_changed_lines
    drawdowns = _drawdowns(facts)
    weights = facts.get("weights") or {}
    holdings = [t for t, w in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0])) if w > 1e-9]
    data_to = date.fromisoformat(facts["data_to"]) if facts.get("data_to") else None
    # "What changed" names HOLDINGS only (round 4, A2): with holdings, interest/watchlist drawdowns are not named
    # (they fall into the one "other instruments" count line). Without an approved portfolio, interests are all
    # there is.
    interests = set() if holdings else set(facts.get("interest_tickers") or [])
    return what_changed_lines(drawdowns, briefing, facts.get("names") or {}, data_to,
                              synthetic=bool(facts.get("synthetic")), holdings=holdings,
                              interest_tickers=interests,
                              drawdown_threshold=facts.get("drawdown_threshold"))


def _run(job: Job, facts: dict, cfg: BriefingConfig, env: Mapping[str, str], get, tone_client_factory, now, clock,
         finbert: bool) -> None:
    start = clock()
    jc = cfg.briefing_job
    interests = InvestorInterests(investor_id="briefing", interest_areas=list(facts.get("interest_areas") or []),
                                  max_r_level=facts.get("max_r_level"), held_assets=list(facts.get("held_assets") or []))
    selection = SimpleNamespace(watchlist_tickers=list(facts.get("watchlist") or []))
    weights = facts.get("weights")
    proposal = SimpleNamespace(weights=dict(weights)) if weights is not None else None
    horizon = float(facts.get("horizon_years") or 10)
    mode = web.briefing_mode(env)
    result: dict[str, Any] = {"week": None, "month": None, "what_changed": [], "what_changed_month": [],
                              "what_changed_action": None, "what_changed_month_action": None}
    if mode == "off":
        job.result = result
        return
    if mode == "fixture":
        week = web._fixture(interests, selection, proposal, horizon, cfg)
        month = week.month
        week.month = None
    else:
        as_of = (now or (lambda: datetime.now(timezone.utc)))()
        feeds = web.briefing_feeds(interests, selection, weights, cfg)
        bodies = web.fetch_feeds([f.url for f in feeds], get or web._http_get(cfg.live_sources.user_agent),
                                 total_seconds=jc.fetch_seconds)
        fetched = RSSAdapter(feeds, cfg, parse=web._parser(bodies)).fetch(as_of)
        if env.get("SEC_USER_AGENT", "").strip() or cfg.live_sources.edgar_enabled:
            by = {i.ticker: i.name for i in DEFAULT_CATALOG}
            held = [t for t in (weights or {}) if t in by][:10]
            if held:
                edgar = EdgarAdapter({t: by[t] for t in held}, cfg, env=dict(env)).fetch(as_of)
                fetched.items += edgar.items
                fetched.skipped += edgar.skipped
        matcher = Matcher(cfg)
        try:
            archive_items(fetched.items, matcher)
        except OSError as exc:
            logger.warning("News archive not written: %s", type(exc).__name__)
        lookback = with_period(cfg, "month")[1].lookback_days
        fresh = {it.id for it in fetched.items}
        items = fetched.items + [it for it in load_archive(since=as_of - timedelta(days=lookback)) if it.id not in fresh]
        labeler = web._WebFundTone(cfg, cache_path=briefing_cache_dir() / "fund_tone_cache.json", allow_calls=False)
        llm_on = (web._llm_switched_on(env) and env.get("OPENAI_API_KEY", "").strip()
                  and cfg.tone.method == "llm_fund_aware")
        if llm_on:
            job.stage = "rating"
            month_cfg = with_period(cfg, "month")[0]
            pairs = web.pending_tone_pairs(items, as_of, month_cfg, interests, selection, weights, labeler,
                                           limit=10_000, periods=("week", "month"))     # both views it shows
            held = set(weights or {})
            pairs.sort(key=lambda p: (p[1] not in held, -p[0].published_at.timestamp()))
            job.total = len(pairs)
            client = (tone_client_factory or web._tone_client)(10.0)
            remaining = max(1.0, jc.max_seconds - (clock() - start))

            def progress(rated: int, total: int) -> None:
                job.rated = rated
            res = rate_pairs(pairs, labeler, client, parallel=jc.parallel_labels, retries=jc.retries,
                             deadline=clock() + remaining, max_pairs=jc.max_pairs, on_progress=progress, clock=clock)
            job.rated = res.rated
            if res.left and web._background_switched_on(env):
                web.schedule_background_tone(res.left, labeler, month_cfg, tone_client_factory or web._tone_client)
            job.left_in_background = len(res.left)
        job.stage = "building"
        inner = _finbert_inner(cfg) if finbert else None
        scorer = CachedToneScorer(inner, cfg.tone.model, cache_path=briefing_cache_dir() / "tone_cache.json",
                                  neutral_gating=cfg.tone.neutral_gating)
        summarizer = web._live_summarizer(cfg, env)

        def one(period: str) -> Briefing:
            return build_briefing(items=items, as_of=as_of, interests=interests, horizon_years=horizon, config=cfg,
                                  scorer=scorer, summarizer=summarizer, proposal=proposal, selection=selection,
                                  sources_skipped=fetched.skipped, fund_tone=labeler, briefing_period=period)
        week, month = one("week"), one("month")
        for b in (week, month):
            b.metadata.n_tone_llm_calls = job.rated
            # a finished job names the plain reason for any headline still without a rating
            b.metadata.tone_fallback_reasons = (
                [] if not b.metadata.n_fund_tones_unavailable
                else ["the rating limit for one briefing was reached; the rest is rated in the background"]
                if job.left_in_background
                else ["AI rating is switched off on this server"] if not llm_on
                else ["the AI rating service did not answer for these headlines"])
        week, month = web._fix_labels(week), web._fix_labels(month)
    for b in (week, month):                               # live and fixture alike: this is the finished version
        if b is not None:
            b.metadata.final_job = job.id                    # (final.news_is_final)
    result["week"] = week.model_dump(mode="json")
    result["month"] = month.model_dump(mode="json") if month is not None else None
    wc, wcm = _what_changed(facts, week), (_what_changed(facts, month) if month is not None else [])
    result.update(what_changed=wc, what_changed_month=wcm,      # the offer: tone falls and held drawdowns only
                  what_changed_action="offer_risk_profile_recheck" if wc and _offer(facts, week) else None,
                  what_changed_month_action="offer_risk_profile_recheck"
                  if wcm and month is not None and _offer(facts, month) else None,
                  rating={"rated": job.rated, "total": job.total, "left_in_background": job.left_in_background})
    job.result = result


__all__ = ["BriefingContextError", "Job", "clear_jobs", "context_facts", "get_job", "open_briefing_context",
           "run_job", "seal_briefing_context", "start_job"]
