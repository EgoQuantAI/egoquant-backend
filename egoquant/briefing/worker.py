"""Background news worker (same pattern as Agent C's warm.py): rates the news BEFORE anyone asks for it.

Every `news_worker.interval_minutes` (15) it:
  1. fetches the feeds for the catalog's core funds and every fund in any interest area (US-listed, at most
     `max_tickers`, in priority order) plus every area's topic searches, every exposure theme's searches and the
     general feeds;
  2. deduplicates and appends the headlines to the local news archive (headline, link, date; no bodies);
  3. rates every new (headline, fund) pair with the unchanged, hash-pinned fund-aware prompt, newest first, in
     parallel, within `labels_per_run` and `labels_per_day` (about $0.30 a day at the default 3000).

It never runs inside a request. It is off with EGOQUANT_NEWS_WORKER=off, in fixture/off briefing mode, without
EGOQUANT_BRIEFING_LLM=1 and an OPENAI_API_KEY, with synthetic prices, and in tests (unless enabled there). The
cache and the archive live in EGOQUANT_CACHE_DIR. Its state is part of /data/status.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from egoquant.agents.market_intel import DEFAULT_CATALOG
from egoquant.briefing import web
from egoquant.briefing.archive import archive_items
from egoquant.briefing.fetch import RSSAdapter, prepare
from egoquant.briefing.loader import (BriefingConfig, CuratedSource, briefing_cache_dir, expand_query_feeds,
                                      expand_ticker_feeds, load_config, read_json_cache, with_period,
                                      write_json_cache)
from egoquant.briefing.matching import Matcher
from egoquant.briefing.models import NewsItem
from egoquant.briefing.rating import rate_pairs
from egoquant.briefing.sources import tier_of

logger = logging.getLogger(__name__)

WORKER_ENV = "EGOQUANT_NEWS_WORKER"


def _state_path():
    return briefing_cache_dir() / "news_worker_state.json"


def worker_tickers(config: BriefingConfig) -> list[str]:
    """Core funds first, then every fund (not single stocks) in any interest area, catalog order, US-listed."""
    funds = [i for i in DEFAULT_CATALOG if i.product_type != "stock" and i.market == "us_listed"]
    ordered = [i.ticker for i in funds if i.core] + [i.ticker for i in funds if not i.core and i.areas]
    return list(dict.fromkeys(ordered))[: config.news_worker.max_tickers]


def worker_feeds(config: BriefingConfig, tickers: list[str]) -> list[CuratedSource]:
    src, wc = config.live_sources, config.news_worker
    feeds = list(src.rss) + expand_ticker_feeds([f for f in src.rss_per_ticker if f.name in wc.per_ticker_feeds],
                                                tickers)
    if wc.topic_queries:
        queries = [q for qs in src.topic_queries.values() for q in qs]
        queries += [q for th in config.exposure_themes.values() for q in th.queries]     # theme news, once per theme
        feeds += expand_query_feeds(src.query_feeds, list(dict.fromkeys(queries)))
    return list({f.url: f for f in feeds}.values())


class NewsWorker:
    def __init__(self, config: BriefingConfig | None = None, *, env: Mapping[str, str] | None = None,
                 get: web.Getter | None = None, client_factory: Callable[[float], Any] | None = None,
                 now: Callable[[], datetime] | None = None):
        self.config = config or load_config()
        self.env = env if env is not None else os.environ
        self.get, self.client_factory = get, client_factory
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.status: dict[str, Any] = {"state": "idle", "runs": 0, "last_run_at": None, "last_run_seconds": None,
                                       "items_fetched": 0, "new_in_archive": 0, "pairs_found": 0, "pairs_rated": 0,
                                       "pairs_failed": 0, "pairs_left": 0, "feeds": 0, "feeds_failed": 0,
                                       "labels_today": 0, "day": None, "next_run_at": None, "last_error": None}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- caps --------------------------------------------------------------------------------------------------
    def _day_budget(self) -> int:
        """Labels left today. The count is kept in the cache folder, so a restart or redeploy does not reset it."""
        today = self.now().date().isoformat()
        if self.status["day"] != today:
            saved = read_json_cache(_state_path())
            used = int(saved.get("labels_today", 0)) if saved.get("day") == today else 0
            self.status.update(day=today, labels_today=used)
        return max(0, self.config.news_worker.labels_per_day - self.status["labels_today"])

    def _save_day(self) -> None:
        try:
            write_json_cache(_state_path(), {"day": self.status["day"], "labels_today": self.status["labels_today"]})
        except OSError as exc:
            logger.warning("News worker state not written: %s", type(exc).__name__)

    # ---- one run -----------------------------------------------------------------------------------------------
    def run_once(self) -> dict:
        cfg, wc = self.config, self.config.news_worker
        started = time.monotonic()
        self.status.update(state="running", stage="collecting news")
        try:
            as_of = self.now()
            tickers = worker_tickers(cfg)
            feeds = worker_feeds(cfg, tickers)
            bodies = web.fetch_feeds([f.url for f in feeds], self.get or web._http_get(cfg.live_sources.user_agent),
                                     total_seconds=wc.fetch_seconds, max_workers=wc.parallel_fetch)
            fetched = RSSAdapter(feeds, cfg, parse=web._parser(bodies)).fetch(as_of)
            matcher = Matcher(cfg)
            new = archive_items(fetched.items, matcher)
            month_cfg = with_period(cfg, "month")[0]
            wanted = set(tickers)
            labeler = web._WebFundTone(cfg, cache_path=briefing_cache_dir() / "fund_tone_cache.json",
                                       allow_calls=False)
            news = [it for it in prepare(fetched.items, as_of, month_cfg).items if tier_of(it, cfg)[0] != "tier3"]
            pairs: list[tuple[NewsItem, str]] = [
                (it, t) for it in sorted(news, key=lambda i: i.published_at, reverse=True)
                for t in matcher.match_item(it).tickers if t in wanted and labeler.key(it.id, t) not in labeler.cache]
            budget = min(wc.labels_per_run, self._day_budget())
            client = (self.client_factory or web._tone_client)(10.0)
            self.status.update(stage="rating", pairs_found=len(pairs), rated_so_far=0)

            def progress(rated: int, total: int) -> None:
                self.status["rated_so_far"] = rated
            res = rate_pairs(pairs, labeler, client, parallel=wc.parallel_labels, retries=wc.retries,
                             max_pairs=budget, on_progress=progress)
            self.status["labels_today"] += res.rated + res.failed
            self._save_day()
            self.status.update(
                state="idle", stage=None, runs=self.status["runs"] + 1, last_run_at=as_of.isoformat(),
                last_run_seconds=round(time.monotonic() - started, 1), items_fetched=len(fetched.items),
                new_in_archive=new, pairs_found=len(pairs), pairs_rated=res.rated, pairs_failed=res.failed,
                pairs_left=len(res.left), feeds=len(feeds),
                feeds_failed=sum(isinstance(v, Exception) for v in bodies.values()) + (len(feeds) - len(bodies)),
                last_error=None)
        except Exception as exc:
            logger.exception("News worker run failed")
            self.status.update(state="idle", last_error=type(exc).__name__,
                               last_run_seconds=round(time.monotonic() - started, 1))
        return dict(self.status)

    # ---- the loop ----------------------------------------------------------------------------------------------
    def start(self) -> "NewsWorker":
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="news-worker", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        interval = self.config.news_worker.interval_minutes * 60
        while not self._stop.is_set():
            self.run_once()
            self.status["next_run_at"] = (self.now() + timedelta(seconds=interval)).isoformat()
            self._stop.wait(interval)


_WORKER: NewsWorker | None = None


def worker_enabled(env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """(on, reason)."""
    env = os.environ if env is None else env
    if env.get(WORKER_ENV, "").strip().lower() in ("off", "0", "false", "no"):
        return False, f"{WORKER_ENV}=off"
    if web.briefing_mode(env) != "live":
        return False, "briefing mode is not live"
    if not (web._llm_switched_on(env) and env.get("OPENAI_API_KEY", "").strip()):
        return False, "EGOQUANT_BRIEFING_LLM=1 and an OPENAI_API_KEY are needed"
    if env.get("EGOQUANT_DATA", "").strip().lower() == "synthetic":
        return False, "synthetic prices"
    return True, "on"


def start_news_worker(env: Mapping[str, str] | None = None) -> NewsWorker | None:
    """Start the worker once per process (no-op when off). Never raises."""
    global _WORKER
    if _WORKER is not None:
        return _WORKER
    on, reason = worker_enabled(env)
    if not on:
        logger.info("News worker off (%s)", reason)
        return None
    try:
        _WORKER = NewsWorker(env=env).start()
        logger.info("News worker started (every %s minutes)", _WORKER.config.news_worker.interval_minutes)
    except Exception:
        logger.exception("News worker could not start")
        _WORKER = None
    return _WORKER


def news_worker_status(env: Mapping[str, str] | None = None) -> dict:
    if _WORKER is None:
        return {"state": "off", "reason": worker_enabled(env)[1]}
    return dict(_WORKER.status)


__all__ = ["NewsWorker", "WORKER_ENV", "news_worker_status", "start_news_worker", "worker_enabled",
           "worker_feeds", "worker_tickers"]
