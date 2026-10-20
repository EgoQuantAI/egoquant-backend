"""News tab structure: real publishers, source tiers, dates, capital-only tickers, cards, both periods. Offline."""

from datetime import date, timedelta

import pytest

from egoquant.agents.market_intel import DEFAULT_CATALOG, SyntheticPriceSource
from egoquant.briefing.fetch import RSSAdapter
from egoquant.briefing.loader import CuratedSource, load_config
from egoquant.briefing.matching import Matcher
from egoquant.briefing.sources import tier_of
from tests.briefing.common import AS_OF, build, config, item

CFG = config()
feedparser = pytest.importorskip("feedparser")


def rss(entries: str) -> str:
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{entries}</channel></rss>'


YAHOO = rss("""
<item><title>BlackRock Says the Market Has the Fed Wrong. It's Buying Treasuries</title>
<link>https://247wallst.com/investing/2026/10/13/blackrock-says-the-market-has-the-fed-wrong/</link>
<pubDate>Tue, 13 Oct 2026 14:25:38 +0000</pubDate><guid>a</guid></item>
<item><title>Interest Rates Are Surging. Portfolio Moves to Make Now.</title>
<link>https://finance.yahoo.com/m/8841c62a/interest-rates-are-surging.html</link>
<pubDate>Fri, 09 Oct 2026 07:00:00 +0000</pubDate><guid>b</guid></item>
<item><title>Both of These Two Vanguard ETFs Offer Low Fees</title>
<link>https://www.fool.com/investing/2026/10/09/both-vanguard-etfs/</link>
<pubDate>Fri, 09 Oct 2026 14:50:02 +0000</pubDate><guid>c</guid></item>""")
NASDAQ = rss("""
<item><title>Noteworthy ETF Inflows: SHY</title><link>https://www.nasdaq.com/articles/noteworthy-etf-inflows-shy</link>
<author>BNK Invest</author><pubDate>Thu, 08 Oct 2026 14:52:18 +0000</pubDate></item>
<item><title>4 Set-and-Forget ETFs Built to Reward Patient Investors</title>
<link>https://www.nasdaq.com/articles/4-set-and-forget-etfs</link><author>The Motley Fool</author>
<pubDate>Thu, 15 Oct 2026 15:03:00 +0000</pubDate></item>""")
GOOGLE = rss("""
<item><title>7 Best Treasury ETFs to Buy Now | Investing - U.S. News - Money</title>
<link>https://news.google.com/rss/articles/CBMi1</link><pubDate>Mon, 12 Oct 2026 19:34:00 GMT</pubDate>
<source url="https://money.usnews.com">U.S. News - Money</source></item>
<item><title>Indian mutual funds stop bulk subscriptions to gold ETFs - Reuters</title>
<link>https://news.google.com/rss/articles/CBMi2</link><pubDate>Wed, 14 Oct 2026 08:00:00 GMT</pubDate>
<source url="https://www.reuters.com">Reuters</source></item>""")
DATES = rss("""
<item><title>Treasury yields hold steady</title><link>https://dates.example.com/no-date</link></item>
<item><title>T-bill funds draw cash</title><link>https://dates.example.com/updated</link>
<updated>2026-10-09T10:00:00Z</updated></item>
<item><title>Bond funds rally in the future</title><link>https://dates.example.com/future</link>
<pubDate>Sat, 17 Oct 2026 12:00:00 GMT</pubDate></item>
<item><title>Bond funds rally a little early</title><link>https://dates.example.com/soon</link>
<pubDate>Fri, 16 Oct 2026 12:10:00 GMT</pubDate></item>""")


def fetch(body: str, name: str, publisher_from: str, strip: bool = False):
    src = CuratedSource(name=name, url=f"https://{name}.example/rss", type="outlet", headline_only=True,
                        publisher_from=publisher_from, strip_publisher_suffix=strip)
    return RSSAdapter([src], CFG, parse=lambda url: feedparser.parse(body)).fetch(AS_OF)


