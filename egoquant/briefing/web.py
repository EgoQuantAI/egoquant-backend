"""build_web_briefing: the market briefing for the web app (/portfolio). Never raises; None = no briefing.

Matches the pipeline's briefing_builder hook: (interests, selection, proposal, as_of, horizon_years).
Mode from the environment, EGOQUANT_BRIEFING:

    live (default)  Live RSS headlines: the official releases in config live_sources.rss plus the per-ticker
                    feeds for at most MAX_WEB_TICKERS of the investor's tickers (holdings first). All feeds
                    are fetched in parallel under one total time cap (TOTAL_FETCH_SECONDS); a feed that is
                    slower is skipped and named in the metadata. Fetched feeds are kept in memory for
                    FEED_TTL_SECONDS, so repeated requests do not hit the feed hosts again.
                    Tone: fund-aware labels from the shared cache (fund_tone_cache.json), else the FinBERT cache
                    file (torch is not installed on the server, so the model is never loaded). Only with
                    EGOQUANT_BRIEFING_LLM=1 AND an OPENAI_API_KEY: up to WEB_TONE_CALLS uncached (headline, fund)
                    pairs are labeled in parallel, inside what is left of the same TOTAL_FETCH_SECONDS budget
                    (per-call timeout WEB_TONE_TIMEOUT_SECONDS; nothing is started with less than
                    WEB_TONE_MIN_SECONDS left). Answers that arrive in time are cached for every investor; the
                    rest, and every pair with neither label, show "tone not yet assessed" and are left out of the
                    tone scores and alerts. The section counts each method and the not-yet-assessed pairs.
                    Pairs not labeled in time (up to BACKGROUND_TONE_PAIRS per request) are then labeled after
                    the response by one background worker (BACKGROUND_TONE_PER_HOUR cap) into the same cache, so
                    the next briefing shows them; EGOQUANT_BRIEFING_TONE_BACKGROUND=off disables only that.
                    Summaries are templates (headline + source). Only with EGOQUANT_BRIEFING_LLM=1 AND an
                    OPENAI_API_KEY are a few (WEB_LLM_CALLS) cached, filtered OpenAI summaries written.
                    If every source fails: None.
    fixture         The DEMO-labeled sample briefing: invented items with the committed FinBERT scores and
                    summaries. No network, no OpenAI. Its as_of is the sample's date, not today's.
    off             No briefing.

Any error -> None (logged). Like build_briefing, it only reads its inputs.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, time as dtime, timezone
from typing import Any

from egoquant.agents.market_intel import DEFAULT_CATALOG, InvestorInterests, UniverseSelection
from egoquant.briefing.advice_filter import AdviceFilter
from egoquant.briefing.build import build_briefing, relevant_tickers, select_tickers
from egoquant.briefing.fetch import FixtureAdapter, RSSAdapter, fixture_as_of, prepare
from egoquant.briefing.fund_tone import FundToneLabeler
from egoquant.briefing.loader import (FIXTURE_FUND_TONES, FIXTURE_LLM_CACHE, FIXTURE_SCORES, BriefingConfig,
                                      CuratedSource, briefing_cache_dir, expand_query_feeds,
                                      expand_ticker_feeds, load_config, read_json_cache, with_period)
from egoquant.briefing.matching import Matcher
from egoquant.briefing.sources import tier_of
from egoquant.briefing.themes import fund_themes
from egoquant.briefing.models import AI_SUMMARIES_LABEL, NO_AI_SUMMARIES_LABEL, Briefing, NewsItem
from egoquant.briefing.summarize import LLMBackend, OpenAIBackend, Summarizer
from egoquant.briefing.tone import CachedToneScorer
from egoquant.models import PortfolioProposal

logger = logging.getLogger(__name__)

MODE_ENV = "EGOQUANT_BRIEFING"                 # live (default) | fixture | off
LLM_ENV = "EGOQUANT_BRIEFING_LLM"              # 1/true/yes/on: allow a few OpenAI summaries (needs OPENAI_API_KEY)
MODES = ("live", "fixture", "off")

# PROVISIONAL. Kept here, not in config.yaml, until the briefing config owner agrees to a "web" section.
TOTAL_FETCH_SECONDS = 5.0                      # all feeds together; slower feeds are skipped
REQUEST_TIMEOUT = (2.0, 3.0)                   # per request: connect, read
MAX_WEB_TICKERS = 8                            # per-ticker feeds for at most this many tickers (holdings first)
MAX_WORKERS = 8
FEED_TTL_SECONDS = 15 * 60                     # a fetched feed is reused for this long
FAILED_FEED_TTL_SECONDS = 2 * 60               # a failing feed is not retried for this long
# /portfolio builds its briefing from the CACHE ONLY (cache_only=True): no feed request, no OpenAI call inside the
# request. The background news worker keeps the feed cache warm; the briefing job (/briefing/start, with a progress
# bar) fetches, rates and summarises the rest. Measured 19 Oct: in-request AI summaries alone took 18-62 s.
CACHE_ONLY_MAX_AGE_SECONDS = 60 * 60           # PROVISIONAL: a cached feed body this old is still used in-request
WEB_LLM_CALLS = 5                              # OpenAI calls per request when the LLM is switched on
WEB_LLM_TIMEOUT_SECONDS = 8.0
WEB_TONE_CALLS = 8                             # fund-aware tone calls per request (EGOQUANT_BRIEFING_LLM=1 only)
WEB_TONE_TIMEOUT_SECONDS = 2.0                 # per call, and never beyond the request's remaining budget
WEB_TONE_MIN_SECONDS = 1.0                     # less time left than this: no calls, "tone not yet assessed"
# Pairs the request could not label in time are labeled AFTER the response by one background worker, into the
# same shared cache, so the next briefing (any investor) shows them. Same switch as above (EGOQUANT_BRIEFING_LLM=1
# and an OPENAI_API_KEY); EGOQUANT_BRIEFING_TONE_BACKGROUND=off disables only this part (tests do).
TONE_BACKGROUND_ENV = "EGOQUANT_BRIEFING_TONE_BACKGROUND"
BACKGROUND_TONE_PAIRS = 60                     # handed to the background worker per request, most recent first
BACKGROUND_TONE_PER_HOUR = 400                 # spending cap for the background worker (all requests together)
BACKGROUND_TONE_WORKERS = 4                    # parallel calls
BACKGROUND_TONE_TIMEOUT_SECONDS = 10.0         # per call

NO_LLM_LABEL = NO_AI_SUMMARIES_LABEL           # build_briefing sets these labels itself; kept for callers
AI_LABEL = AI_SUMMARIES_LABEL

Getter = Callable[[str], bytes]


# --------------------------------------------------------------------------
# Mode
# --------------------------------------------------------------------------
def briefing_mode(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    mode = env.get(MODE_ENV, "").strip().lower() or "live"
    if mode not in MODES:
        logger.warning("%s=%r is not one of %s; using live", MODE_ENV, mode, MODES)
        return "live"
    return mode


def _llm_switched_on(env: Mapping[str, str]) -> bool:
    return env.get(LLM_ENV, "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# Feeds: parallel fetch under one total time cap, with an in-process cache across requests
# --------------------------------------------------------------------------
_cache_lock = threading.Lock()
_feed_cache: dict[str, tuple[float, bytes | None, str]] = {}      # url -> (monotonic time, body or None, error)


def clear_feed_cache() -> None:
    with _cache_lock:
        _feed_cache.clear()


def _http_get(user_agent: str) -> Getter:
    def get(url: str) -> bytes:
        import requests
        r = requests.get(url, headers={"User-Agent": user_agent}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.content
    return get


def fetch_feeds(urls: list[str], get: Getter, *, total_seconds: float = TOTAL_FETCH_SECONDS,
                clock: Callable[[], float] = time.monotonic, max_workers: int = MAX_WORKERS,
                network: bool = True) -> dict[str, bytes | Exception]:
    """Body or the reason it is missing, per URL. Returns within about total_seconds whatever the hosts do.

    A request still running at the deadline is abandoned, not awaited; if it finishes later, its body
    still goes into the cache for the next request. network=False: cached bodies only (up to
    CACHE_ONLY_MAX_AGE_SECONDS old), no request at all.
    """
    out: dict[str, bytes | Exception] = {}
    todo: list[str] = []
    now = clock()
    max_age = FEED_TTL_SECONDS if network else CACHE_ONLY_MAX_AGE_SECONDS
    with _cache_lock:
        for url in dict.fromkeys(urls):
            hit = _feed_cache.get(url)
            if hit and hit[1] is not None and now - hit[0] < max_age:
                out[url] = hit[1]
            elif not network:
                out[url] = LookupError("not in the feed cache yet")
            elif hit and hit[1] is None and now - hit[0] < FAILED_FEED_TTL_SECONDS:
                out[url] = ConnectionError(f"failed recently ({hit[2]})")
            else:
                todo.append(url)
    if not todo:
        return out

    def fetch_one(url: str) -> bytes:
        try:
            body = get(url)
        except Exception as exc:
            with _cache_lock:
                _feed_cache[url] = (clock(), None, type(exc).__name__)
            raise
        with _cache_lock:
            _feed_cache[url] = (clock(), body, "")
        return body

    pool = ThreadPoolExecutor(max_workers=min(max_workers, len(todo)), thread_name_prefix="briefing-feed")
    futures = {pool.submit(fetch_one, url): url for url in todo}
    done, not_done = wait(futures, timeout=total_seconds)
    pool.shutdown(wait=False, cancel_futures=True)
    for f in done:
        try:
            out[futures[f]] = f.result()
        except Exception as exc:
            out[futures[f]] = exc
    for f in not_done:
        out[futures[f]] = TimeoutError(f"over the {total_seconds:g} s total fetch limit")
    return out


def _parser(bodies: dict[str, bytes | Exception]) -> Callable[[str], Any]:
    """A parse(url) for RSSAdapter that reads the prefetched bodies instead of the network."""
    import feedparser

    def parse(url: str) -> Any:
        body = bodies.get(url)
        if body is None:
            raise KeyError(url)
        if isinstance(body, Exception):
            raise body
        return feedparser.parse(body)
    return parse


def briefing_feeds(interests: InvestorInterests, selection: UniverseSelection | None,
                   weights: dict[str, float] | None, config: BriefingConfig,
                   per_ticker: list[str] | None = None, tickers: list[str] | None = None) -> list[CuratedSource]:
    """Every feed for one investor's full briefing (the briefing job; never inside /portfolio): the general feeds,
    each per-ticker feed for the covered US-listed tickers (holdings first, max_live_tickers), Google News by the
    NAME of each held fund, the topic searches of the investor's areas and of the holdings' areas, and the searches
    of each held fund's exposure themes (once per theme)."""
    src = config.live_sources
    by = {i.ticker: i for i in DEFAULT_CATALOG}
    if tickers is None:
        tickers = select_tickers(interests, selection, weights, DEFAULT_CATALOG, src.max_live_tickers)[0]
    us = [t for t in tickers if t in by and by[t].market == "us_listed"]
    feeds = [f for f in src.rss_per_ticker if per_ticker is None or f.name in per_ticker]
    out = list(src.rss) + expand_ticker_feeds(feeds, us)
    queries: list[str] = []
    held = [t for t, w in sorted((weights or {}).items(), key=lambda kv: -kv[1]) if w > 1e-9 and t in by]
    if src.name_queries:
        queries += [f'"{by[t].name}"' for t in held if by[t].product_type != "stock" and by[t].market == "us_listed"]
    areas = list(dict.fromkeys([*interests.interest_areas, *(a for t in held for a in by[t].areas)]))
    queries += [q for a in areas for q in src.topic_queries.get(a, [])]
    # exposure news: each held fund's themes once (largest holding first), e.g. BIL -> "FOMC decision"
    themes = dict.fromkeys(th for t in held for th in fund_themes(by[t], config))
    queries += [q for th in themes for q in config.exposure_themes[th].queries]
    out += expand_query_feeds(src.query_feeds, list(dict.fromkeys(queries)))
    return list({f.url: f for f in out}.values())


