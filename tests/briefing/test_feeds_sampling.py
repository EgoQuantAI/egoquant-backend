"""Per-ticker feeds, Google News publisher suffix, faster dedupe, narrowed relevant tickers, sampling cap."""

import difflib
import random

import pytest

from egoquant.agents.market_intel import DEFAULT_CATALOG, InvestorInterests, UniverseSelection
from egoquant.briefing.build import relevant_tickers, select_tickers
from egoquant.briefing.evaluation import rows_per_ticker, sample_for_labeling
from egoquant.briefing.fetch import RSSAdapter, _title_key, prepare, split_publisher
from egoquant.briefing.loader import CuratedSource, TickerFeed, expand_ticker_feeds, load_config
from tests.briefing.common import AS_OF, build, config, interests, item

CFG = load_config()
GOOGLE_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Google News</title>
<item><title>REITs fall as office vacancies climb - Example Times</title><link>https://news.example.com/g1</link>
<pubDate>Wed, 14 Oct 2026 10:00:00 GMT</pubDate><description>long snippet that must not be kept</description>
<source url="https://example.com">Example Times</source></item>
</channel></rss>"""


# ---- Google News publisher suffix -------------------------------------------------------------------
@pytest.mark.parametrize("title, source, expected", [
    ("REITs fall - Example Times", "Example Times", ("REITs fall", "Example Times")),
    ("REITs fall - Example Times", None, ("REITs fall", "Example Times")),
    ("Bonds rally - a 10-year high - Example Times", "Example Times", ("Bonds rally - a 10-year high", "Example Times")),
    # with a <source> element that element IS the publisher; a title without its suffix is left as it is
    ("Year-to-date returns - explained", "Example Times", ("Year-to-date returns - explained", "Example Times")),
    ("No suffix here", "Example Times", ("No suffix here", "Example Times")),
    ("7 Best Treasury ETFs to Buy Now | Investing - U.S. News - Money", "U.S. News - Money",
     ("7 Best Treasury ETFs to Buy Now | Investing", "U.S. News - Money")),
    ("No suffix here", None, ("No suffix here", None)),
])
def test_split_publisher(title, source, expected):
    assert split_publisher(title, source) == expected


def test_google_items_lose_the_suffix_and_keep_headline_link_date_only():
    feedparser = pytest.importorskip("feedparser")
    src = CuratedSource(name="Google News", url="u", type="outlet", headline_only=True, strip_publisher_suffix=True)
    it = RSSAdapter([src], CFG, parse=lambda url: feedparser.parse(GOOGLE_RSS)).fetch(AS_OF).items[0]
    assert (it.title, it.source_name, it.summary_text) == ("REITs fall as office vacancies climb", "Example Times", "")


def test_stripped_google_title_is_caught_as_a_cross_source_duplicate():
    yahoo = item("REITs fall as office vacancies climb", 2, source_name="Yahoo Finance")
    google = item("REITs fall as office vacancies climb", 1.5, source_name="Example Times")   # after stripping
    assert len(prepare([yahoo, google], AS_OF, CFG).items) == 1


# ---- per-ticker expansion: explicit ticker list ----------------------------------------------------------
def test_expansion_uses_only_the_given_tickers():
    feeds = CFG.live_sources.rss_per_ticker
    out = expand_ticker_feeds(feeds, ["VNQ", "QQQ", "VNQ"])
    assert len(out) == len(feeds) * 2
    assert {s.name for s in out} == {"Yahoo Finance", "Nasdaq", "Google News", "Seeking Alpha"}
    assert all(("VNQ" in s.url) != ("QQQ" in s.url) for s in out)
    assert all(s.headline_only for s in out) and [s.strip_publisher_suffix for s in out if s.name == "Google News"] == [True, True]


def test_feed_template_needs_a_ticker_placeholder():
    with pytest.raises(ValueError, match="ticker"):
        TickerFeed(name="x", url_template="https://example.com/rss", type="outlet")


# ---- dedupe: faster, same result as the plain pairwise check ------------------------------------------------
def brute_force_dedupe(items, threshold):
    kept, urls = [], set()
    for it in sorted(items, key=lambda i: (i.published_at, i.id)):
        if it.url in urls:
            continue
        key = _title_key(it.title)
        if any(difflib.SequenceMatcher(None, key, _title_key(k.title)).ratio() >= threshold for k in kept):
            continue
        urls.add(it.url)
        kept.append(it)
    return kept


def test_fast_dedupe_matches_the_pairwise_check():
    rng = random.Random(7)
    words = "REITs bonds Treasury yields rise fall tech stocks gold record funds inflows outflows week".split()
    items = []
    for n in range(300):
        title = " ".join(rng.choice(words) for _ in range(rng.randint(3, 8)))
        if n % 5 == 0 and items:                                   # near-duplicates of earlier titles
            title = items[rng.randrange(len(items))].title + rng.choice(["", ".", " again", "s"])
        items.append(item(title, rng.uniform(0, 13), source_name=f"S{n % 4}"))
    fast = prepare(items, AS_OF, config()).items
    slow = brute_force_dedupe(items, CFG.dedupe["title_similarity"])
    assert sorted(i.id for i in fast) == sorted(i.id for i in slow)


# ---- relevant tickers: portfolio + interests only, capped --------------------------------------------------
def test_relevant_tickers_priority_and_scope():
    interests = InvestorInterests(investor_id="x", interest_areas=["real_estate", "crypto"], held_assets=["TLT"])
    selection = UniverseSelection(investor_id="x", interest_areas=["real_estate", "crypto"], instruments=[],
                                  watchlist=[])
    weights = {"VT": 0.30, "VNQ": 0.40, "BIL": 0.0, "AGG": 0.30}
    got = relevant_tickers(interests, selection, weights, DEFAULT_CATALOG)
    assert got[:3] == ["VNQ", "AGG", "VT"]                          # holdings first, largest weight first
    assert got[3] == "TLT"                                          # then other held assets
    in_areas = [i for i in DEFAULT_CATALOG if {"real_estate", "crypto"} & set(i.areas) and i.ticker != "VNQ"]
    funds = [i.ticker for i in in_areas if i.product_type != "stock"]
    stocks = [i.ticker for i in in_areas if i.product_type == "stock"]
    assert sorted(got[4:]) == sorted(funds + stocks)               # then every interest-area instrument
    re_funds = [t for t in funds if "real_estate" in next(i for i in DEFAULT_CATALOG if i.ticker == t).areas]
    cr_funds = [t for t in funds if t not in re_funds]
    assert got[4:6] == [re_funds[0], cr_funds[0]]                  # round-robin: one per area, funds first
    assert {"IBIT", "BTCUSDT", "ETHUSDT"} <= set(funds)            # (VNQ already in)
    assert "BIL" not in got and "SHY" not in got and "QQQ" not in got   # weight 0 / rest of the universe excluded


def test_relevant_tickers_cap_keeps_holdings_first():
    interests = InvestorInterests(investor_id="x", interest_areas=["stocks", "bonds", "crypto"])
    weights = {"VT": 0.5, "AGG": 0.5}
    got = relevant_tickers(interests, None, weights, DEFAULT_CATALOG, max_tickers=3)
    assert got == ["AGG", "VT", "VTI"]


def test_round_robin_gives_every_interest_area_a_slot():
    # "stocks" alone has dozens of instruments; "gold" must still be covered under the cap
    interests = InvestorInterests(investor_id="x", interest_areas=["stocks", "gold"])
    weights = {"VT": 0.6, "AGG": 0.4}
    got, left_out = select_tickers(interests, None, weights, DEFAULT_CATALOG, max_tickers=15)
    gold = {i.ticker for i in DEFAULT_CATALOG if "gold" in i.areas}
    assert gold and gold & set(got) and got[:2] == ["VT", "AGG"]
    assert got[2] in {i.ticker for i in DEFAULT_CATALOG if "stocks" in i.areas} and got[3] in gold
    everything, none = select_tickers(interests, None, weights, DEFAULT_CATALOG)
    assert none == [] and left_out == everything[15:] and not set(got) & set(left_out)
    assert len(got) == 15 and set(got) | set(left_out) == set(everything)


def test_left_out_tickers_are_in_the_briefing_metadata():
    cfg = config()
    cfg.live_sources.max_live_tickers = 2
    b = build([item("REITs fall as office vacancies climb", 1)], areas=("real_estate", "technology"),
              weights={"VNQ": 0.2, "QQQ": 0.1}, cfg=cfg)
    full = select_tickers(interests("real_estate", "technology"), None, {"VNQ": 0.2, "QQQ": 0.1}, DEFAULT_CATALOG)[0]
    assert b.metadata.tickers_left_out_by_cap == full[2:] and "VNQ" not in b.metadata.tickers_left_out_by_cap


def test_build_drops_items_outside_the_capped_ticker_list():
    cfg = config()
    cfg.live_sources.max_live_tickers = 1
    items = [item("REITs fall as office vacancies climb", 1), item("Nasdaq-100 gains on chip demand", 1)]
    b = build(items, areas=("real_estate", "technology"), weights={"VNQ": 0.2, "QQQ": 0.1}, cfg=cfg)
    assert [x.matched_tickers for x in b.items] == [["VNQ"]] and b.metadata.n_unmatched == 1


# ---- sampling cap per ticker ---------------------------------------------------------------------------------
def test_sampling_caps_each_ticker_and_is_reproducible():
    pool = [(item(f"REIT headline {i}", i * 0.1), ["VNQ"]) for i in range(40)]
    pool += [(item(f"Tech headline {i}", i * 0.1), ["QQQ"]) for i in range(10)]
    pool += [(item(f"Mixed headline {i}", i * 0.1), ["VNQ", "QQQ"]) for i in range(10)]
    rows = sample_for_labeling(pool, 30, seed=42, max_per_ticker=15)
    counts = rows_per_ticker(rows)
    assert counts["VNQ"] <= 15 and counts["QQQ"] <= 15
    assert rows == sample_for_labeling(list(reversed(pool)), 30, seed=42, max_per_ticker=15)
    assert len(rows) <= 30
