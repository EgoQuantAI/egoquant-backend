"""Exchange tags count as a ticker mention only; bare "Nasdaq" is not QQQ; quote/listing pages are not news."""

import pytest

from egoquant.briefing.fetch import prepare
from egoquant.briefing.loader import load_config
from egoquant.briefing.matching import Matcher
from tests.briefing.common import AS_OF, build, config, item

CFG = load_config()
M = Matcher(CFG)


# ---- exchange tags ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("title, tickers", [
    ("What Did 197,000 Jobless Claims Say for SHY (NASDAQ:SHY)?", ("SHY",)),         # was wrongly QQQ
    ("What Did Waller Say About Hikes and IEF (NASDAQ:IEF)?", ("IEF",)),             # was QQQ + IEF
    ("Why Did 10-Year Yields Hit 5.35% for IEF (NASDAQ:IEF)?", ("IEF",)),
    ("TLT’s Yield Edge Is Thin Over SGOV (NASDAQ:TLT)", ("TLT", "SGOV")),            # was QQQ + TLT; SGOV is named
    ("SCHD: Total Return Still Matters (Rating Downgrade) (NYSEARCA:SCHD)", ("SCHD",)),
    ("A covered call fund review (NYSE ARCA:VNQ)", ("VNQ",)),
    ("Small caps in focus (NYSE:VB)", ("VB",)),                                       # VB is a stopword, the tag still counts
    ("Apple rallies (NASDAQ:AAPL)", ("AAPL",)),                                       # the tag counts; "NASDAQ" is no keyword (not QQQ)
    ("Shares listed (nasdaq: shy) today", ("SHY",)),                                  # case and spacing do not matter
])
def test_exchange_tag_is_a_ticker_mention_only(title, tickers):
    assert M.match_text(title).tickers == tickers


@pytest.mark.parametrize("title, tickers", [
    ("The Nasdaq exchange opens late after a glitch", ()),                            # bare "Nasdaq" is the exchange
    ("Nasdaq Composite falls 2% as chip stocks slide", ("QQQ",)),
    ("Nasdaq-100 hits a record", ("QQQ",)),
    ("Stocks lower as chipmakers slump", ("QQQ",)),
])
def test_nasdaq_keywords(title, tickers):
    assert M.match_text(title).tickers == tickers


def test_bare_nasdaq_is_no_longer_a_qqq_keyword():
    assert "Nasdaq" not in CFG.instrument_keywords["QQQ"] and "Nasdaq Composite" in CFG.instrument_keywords["QQQ"]


# ---- non-news pages ----------------------------------------------------------------------------------------
NON_NEWS = [
    "iShares Gold Trust (IAU) Stock Price, News, Quote & History",
    "iShares Core U.S. Aggregate Bond ETF (AGG.MX) stock price, news, quote and history",
    "Farnam Financial LLC's Vanguard Real Estate Index Fund ETF(VNQ) Holding History",
    "LQD Price, Quote & Chart, NYSE ARCA",
    "Schwab U.S. Dividend Equity ETF (SCHD) Options Chain - Yahoo Finance",
    "Vanguard FTSE All-World Ex-U.S. UCITS ETF USD Acc (VXUS.L) interactive stock chart",
    "Vanguard Funds PLC - Vanguard FTSE All-World Ex-U.S. UCITS ETF USD Acc (VXUS.DE) holdings",
    "VNQ Top Holdings List & Exposure (Vanguard Real Estate ETF)",
    "Vanguard Morningstar Total Stock Market ETF • Robinhood Token - VTI - Stock Price & News",
    "$Schwab US Dividend Equity ETF (SCHD.US)$",
    "Investor eyes iShares Core US Aggregate Bond ET...",
    "BlackRock's IBIT ETF adds 1,426 BTC while Winte…",
    "Pre-Market Most Active for Oct 9, 2026 : PCG, SPCX, NOK, TQQQ, IBIT, VZ",
    "TLT October 26th Options Begin Trading",
    "iShares Bitcoin Trust ETF (IBIT)",
    "iShares 20+ Year Treasury Bond ETF (TLT)",
    "Vanguard Funds PLC - Vanguard FTSE All-World Ex-U.S. UCITS ETF USD Acc (VXUS.DE)",
]
NEWS = [
    "TLT: Popular Treasury Bond ETF Inflows Are Rising as 30-Year Yield Soars",
    "Office vacancies climb to record high, weighing on REITs",
    "Fund holdings of Treasuries rose last quarter",                   # "holdings" inside a sentence is fine
    "Wait... bitcoin rallied again",                                   # an ellipsis inside the title is fine
    "SCHD vs. VYM: Which Dividend ETF Deserves a Spot in Your Income Portfolio?",
    "Is It Time to Look at This Dividend ETF (SCHD)?",                 # a question: news, not a quote page
    "Treasury ETF Inflows Jump 12% (TLT)",                             # numbers/% : news
]


