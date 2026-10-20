"""Local news archive: every fetched headline is kept, so monthly comparisons gain history over time.

Stored per item, deduplicated by id: headline, link, publication date, source name and type, matched tickers,
when it was first fetched, and whether it was live or sample data. Never article bodies (not even the feed's
summary): only what the copyright rule allows us to keep. The file lives in the briefing cache directory
(settings.cache_dir()/briefing, outside the repository; `briefing_cache/` is gitignored for in-repo cache dirs).
Live briefings read the archive plus fresh items; fresh items come first, so they win the dedupe.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from egoquant.briefing.loader import briefing_cache_dir, read_json_cache, write_json_cache
from egoquant.briefing.matching import Matcher
from egoquant.briefing.models import NewsItem

ARCHIVE_FIELDS = ("id", "title", "url", "published_at", "source_name", "source_type", "matched_tickers",
                  "first_fetched_at", "data_source")


def archive_path() -> Path:
    return briefing_cache_dir() / "news_archive.json"


def archive_items(items: list[NewsItem], matcher: Matcher, path: Path | None = None) -> int:
    """Add items not yet archived (by id). Returns how many were new. Existing entries are never changed."""
    path = path or archive_path()
    archive = read_json_cache(path)
    new = 0
    for it in items:
        if it.id in archive:
            continue
        archive[it.id] = {"id": it.id, "title": it.title, "url": it.url, "published_at": it.published_at.isoformat(),
                          "source_name": it.source_name, "source_type": it.source_type,
                          "matched_tickers": list(matcher.match_item(it).tickers),
                          "first_fetched_at": it.fetched_at.isoformat(), "data_source": it.data_source}
        new += 1
    if new:
        write_json_cache(path, archive)
    return new


def load_archive(path: Path | None = None, since: datetime | None = None) -> list[NewsItem]:
    """Archived items as NewsItems (summary_text empty: no bodies are kept), optionally only those published
    on or after `since`."""
    out = []
    for r in read_json_cache(path or archive_path()).values():
        published = datetime.fromisoformat(r["published_at"])
        if since is not None and published < since:
            continue
        out.append(NewsItem(id=r["id"], title=r["title"], summary_text="", url=r["url"], published_at=published,
                            source_name=r["source_name"], source_type=r["source_type"],
                            fetched_at=datetime.fromisoformat(r["first_fetched_at"]), data_source=r["data_source"]))
    return sorted(out, key=lambda i: (i.published_at, i.id))