def web_tickers(interests: InvestorInterests, selection: UniverseSelection | None,
                weights: dict[str, float] | None) -> list[str]:
    """US-listed tickers that get per-ticker feeds: the briefing's own priority order, at most MAX_WEB_TICKERS."""
    us_listed = {i.ticker for i in DEFAULT_CATALOG if i.market == "us_listed"}
    ordered = relevant_tickers(interests, selection, weights, DEFAULT_CATALOG)
    return [t for t in ordered if t in us_listed][:MAX_WEB_TICKERS]


# --------------------------------------------------------------------------
# Summaries
# --------------------------------------------------------------------------
_tone_cache_lock = threading.Lock()


class _WebFundTone(FundToneLabeler):
    """Writing the tone cache must never cost the briefing either. Requests and the background worker share one
    file, so a flush merges with what is on disk (under a lock) instead of overwriting newer labels."""

    def flush(self) -> None:
        if not (self._dirty and self.cache_path is not None):
            return
        try:
            with _tone_cache_lock:
                merged = read_json_cache(self.cache_path)
                merged.update(self.cache)
                self.cache = merged
                super().flush()
        except OSError as exc:
            logger.warning("Fund-aware tone cache not written: %s", type(exc).__name__)


class _WebSummarizer(Summarizer):
    """Writing the LLM cache must never cost the briefing (concurrent requests, read-only disk)."""

    def flush(self) -> None:
        try:
            super().flush()
        except OSError as exc:
            logger.warning("Briefing LLM cache not written: %s", type(exc).__name__)