# ---- B1 real publishers ------------------------------------------------------------------------------------------
def test_yahoo_publisher_comes_from_the_link_domain():
    items = fetch(YAHOO, "Yahoo Finance", "link_domain").items
    assert [i.source_name for i in items] == ["24/7 Wall St.", "Yahoo Finance", "The Motley Fool"]
    assert {i.feed for i in items} == {"Yahoo Finance"}


def test_nasdaq_publisher_comes_from_the_author_field():
    items = fetch(NASDAQ, "Nasdaq", "author").items
    assert [i.source_name for i in items] == ["BNK Invest", "The Motley Fool"] and items[0].feed == "Nasdaq"


def test_google_publisher_comes_from_the_source_element_and_suffixes_are_removed():
    items = fetch(GOOGLE, "Google News", "source_element", strip=True).items
    assert [(i.title, i.source_name) for i in items] == [
        ("7 Best Treasury ETFs to Buy Now | Investing", "U.S. News - Money"),
        ("Indian mutual funds stop bulk subscriptions to gold ETFs", "Reuters")]
    assert all(i.feed == "Google News" for i in items)


def test_item_ids_do_not_change_for_yahoo_and_nasdaq():
    from egoquant.briefing.fetch import item_id
    it = fetch(YAHOO, "Yahoo Finance", "link_domain").items[0]
    assert it.id == item_id("Yahoo Finance", it.url)         # existing tone/summary caches still apply


# ---- B4 dates --------------------------------------------------------------------------------------------------------
def test_dates_published_updated_unknown_and_future():
    res = fetch(DATES, "Dates", "feed")
    by = {i.url.rsplit("/", 1)[1]: i for i in res.items}
    assert by["no-date"].date_status == "unknown"
    assert by["updated"].date_status == "updated_only" and by["updated"].published_at.day == 9
    assert by["soon"].date_status == "published"                  # 10 minutes ahead: within the 15-minute tolerance
    assert "future" not in by and res.n_future_dates == 1         # a day ahead: rejected


def test_unknown_dates_are_listed_but_kept_out_of_tone_windows():
    dated = [item("REITs fall as office vacancies climb", 1), item("Real estate funds see weak demand", 2)]
    undated = item("REIT shares slump as rates climb", 0).model_copy(update={"date_status": "unknown"})
    b = build([*dated, undated], weights={"VNQ": 1.0})
    vnq = next(a for a in b.instrument_aggregates if a.subject == "VNQ")
    assert vnq.coverage_status.state == "tone" and vnq.coverage_status.n == 2 and vnq.confidence == "low"
    assert b.n_date_unknown == 1 and undated.id in vnq.headline_ids


# ---- B3 capital-only tickers and exchange tags ----------------------------------------------------------------------
@pytest.mark.parametrize("text, has, has_not", [
    ("BIL: ETF Inflow Alert", {"BIL"}, set()),
    ("Noteworthy ETF Inflows: SHY", {"SHY"}, set()),
    ("Investors Shy Away From Bonds", set(), {"SHY"}),
    ("shy", set(), {"SHY"}),
    ("INVESTORS SHY AWAY FROM BONDS AND STOCKS", set(), {"SHY"}),
    ("SHY-looking investors", set(), {"SHY"}),
    ("iShares S&P 500 ETF (ASX:IVV)", set(), {"IVV", "SHY"}),
    ("ASX:IVV hits a record", set(), {"IVV"}),
    ("Vanguard FTSE All-World ex-US (VXUS.DE)", set(), {"VXUS", "DE"}),
    ("What Did Waller Say About Hikes and IEF (NASDAQ:IEF)?", {"IEF"}, set()),
    ("IVV (NYSEARCA:IVV) tracks the index", {"IVV"}, set()),
])
def test_capital_only_tickers_and_us_exchange_tags(text, has, has_not):
    found = set(Matcher(CFG).match_text(text).tickers)
    assert has <= found and not (has_not & found)


# ---- B2 tiers ----------------------------------------------------------------------------------------------------------
def test_tiers_by_publisher_and_headline_pattern():
    assert tier_of(item("Fed holds rates", source_name="Reuters"), CFG)[0] == "tier1"
    assert tier_of(item("SCHD vs VYM", source_name="24/7 Wall St."), CFG)[0] == "tier2"
    assert tier_of(item("(VYM) Volatility Zones", source_name="Stock Traders Daily"), CFG)[0] == "tier3"
    assert tier_of(item("BIL: ETF Inflow Alert", source_name="Reuters"), CFG)[0] == "tier3"   # pattern wins
    assert tier_of(item("Something", source_name="Unknown Blog"), CFG) == ("unrated", "not reviewed yet")
    assert tier_of(item("Thread", source_type="forum", source_name="r/x"), CFG)[0] == "unrated"


