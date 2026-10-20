"""Collect recent headlines for hand labeling. Needs network access; NOT part of the test suite.

    python eval/collect_headlines.py

Sources (egoquant/briefing/config.yaml live_sources): the RSS feeds, the free per-ticker headline feeds
(Yahoo Finance, Nasdaq, Google News) for ALL US-listed Agent C catalog funds (variety for labeling), and
Reddit if REDDIT_* credentials are set. Per-ticker feeds keep headline, link and date only. Google News
titles lose their " - <Publisher>" suffix (the publisher becomes the source) before dedupe and matching.

Keeps only headlines that match catalog instruments (deterministic matcher), removes duplicates, samples
eval.sample_size of them with a fixed seed and at most eval.max_rows_per_ticker rows per fund, and writes
eval/headlines_to_label.csv (gitignored: third-party text) with empty label columns. It does NOT score or
label anything. Feeds are free, non-commercial use; we store only headline, link and date.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egoquant.agents.market_intel import DEFAULT_CATALOG  # noqa: E402
from egoquant.briefing.evaluation import TO_LABEL_COLUMNS, rows_per_ticker, sample_for_labeling, write_csv  # noqa: E402
from egoquant.briefing.fetch import RedditAdapter, RSSAdapter, prepare  # noqa: E402
from egoquant.briefing.loader import expand_ticker_feeds, load_config  # noqa: E402
from egoquant.briefing.matching import Matcher  # noqa: E402


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    config = load_config()
    # Labeling wants variety: every US-listed fund in Agent C's catalog (explicit list).
    config.window.max_items = 100_000                       # the briefing's per-run cap does not apply here
    now = datetime.now(timezone.utc)
    src = config.live_sources
    tickers = [i.ticker for i in DEFAULT_CATALOG if i.market == "us_listed"]
    groups = {"RSS feeds (config)": [RSSAdapter(src.rss, config)]}
    for feed in src.rss_per_ticker:
        groups[feed.name] = [RSSAdapter(expand_ticker_feeds([feed], tickers), config)]
    groups["Reddit"] = [RedditAdapter(src.subreddits, config)]

    items, origin, skipped = [], {}, []
    for name, adapters in groups.items():
        n = 0
        for a in adapters:
            r = a.fetch(now)
            items += r.items
            skipped += r.skipped
            n += len(r.items)
            for it in r.items:
                origin.setdefault(it.id, name)
        print(f"fetched {n:5d}  from {name}")
    for s in skipped:
        print("skipped:", s)

    prepared = prepare(items, now, config)
    matcher = Matcher(config)
    matched = [(it, list(m.tickers)) for it in prepared.items if (m := matcher.match_item(it)).tickers]
    print(f"\n{prepared.n_fetched} fetched, {prepared.n_non_news} dropped as non-news, "
          f"{prepared.n_after_dedupe} after dedupe, {len(prepared.items)} in the "
          f"{config.window.lookback_days:g}-day window, {len(matched)} matched")
    for name in groups:
        print(f"  matched from {name}: {sum(origin.get(it.id) == name for it, _ in matched)}")

    rows = sample_for_labeling(matched, config.eval.sample_size, config.eval.random_seed,
                               config.eval.max_rows_per_ticker)
    out = ROOT / "eval" / "headlines_to_label.csv"
    write_csv(out, rows, TO_LABEL_COLUMNS)
    print(f"\nwrote {len(rows)} rows to {out} (seed {config.eval.random_seed}, "
          f"at most {config.eval.max_rows_per_ticker} rows per ticker)")
    print("rows per ticker:", ", ".join(f"{t} {n}" for t, n in rows_per_ticker(rows).items()))
    if len(rows) < config.eval.sample_size:
        print(f"note: fewer than {config.eval.sample_size} rows; add sources or run again later")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
