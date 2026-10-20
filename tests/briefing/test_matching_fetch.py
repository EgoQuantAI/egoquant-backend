"""Matching (deterministic), fetch adapters (offline fakes), dedupe and window."""

import json

import pytest

from egoquant.briefing.fetch import (EdgarAdapter, FixtureAdapter, RedditAdapter, RSSAdapter, TiingoNewsAdapter,
                                     clean_text, prepare)
from egoquant.briefing.loader import CuratedSource, load_config
from egoquant.briefing.matching import Matcher
from tests.briefing.common import AS_OF, config, item

CFG = load_config()
M = Matcher(CFG)


# ---- matching ---------------------------------------------------------------------------------
@pytest.mark.parametrize("text, tickers", [
    ("Watching $VNQ this week", ("VNQ",)),                      # cashtag
    ("$shy holders are calm", ("SHY",)),                        # cashtag works even for stopword tickers
    ("QQQ and VNQ both moved", ("QQQ", "VNQ")),                 # whole-word tickers
    ("SHY TIP VT BIL VB AGG all in caps", ("BIL", "AGG", "SHY", "TIP")),   # capital-only tickers match in capitals;
                                                                # VT and VB stay stopwords (never bare)
    ("I feel shy about these tips", ()),                        # lower-case words never match tickers
    ("TIPS auctions drew demand", ("TIP",)),                    # ALL-CAPS keyword is case-sensitive...
    ("tips for saving money", ()),                              # ...so ordinary "tips" does not match
    ("Office vacancies hit REITs", ("VNQ",)),                   # keyword alias
    ("The Nasdaq-100 rose", ("QQQ",)),
    ("Spot bitcoin ETF inflows", ("IBIT", "BTCUSDT")),          # catalog aliases + keywords
    ("Team wins gold medal", ()),                               # bare "gold" is deliberately not a keyword
    ("VNQX and AVNQ are other tickers", ()),                    # no partial-word matches
])
def test_matching(text, tickers):
    assert M.match_text(text).tickers == tickers


def test_areas_come_from_the_catalog():
    assert M.match_text("REITs fell").areas == ("real_estate",)


def test_keyword_keys_must_be_catalog_tickers():
    cfg = config()
    cfg.instrument_keywords["NOTREAL"] = ["x"]
    with pytest.raises(ValueError, match="NOTREAL"):
        Matcher(cfg)


# ---- adapters -----------------------------------------------------------------------------------
RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>REITs fall &amp; vacancies rise</title><link>https://feed.example.com/a</link>
<pubDate>Wed, 14 Oct 2026 10:00:00 GMT</pubDate><description>&lt;p&gt;Short &lt;b&gt;summary&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>No date</title><link>https://feed.example.com/b</link></item>
</channel></rss>"""


def test_rss_keeps_title_summary_link_date_only():
    feedparser = pytest.importorskip("feedparser")
    src = CuratedSource(name="Feed", url="https://feed.example.com/rss", type="outlet")
    res = RSSAdapter([src], CFG, parse=lambda url: feedparser.parse(RSS)).fetch(AS_OF)
    assert len(res.items) == 2                                   # the undated entry is kept, flagged "unknown"
    it, undated = (res.items if res.items[0].date_status == "published" else res.items[::-1])
    assert undated.date_status == "unknown"
    assert (it.title, it.summary_text, it.url) == ("REITs fall & vacancies rise", "Short summary", "https://feed.example.com/a")
    assert it.published_at.isoformat() == "2026-10-14T10:00:00+00:00" and it.data_source == "live"
    assert set(it.model_dump()) == {"id", "title", "summary_text", "url", "published_at", "source_name",
                                    "source_type", "fetched_at", "data_source", "feed", "date_status"}


def test_rss_failure_is_skipped_not_raised():
    def boom(url):
        raise OSError("offline")
    res = RSSAdapter([CuratedSource(name="F", url="u", type="outlet")], CFG, parse=boom).fetch(AS_OF)
    assert res.items == [] and res.skipped == ["rss F: OSError"]


def test_missing_reddit_credentials_skip_reddit_with_a_note():
    res = RedditAdapter(["investing"], CFG, env={}).fetch(AS_OF)
    assert res.items == []
    assert res.skipped == ["reddit: skipped, credentials not set (REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT)"]


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeRedditSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(("post", url))
        return FakeResponse({"access_token": "t"})

    def get(self, url, **kw):
        self.calls.append(("get", url))
        return FakeResponse({"data": {"children": [{"data": {
            "title": "REITs slump", "permalink": "/r/investing/comments/x/", "created_utc": 1760500000,
            "selftext": "word " * 400}}]}})


def test_reddit_with_credentials_truncates_selftext():
    env = {"REDDIT_CLIENT_ID": "id", "REDDIT_CLIENT_SECRET": "s", "REDDIT_USER_AGENT": "egoquant-test/0.1"}
    session = FakeRedditSession()
    res = RedditAdapter(["investing"], CFG, session=session, env=env).fetch(AS_OF)
    it = res.items[0]
    assert it.source_name == "r/investing" and it.source_type == "forum"
    assert it.url == "https://www.reddit.com/r/investing/comments/x/"
    assert len(it.summary_text) <= CFG.truncation["reddit_selftext_chars"] + 1
    assert session.calls[0] == ("post", RedditAdapter.TOKEN_URL)


def test_edgar_needs_a_user_agent_and_tiingo_is_a_disabled_stub():
    assert "SEC_USER_AGENT not set" in EdgarAdapter({"AGG": "x"}, CFG, env={}).fetch(AS_OF).skipped[0]
    assert "paid Tiingo plan" in TiingoNewsAdapter().fetch(AS_OF).skipped[0]
    raw = CFG.live_sources.model_dump() | {"tiingo_news_enabled": True}
    with pytest.raises(ValueError, match="paid plan"):
        type(CFG.live_sources).model_validate(raw)


# ---- normalize ----------------------------------------------------------------------------------
def test_dedupe_and_window():
    a = item("REITs fall as vacancies rise", 2, url="https://x.example.com/1")
    dup_url = item("Different title", 1, url="https://x.example.com/1", source_name="Other")
    near = item("REITs fell as vacancies rise", 1.5, source_name="Other Wire")
    old = item("Old story", 30)
    future = item("Future story", -1)
    p = prepare([a, dup_url, near, old, future], AS_OF, CFG)
    assert [i.title for i in p.items] == ["REITs fall as vacancies rise"]
    assert (p.n_fetched, p.n_after_dedupe, p.n_outside_window) == (5, 3, 2)


def test_clean_text_strips_html_and_truncates():
    assert clean_text("<p>a &amp; b</p>", 100) == "a & b"
    assert clean_text("one two three four", 9).endswith("…")


def test_fixture_items_are_invented_and_labeled():
    items = FixtureAdapter().fetch(AS_OF).items
    assert 40 <= len(items) <= 80
    assert all(i.data_source == "fixture" for i in items)
    assert all(".example.com/" in i.url or ".example.org/" in i.url for i in items)   # reserved example domains
    raw = json.load(open(FixtureAdapter().path))
    assert "INVENTED" in raw["note"]