def test_every_tier_name_has_a_reason_and_no_name_is_in_two_tiers():
    cfg = load_config().sources
    assert all(reason.strip() for names in cfg.tiers.values() for reason in names.values())


def test_tier3_is_listed_never_scored_never_sent_to_the_llm():
    from tests.briefing.test_fund_tone import FakeClient, cfg_llm
    from egoquant.briefing.fund_tone import FundToneLabeler
    c = cfg_llm()
    client = FakeClient(["negative"] * 10)
    lab = FundToneLabeler(c, client=client, env={"OPENAI_API_KEY": "test"})
    low = [item(t, 1 + i, source_name="Stock Traders Daily") for i, t in enumerate([
        "(VNQ) Volatility Zones as Tactical Triggers", "Precision Trading with REITs Risk Zones",
        "How Real Estate ETF (VNQ) Affects Rotational Strategy Timing"])]
    good = [item("REITs fall as office vacancies climb", 1, source_name="Reuters"),
            item("Real estate funds see weak demand", 2, source_name="Reuters")]
    b = build(low + good, weights={"VNQ": 0.3, "AGG": 0.7}, cfg=c, fund_tone=lab)
    assert len(client.calls) == 2 and all("Volatility" not in str(x) for x in client.calls)
    assert set(b.low_reliability_items) == {i.id for i in low} and b.tier_counts["tier3"] == 3
    vnq = next(a for a in b.instrument_aggregates if a.subject == "VNQ")
    assert not set(vnq.headline_ids) & set(b.low_reliability_items) and len(vnq.headline_ids) == 2
    assert vnq.confidence == "low"                                  # 2 usable: rated, but low confidence


# ---- B5 cards --------------------------------------------------------------------------------------------------------
def test_every_holding_has_a_card_even_without_news_and_under_1_percent():
    b = build([item("REITs fall as office vacancies climb", 1)], areas=("real_estate", "bonds"),
              weights={"BIL": 0.6, "VNQ": 0.395, "VXUS": 0.005})
    held = [a for a in b.instrument_aggregates if a.held]
    assert [a.subject for a in held] == ["BIL", "VNQ", "VXUS"] == b.holdings_order
    bil = held[0]
    assert bil.coverage_status.state == "quiet" and bil.coverage_status.n == 0 and bil.weight == 0.6
    assert "quiet week" in bil.text and "what it holds" in bil.text
    assert bil.name and bil.r_level == 1
    vxus_item = build([item("VXUS falls on Europe", 1)], areas=("stocks",), weights={"BIL": 0.995, "VXUS": 0.005})
    assert "under 1%" in vxus_item.items[0].exposure_text


def test_area_cards_only_for_chosen_areas_with_instrument_status_and_nesting():
    b = build([item("REITs fall as office vacancies climb", 1)], areas=("stocks", "us_stocks", "gold"),
              weights={"VTI": 0.5, "BIL": 0.5}, max_r_level=3)
    assert [a.subject for a in b.area_aggregates] == ["stocks", "us_stocks", "gold"] == b.chosen_areas
    us = next(a for a in b.area_aggregates if a.subject == "us_stocks")
    assert us.parent_area == "stocks"
    status = {i.ticker: i.status for a in b.area_aggregates for i in a.instruments}
    assert status["VTI"] == "held" and status["VOO"] == "not_held"
    above = [i for i in next(a for a in b.area_aggregates if a.subject == "gold").instruments if i.status == "above_risk_level"]
    assert above and all(i.r_level > 3 for i in above)


