"""Warm Agent C's price cache ahead of time: the long history of the most relevant tickers of every cluster.

Tiingo serves one ticker per request (no batch endpoint), 50 requests an hour, 1,000 a day and 500
distinct tickers a month on the free tier. A portfolio build may spend only DEFAULT_FETCH_BUDGET new
requests, so without a warm cache a trader's first build sees ~2 years for most funds and the goal
simulation leans on completed histories. This job fetches the ~20-year history (Agent A's
goal_history_days) once, in priority order, and pauses when Tiingo's hourly limit is reached:

    core funds  ->  rank 1 of EVERY cluster  ->  rank 2 of every cluster  ->  ...  up to --per-cluster

Round-robin over clusters keeps coverage even: whatever a trader picks, its most traded funds are warm.
Already-cached tickers cost nothing; the job stops before the monthly distinct-ticker guard
(sources.TiingoUsage). Afterwards portfolio builds read these prices from disk at no request cost.

    python -m egoquant warm-cache [--per-cluster 10] [--areas technology,bonds] [--wait] [--dry-run]

Adjusted closes are restated after dividends and splits, and a cached range is trusted up to the day
before it was fetched, so run it again when the decision date (as_of) moves on (e.g. daily).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from egoquant import settings

from egoquant.agents.market_intel.catalog import DEFAULT_CATALOG, Instrument
from egoquant.agents.market_intel.directory import Directory, merge_catalog
from egoquant.agents.market_intel.sources import (
    CachedPriceSource,
    PriceSourceError,
    RateLimitError,
    TiingoPriceSource,
    TiingoUsage,
    tiingo_limit_status,
    tiingo_requests_in_last,
)

logger = logging.getLogger(__name__)


@dataclass
class WarmReport:
    planned: int = 0
    already_cached: list[str] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    waits: int = 0
    stopped: str | None = None                # why it stopped before the end of the plan, if it did


def warm_plan(directory: Directory | None, per_cluster: int = 10, areas: list[str] | None = None,
              curated: list[Instrument] | None = None) -> list[str]:
    """US-listed tickers to warm, in priority order (core funds, then round-robin over the clusters).

    One fund per exposure group per cluster (the representative Agent A would get); single stocks and
    funds alike, in the directory's ranking (most traded first)."""
    catalog = merge_catalog(directory, curated or DEFAULT_CATALOG)
    us = [i for i in catalog if i.market == "us_listed"]
    plan: list[str] = [i.ticker for i in us if i.core]
    clusters: dict[str, list[str]] = {}
    for inst in us:
        for area in inst.areas:
            if areas is None or area in areas:
                clusters.setdefault(area, []).append(inst.ticker)
    group_of = {i.ticker: i.exposure_group for i in us}
    queues: list[list[str]] = []
    for area in sorted(clusters):
        seen_groups: set[str] = set()
        queue: list[str] = []
        for t in clusters[area]:
            g = group_of[t]
            if g and g in seen_groups:
                continue
            if g:
                seen_groups.add(g)
            queue.append(t)
            if len(queue) >= per_cluster:
                break
        queues.append(queue)
    for rank in range(per_cluster):
        for queue in queues:
            if rank < len(queue) and queue[rank] not in plan:
                plan.append(queue[rank])
    return plan


def warm_cache(plan: list[str], as_of: date, start: date, *, source: CachedPriceSource | None = None,
               usage: TiingoUsage | None = None, wait: bool = True, wait_minutes: float = 15.0,
               max_waits: int = 16, max_requests: int | None = None,
               log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
               before_request: Callable[[], None] | None = None) -> WarmReport:
    """Fetch [start, as_of] for every planned ticker the cache does not already cover.

    before_request: called before each NEW request (the background builder waits there while the shared
    hourly Tiingo budget is in use)."""
    key = settings.tiingo_warm_api_key()                 # the builder's own key (falls back to the traders')
    usage = usage or TiingoUsage(api_key=key or None)
    source = source or CachedPriceSource(TiingoPriceSource(api_key=key, usage=usage))
    report = WarmReport(planned=len(plan))
    for n, ticker in enumerate(plan, 1):
        if not source.needs_request(ticker, start, as_of):
            report.already_cached.append(ticker)
            continue
        if max_requests is not None and len(report.fetched) + len(report.failed) >= max_requests:
            report.stopped = f"request cap of {max_requests} reached"
            break
        if not usage.allows(ticker):
            report.stopped = "Tiingo's monthly distinct-ticker guard reached"
            break
        while True:
            if before_request is not None:
                before_request()
            try:
                frame = source.fetch_adjusted_close([ticker], start, as_of)
                if ticker in frame.columns and frame[ticker].notna().any():
                    report.fetched.append(ticker)
                    s = frame[ticker].dropna()
                    log(f"[{n}/{len(plan)}] {ticker}: {len(s)} days from {s.index.min().date()}")
                else:
                    report.failed[ticker] = "no data from Tiingo"
                    log(f"[{n}/{len(plan)}] {ticker}: no data")
                break
            except RateLimitError:
                if not wait or report.waits >= max_waits:
                    report.stopped = "Tiingo's hourly limit reached"
                    return report
                report.waits += 1
                log(f"Tiingo's hourly limit reached after {len(report.fetched)} fetches; waiting {wait_minutes:g} min "
                    f"({report.waits}/{max_waits})")
                sleep(wait_minutes * 60)
            except PriceSourceError as exc:
                report.failed[ticker] = str(exc)
                log(f"[{n}/{len(plan)}] {ticker}: failed ({exc})")
                if "refused the API key" in str(exc):
                    report.stopped = "Tiingo refused the API key"
                    return report
                break
    return report