def _llm_backend(config: BriefingConfig) -> LLMBackend:
    return OpenAIBackend(config)


def _live_summarizer(config: BriefingConfig, env: Mapping[str, str], cache_only: bool = False) -> Summarizer:
    cfg = config.model_copy(deep=True)
    advice = AdviceFilter(cfg.forbidden_patterns)
    if not (_llm_switched_on(env) and env.get("OPENAI_API_KEY", "").strip()):
        cfg.llm.enabled = False
        return _WebSummarizer(cfg, None, advice)
    if cache_only:                                      # cached summaries are used; nothing new is asked for
        return _WebSummarizer(cfg, None, advice, cache_path=briefing_cache_dir() / "llm_cache.json")
    cfg.llm.max_llm_calls_per_run = min(cfg.llm.max_llm_calls_per_run, WEB_LLM_CALLS)
    cfg.llm.timeout_seconds = min(cfg.llm.timeout_seconds, WEB_LLM_TIMEOUT_SECONDS)
    return _WebSummarizer(cfg, _llm_backend(cfg), advice, cache_path=briefing_cache_dir() / "llm_cache.json")


def _fix_labels(briefing: Briefing) -> Briefing:
    """No item has an AI summary -> do not claim "AI-generated summaries"."""
    if any(b.summary_kind == "llm" for b in briefing.items):
        return briefing
    briefing.labels = [NO_LLM_LABEL if label == AI_LABEL else label for label in briefing.labels]
    return briefing