@pytest.mark.parametrize("title", NON_NEWS)
def test_non_news_titles_are_dropped(title):
    p = prepare([item(title, 1)], AS_OF, CFG)
    assert p.items == [] and p.n_non_news == 1


@pytest.mark.parametrize("title", NEWS)
def test_news_titles_are_kept(title):
    p = prepare([item(title, 1)], AS_OF, CFG)
    assert len(p.items) == 1 and p.n_non_news == 0


def test_non_news_is_dropped_before_dedupe_and_counted_in_the_briefing():
    quote_page = item("Vanguard Real Estate ETF (VNQ) Stock Price, News, Quote & History", 3, source_name="Quotes")
    story = item("REITs fall as office vacancies climb", 1)
    p = prepare([quote_page, story], AS_OF, CFG)
    assert [i.title for i in p.items] == ["REITs fall as office vacancies climb"]
    assert (p.n_fetched, p.n_non_news, p.n_after_dedupe) == (2, 1, 1)
    b = build([quote_page, story], weights={"VNQ": 0.2})
    assert b.metadata.n_non_news == 1 and len(b.items) == 1


def test_non_news_patterns_come_from_config():
    cfg = config()
    cfg.non_news_patterns = []
    assert len(prepare([item(NON_NEWS[0], 1)], AS_OF, cfg).items) == 1


def test_sampling_cap_is_20():
    assert CFG.eval.max_rows_per_ticker == 20 and CFG.eval.random_seed == 42


# ---- common-word stock tickers (Agent C's catalog now lists single stocks) ------------------------------------
@pytest.mark.parametrize("title, tickers", [
    ("T-bills yield more than savings accounts", ()),             # not AT&T; generic "T-bills" needs a US signal
    ("U.S. T-bills yield more than savings accounts", ("BIL",)),
    ("Startup raises Series C funding", ()),                        # not Citigroup
    ("STOCKS FALL NOW AS YIELDS RISE", ()),                         # all-caps "NOW" is not ServiceNow
    ("Investors buy LOW and SO on dips", ()),
    ("$T dividend in focus", ("T",)),                               # a cashtag still counts
    ("AT&T results (NYSE:T)", ("T",)),                              # an exchange tag still counts
])
def test_common_word_stock_tickers_need_a_cashtag_or_exchange_tag(title, tickers):
    assert M.match_text(title).tickers == tickers


def test_every_single_letter_or_common_word_catalog_ticker_is_a_stopword():
    from egoquant.agents.market_intel import DEFAULT_CATALOG
    common = {"C", "CAT", "D", "DIS", "LOW", "MA", "NOW", "O", "SO", "T", "V", "WELL"}
    in_catalog = {i.ticker for i in DEFAULT_CATALOG}
    single = {t for t in in_catalog if len(t) == 1}
    assert (common & in_catalog) | single <= set(CFG.ticker_stopwords)
