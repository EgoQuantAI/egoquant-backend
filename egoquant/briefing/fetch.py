"""Fetching: one adapter per source type, then normalize, deduplicate and apply the time window.

Copyright rule: we keep only what feeds and APIs hand out (headline, their own short summary or
description, link, date). We never scrape or store full article bodies.

Every network client is injectable (feedparser's parse function, a requests-like session), so the
adapters are tested offline. Missing credentials or a failing source never crash a briefing: the
source is skipped and the reason goes into the briefing metadata.
"""

from __future__ import annotations

import calendar
import difflib
import hashlib
import html
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from egoquant.briefing.loader import FIXTURE_ITEMS, BriefingConfig, CuratedSource
from egoquant.briefing.models import NewsItem, SourceType

_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


def item_id(source_name: str, url: str) -> str:
    """Stable id: the same story from the same source always gets the same id."""
    return hashlib.sha256(f"{source_name}\n{url}".encode()).hexdigest()[:16]


def clean_text(text: str | None, limit: int) -> str:
    """Strip HTML tags and entities, collapse whitespace, truncate on a word boundary."""
    text = _SPACES.sub(" ", html.unescape(_TAGS.sub(" ", text or ""))).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…"


@dataclass
class FetchResult:
    items: list[NewsItem] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)     # "<source>: <reason>"
    n_future_dates: int = 0                              # entries rejected: dated after fetch time + tolerance


class Adapter(Protocol):
    def fetch(self, fetched_at: datetime) -> FetchResult: ...


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# RSS (feedparser)
# --------------------------------------------------------------------------
def split_publisher(title: str, entry_source: str | None = None) -> tuple[str, str | None]:
    """Google News titles end with " - <Publisher>". Return (headline, publisher), or (title, None).

    With the entry's own <source> name, that name IS the publisher, and a trailing " - <source>" is removed even
    when the source itself contains " - " ("... | Investing - U.S. News - Money"). Without one, the last " - "
    suffix is taken only when it is short enough to be a publisher name, so a headline that merely contains
    " - " is left alone.
    """
    if entry_source and entry_source.strip():
        src = entry_source.strip()
        for sep in (" - ", " | "):
            if title.endswith(sep + src):
                return title[: -len(sep + src)].strip(), src
        return title, src
    if " - " not in title:
        return title, None
    head, tail = title.rsplit(" - ", 1)
    tail = tail.strip()
    return (head.strip(), tail) if head.strip() and 0 < len(tail) <= 60 else (title, None)