# --------------------------------------------------------------------------
# Fund-aware tone inside the request budget
# --------------------------------------------------------------------------
def _tone_client(timeout: float):
    from openai import OpenAI                           # optional dependency (pip install -e ".[briefing]")
    return OpenAI(timeout=timeout, max_retries=0)


def pending_tone_pairs(items: list[NewsItem], as_of: datetime, config: BriefingConfig, interests: InvestorInterests,
                       selection: UniverseSelection | None, weights: dict[str, float] | None,
                       labeler: FundToneLabeler, limit: int | None = None,
                       periods: tuple[str, ...] = ("month",)) -> list[tuple[NewsItem, str]]:
    """(item, ticker) pairs the briefing will show that have no cached label, most recent first, capped: funds a
    headline is about and held funds it concerns through an exposure theme (build.briefing_pairs).

    periods: every view that will be shown ("week", "month"). Each has its own exposure caps per theme and window
    (15 a week, 40 a month), so the weekly view's "last week" exposure news is not always inside the monthly
    view's newest 40: the pairs of each view are collected and joined, so nothing shown is left unrated.
    (config: any period's config; each view's window is applied here.)"""
    from egoquant.briefing.build import briefing_pairs
    seen: dict[tuple[str, str], tuple[NewsItem, str]] = {}
    for name in periods:
        cfg = with_period(config, name)[0]
        for it, t in briefing_pairs(items, as_of, cfg, interests, selection, weights, DEFAULT_CATALOG,
                                    period_name=name):
            if labeler.key(it.id, t) not in labeler.cache:
                seen.setdefault((it.id, t), (it, t))
    pairs = list(seen.values())
    pairs.sort(key=lambda p: -p[0].published_at.timestamp())
    return pairs[:WEB_TONE_CALLS if limit is None else limit]