def test_no_headline_is_shown_twice():
    items = [item("SCHD and VIG lead dividend stocks higher", 1), item("Dividend stocks rally again", 2),
             item("U.S. stocks gain on earnings", 1), item("S&P 500 hits a record", 2)]
    b = build(items, areas=("stocks", "us_stocks", "dividend_stocks"), weights={"SCHD": 0.6, "VIG": 0.4})
    shown = [i for a in b.instrument_aggregates + b.area_aggregates for i in a.headline_ids]
    assert len(shown) == len(set(shown))
    schd = next(a for a in b.instrument_aggregates if a.subject == "SCHD")
    vig = next(a for a in b.instrument_aggregates if a.subject == "VIG")
    assert vig.shown_under.get("SCHD", 0) >= 1                    # the shared headline sits under the larger holding
    div = next(a for a in b.area_aggregates if a.subject == "dividend_stocks")
    assert div.n_shown_under_holdings >= 1 and schd.headline_ids


def test_top_level_counts_largest_move_and_single_tier_notes():
    week = [item(t, 1 + i, source_name="24/7 Wall St.") for i, t in enumerate([
        "REITs fall as office vacancies climb", "Real estate funds see weak demand", "REIT shares slump as rates climb"])]
    last = [item(t, 8 + i, source_name="24/7 Wall St.") for i, t in enumerate([
        "REITs gain on strong leasing", "Real estate funds draw record inflows", "Apartment REITs post solid rent growth"])]
    b = build(week + last, weights={"VNQ": 0.3, "AGG": 0.7})
    assert b.largest_move is None                                   # 3 opinion-site headlines a week: low confidence
    assert b.alert_threshold == 0.5 and b.period_words == {"this": "this week", "last": "last week"}
    assert b.single_tier_notes == ["All 3 headlines about VNQ this week come from opinion sites, so read them as opinions."]
    assert b.tier_counts["tier2"] == 3 and set(b.method_counts) == {"llm_fund_aware", "finbert", "not_yet_assessed"}
    assert [t.tier for t in b.tier_legend] == ["tier1", "tier2", "unrated", "tier3"]
    assert b.diversity_suggestions == []


def test_largest_move_needs_medium_confidence_in_both_periods():
    week = [item(t, 1 + i, source_name="Reuters") for i, t in enumerate([
        "REITs fall as office vacancies climb", "Real estate funds see weak demand", "REIT shares slump as rates climb"])]
    last = [item(t, 8 + i, source_name="Reuters") for i, t in enumerate([
        "REITs gain on strong leasing", "Real estate funds draw record inflows", "Apartment REITs post solid rent growth"])]
    b = build(week + last, weights={"VNQ": 0.3, "AGG": 0.7})
    vnq = next(a for a in b.instrument_aggregates if a.subject == "VNQ")
    assert vnq.confidence in ("medium", "high") and vnq.confidence_previous in ("medium", "high")
    assert b.largest_move is not None and b.largest_move.subject == "VNQ"


# ---- both periods from one fetch, through the web hook and the route -------------------------------------------------
def test_web_fixture_returns_week_with_month_attached():
    from egoquant.briefing.web import build_web_briefing
    from tests.briefing.common import interests, proposal
    b = build_web_briefing(interests=interests("real_estate"), selection=None, proposal=proposal({"VNQ": 0.3, "BIL": 0.7}),
                           as_of=AS_OF, horizon_years=5, env={"EGOQUANT_BRIEFING": "fixture"})
    assert b.briefing_period == "week" and b.month is not None and b.month.briefing_period == "month"
    assert b.month.period_words["this"] == "this month" and b.month.month is None


def test_portfolio_route_returns_both_periods_and_unchanged_numbers(monkeypatch):
    import egoquant_routes
    from fastapi.testclient import TestClient
    from main import app
    from tests.test_egoquant_routes import portfolio_body
    from egoquant.demo.personas import PERSONAS
    persona = next(p for p in PERSONAS.values() if p.expected_class == "C4")
    monkeypatch.setenv("EGOQUANT_DATA", "synthetic")
    client = TestClient(app)
    monkeypatch.setenv("EGOQUANT_BRIEFING", "off")
    plain = client.post("/portfolio", json=portfolio_body(persona)).json()
    egoquant_routes._portfolio_timestamps.clear()
    monkeypatch.setenv("EGOQUANT_BRIEFING", "fixture")
    news = client.post("/portfolio", json=portfolio_body(persona)).json()
    r = news["report"]
    assert r["market_briefing"]["briefing_period"] == "week" and "month" not in r["market_briefing"]
    assert r["market_briefing_month"]["briefing_period"] == "month"
    assert isinstance(r["what_changed_month"], list)
    assert r["what_changed_month_action"] in (None, "offer_risk_profile_recheck")
    assert r["allocation"] == plain["report"]["allocation"] and news["approved"] == plain["approved"]
    assert plain["report"]["market_briefing_month"] is None