def last_weekday_before(day: date) -> date:
    """The last Monday-Friday strictly before `day`: the most recent close that is final."""
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


class CacheWarmer:
    """Keeps the price cache warm continuously, whether or not traders use the app.

    Every cycle: today's decision date, the warm plan (warm_plan), and warm_cache over it. New requests are
    made only while the process has made fewer than `per_hour` Tiingo requests in the last hour (portfolio
    builds count too), so traders keep the rest of the hourly allowance. When everything is warm it checks
    again every `idle_minutes` (a new trading day makes the cached ranges one day short: they are refreshed).
    """

    def __init__(self, per_hour: int, per_cluster: int, idle_minutes: float = 60.0,
                 sleep: Callable[[float], None] = time.sleep, today: Callable[[], date] = date.today):
        self.per_hour, self.per_cluster, self.idle_minutes = per_hour, per_cluster, idle_minutes
        self.sleep, self.today = sleep, today
        self.thread: threading.Thread | None = None
        self.status: dict = {"state": "not started", "per_hour": per_hour, "per_cluster": per_cluster}

    def _throttle(self) -> None:
        # With its own key only its own requests count; sharing the traders' key, theirs count too.
        key = settings.tiingo_warm_api_key() if settings.warm_has_own_key() else None
        while tiingo_requests_in_last(3600, key) >= self.per_hour:
            self.status["state"] = "waiting: hourly Tiingo share in use"
            self.sleep(60)
        self.status["state"] = "fetching"

    def cycle(self) -> WarmReport:
        from egoquant.agents.market_intel.agent import MarketIntelligenceAgent
        from egoquant.agents.market_intel.directory import load_directory
        from egoquant.models import AgentConfig

        as_of = last_weekday_before(self.today())
        start = MarketIntelligenceAgent.window_start(as_of, AgentConfig().goal_history_days)
        plan = warm_plan(load_directory(), self.per_cluster)
        self.status.update(state="fetching", as_of=str(as_of), planned=len(plan))
        report = warm_cache(plan, as_of, start, wait=True, wait_minutes=15, max_waits=10_000,
                            log=logger.info, sleep=self.sleep, before_request=self._throttle)
        covered = len(report.already_cached) + len(report.fetched)
        self.status.update(state="idle" if not report.stopped else f"paused: {report.stopped}", warm=covered,
                           fetched_last_cycle=len(report.fetched), failed=len(report.failed),
                           last_cycle=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return report

    def run_forever(self) -> None:
        while True:
            try:
                report = self.cycle()
                pause = 6 * 60 if report.stopped and "monthly" in report.stopped else self.idle_minutes
            except Exception:                              # never let the builder die silently
                logger.exception("cache warm-up cycle failed; retrying in 15 minutes")
                self.status["state"] = "error; retrying"
                pause = 15
            self.sleep(pause * 60)

    def start(self) -> "CacheWarmer":
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.run_forever, daemon=True, name="agent-c-cache-warmer")
            self.thread.start()
            self.status["state"] = "starting"
        return self


_WARMER: CacheWarmer | None = None
_OFF_REASON: str | None = None
SHARED_KEY_OFF = ("off: no TIINGO_WARM_API_KEY of its own, and the traders' key is kept for portfolio builds "
                  "(EGOQUANT_WARM=shared runs it on that key)")


def start_background_warmer() -> CacheWarmer | None:
    """Start the continuous cache builder once per process (no-op when off, synthetic, without a key, or when it
    would share the traders' key and EGOQUANT_WARM is not 'shared')."""
    global _WARMER, _OFF_REASON
    if _WARMER is not None:
        return _WARMER
    import os
    _OFF_REASON = None
    if (not settings.warm_enabled() or not settings.tiingo_warm_api_key()
            or os.getenv("EGOQUANT_DATA", "").strip().lower() == "synthetic"):
        return None
    if not settings.warm_has_own_key() and not settings.warm_may_share_key():
        _OFF_REASON = SHARED_KEY_OFF
        logger.info("Agent C cache warmer not started: %s", SHARED_KEY_OFF)
        return None
    _WARMER = CacheWarmer(settings.warm_per_hour(), settings.warm_per_cluster()).start()
    logger.info("Agent C cache warmer started (%s, %s Tiingo requests/hour, %s funds per cluster)",
                "own key" if settings.warm_has_own_key() else "shared key", _WARMER.per_hour, _WARMER.per_cluster)
    return _WARMER


def warmer_status() -> dict:
    """The builder's state, plus each Tiingo key's last 429 (Tiingo's own message says which limit) and cooldown."""
    limits = {"tiingo_traders_key": tiingo_limit_status(settings.tiingo_api_key() or None)}
    if settings.warm_has_own_key():
        limits["tiingo_builder_key"] = tiingo_limit_status(settings.tiingo_warm_api_key())
    if _WARMER is None:
        return {"state": _OFF_REASON or "off", **limits}
    return {**_WARMER.status, "key": "own" if settings.warm_has_own_key() else "shared with traders", **limits}


__all__ = ["CacheWarmer", "WarmReport", "start_background_warmer", "warm_cache", "warm_plan", "warmer_status"]