def label_within_budget(pairs: list[tuple[NewsItem, str]], labeler: FundToneLabeler, deadline: float,
                        client_factory: Callable[[float], Any] = _tone_client,
                        clock: Callable[[], float] = time.monotonic) -> tuple[int, int]:
    """Ask for the pairs in parallel; keep the answers that arrive before the deadline.
    Returns (calls started, answers stored). Never raises: a failed or late call only leaves that pair
    "not yet assessed"."""
    remaining = deadline - clock()
    if not pairs or remaining < WEB_TONE_MIN_SECONDS:
        return 0, 0
    try:
        client = client_factory(min(WEB_TONE_TIMEOUT_SECONDS, remaining))
    except Exception as exc:
        logger.warning("Fund-aware tone client unavailable (%s)", type(exc).__name__)
        return 0, 0
    pool = ThreadPoolExecutor(max_workers=len(pairs))
    futures = {pool.submit(lambda it=it, t=t: client.responses.create(**labeler.request(it.title, t))): (it, t)
               for it, t in pairs}
    done, _ = wait(futures, timeout=max(0.0, deadline - clock()))
    pool.shutdown(wait=False, cancel_futures=True)      # late calls are abandoned, not waited for
    stored = 0
    for f in done:
        if f.exception() is not None:
            continue
        it, t = futures[f]
        labeler.store(it.id, t, getattr(f.result(), "output_text", "") or "")
        stored += 1
    return len(pairs), stored


# --------------------------------------------------------------------------
# Fund-aware tone after the response (background worker)
# --------------------------------------------------------------------------
_bg_lock = threading.Lock()
_bg_queue: deque[tuple[NewsItem, str]] = deque()
_bg_keys: set[str] = set()                         # queued or in flight, so a pair is never asked twice
_bg_calls: deque[float] = deque()                  # start times of background calls in the last hour
_bg_thread: threading.Thread | None = None
_bg_state: dict[str, Any] = {}                     # config and client factory of the latest request


def _background_switched_on(env: Mapping[str, str]) -> bool:
    value = env.get(TONE_BACKGROUND_ENV, os.environ.get(TONE_BACKGROUND_ENV, ""))   # process env as the default
    return value.strip().lower() not in ("off", "0", "false", "no")


def schedule_background_tone(pairs: list[tuple[NewsItem, str]], labeler: FundToneLabeler, config: BriefingConfig,
                             client_factory: Callable[[float], Any] = _tone_client) -> int:
    """Queue uncached pairs for the background worker; returns how many were new. Never raises."""
    added = 0
    with _bg_lock:
        _bg_state.update(config=config, client_factory=client_factory)
        for it, t in pairs:
            key = labeler.key(it.id, t)
            if key in _bg_keys or key in labeler.cache:
                continue
            _bg_keys.add(key)
            _bg_queue.append((it, t))
            added += 1
        global _bg_thread
        if added and (_bg_thread is None or not _bg_thread.is_alive()):
            _bg_thread = threading.Thread(target=_background_worker, name="briefing-tone", daemon=True)
            _bg_thread.start()
    return added


def _take_batch(clock: Callable[[], float]) -> list[tuple[NewsItem, str]]:
    with _bg_lock:
        now = clock()
        while _bg_calls and now - _bg_calls[0] > 3600:
            _bg_calls.popleft()
        room = min(BACKGROUND_TONE_WORKERS, BACKGROUND_TONE_PER_HOUR - len(_bg_calls))
        if room <= 0 and _bg_queue:
            logger.warning("Background tone: hourly cap of %d calls reached; %d pairs dropped",
                           BACKGROUND_TONE_PER_HOUR, len(_bg_queue))
            _bg_queue.clear()
            _bg_keys.clear()
        batch = [_bg_queue.popleft() for _ in range(min(max(room, 0), len(_bg_queue)))]
        _bg_calls.extend([now] * len(batch))
        return batch