def link_publisher(url: str, domains: dict[str, str]) -> str | None:
    """Yahoo Finance links point at the publisher's own site: map the domain to its name (config)."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None
    return domains.get(host) or domains.get(host.removeprefix("www.")) or host.removeprefix("www.")


def entry_date(e: Any, fetched_at: datetime, tolerance_minutes: float) -> tuple[datetime | None, str]:
    """(date, date_status) from the feed entry's own fields: published, else updated ("updated_only"), else
    unknown (fetch time as a placeholder; the item is flagged and kept out of tone windows). Never fetch time
    silently. A date more than tolerance_minutes after fetch time is rejected: (None, "future")."""
    when, status = e.get("published_parsed"), "published"
    if not when:
        when, status = e.get("updated_parsed"), "updated_only"
    if not when:
        return _utc(fetched_at), "unknown"
    dt = datetime.fromtimestamp(calendar.timegm(when), tz=timezone.utc)
    if dt > _utc(fetched_at) + timedelta(minutes=tolerance_minutes):
        return None, "future"
    return dt, status


class RSSAdapter:
    def __init__(self, sources: list[CuratedSource], config: BriefingConfig, parse: Callable[[str], Any] | None = None,
                 pause_seconds: float | None = None):
        self.sources, self.config, self._parse = sources, config, parse
        # Injected parsers (tests) never sleep; the real one pauses politely between requests.
        self.pause = 0.0 if parse is not None else (
            config.live_sources.request_pause_seconds if pause_seconds is None else pause_seconds)

    def _parser(self) -> Callable[[str], Any] | None:
        if self._parse is not None:
            return self._parse
        try:
            import feedparser  # optional dependency (pip install -e ".[briefing]")
        except ImportError:
            return None
        agent = self.config.live_sources.user_agent
        return lambda url: feedparser.parse(url, agent=agent)

    def fetch(self, fetched_at: datetime) -> FetchResult:
        out = FetchResult()
        parse = self._parser()
        if parse is None:
            out.skipped.append("rss: feedparser is not installed (pip install -e '.[briefing]')")
            return out
        limit = self.config.truncation["summary_chars"]
        for n, src in enumerate(self.sources):
            if n and self.pause:
                time.sleep(self.pause)
            try:
                feed = parse(src.url)
            except Exception as exc:  # network errors, malformed feeds
                out.skipped.append(f"rss {src.name}: {type(exc).__name__}")
                continue
            entries = getattr(feed, "entries", None) or feed.get("entries", [])
            if not entries:
                out.skipped.append(f"rss {src.name}: no entries")
                continue
            for e in entries:
                link, title = e.get("link"), clean_text(e.get("title"), 300)
                if not link or not title:
                    continue
                published, date_status = entry_date(e, fetched_at, self.config.sources.future_date_tolerance_minutes)
                if published is None:
                    out.n_future_dates += 1
                    continue
                id_name = src.name                     # item ids stay as before, so existing caches still apply
                source_name = src.name
                if src.strip_publisher_suffix or src.publisher_from == "source_element":
                    feed_source = e.get("source") or {}
                    title, publisher = split_publisher(title, feed_source.get("title") if hasattr(feed_source, "get") else None)
                    source_name = id_name = publisher or src.name
                elif src.publisher_from == "link_domain":
                    source_name = link_publisher(link, self.config.sources.publisher_domains) or src.name
                elif src.publisher_from == "author":
                    source_name = clean_text(e.get("author"), 80) or src.name
                summary = "" if src.headline_only else clean_text(e.get("summary"), limit)
                out.items.append(NewsItem(
                    id=item_id(id_name, link), title=title, summary_text=summary,
                    url=link, published_at=published, source_name=source_name, source_type=src.type,
                    fetched_at=_utc(fetched_at), data_source="live",
                    feed=src.name, date_status=date_status))
        return out


# --------------------------------------------------------------------------
# Reddit (official API, application-only OAuth)
# --------------------------------------------------------------------------
REDDIT_ENV = ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT")


class RedditAdapter:
    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
    API = "https://oauth.reddit.com"

    def __init__(self, subreddits: list[str], config: BriefingConfig, session: Any | None = None,
                 env: dict[str, str] | None = None):
        self.subreddits, self.config, self.session = subreddits, config, session
        self.env = env if env is not None else os.environ

    def fetch(self, fetched_at: datetime) -> FetchResult:
        out = FetchResult()
        if not self.subreddits:
            return out
        missing = [k for k in REDDIT_ENV if not self.env.get(k, "").strip()]
        if missing:
            out.skipped.append(f"reddit: skipped, credentials not set ({', '.join(missing)})")
            return out
        session = self.session
        if session is None:
            import requests
            session = requests.Session()
        agent = {"User-Agent": self.env["REDDIT_USER_AGENT"]}
        try:
            resp = session.post(self.TOKEN_URL, auth=(self.env["REDDIT_CLIENT_ID"], self.env["REDDIT_CLIENT_SECRET"]),
                                data={"grant_type": "client_credentials"}, headers=agent, timeout=20)
            resp.raise_for_status()
            token = resp.json()["access_token"]
        except Exception as exc:
            out.skipped.append(f"reddit: authentication failed ({type(exc).__name__})")
            return out
        headers = {**agent, "Authorization": f"bearer {token}"}
        n = self.config.live_sources.reddit_posts_per_subreddit
        limit = self.config.truncation["reddit_selftext_chars"]
        for sub in self.subreddits:
            try:
                r = session.get(f"{self.API}/r/{sub}/new", params={"limit": n}, headers=headers, timeout=20)
                r.raise_for_status()
                children = r.json()["data"]["children"]
            except Exception as exc:
                out.skipped.append(f"reddit r/{sub}: {type(exc).__name__}")
                continue
            for child in children:
                d = child.get("data", {})
                if not d.get("permalink") or not d.get("title"):
                    continue
                url = "https://www.reddit.com" + d["permalink"]
                out.items.append(NewsItem(
                    id=item_id(f"r/{sub}", url), title=clean_text(d["title"], 300),
                    summary_text=clean_text(d.get("selftext"), limit), url=url,
                    published_at=datetime.fromtimestamp(float(d["created_utc"]), tz=timezone.utc),
                    source_name=f"r/{sub}", source_type="forum", fetched_at=_utc(fetched_at), data_source="live"))
        return out


# --------------------------------------------------------------------------
# SEC EDGAR (optional): recent filings of the registrants behind catalog tickers
# --------------------------------------------------------------------------
class EdgarAdapter:
    """SEC fair access: a descriptive User-Agent with contact details (env SEC_USER_AGENT), and at most
    10 requests per second (we make a handful). ETF filings are made by the fund's trust, which often
    covers many funds, so a filing is relevant to the ticker but not necessarily about it alone."""

    FUND_TICKERS = "https://www.sec.gov/files/company_tickers_mf.json"
    SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

    def __init__(self, tickers: dict[str, str], config: BriefingConfig, session: Any | None = None,
                 env: dict[str, str] | None = None):
        self.tickers, self.config, self.session = tickers, config, session     # ticker -> fund name
        self.env = env if env is not None else os.environ

    def fetch(self, fetched_at: datetime) -> FetchResult:
        out = FetchResult()
        agent = self.env.get("SEC_USER_AGENT", "").strip()
        if not agent:
            out.skipped.append("sec edgar: skipped, SEC_USER_AGENT not set (SEC requires a descriptive User-Agent)")
            return out
        session = self.session
        if session is None:
            import requests
            session = requests.Session()
        headers = {"User-Agent": agent}
        try:
            r = session.get(self.FUND_TICKERS, headers=headers, timeout=20)
            r.raise_for_status()
            payload = r.json()
            fields = payload["fields"]
            rows = [dict(zip(fields, row)) for row in payload["data"]]
            cik_of = {row["symbol"].upper(): int(row["cik"]) for row in rows}
        except Exception as exc:
            out.skipped.append(f"sec edgar: ticker map unavailable ({type(exc).__name__})")
            return out
        per = self.config.live_sources.edgar_filings_per_ticker
        for ticker, name in sorted(self.tickers.items()):
            cik = cik_of.get(ticker.upper())
            if cik is None:
                out.skipped.append(f"sec edgar {ticker}: not in SEC's fund ticker file")
                continue
            try:
                r = session.get(self.SUBMISSIONS.format(cik=cik), headers=headers, timeout=20)
                r.raise_for_status()
                recent = r.json()["filings"]["recent"]
            except Exception as exc:
                out.skipped.append(f"sec edgar {ticker}: {type(exc).__name__}")
                continue
            for i in range(min(per, len(recent.get("form", [])))):
                form, day = recent["form"][i], recent["filingDate"][i]
                acc, doc = recent["accessionNumber"][i], recent["primaryDocument"][i]
                url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"
                out.items.append(NewsItem(
                    id=item_id("SEC EDGAR", url), title=f"Form {form} filed by the registrant of {name}",
                    summary_text=f"SEC filing (form {form}) dated {day} by the trust that issues {name} (${ticker}).",
                    url=url, published_at=datetime.fromisoformat(day).replace(tzinfo=timezone.utc),
                    source_name="SEC EDGAR", source_type="filing", fetched_at=_utc(fetched_at), data_source="live"))
        return out


# --------------------------------------------------------------------------
# Tiingo news: DISABLED STUB
# --------------------------------------------------------------------------
class TiingoNewsAdapter:
    """Not implemented on purpose. Tiingo's news endpoint requires a paid plan, and the cost rule says no
    paid news or sentiment APIs. Kept as a stub so the source list shows why it is absent."""

    def fetch(self, fetched_at: datetime) -> FetchResult:
        return FetchResult(skipped=["tiingo news: disabled (requires a paid Tiingo plan; cost rule)"])


# --------------------------------------------------------------------------
# Fixture (offline demo and tests)
# --------------------------------------------------------------------------
class FixtureAdapter:
    """Invented sample items under invented source names, labeled data_source="fixture". Not real news."""

    def __init__(self, path: Path | str = FIXTURE_ITEMS):
        self.path = Path(path)

    def fetch(self, fetched_at: datetime) -> FetchResult:
        with open(self.path, encoding="utf-8") as fh:
            raw = json.load(fh)
        items = []
        for r in raw["items"]:
            items.append(NewsItem(
                id=item_id(r["source_name"], r["url"]), title=r["title"], summary_text=r.get("summary_text", ""),
                url=r["url"], published_at=datetime.fromisoformat(r["published_at"]),
                source_name=r["source_name"], source_type=r["source_type"],
                fetched_at=datetime.fromisoformat(raw["fetched_at"]), data_source="fixture"))
        return FetchResult(items=items)


def fixture_as_of(path: Path | str = FIXTURE_ITEMS) -> datetime:
    with open(path, encoding="utf-8") as fh:
        return datetime.fromisoformat(json.load(fh)["as_of"])


# --------------------------------------------------------------------------
# Normalize: deduplicate and apply the window
# --------------------------------------------------------------------------
@dataclass
class Prepared:
    items: list[NewsItem]
    n_fetched: int
    n_after_dedupe: int
    n_outside_window: int
    n_non_news: int = 0          # quote/chart/holdings/listing pages and truncated titles (config non_news_patterns)


def is_non_news(title: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.search(title) for p in patterns)


_EXCHANGE_TAG = re.compile(r"\(\s*[A-Za-z][A-Za-z ]{0,15}\s*:\s*[A-Za-z][A-Za-z0-9.]{0,12}\s*\)")


def _title_key(title: str) -> str:
    """Lower case, letters and digits only, without exchange tags ("(NASDAQ:VXUS)" copies are the same story)."""
    return " ".join(re.sub(r"[^a-z0-9 ]", "", _EXCHANGE_TAG.sub(" ", title).lower()).split())


def _long_words(key: str, n: int = 4) -> list[str]:
    """The n longest words (4+ letters) of a normalized title: near-identical titles always share some."""
    words = sorted({w for w in key.split() if len(w) >= 4}, key=lambda w: (-len(w), w))
    return words[:n]


def prepare(items: list[NewsItem], as_of: datetime, config: BriefingConfig) -> Prepared:
    """Deterministic: drop non-news pages, sort, drop duplicate URLs and near-identical titles, keep
    [as_of - lookback, as_of]."""
    non_news = [re.compile(p, re.IGNORECASE) for p in config.non_news_patterns]
    news = [it for it in items if not is_non_news(it.title, non_news)]      # before dedupe: a quote page never
    n_non_news = len(items) - len(news)                                      # wins over the real story
    ordered = sorted(news, key=lambda i: (i.published_at, i.id))      # the earliest copy of a story wins
    kept: list[NewsItem] = []
    seen_urls: set[str] = set()
    seen_keys: set[str] = set()
    matchers: list[difflib.SequenceMatcher] = []      # one per kept title; its index of the title is reused
    by_word: dict[str, list[int]] = {}                # longest words -> kept titles that contain them
    threshold = config.dedupe["title_similarity"]
    for it in ordered:
        if it.url in seen_urls:
            continue
        key = _title_key(it.title)
        if key in seen_keys:                          # exact duplicate title: no similarity check needed
            continue
        # Titles this similar share their longest words, so only kept titles with one of them are compared
        # (the full comparison against every kept title was quadratic: seconds for a few thousand headlines).
        words = _long_words(key)
        candidates = sorted({i for w in words for i in by_word.get(w, ())}) if words else range(len(matchers))
        duplicate = False
        for i in candidates:                          # same ratio as SequenceMatcher(None, key, kept_title)
            sm = matchers[i]
            sm.set_seq1(key)
            if sm.real_quick_ratio() >= threshold and sm.quick_ratio() >= threshold and sm.ratio() >= threshold:
                duplicate = True                      # the cheap upper bounds skip most pairs
                break
        if duplicate:
            continue
        seen_urls.add(it.url)
        seen_keys.add(key)
        for w in words:
            by_word.setdefault(w, []).append(len(matchers))
        matchers.append(difflib.SequenceMatcher(None, "", key))
        kept.append(it)
    n_after = len(kept)
    start = _utc(as_of) - timedelta(days=config.window.lookback_days)
    in_window = [it for it in kept if start <= it.published_at <= _utc(as_of)]
    in_window.sort(key=lambda i: (-i.published_at.timestamp(), i.id))
    in_window = in_window[: config.window.max_items]
    return Prepared(items=in_window, n_fetched=len(items), n_after_dedupe=n_after,
                    n_outside_window=n_after - len(in_window), n_non_news=n_non_news)


def fetch_all(adapters: list[Adapter], fetched_at: datetime) -> FetchResult:
    total = FetchResult()
    for a in adapters:
        r = a.fetch(fetched_at)
        total.items.extend(r.items)
        total.skipped.extend(r.skipped)
        total.n_future_dates += r.n_future_dates
    return total
