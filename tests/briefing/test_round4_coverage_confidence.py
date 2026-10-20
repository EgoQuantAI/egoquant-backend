"""Round 4: region-aware matching (A1), holdings-only "What changed" (A2/A3), exposure themes (B), damped scores and
confidence (C), grouping and small positions (D). Offline: no network, no OpenAI."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from egoquant.agents.market_intel import DEFAULT_CATALOG
from egoquant.briefing import aggregate as agg
from egoquant.briefing.matching import Matcher
from egoquant.briefing.themes import ThemeMatcher, fund_themes, theme_map
from tests.briefing.common import AS_OF, build, config, interests, item

CFG = config()
M = Matcher(CFG)
PROMPT = Path(__file__).resolve().parents[2] / "egoquant/briefing/prompts/fund_aware_tone.txt"
US_CASH_AND_TREASURY = {"BIL", "SHY", "SGOV", "SHV", "USFR", "IEF", "TLT", "GOVT", "VGSH", "SCHO"}


# ---- A1: non-US government bills never match US T-bill / Treasury funds ------------------------------------------
@pytest.mark.parametrize("title", [
    # the three reported headlines
    "SBP raises Rs685.5bn through treasury bill auction",
    "CBN sells ₦450bn T-bills as yields climb",
    "Treasury Bill Auction Announcement – RIKV 27 0120 – RIKV 27 0721",
    # five more of the same kind
    "Ghana's T-bill auction undersubscribed for third week",
    "Kenya treasury bills oversubscribed as rates fall",
    "RBI to auction ₹20,000 crore of treasury bills",
    "Japan's Ministry of Finance sells 3-month bills at negative yield",
    "Bangladesh Bank treasury bill yields edge up at weekly auction",
])
def test_foreign_government_bills_match_no_us_fund(title):
    assert not set(M.match_text(title).tickers) & US_CASH_AND_TREASURY
    assert "us_tbills_fed" not in ThemeMatcher(CFG).themes_for_text(title)


@pytest.mark.parametrize("title, ticker", [
    ("U.S. Treasury bill yields jump ahead of Fed meeting", "BIL"),
    ("BIL: ETF Inflow Alert", "BIL"),
    ("Treasury bill auction draws $80 billion of demand", "BIL"),           # a dollar amount is a US signal
])
def test_us_bill_news_still_matches(title, ticker):
    assert ticker in M.match_text(title).tickers


def test_generic_bill_word_without_a_us_signal_is_not_enough():
    assert M.match_text("T-bills yield more than savings accounts").tickers == ()


def test_foreign_signal_also_blocks_other_us_region_funds():
    assert not {"VTI", "VOO", "IVV"} & set(M.match_text("Nikkei 225 stocks rally as yen weakens").tickers)


# ---- B1: every catalog fund has exposure themes, single stocks none ----------------------------------------------
def test_theme_mapping_covers_all_catalog_funds():
    themes = theme_map(CFG)
    assert len(themes) == len(DEFAULT_CATALOG) == 292
    for inst in DEFAULT_CATALOG:
        if inst.product_type == "stock":
            assert themes[inst.ticker] == [], inst.ticker
        else:
            assert themes[inst.ticker] and set(themes[inst.ticker]) <= set(CFG.exposure_themes), inst.ticker


def test_overrides_for_core_funds():
    by = {i.ticker: i for i in DEFAULT_CATALOG}
    assert fund_themes(by["BIL"], CFG) == ["us_tbills_fed"] and fund_themes(by["SHY"], CFG) == ["short_ust"]
    assert fund_themes(by["VT"], CFG) == ["global_stocks"]


# ---- B2/B3: exposure news is attached to held funds and weighted 0.6 ----------------------------------------------
def test_fed_news_is_about_what_bil_holds_and_counts_at_the_exposure_factor():
    b = build([item("Fed holds rates steady as FOMC signals patience", 1, source_name="Reuters")],
              areas=("cash",), weights={"BIL": 1.0})
    bi = b.items[0]
    assert bi.matched_tickers == [] and bi.exposure_of == {"BIL": "us_tbills_fed"}
    assert "About what BIL holds" in bi.exposure_text
    bil = next(a for a in b.instrument_aggregates if a.subject == "BIL")
    recency = 0.5 ** (1 / CFG.tone.recency_half_life_days)                        # 1 day old, tier1 weight 1.0
    assert bil.n_exposure == 1 and bil.evidence == pytest.approx(CFG.exposure.factor * recency, abs=1e-3)


def test_exposure_cap_per_theme_and_period():
    cfg = config()
    cfg.exposure.cap_per_theme = {"week": 2, "month": 40}
    news = [item(f"Fed official {w} speaks on rates", 1 + i * 0.1, source_name="Reuters")
            for i, w in enumerate(["Waller", "Bowman", "Barr", "Logan", "Goolsbee"])]
    b = build(news, areas=("cash",), weights={"BIL": 1.0}, cfg=cfg)
    assert sum(bool(x.exposure_of) for x in b.items) == 2


def test_briefing_feeds_search_each_held_theme_once():
    from egoquant.briefing.web import briefing_feeds
    feeds = briefing_feeds(interests("cash"), None, {"BIL": 0.5, "SGOV": 0.5}, config())
    urls = [f.url for f in feeds]
    assert any("FOMC" in u for u in urls) and len(urls) == len(set(urls))


# ---- C1/C2: damped score and confidence ----------------------------------------------------------------------------
def test_damped_score_maths():
    news = [item("REITs fall as office vacancies climb", 1, source_name="Reuters")]
    b = build(news, weights={"VNQ": 1.0})
    vnq = next(a for a in b.instrument_aggregates if a.subject == "VNQ")
    w, v = vnq.evidence, agg.item_value(b.items[0], ["VNQ"])
    assert w == pytest.approx(0.5 ** (1 / CFG.tone.recency_half_life_days), abs=1e-3)
    assert v < 0 and vnq.tone == pytest.approx(w * v / (w + CFG.confidence.k), abs=1e-3)   # sum(w*s) / (sum(w) + k)
    assert abs(vnq.tone) < abs(v) and vnq.confidence == "low"                    # one headline is pulled toward 0


@pytest.mark.parametrize("evidence, label", [(0, None), (0.5, "low"), (1.5, "medium"), (3.9, "medium"), (4.0, "high")])
def test_confidence_thresholds(evidence, label):
    assert agg.confidence_of(evidence, CFG) == label


# ---- C3: alerts only when both periods are medium or high confidence ----------------------------------------------
def test_alert_needs_medium_confidence_in_both_periods():
    strong_now = [item(t, 1 + i * 0.5, source_name="Reuters") for i, t in enumerate([
        "REITs fall as office vacancies climb", "Real estate funds see weak demand", "REIT shares slump as rates climb",
        "Real estate trusts post losses on empty towers"])]
    thin_last = [item("REITs gain on strong leasing", 9, source_name="Some Blog")]
    b = build(strong_now + thin_last, weights={"VNQ": 1.0})
    vnq = next(a for a in b.instrument_aggregates if a.subject == "VNQ")
    assert vnq.confidence_previous == "low" and vnq.change is None and not b.alerts
    assert "VNQ: too little news last week to compare." in b.compare_notes


# ---- A2: "What changed" and alerts use holdings only -----------------------------------------------------------------
def test_interest_areas_never_drive_what_changed_or_alerts():
    week = [item(t, 1 + i * 0.5, source_name="Reuters") for i, t in enumerate([
        "Gold prices fall as dollar firms", "Gold slides on strong jobs data", "Gold drops to a two-month low",
        "Bullion slumps as yields climb", "Gold miners sink with metal prices"])]
    last = [item(t, 8 + i * 0.5, source_name="Reuters") for i, t in enumerate([
        "Gold prices rise on safe-haven demand", "Gold climbs to a record", "Gold gains as dollar weakens",
        "Bullion rallies on rate-cut hopes", "Gold miners surge with metal prices"])]
    b = build(week + last, areas=("gold",), weights={"BIL": 1.0})
    assert not b.alerts and (b.largest_move is None or b.largest_move.kind == "instrument")
    assert all(n.split(":")[0] == "BIL" for n in b.compare_notes)


# ---- C4: final states only -------------------------------------------------------------------------------------------
def test_no_stuck_or_not_enough_coverage_state():
    b = build([item("REITs fall as office vacancies climb", 1)], areas=("real_estate",),
              weights={"BIL": 0.5, "VNQ": 0.5})
    for a in b.instrument_aggregates:
        if a.held:
            assert a.coverage_status.state in ("tone", "quiet", "unrated")
            assert "not enough coverage" not in a.text and "in progress" not in a.text
    bil = next(a for a in b.instrument_aggregates if a.subject == "BIL")
    assert bil.coverage_status.state == "quiet" and bil.text.startswith("BIL: quiet week")


# ---- D1/D2: grouping above 6 holdings, small positions --------------------------------------------------------------
def test_more_than_six_holdings_are_grouped_and_small_positions_listed():
    w = {"VT": 0.3, "BIL": 0.2, "SHY": 0.2, "AGG": 0.1, "VNQ": 0.1, "GLD": 0.05, "QQQ": 0.04, "TLT": 0.01}
    b = build([item("REITs fall as office vacancies climb", 1)], areas=("stocks",), weights=w)
    assert b.holding_groups and {t for g in b.holding_groups for t in g.tickers} == set(w)
    assert all(g.aggregate.text.split(":")[0] == g.label for g in b.holding_groups)
    assert b.small_positions == ["TLT"]
    few = build([item("REITs fall as office vacancies climb", 1)], weights={"VNQ": 0.5, "BIL": 0.5})
    assert few.holding_groups == []


# ---- the pinned prompt is unchanged ----------------------------------------------------------------------------------
def test_fund_aware_prompt_hash_is_unchanged():
    assert hashlib.sha256(PROMPT.read_bytes()).hexdigest() == \
        "ebb752b547507ffcf8edd43fa8eac0bb4dd9990249355a86bbe2bb50ea5c8544"
    assert AS_OF.tzinfo is not None


def test_what_changed_names_held_drawdowns_only():
    from egoquant.agents.market_intel import InvestorInterests
    from egoquant.agents.market_intel.agent import DrawdownAlert
    from egoquant.briefing import jobs
    facts = jobs.context_facts(
        interests=InvestorInterests(investor_id="x", interest_areas=["crypto"], max_r_level=5), watchlist=[],
        weights={"IBIT": 0.5, "BIL": 0.5}, horizon_years=10,
        drawdowns=[DrawdownAlert(ticker=t, window_days=21, drawdown_from_peak=-0.2, message="") for t in ("IBIT", "ETHA")],
        names={}, data_to=AS_OF.date(), synthetic=True, drawdown_threshold=0.10, interest_tickers=["ETHA"])
    lines = jobs._what_changed(facts, None)
    assert lines[0].startswith("IBIT closed") and not any(line.startswith("ETHA") for line in lines)


# ---- E3 precision fixes ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("title", [
    "Global X Emerging Markets Bond ETF (EMBD) 1Q26 Commentary",
    "Is Northern Trust Morningstar Emerging Markets Factor Tilt ETF (TLTE) a Strong ETF Right Now?",
])
def test_a_headline_about_another_fund_is_not_about_ours(title):
    assert "VWO" not in M.match_text(title).tickers


def test_our_fund_named_next_to_its_tag_still_matches():
    assert "VWO" in M.match_text("Vanguard FTSE Emerging Markets ETF (VWO) draws inflows").tickers
    assert "VWO" in M.match_text("Emerging markets rally as dollar slides").tickers


@pytest.mark.parametrize("title, theme", [
    ("BlackRock wants tokenized funds to back traditional Wall Street trades", False),
    ("The AI Boom: 2 Stocks to Buy Now, According to Wall Street Analysts", False),
    ("Stocks slip on Wall Street under pressure from rising oil prices", True),
    ("Wall Street closes higher on earnings", True),
])
def test_wall_street_means_the_market_moving_not_the_industry(title, theme):
    assert ("us_stocks" in ThemeMatcher(CFG).themes_for_text(title)) is theme


def test_no_exposure_from_a_headline_about_another_named_fund():
    news = [item("Is WisdomTree Emerging Markets SmallCap Dividend ETF (DGS) a Strong ETF Right Now?", 1,
                 source_name="Reuters"),
            item("Emerging markets rally as dollar slides", 1, source_name="Reuters")]
    b = build(news, areas=("stocks",), weights={"VXUS": 1.0})
    attached = [x.item.title for x in b.items if x.exposure_of or x.matched_tickers]
    assert attached == ["Emerging markets rally as dollar slides"]


def test_em_credit_is_not_us_bond_news():
    assert "us_bonds" not in ThemeMatcher(CFG).themes_for_text("Tight EM credit spreads mask lingering fiscal risks")
    assert "us_bonds" in ThemeMatcher(CFG).themes_for_text("US credit spreads widen as junk bonds slide")


@pytest.mark.parametrize("title, about, not_about", [
    ("AI Semiconductor Stocks Are a 'Dangerous Playground' - VanEck Semiconductor ETF (NASDAQ:SMH)", {"SMH"}, {"QQQ"}),
    ("VOO vs. IVV: Same S&P 500, Same Fee, So Does It Even Matter Which One You Buy?", {"VOO", "IVV"}, {"VTI"}),
    ("Why I'd Choose VOO Over QQQ For The Next 20 Years (NYSEARCA:VOO)", {"VOO", "QQQ"}, set()),
])
def test_headlines_comparing_or_tagging_funds_are_about_those_funds(title, about, not_about):
    got = set(M.match_text(title).tickers)
    assert about <= got and not (not_about & got)


# ---- cache files: a damaged file never breaks a briefing; concurrent writers never corrupt one ---------------------
def test_damaged_cache_reads_as_empty(tmp_path):
    from egoquant.briefing.loader import read_json_cache
    bad = tmp_path / "news_archive.json"
    bad.write_bytes(b'{"a": "caf\xc3')                       # cut inside a UTF-8 character
    assert read_json_cache(bad) == {}
    bad.write_text('{"a": ', encoding="utf-8")
    assert read_json_cache(bad) == {}


def test_concurrent_cache_writes_stay_valid_json(tmp_path):
    import json
    import threading
    from egoquant.briefing.loader import read_json_cache, write_json_cache
    path = tmp_path / "cache.json"
    big = {f"k{i}": "é" * 200 for i in range(300)}
    threads = [threading.Thread(target=lambda n=n: [write_json_cache(path, {**big, "n": n}) for _ in range(5)])
               for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["k0"] == "é" * 200 and read_json_cache(path) == data
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("title, tickers", [
    ("Tech Stocks Hold This Baron Emerging Markets Fund Back", set()),                 # about one other fund
    ("Vanguard Total Stock Market Index Fund Admiral Shares (VTSAX): What Investors Should Know", set()),
    ("Bitcoin ETF Flows Turn Positive For The Year", {"IBIT"}),                         # the fund's own keyword
    ("I Think BND Is the Best Bond ETF for Most Investors", {"BND"}),                   # our fund named
    ("Money Market Fund Assets Hit a Record as T-Bill Yields Stay High in the U.S.", {"BIL"}),   # a kind of fund
])
def test_write_ups_about_one_other_product(title, tickers):
    got = set(M.match_text(title).tickers)
    assert tickers <= got and (tickers or not got & {"QQQ", "VWO", "VTI", "VOO", "IBIT"})


def test_no_exposure_from_a_write_up_about_one_product():
    news = [item("The S&P 500 ETF You Know Charges 0.0945%. These 2 Charge 0.03%", 1, source_name="Reuters")]
    b = build(news, areas=("stocks",), weights={"SCHD": 1.0})
    assert not any(x.exposure_of for x in b.items)


@pytest.mark.parametrize("title, ticker, matched", [
    ("How Investors Are Reacting To Tritax Big Box REIT (LSE:BBOX) New Chair And Dividend", "VNQ", False),
    ("REITs fall as office vacancies climb", "VNQ", True),
    ("Amazon Stock Has Been Flat for the Past Month, But AMZN Put Credit Spreads Have Worked Out Well", "LQD", False),
    ("Corporate credit spreads widen as recession fears grow", "LQD", True),
])
def test_foreign_listings_and_options_spreads(title, ticker, matched):
    assert (ticker in M.match_text(title).tickers) is matched


def test_foreign_dividend_stocks_are_not_exposure_news_for_a_us_dividend_fund():
    news = [item("Peyto Exploration Development And 2 Canadian Dividend Stocks To Watch", 1, source_name="Reuters"),
            item("Dividend stocks rally as yields ease", 1, source_name="Reuters")]
    b = build(news, areas=("stocks",), weights={"VIG": 1.0})
    vig = [x.item.title for x in b.items if "VIG" in x.exposure_of or "VIG" in x.matched_tickers]
    assert vig == ["Dividend stocks rally as yields ease"]


def test_every_holding_stays_inside_the_ticker_cap_small_positions_last_among_holdings():
    from egoquant.briefing.build import select_tickers
    w = {f: 0.9 / 20 for f in ["VT", "VTI", "VOO", "QQQ", "VXUS", "VWO", "BND", "AGG", "BIL", "SHY", "IEF", "TLT",
                               "TIP", "LQD", "SGOV", "VNQ", "GLD", "SCHD", "VIG", "IBIT"]}
    w.update({t: 0.02 - 1e-6 for t in ["XLK", "XLV", "XLE", "AAPL", "MSFT"]})
    got, _ = select_tickers(interests("crypto", "stocks"), None, w, DEFAULT_CATALOG, max_tickers=30)
    assert set(w) <= set(got[:25]) and got[20:25] == ["AAPL", "MSFT", "XLE", "XLK", "XLV"]


# ---- close-out regressions ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("title, fed", [
    ("Powell's downtown revitalization moves forward with new projects", False),   # "Powell" alone is not the Fed
    ("Powell says the Fed will keep rates on hold", True),
    ("Fed Chair Powell signals patience", True),
    ("Powell warns inflation is sticky", True),
])
def test_powell_alone_is_not_a_fed_signal(title, fed):
    themes = ThemeMatcher(CFG).themes_for_text(title)
    assert ("us_tbills_fed" in themes) is fed and ("short_ust" in themes) is fed


@pytest.mark.parametrize("title", [
    "BNB Chain claims 42.6% of tokenized stock market, beating Ethereum and Solana",
    "Following the SEC's new regulations, 63 U.S. stocks have been tokenized on-chain",
])
def test_crypto_news_is_not_us_stock_market_news(title):
    assert "us_stocks" not in ThemeMatcher(CFG).themes_for_text(title)
    assert not {"VTI", "VOO", "IVV", "SPY", "ITOT", "SCHB"} & set(M.match_text(title).tickers)


def test_us_stock_news_without_crypto_still_counts():
    assert "us_stocks" in ThemeMatcher(CFG).themes_for_text("U.S. stocks rally on earnings")
    assert "VTI" in M.match_text("U.S. stocks rally on earnings").tickers