def _background_worker(clock: Callable[[], float] = time.monotonic) -> None:
    """Label queued pairs a few at a time until the queue is empty, flushing the shared cache after each batch."""
    client = None
    while True:
        batch = _take_batch(clock)
        if not batch:
            return
        config, factory = _bg_state["config"], _bg_state["client_factory"]
        labeler = _WebFundTone(config, cache_path=briefing_cache_dir() / "fund_tone_cache.json", allow_calls=False)
        try:
            client = client or factory(BACKGROUND_TONE_TIMEOUT_SECONDS)
            with ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="briefing-tone-call") as pool:
                futures = {pool.submit(lambda it=it, t=t: client.responses.create(**labeler.request(it.title, t))):
                           (it, t) for it, t in batch}
                for f in futures:
                    it, t = futures[f]
                    try:
                        labeler.store(it.id, t, getattr(f.result(), "output_text", "") or "")
                    except Exception as exc:          # one failed call only leaves that pair for a later request
                        logger.info("Background tone call failed (%s)", type(exc).__name__)
            labeler.flush()
        except Exception as exc:
            logger.warning("Background tone batch failed (%s)", type(exc).__name__)
        finally:
            with _bg_lock:
                for it, t in batch:
                    _bg_keys.discard(labeler.key(it.id, t))


def wait_for_background_tone(timeout: float = 30.0) -> bool:
    """For tests and the CLI: True once the worker has finished (or none is running)."""
    thread = _bg_thread
    if thread is not None:
        thread.join(timeout)
        return not thread.is_alive()
    return True


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def _as_datetime(as_of: date | datetime, now: datetime) -> datetime:
    """A date means "that day, up to now": end of the day in UTC, but never later than now."""
    if isinstance(as_of, datetime):
        return as_of.replace(tzinfo=timezone.utc) if as_of.tzinfo is None else as_of.astimezone(timezone.utc)
    return min(datetime.combine(as_of, dtime.max, tzinfo=timezone.utc), now)


def _fixture(interests: InvestorInterests, selection: UniverseSelection | None, proposal: PortfolioProposal | None,
             horizon_years: float, config: BriefingConfig, briefing_period: str | None = None) -> Briefing:
    as_of = fixture_as_of()
    scorer = CachedToneScorer(None, config.tone.model, precomputed=read_json_cache(FIXTURE_SCORES))
    summarizer = Summarizer(config, None, AdviceFilter(config.forbidden_patterns),
                            precomputed=read_json_cache(FIXTURE_LLM_CACHE))
    summarizer.model = config.llm.model_default       # the committed summaries were written with the default model
    items = FixtureAdapter().fetch(as_of).items
    labels = read_json_cache(FIXTURE_FUND_TONES)

    def one(period: str | None) -> Briefing:
        return build_briefing(items=items, as_of=as_of, interests=interests, horizon_years=horizon_years,
                              config=config, scorer=scorer, summarizer=summarizer, proposal=proposal,
                              selection=selection, briefing_period=period,
                              fund_tone=FundToneLabeler(config, precomputed=labels, allow_calls=False))
    return _with_month(one(briefing_period), one, briefing_period)


def _with_month(week: Briefing, one: Callable[[str | None], Briefing], period: str | None) -> Briefing:
    """The same items summarised by month too (no second fetch), attached as week.month. Only for the default
    (weekly) briefing; a failure only drops the monthly view."""
    if period not in (None, "week"):
        return week
    try:
        week.month = one("month")
    except Exception:
        logger.exception("Monthly view of the briefing failed; the weekly view is sent alone")
    return week


