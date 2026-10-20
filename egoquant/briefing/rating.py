"""Rate (headline, fund) pairs with the fund-aware prompt: in parallel, with retries, never inside a web request.

Used by the briefing job (/briefing/start) and the background news worker. Every request is exactly the labeler's
own (FundToneLabeler.request: the evaluated, hash-pinned prompt; the headline and ONE fund; no investor data), and
every answer goes into the labeler's shared cache, so a pair is never sent twice.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from egoquant.briefing.fund_tone import FundToneLabeler
from egoquant.briefing.models import NewsItem

logger = logging.getLogger(__name__)


@dataclass
class RateResult:
    rated: int = 0                                   # answers stored (an unscored answer counts: it was asked)
    failed: int = 0                                  # gave up after the retries
    left: list[tuple[NewsItem, str]] = field(default_factory=list)   # not started: deadline or ceiling reached
    errors: dict[str, int] = field(default_factory=dict)            # exception name -> count


def rate_pairs(pairs: list[tuple[NewsItem, str]], labeler: FundToneLabeler, client: Any, *, parallel: int = 10,
               retries: int = 2, deadline: float | None = None, max_pairs: int | None = None,
               on_progress: Callable[[int, int], None] | None = None,
               clock: Callable[[], float] = time.monotonic, flush_every: int = 50) -> RateResult:
    """Ask for every pair (in the given order) until done, the deadline or max_pairs; returns what happened.
    Pairs already in the cache are skipped. Never raises."""
    result = RateResult()
    todo = [(it, t) for it, t in pairs if labeler.key(it.id, t) not in labeler.cache]
    if max_pairs is not None and len(todo) > max_pairs:
        result.left = todo[max_pairs:]
        todo = todo[:max_pairs]
    lock = threading.Lock()
    done_count = 0

    def ask(it: NewsItem, ticker: str) -> str:
        last: Exception | None = None
        for attempt in range(retries + 1):
            if deadline is not None and clock() > deadline:
                raise TimeoutError("deadline")
            try:
                resp = client.responses.create(**labeler.request(it.title, ticker))
                return getattr(resp, "output_text", "") or ""
            except Exception as exc:                 # network, rate limit, timeout: try again, a little later
                last = exc
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
        raise last or RuntimeError("no answer")

    pool = ThreadPoolExecutor(max_workers=max(1, parallel), thread_name_prefix="tone-rating")
    queue = list(todo)
    running: dict[Any, tuple[NewsItem, str]] = {}
    try:
        while queue or running:
            while queue and len(running) < parallel and (deadline is None or clock() < deadline):
                it, t = queue.pop(0)
                running[pool.submit(ask, it, t)] = (it, t)
            if not running:
                break
            timeout = None if deadline is None else max(0.0, deadline - clock())
            finished, _ = wait(list(running), timeout=timeout, return_when=FIRST_COMPLETED)
            if not finished:                          # deadline: what is still running is abandoned
                result.left.extend(running.values())
                running.clear()
                break
            for f in finished:
                it, t = running.pop(f)
                try:
                    labeler.store(it.id, t, f.result())
                    result.rated += 1
                except Exception as exc:
                    name = type(exc).__name__
                    result.errors[name] = result.errors.get(name, 0) + 1
                    if name == "TimeoutError":
                        result.left.append((it, t))
                    else:
                        result.failed += 1
                with lock:
                    done_count += 1
                    if on_progress:
                        on_progress(result.rated, len(todo))
                    if flush_every and done_count % flush_every == 0:
                        _flush(labeler)
            if deadline is not None and clock() >= deadline and queue:
                result.left.extend(queue)
                queue.clear()
        result.left.extend(queue)                     # not started (deadline before the first submit)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        _flush(labeler)
    return result


def _flush(labeler: FundToneLabeler) -> None:
    try:
        labeler.flush()
    except OSError as exc:
        logger.warning("Fund-aware tone cache not written: %s", type(exc).__name__)


__all__ = ["RateResult", "rate_pairs"]