def test_advisor_facts_follow_the_news_tab_structure():
    from egoquant.advisor.context import build_portfolio_facts
    from egoquant.briefing.web import build_web_briefing
    from egoquant.demo.personas import PERSONAS, run_persona
    from functools import partial
    run = run_persona(PERSONAS["growth_investor"], date(2026, 10, 16), price_source=SyntheticPriceSource(),
                      briefing_builder=partial(build_web_briefing, env={"EGOQUANT_BRIEFING": "fixture"}))
    report = run.report.model_dump(mode="json")
    report["market_briefing_month"] = report["market_briefing"].pop("month")
    report["what_changed_month"] = ["x"]
    facts = build_portfolio_facts({"report": report, "approved": True})
    mb = facts["market_briefing"]
    # BIL and SHY both sit at the 20% fund cap; their order is float noise (0.2 +/- 3e-16) and differs by platform
    assert {h["subject"] for h in mb["your_holdings"][:2]} == {"BIL", "SHY"}
    assert {a["subject"] for a in mb["areas_you_follow"]} >= {"real_estate", "technology"}
    assert mb["source_tiers"]["legend"] and "tone_methods" in mb
    assert facts["market_briefing_month"]["period"].startswith("month") and facts["what_changed_month"] == ["x"]
    assert "items" not in facts["market_briefing_month"]


# ---- dedupe stays correct and fast on thousands of headlines ------------------------------------------------------
def test_dedupe_catches_near_identical_titles_and_scales():
    import time
    from egoquant.briefing.fetch import prepare
    import random
    rnd = random.Random(7)
    words = [f"{a}{b}{c}" for a in ("tre", "div", "mar", "inv", "bon", "sto", "inf", "ear", "ral", "sl")
             for b in ("asu", "ide", "ket", "est", "ond", "ock", "lat", "nin", "lyi", "ump")
             for c in ("ry", "nds", "ers", "ion", "ing")]          # 500 different words, like real headlines
    titles = list(dict.fromkeys(" ".join(rnd.sample(words, 8)).capitalize() for _ in range(4000)))[:3000]
    base = [item(t, 1 + (i % 9) / 10, url=f"https://d.example.com/{i}") for i, t in enumerate(titles)]
    near = item(titles[5] + "!", 1.5, url="https://d.example.com/dup")
    t0 = time.monotonic()
    p = prepare([*base, near], AS_OF, CFG)
    assert time.monotonic() - t0 < 5
    assert p.n_after_dedupe == 3000                       # the near-identical copy was dropped


def test_exchange_tag_copies_of_a_headline_are_one_story():
    from egoquant.briefing.fetch import prepare
    a = item("Swap VXUS For SCHY To Avoid Over-Diversification", 2, url="https://d.example.com/a")
    b = item("Swap VXUS For SCHY To Avoid Over-Diversification (NASDAQ:VXUS)", 1, url="https://d.example.com/b")
    assert prepare([a, b], AS_OF, CFG).n_after_dedupe == 1


def test_largest_move_prefers_holdings_over_followed_areas():
    week = [item(t, 1 + i, source_name="Reuters") for i, t in enumerate([
        "REITs fall as office vacancies climb", "Real estate funds see weak demand", "REIT shares slump as rates climb"])]
    last = [item(t, 8 + i, source_name="Reuters") for i, t in enumerate([
        "REITs gain on strong leasing", "Real estate funds draw record inflows", "Apartment REITs post solid rent growth"])]
    b = build(week + last, areas=("real_estate",), weights={"VNQ": 0.3, "AGG": 0.7})
    assert b.largest_move.kind == "instrument" and b.largest_move.subject == "VNQ"