def _live(interests: InvestorInterests, selection: UniverseSelection | None, proposal: PortfolioProposal | None,
          as_of: datetime, horizon_years: float, config: BriefingConfig, env: Mapping[str, str],
          get: Getter | None, total_seconds: float, briefing_period: str | None = None,
          tone_client_factory: Callable[[float], Any] | None = None,
          clock: Callable[[], float] = time.monotonic, both_periods: bool = True,
          cache_only: bool = False) -> Briefing | None:
    deadline = clock() + total_seconds                  # one budget for feeds and tone calls together
    src = config.live_sources
    weights = dict(proposal.weights) if proposal is not None else None
    feeds = src.rss + expand_ticker_feeds(src.rss_per_ticker, web_tickers(interests, selection, weights))
    bodies = fetch_feeds([f.url for f in feeds], get or _http_get(src.user_agent), total_seconds=total_seconds,
                         network=not cache_only)
    fetched = RSSAdapter(feeds, config, parse=_parser(bodies)).fetch(as_of)
    if not fetched.items:
        logger.warning("Market briefing: no live items (%d sources skipped); sent without it", len(fetched.skipped))
        return None
    # Tone from the FinBERT cache only (inner=None: the model is never loaded on the server).
    scorer = CachedToneScorer(None, config.tone.model, cache_path=briefing_cache_dir() / "tone_cache.json")
    # Fund-aware tone: the shared cache; build_briefing itself makes no calls (allow_calls=False).
    labeler = _WebFundTone(config, cache_path=briefing_cache_dir() / "fund_tone_cache.json", allow_calls=False)
    n_web_calls = 0
    if _llm_switched_on(env) and env.get("OPENAI_API_KEY", "").strip() and config.tone.method == "llm_fund_aware":
        # the widest window (month) covers the week too, most recent first
        period_cfg = with_period(config, "month" if both_periods else briefing_period)[0]
        periods = ("week", "month") if both_periods else (briefing_period or config.default_briefing_period,)
        pairs = pending_tone_pairs(fetched.items, as_of, period_cfg, interests, selection, weights, labeler,
                                   periods=periods)
        if not cache_only:                           # cache_only: the cached labels only, no call in the request
            n_web_calls = label_within_budget(pairs, labeler, deadline, tone_client_factory or _tone_client,
                                              clock)[0]
        if _background_switched_on(env):             # the rest after the response, for the next briefing
            rest = pending_tone_pairs(fetched.items, as_of, period_cfg, interests, selection, weights, labeler,
                                      limit=BACKGROUND_TONE_PAIRS, periods=periods)
            schedule_background_tone(rest, labeler, period_cfg, tone_client_factory or _tone_client)
    summarizer = _live_summarizer(config, env, cache_only)

    def one(period: str | None) -> Briefing:
        b = build_briefing(items=fetched.items, as_of=as_of, interests=interests, horizon_years=horizon_years,
                           config=config, scorer=scorer, summarizer=summarizer,
                           proposal=proposal, selection=selection, sources_skipped=fetched.skipped,
                           fund_tone=labeler, briefing_period=period)
        b.metadata.n_tone_llm_calls = n_web_calls
        return b
    week = one(briefing_period)
    return _with_month(week, one, briefing_period) if both_periods else week


def build_web_briefing(
    *,
    interests: InvestorInterests,
    selection: UniverseSelection | None,
    proposal: PortfolioProposal | None,
    as_of: date | datetime,
    horizon_years: float,
    config: BriefingConfig | None = None,
    env: Mapping[str, str] | None = None,
    get: Getter | None = None,
    now: Callable[[], datetime] | None = None,
    total_seconds: float = TOTAL_FETCH_SECONDS,
    briefing_period: str | None = None,
    tone_client_factory: Callable[[float], Any] | None = None,
    cache_only: bool = False,
) -> Briefing | None:
    """The briefing for one /portfolio request, or None (mode off, no live items, or any error).

    The first five arguments and briefing_period ("week" | "month"; None = week) are the hook; the rest are for
    tests (injected config, environment, HTTP getter, clock and tone client). cache_only=True (what /portfolio
    uses): no feed request and no OpenAI call; what is not cached yet comes with the briefing job.
    """
    env = os.environ if env is None else env
    try:
        mode = briefing_mode(env)
        if mode == "off":
            return None
        config = config or load_config()
        if mode == "fixture":
            briefing = _fixture(interests, selection, proposal, horizon_years, config, briefing_period)
        else:
            # The news period ends NOW, like the briefing job's (not at the price date: prices can be days old,
            # news is not). The report shows both: "Prices as of 16 Oct · news 12-19 Oct".
            current = (now or (lambda: datetime.now(timezone.utc)))()
            briefing = _live(interests, selection, proposal, current, horizon_years, config,
                             env, get, total_seconds, briefing_period, tone_client_factory, cache_only=cache_only)
        return _fix_labels(briefing) if briefing is not None else None
    except Exception:
        logger.exception("Market briefing failed; report sent without it")
        return None


__all__ = ["LLM_ENV", "MODE_ENV", "MODES", "TOTAL_FETCH_SECONDS", "briefing_mode", "build_web_briefing",
           "clear_feed_cache", "fetch_feeds", "label_within_budget", "pending_tone_pairs", "schedule_background_tone",
           "wait_for_background_tone", "web_tickers"]
