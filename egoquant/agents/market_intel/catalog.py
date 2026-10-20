"""Agent C instrument catalog: which real, tradable instruments represent each interest area.

The chatbot talks in INTEREST AREAS ("stocks", "gold", "real estate"). Agent A talks in ASSETS with
prices and metadata. This file is the bridge: a fixed, reviewable table, not something an LLM decides.

Every number below is a STARTING POINT that the team must review before submission:
  * expense ratios, durations and convexities are APPROXIMATE (check each issuer's fact sheet);
  * r_level (R1..R5) is a PLACEHOLDER: who assigns product risk levels is an open team question.
    The rule used, so every rating can be checked against it:
        R1  cash and T-bill funds; 1-3 year Treasuries (kept at R1 so a C1 portfolio is not all T-bills)
        R2  investment-grade bond funds (government, aggregate, corporate, TIPS, international)
        R3  broad, diversified equity funds; long-duration Treasuries; gold
        R4  concentrated equity (one sector, one region, emerging markets, small caps, REITs);
            large-cap single stocks; silver, platinum, palladium, copper and broad commodity funds
        R5  crypto-assets (spot pairs, crypto ETPs and gold-backed tokens such as PAXG)
    The Risk Engine re-checks every holding against the investor's max_r_level, and the C-level
    table caps commodity and crypto weights on top of the R-level (risk_profile/config.yaml).
  * single stocks also carry a per-instrument cap (SINGLE_STOCK_MAX_WEIGHT, PROVISIONAL) that Agent A
    enforces and the Risk Engine re-checks.
  * synthetic_* parameters are INVENTED and are only used by the offline SyntheticPriceSource.

CATALOG ORDER IS PRIORITY ORDER. Within each interest area the more common instrument (larger, more
traded) comes first, so when Agent C cannot fetch everything (Tiingo's free tier allows about 50
requests an hour) it fetches the most common choices first. The order reflects approximate size on
2026-10-18; every US ticker was checked against Tiingo's supported-tickers list that day.

EXPOSURE GROUPS: funds that track (nearly) the same thing, e.g. VOO, IVV and SPY. Agent A receives
only the first member of a group that can be priced; the others are fallbacks if it cannot be. Two
near-identical funds would only make the optimizer split money between them.

asset_class is Agent A's vocabulary (AssetClass in egoquant/models.py): "equity", "bond", "cash",
"commodity" (gold, silver, broad commodities) and "crypto". Listed REITs count as "equity" (see VNQ).
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field

from egoquant.models import AssetClass

# --------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------
# Industries (GICS-style sectors). Each is also an interest area, so it flows through universe selection.
Sector = Literal[
    "technology", "healthcare", "financials", "energy", "consumer", "industrials", "utilities",
    "communication", "materials", "real_estate",
]
SECTORS: tuple[str, ...] = get_args(Sector)

InterestArea = Literal[
    "stocks", "us_stocks", "international_stocks", "emerging_markets", "china", "small_caps",
    "dividend_stocks",
    "technology", "healthcare", "financials", "energy", "consumer", "industrials", "utilities",
    "communication", "materials", "real_estate",
    "bonds", "government_bonds", "long_term_bonds", "corporate_bonds", "inflation_protected",
    "international_bonds",
    "gold", "precious_metals", "commodities", "crypto", "altcoins", "cash",
]

ProductType = Literal["cash_etf", "bond_etf", "equity_etf", "reit_etf", "stock", "commodity_etp", "crypto_etp",
                      "crypto_spot"]

# How the investor likes to hold stocks: funds only, individual stocks, or both (preferences module).
InstrumentStyle = Literal["funds", "individual", "mix"]

SINGLE_STOCK_MAX_WEIGHT = 0.10    # PROVISIONAL: most one single stock may weigh in a portfolio

# Where an instrument trades. This decides (1) which trading platforms can buy it and (2) which data
# feed prices it: "us_listed" -> an end-of-day US market data vendor (Tiingo or a CSV you downloaded);
# "crypto_spot" -> Binance's public market-data API (the venue's own prices).
Market = Literal["us_listed", "crypto_spot"]

# Free text the chatbot may pass through -> canonical area. Matching is done after lower-casing and
# turning spaces, hyphens, "&" and "and" into underscores (see normalize_interest_area).
INTEREST_ALIASES: dict[str, InterestArea] = {
    "stock": "stocks", "equities": "stocks", "equity": "stocks", "shares": "stocks",
    "global_stocks": "stocks", "world_stocks": "stocks",
    "us_equities": "us_stocks", "american_stocks": "us_stocks", "us_market": "us_stocks",
    "international": "international_stocks", "developed_markets": "international_stocks",
    "non_us_stocks": "international_stocks",
    "emerging": "emerging_markets", "em": "emerging_markets", "emerging_market_stocks": "emerging_markets",
    "chinese_stocks": "china", "china_stocks": "china",
    "tech": "technology", "tech_stocks": "technology",
    "health": "healthcare", "health_care": "healthcare", "pharma": "healthcare", "biotech": "healthcare",
    "banks": "financials", "finance": "financials", "financial": "financials",
    "oil_and_gas": "energy",
    "consumer_goods": "consumer", "retail": "consumer",
    "industrial": "industrials", "manufacturing": "industrials",
    "utility": "utilities",
    "telecom": "communication", "media": "communication", "communications": "communication",
    "mining": "materials", "chemicals": "materials",
    "small_cap": "small_caps", "small_cap_stocks": "small_caps", "small_companies": "small_caps",
    "dividends": "dividend_stocks", "dividend": "dividend_stocks", "income_stocks": "dividend_stocks",
    "bond": "bonds", "fixed_income": "bonds",
    "treasuries": "government_bonds", "treasury": "government_bonds", "govt_bonds": "government_bonds",
    "long_bonds": "long_term_bonds",
    "corporate": "corporate_bonds", "credit": "corporate_bonds",
    "tips": "inflation_protected", "inflation_linked": "inflation_protected",
    "inflation_protected_bonds": "inflation_protected",
    "international_bond": "international_bonds", "global_bonds": "international_bonds",
    "foreign_bonds": "international_bonds",
    "realestate": "real_estate", "reits": "real_estate", "reit": "real_estate", "property": "real_estate",
    "gold_metals": "precious_metals", "gold_and_metals": "precious_metals", "metals": "precious_metals",
    "silver": "precious_metals",
    "commodity": "commodities", "raw_materials": "commodities", "oil": "commodities",
    "cryptocurrency": "crypto", "cryptocurrencies": "crypto", "bitcoin": "crypto", "digital_assets": "crypto",
    "altcoin": "altcoins", "solana": "altcoins",
    "money_market": "cash", "savings": "cash", "t_bills": "cash",
}


class Instrument(BaseModel):
    """One tradable instrument Agent C knows how to price."""

    ticker: str                                       # becomes asset_id for Agent A
    name: str
    asset_class: AssetClass
    product_type: ProductType
    r_level: int = Field(ge=1, le=5)                  # PLACEHOLDER product risk level R1..R5
    areas: list[InterestArea]                         # interest areas this instrument represents
    sector: Sector | None = None                      # industry, for sector funds and single stocks
    core: bool = False                                # always in the universe (see below)
    expense_ratio: float = Field(0.0, ge=0)           # APPROXIMATE annual fee, fraction
    trading_cost_bps: float = Field(2.0, ge=0)        # rough one-way cost for a liquid ETF
    duration_years: float | None = None               # APPROXIMATE, bonds only
    convexity: float | None = None                    # APPROXIMATE, bonds only (years^2)
    liquidity_tier: int = 1
    market: Market = "us_listed"
    aliases: list[str] = Field(default_factory=list)  # other names a user may type, e.g. "BTC"
    max_weight: float | None = Field(None, gt=0, le=1)  # per-instrument cap (single stocks)
    exposure_group: str | None = None                 # set from EXPOSURE_GROUPS below
    # Offline synthetic generator only (INVENTED): annual mean, annual vol, equity-factor and
    # rate-factor loadings (|loadings| must be <= 1 in total).
    synthetic_mu: float = 0.05
    synthetic_vol: float = 0.15
    synthetic_equity_loading: float = 0.0
    synthetic_rate_loading: float = 0.0

    @property
    def trades_24_7(self) -> bool:
        """Crypto spot trades every day; exchange-listed funds only on exchange business days."""
        return self.market == "crypto_spot"


def _sector_fund(ticker: str, name: str, sector: str) -> Instrument:
    """A one-industry equity fund (R4: concentrated equity)."""
    return Instrument(ticker=ticker, name=name, asset_class="equity", product_type="equity_etf", r_level=4,
                      areas=[sector], sector=sector, expense_ratio=0.0009, trading_cost_bps=1.0,
                      synthetic_mu=0.07, synthetic_vol=0.20, synthetic_equity_loading=0.85)


def _stock(ticker: str, name: str, sector: str) -> Instrument:
    """A large US single stock: R4 and capped at SINGLE_STOCK_MAX_WEIGHT of the portfolio."""
    return Instrument(ticker=ticker, name=name, asset_class="equity", product_type="stock", r_level=4,
                      areas=[sector], sector=sector, trading_cost_bps=2.0, max_weight=SINGLE_STOCK_MAX_WEIGHT,
                      synthetic_mu=0.08, synthetic_vol=0.28, synthetic_equity_loading=0.70)


def _fund(ticker: str, name: str, areas: list[str], expense_ratio: float, *, r_level: int = 3,
          asset_class: str = "equity", product_type: str = "equity_etf", sector: str | None = None,
          trading_cost_bps: float = 2.0, mu: float = 0.07, vol: float = 0.16, equity: float = 0.9,
          rate: float = 0.0) -> Instrument:
    """An exchange-traded fund or trust. mu, vol, equity and rate are the INVENTED synthetic parameters."""
    return Instrument(ticker=ticker, name=name, asset_class=asset_class, product_type=product_type,
                      r_level=r_level, areas=areas, sector=sector, expense_ratio=expense_ratio,
                      trading_cost_bps=trading_cost_bps, synthetic_mu=mu, synthetic_vol=vol,
                      synthetic_equity_loading=equity, synthetic_rate_loading=rate)


def _bond_fund(ticker: str, name: str, areas: list[str], expense_ratio: float, duration: float, *,
               r_level: int = 2, equity: float = 0.0) -> Instrument:
    """A bond fund. Duration is APPROXIMATE; convexity is a rough 1.2 x duration^2 rule of thumb."""
    return Instrument(ticker=ticker, name=name, asset_class="bond", product_type="bond_etf", r_level=r_level,
                      areas=areas, expense_ratio=expense_ratio, trading_cost_bps=1.0, duration_years=duration,
                      convexity=round(1.2 * duration**2, 1), synthetic_mu=0.035,
                      synthetic_vol=round(0.01 + 0.008 * duration, 3), synthetic_equity_loading=equity,
                      synthetic_rate_loading=0.9)


def _cash_fund(ticker: str, name: str, expense_ratio: float, duration: float) -> Instrument:
    """A Treasury-bill or floating-rate Treasury fund (R1)."""
    return Instrument(ticker=ticker, name=name, asset_class="cash", product_type="cash_etf", r_level=1,
                      areas=["cash"], expense_ratio=expense_ratio, trading_cost_bps=1.0, duration_years=duration,
                      convexity=0.0, synthetic_mu=0.035, synthetic_vol=0.005, synthetic_rate_loading=0.05)


# Why a core: Agent A's suitability limits (max equity, min cash buffer, loss limits) need somewhere
# to put the money that may NOT go into the investor's chosen areas. An investor who only says
# "crypto" or "tech" still needs defensive assets for a C1/C2 portfolio to be feasible. Agent A caps
# every single asset at max_asset_weight (40% by default), so a C1 portfolio (90%+ outside equities)
# needs at least three defensive funds: T-bills, short Treasuries and the aggregate bond index.
# Agent A's candidate menu runs along the equity axis, so a broad equity fund is included too.
DEFAULT_CATALOG: list[Instrument] = [
    # ---- core (always included) ----
    Instrument(ticker="BIL", name="SPDR Bloomberg 1-3 Month T-Bill ETF", asset_class="cash",
               product_type="cash_etf", r_level=1, areas=["cash"], core=True,
               expense_ratio=0.0014, trading_cost_bps=1.0, duration_years=0.1, convexity=0.0,
               synthetic_mu=0.035, synthetic_vol=0.005, synthetic_rate_loading=0.05),
    Instrument(ticker="AGG", name="iShares Core U.S. Aggregate Bond ETF", asset_class="bond",
               product_type="bond_etf", r_level=2, areas=["bonds"], core=True,
               expense_ratio=0.0003, trading_cost_bps=1.0, duration_years=6.0, convexity=50.0,
               synthetic_mu=0.035, synthetic_vol=0.06, synthetic_equity_loading=0.15, synthetic_rate_loading=0.85),
    Instrument(ticker="SHY", name="iShares 1-3 Year Treasury Bond ETF", asset_class="bond",
               product_type="bond_etf", r_level=1, areas=["government_bonds"], core=True,
               expense_ratio=0.0015, trading_cost_bps=1.0, duration_years=1.9, convexity=5.0,
               synthetic_mu=0.03, synthetic_vol=0.02, synthetic_rate_loading=0.80),
    Instrument(ticker="VT", name="Vanguard Total World Stock ETF", asset_class="equity",
               product_type="equity_etf", r_level=3, areas=["stocks"], core=True,
               expense_ratio=0.0006, trading_cost_bps=2.0,
               synthetic_mu=0.07, synthetic_vol=0.16, synthetic_equity_loading=0.95),
    # ---- equity sleeves ----
    Instrument(ticker="VTI", name="Vanguard Total Stock Market ETF", asset_class="equity",
               product_type="equity_etf", r_level=3, areas=["stocks", "us_stocks"],
               expense_ratio=0.0003, trading_cost_bps=1.0,
               synthetic_mu=0.075, synthetic_vol=0.17, synthetic_equity_loading=0.92),
    Instrument(ticker="VXUS", name="Vanguard Total International Stock ETF", asset_class="equity",
               product_type="equity_etf", r_level=3, areas=["stocks", "international_stocks"],
               expense_ratio=0.0005, trading_cost_bps=2.0,
               synthetic_mu=0.065, synthetic_vol=0.17, synthetic_equity_loading=0.85),
    Instrument(ticker="VWO", name="Vanguard FTSE Emerging Markets ETF", asset_class="equity",
               product_type="equity_etf", r_level=4, areas=["emerging_markets"],
               expense_ratio=0.0007, trading_cost_bps=3.0,
               synthetic_mu=0.075, synthetic_vol=0.21, synthetic_equity_loading=0.75),
    Instrument(ticker="QQQ", name="Invesco QQQ Trust (Nasdaq-100)", asset_class="equity",
               product_type="equity_etf", r_level=4, areas=["technology"], sector="technology",
               expense_ratio=0.0020, trading_cost_bps=1.0,
               synthetic_mu=0.09, synthetic_vol=0.23, synthetic_equity_loading=0.85),
    Instrument(ticker="VB", name="Vanguard Small-Cap ETF", asset_class="equity",
               product_type="equity_etf", r_level=4, areas=["small_caps"],
               expense_ratio=0.0005, trading_cost_bps=2.0,
               synthetic_mu=0.08, synthetic_vol=0.21, synthetic_equity_loading=0.85),
    Instrument(ticker="SCHD", name="Schwab U.S. Dividend Equity ETF", asset_class="equity",
               product_type="equity_etf", r_level=3, areas=["dividend_stocks"],
               expense_ratio=0.0006, trading_cost_bps=1.0,
               synthetic_mu=0.07, synthetic_vol=0.15, synthetic_equity_loading=0.88),
    Instrument(ticker="MCHI", name="iShares MSCI China ETF", asset_class="equity",
               product_type="equity_etf", r_level=4, areas=["china", "emerging_markets"],
               expense_ratio=0.0059, trading_cost_bps=3.0,
               synthetic_mu=0.07, synthetic_vol=0.27, synthetic_equity_loading=0.60),
    *[_fund(t, n, ["stocks", "us_stocks"], er, mu=0.075, vol=0.17, equity=0.92) for t, n, er in [
        ("VOO", "Vanguard S&P 500 ETF", 0.0003), ("IVV", "iShares Core S&P 500 ETF", 0.0003),
        ("SPY", "SPDR S&P 500 ETF Trust", 0.0009), ("ITOT", "iShares Core S&P Total U.S. Stock Market ETF", 0.0003),
        ("SCHB", "Schwab U.S. Broad Market ETF", 0.0003)]],
    *[_fund(t, n, ["stocks", "international_stocks"], er, mu=0.065, vol=0.17, equity=0.85) for t, n, er in [
        ("IEFA", "iShares Core MSCI EAFE ETF", 0.0007), ("VEA", "Vanguard FTSE Developed Markets ETF", 0.0003),
        ("EFA", "iShares MSCI EAFE ETF", 0.0035), ("SCHF", "Schwab International Equity ETF", 0.0006)]],
    *[_fund(t, n, ["emerging_markets"], er, r_level=4, trading_cost_bps=3.0, mu=0.075, vol=0.21, equity=0.75)
      for t, n, er in [("IEMG", "iShares Core MSCI Emerging Markets ETF", 0.0009),
                       ("EEM", "iShares MSCI Emerging Markets ETF", 0.0070),
                       ("SCHE", "Schwab Emerging Markets Equity ETF", 0.0007)]],
    *[_fund(t, n, ["china", "emerging_markets"], er, r_level=4, trading_cost_bps=3.0, mu=0.07, vol=v, equity=0.60)
      for t, n, er, v in [("FXI", "iShares China Large-Cap ETF", 0.0074, 0.27),
                          ("KWEB", "KraneShares CSI China Internet ETF", 0.0070, 0.38)]],
    *[_fund(t, n, ["small_caps"], er, r_level=4, mu=0.08, vol=0.21, equity=0.85) for t, n, er in [
        ("IWM", "iShares Russell 2000 ETF", 0.0019), ("IJR", "iShares Core S&P Small-Cap ETF", 0.0006),
        ("SCHA", "Schwab U.S. Small-Cap ETF", 0.0004), ("VBR", "Vanguard Small-Cap Value ETF", 0.0007)]],
    *[_fund(t, n, ["dividend_stocks"], er, mu=0.07, vol=0.15, equity=0.88) for t, n, er in [
        ("VIG", "Vanguard Dividend Appreciation ETF", 0.0005), ("VYM", "Vanguard High Dividend Yield ETF", 0.0006),
        ("DGRO", "iShares Core Dividend Growth ETF", 0.0008), ("DVY", "iShares Select Dividend ETF", 0.0038)]],
    # ---- sector funds (one industry each; technology is QQQ above, real estate is VNQ below) ----
    *[_sector_fund(t, n, sec) for t, n, sec in [
        ("XLV", "Health Care Select Sector SPDR Fund", "healthcare"),
        ("XLF", "Financial Select Sector SPDR Fund", "financials"),
        ("XLE", "Energy Select Sector SPDR Fund", "energy"),
        ("XLY", "Consumer Discretionary Select Sector SPDR Fund", "consumer"),
        ("XLP", "Consumer Staples Select Sector SPDR Fund", "consumer"),
        ("XLI", "Industrial Select Sector SPDR Fund", "industrials"),
        ("XLU", "Utilities Select Sector SPDR Fund", "utilities"),
        ("XLC", "Communication Services Select Sector SPDR Fund", "communication"),
        ("XLB", "Materials Select Sector SPDR Fund", "materials"),
        ("VGT", "Vanguard Information Technology ETF", "technology"),
        ("XLK", "Technology Select Sector SPDR Fund", "technology"),
        ("SMH", "VanEck Semiconductor ETF", "technology"),
        ("IGV", "iShares Expanded Tech-Software Sector ETF", "technology"),
        ("VHT", "Vanguard Health Care ETF", "healthcare"),
        ("IBB", "iShares Biotechnology ETF", "healthcare"),
        ("VFH", "Vanguard Financials ETF", "financials"),
        ("KRE", "SPDR S&P Regional Banking ETF", "financials"),
        ("VDE", "Vanguard Energy ETF", "energy"),
        ("XOP", "SPDR S&P Oil & Gas Exploration & Production ETF", "energy"),
        ("VCR", "Vanguard Consumer Discretionary ETF", "consumer"),
        ("VDC", "Vanguard Consumer Staples ETF", "consumer"),
        ("VIS", "Vanguard Industrials ETF", "industrials"),
        ("ITA", "iShares U.S. Aerospace & Defense ETF", "industrials"),
        ("VPU", "Vanguard Utilities ETF", "utilities"),
        ("VOX", "Vanguard Communication Services ETF", "communication"),
        ("VAW", "Vanguard Materials ETF", "materials"),
        ("GDX", "VanEck Gold Miners ETF", "materials"),
    ]],
    # Listed REITs trade and fall like equities, so Agent A treats them as equity (they count toward
    # the equity cap and take the equity stress shock). This is a modelling choice for the team to confirm.
    Instrument(ticker="VNQ", name="Vanguard Real Estate ETF", asset_class="equity",
               product_type="reit_etf", r_level=4, areas=["real_estate"], sector="real_estate",
               expense_ratio=0.0013, trading_cost_bps=2.0,
               synthetic_mu=0.06, synthetic_vol=0.20, synthetic_equity_loading=0.70, synthetic_rate_loading=0.30),
    *[_fund(t, n, ["real_estate"], er, r_level=4, product_type="reit_etf", sector="real_estate",
            mu=0.06, vol=0.20, equity=0.70, rate=0.30) for t, n, er in [
        ("SCHH", "Schwab U.S. REIT ETF", 0.0007), ("XLRE", "Real Estate Select Sector SPDR Fund", 0.0008),
        ("IYR", "iShares U.S. Real Estate ETF", 0.0039)]],
    # ---- individual stocks: up to 20 large US companies per industry (offered only to investors who
    # want individual stocks; R4; at most SINGLE_STOCK_MAX_WEIGHT each). A starting list, not a stock
    # pick: the largest, most liquid names, so each industry is represented. The first five of each
    # industry are the original list; the rest follow in approximate size order. ----
    *[_stock(t, n, sec) for sec, names in [
        ("technology", [("AAPL", "Apple"), ("MSFT", "Microsoft"), ("NVDA", "NVIDIA"), ("AVGO", "Broadcom"),
                        ("ORCL", "Oracle"), ("AMD", "Advanced Micro Devices"), ("CRM", "Salesforce"),
                        ("CSCO", "Cisco Systems"), ("IBM", "IBM"), ("ACN", "Accenture"), ("ADBE", "Adobe"),
                        ("QCOM", "Qualcomm"), ("TXN", "Texas Instruments"), ("INTU", "Intuit"),
                        ("NOW", "ServiceNow"), ("AMAT", "Applied Materials"), ("MU", "Micron Technology"),
                        ("LRCX", "Lam Research"), ("KLAC", "KLA"), ("ADI", "Analog Devices")]),
        ("healthcare", [("LLY", "Eli Lilly"), ("UNH", "UnitedHealth Group"), ("JNJ", "Johnson & Johnson"),
                        ("MRK", "Merck & Co."), ("PFE", "Pfizer"), ("ABBV", "AbbVie"),
                        ("TMO", "Thermo Fisher Scientific"), ("ABT", "Abbott Laboratories"),
                        ("ISRG", "Intuitive Surgical"), ("DHR", "Danaher"), ("AMGN", "Amgen"),
                        ("BSX", "Boston Scientific"), ("SYK", "Stryker"), ("GILD", "Gilead Sciences"),
                        ("VRTX", "Vertex Pharmaceuticals"), ("MDT", "Medtronic"), ("BMY", "Bristol-Myers Squibb"),
                        ("CVS", "CVS Health"), ("ELV", "Elevance Health"), ("CI", "Cigna Group")]),
        ("financials", [("JPM", "JPMorgan Chase"), ("BRK-B", "Berkshire Hathaway (Class B)"), ("V", "Visa"),
                        ("MA", "Mastercard"), ("BAC", "Bank of America"), ("WFC", "Wells Fargo"),
                        ("GS", "Goldman Sachs"), ("MS", "Morgan Stanley"), ("AXP", "American Express"),
                        ("C", "Citigroup"), ("SCHW", "Charles Schwab"), ("BLK", "BlackRock"),
                        ("SPGI", "S&P Global"), ("PGR", "Progressive"), ("CB", "Chubb"),
                        ("ICE", "Intercontinental Exchange"), ("CME", "CME Group"),
                        ("PNC", "PNC Financial Services"), ("USB", "U.S. Bancorp")]),
        ("energy", [("XOM", "Exxon Mobil"), ("CVX", "Chevron"), ("COP", "ConocoPhillips"), ("EOG", "EOG Resources"),
                    ("SLB", "SLB"), ("MPC", "Marathon Petroleum"), ("PSX", "Phillips 66"),
                    ("OXY", "Occidental Petroleum"), ("WMB", "Williams Companies"), ("KMI", "Kinder Morgan"),
                    ("OKE", "ONEOK"), ("VLO", "Valero Energy"), ("BKR", "Baker Hughes"),
                    ("FANG", "Diamondback Energy"), ("DVN", "Devon Energy"), ("HAL", "Halliburton"),
                    ("TRGP", "Targa Resources"), ("EQT", "EQT Corporation"), ("LNG", "Cheniere Energy")]),
        ("consumer", [("AMZN", "Amazon"), ("WMT", "Walmart"), ("PG", "Procter & Gamble"), ("KO", "Coca-Cola"),
                      ("MCD", "McDonald's"), ("TSLA", "Tesla"), ("COST", "Costco Wholesale"), ("HD", "Home Depot"),
                      ("PEP", "PepsiCo"), ("NKE", "Nike"), ("LOW", "Lowe's"), ("SBUX", "Starbucks"),
                      ("BKNG", "Booking Holdings"), ("TJX", "TJX Companies"),
                      ("PM", "Philip Morris International"), ("MO", "Altria Group"),
                      ("MDLZ", "Mondelez International"), ("CL", "Colgate-Palmolive"), ("TGT", "Target"),
                      ("CMG", "Chipotle Mexican Grill")]),
        ("industrials", [("GE", "GE Aerospace"), ("CAT", "Caterpillar"), ("HON", "Honeywell"),
                         ("UNP", "Union Pacific"), ("DE", "Deere & Company"), ("RTX", "RTX Corporation"),
                         ("BA", "Boeing"), ("LMT", "Lockheed Martin"), ("UPS", "United Parcel Service"),
                         ("ETN", "Eaton"), ("ADP", "Automatic Data Processing"), ("WM", "Waste Management"),
                         ("GD", "General Dynamics"), ("NOC", "Northrop Grumman"), ("ITW", "Illinois Tool Works"),
                         ("EMR", "Emerson Electric"), ("CSX", "CSX Corporation"), ("FDX", "FedEx"), ("MMM", "3M"),
                         ("PH", "Parker-Hannifin")]),
        ("utilities", [("NEE", "NextEra Energy"), ("SO", "Southern Company"), ("DUK", "Duke Energy"),
                       ("AEP", "American Electric Power"), ("D", "Dominion Energy"), ("SRE", "Sempra"),
                       ("EXC", "Exelon"), ("XEL", "Xcel Energy"), ("PCG", "PG&E"), ("ED", "Consolidated Edison"),
                       ("PEG", "Public Service Enterprise Group"), ("WEC", "WEC Energy Group"),
                       ("EIX", "Edison International"), ("ETR", "Entergy"), ("DTE", "DTE Energy"),
                       ("AEE", "Ameren"), ("PPL", "PPL Corporation"), ("CEG", "Constellation Energy"),
                       ("VST", "Vistra"), ("FE", "FirstEnergy")]),
        ("communication", [("GOOGL", "Alphabet (Class A)"), ("META", "Meta Platforms"), ("NFLX", "Netflix"),
                           ("DIS", "Walt Disney"), ("VZ", "Verizon"), ("T", "AT&T"), ("TMUS", "T-Mobile US"),
                           ("CMCSA", "Comcast"), ("CHTR", "Charter Communications"),
                           ("TTWO", "Take-Two Interactive"), ("OMC", "Omnicom Group"),
                           ("LYV", "Live Nation Entertainment"), ("FOXA", "Fox Corporation (Class A)"),
                           ("PINS", "Pinterest")]),
        ("materials", [("LIN", "Linde"), ("SHW", "Sherwin-Williams"), ("APD", "Air Products"),
                       ("FCX", "Freeport-McMoRan"), ("NEM", "Newmont"), ("ECL", "Ecolab"), ("NUE", "Nucor"),
                       ("DOW", "Dow"), ("PPG", "PPG Industries"), ("CTVA", "Corteva"),
                       ("VMC", "Vulcan Materials"), ("MLM", "Martin Marietta Materials"), ("DD", "DuPont"),
                       ("IP", "International Paper"), ("BALL", "Ball Corporation")]),
        ("real_estate", [("PLD", "Prologis"), ("AMT", "American Tower"), ("EQIX", "Equinix"),
                         ("O", "Realty Income"), ("SPG", "Simon Property Group"), ("WELL", "Welltower"),
                         ("PSA", "Public Storage"), ("CCI", "Crown Castle"), ("DLR", "Digital Realty"),
                         ("VICI", "VICI Properties"), ("EXR", "Extra Space Storage"), ("CBRE", "CBRE Group"),
                         ("IRM", "Iron Mountain")]),
    ] for t, n in names],
    # ---- bond sleeves ----
    Instrument(ticker="IEF", name="iShares 7-10 Year Treasury Bond ETF", asset_class="bond",
               product_type="bond_etf", r_level=2, areas=["bonds", "government_bonds"],
               expense_ratio=0.0015, trading_cost_bps=1.0, duration_years=7.0, convexity=60.0,
               synthetic_mu=0.035, synthetic_vol=0.07, synthetic_equity_loading=-0.10, synthetic_rate_loading=0.95),
    Instrument(ticker="TLT", name="iShares 20+ Year Treasury Bond ETF", asset_class="bond",
               product_type="bond_etf", r_level=3, areas=["long_term_bonds"],
               expense_ratio=0.0015, trading_cost_bps=1.0, duration_years=16.5, convexity=350.0,
               synthetic_mu=0.04, synthetic_vol=0.14, synthetic_equity_loading=-0.10, synthetic_rate_loading=0.95),
    Instrument(ticker="LQD", name="iShares iBoxx $ Investment Grade Corporate Bond ETF", asset_class="bond",
               product_type="bond_etf", r_level=2, areas=["corporate_bonds"],
               expense_ratio=0.0014, trading_cost_bps=1.0, duration_years=8.3, convexity=100.0,
               synthetic_mu=0.045, synthetic_vol=0.08, synthetic_equity_loading=0.30, synthetic_rate_loading=0.85),
    Instrument(ticker="TIP", name="iShares TIPS Bond ETF", asset_class="bond",
               product_type="bond_etf", r_level=2, areas=["inflation_protected"],
               expense_ratio=0.0018, trading_cost_bps=1.0, duration_years=6.7, convexity=55.0,
               synthetic_mu=0.035, synthetic_vol=0.055, synthetic_rate_loading=0.80),
    Instrument(ticker="BNDX", name="Vanguard Total International Bond ETF (USD hedged)", asset_class="bond",
               product_type="bond_etf", r_level=2, areas=["international_bonds"],
               expense_ratio=0.0007, trading_cost_bps=1.0, duration_years=7.0, convexity=60.0,
               synthetic_mu=0.03, synthetic_vol=0.05, synthetic_equity_loading=0.05, synthetic_rate_loading=0.85),
    _bond_fund("BND", "Vanguard Total Bond Market ETF", ["bonds"], 0.0003, 5.9, equity=0.15),
    _bond_fund("SCHZ", "Schwab U.S. Aggregate Bond ETF", ["bonds"], 0.0003, 6.0, equity=0.15),
    _bond_fund("VGIT", "Vanguard Intermediate-Term Treasury ETF", ["government_bonds"], 0.0004, 5.1),
    _bond_fund("GOVT", "iShares U.S. Treasury Bond ETF", ["government_bonds"], 0.0005, 5.8),
    _bond_fund("IEI", "iShares 3-7 Year Treasury Bond ETF", ["government_bonds"], 0.0015, 4.3),
    _bond_fund("VGLT", "Vanguard Long-Term Treasury ETF", ["long_term_bonds"], 0.0004, 15.5, r_level=3),
    _bond_fund("SPTL", "SPDR Portfolio Long Term Treasury ETF", ["long_term_bonds"], 0.0003, 15.5, r_level=3),
    _bond_fund("EDV", "Vanguard Extended Duration Treasury ETF", ["long_term_bonds"], 0.0005, 24.0, r_level=3),
    _bond_fund("VCIT", "Vanguard Intermediate-Term Corporate Bond ETF", ["corporate_bonds"], 0.0004, 6.0, equity=0.3),
    _bond_fund("IGIB", "iShares 5-10 Year Investment Grade Corporate Bond ETF", ["corporate_bonds"], 0.0004, 6.2,
               equity=0.3),
    _bond_fund("VCSH", "Vanguard Short-Term Corporate Bond ETF", ["corporate_bonds"], 0.0004, 2.6, equity=0.2),
    _bond_fund("SCHP", "Schwab U.S. TIPS ETF", ["inflation_protected"], 0.0003, 6.5),
    _bond_fund("VTIP", "Vanguard Short-Term Inflation-Protected Securities ETF", ["inflation_protected"], 0.0004, 2.5),
    _bond_fund("STIP", "iShares 0-5 Year TIPS Bond ETF", ["inflation_protected"], 0.0003, 2.4),
    _bond_fund("IAGG", "iShares Core International Aggregate Bond ETF", ["international_bonds"], 0.0007, 7.0),
    _bond_fund("BWX", "SPDR Bloomberg International Treasury Bond ETF (not hedged)", ["international_bonds"],
               0.0035, 7.5),
    # ---- more cash funds (Treasury bills) ----
    _cash_fund("SGOV", "iShares 0-3 Month Treasury Bond ETF", 0.0009, 0.1),
    _cash_fund("SHV", "iShares Short Treasury Bond ETF", 0.0015, 0.3),
    _cash_fund("USFR", "WisdomTree Floating Rate Treasury Fund", 0.0015, 0.02),
    # ---- commodities: gold, silver, broad commodity futures ----
    # Gold is rated like a broad equity fund (R3): its long-run volatility (~15%) is similar, and it has
    # no single-issuer or leverage risk. Silver and broad commodity futures swing more (R4).
    Instrument(ticker="IAU", name="iShares Gold Trust", asset_class="commodity",
               product_type="commodity_etp", r_level=3, areas=["gold"],
               expense_ratio=0.0025, trading_cost_bps=2.0,
               synthetic_mu=0.04, synthetic_vol=0.15, synthetic_equity_loading=0.10, synthetic_rate_loading=0.20),
    Instrument(ticker="SLV", name="iShares Silver Trust", asset_class="commodity",
               product_type="commodity_etp", r_level=4, areas=["precious_metals"],
               expense_ratio=0.0050, trading_cost_bps=2.0,
               synthetic_mu=0.04, synthetic_vol=0.28, synthetic_equity_loading=0.25, synthetic_rate_loading=0.15),
    Instrument(ticker="PDBC", name="Invesco Optimum Yield Diversified Commodity Strategy ETF",
               asset_class="commodity", product_type="commodity_etp", r_level=4, areas=["commodities"],
               expense_ratio=0.0059, trading_cost_bps=3.0,
               synthetic_mu=0.03, synthetic_vol=0.18, synthetic_equity_loading=0.30),
    Instrument(ticker="PPLT", name="abrdn Physical Platinum Shares ETF", asset_class="commodity",
               product_type="commodity_etp", r_level=4, areas=["precious_metals"],
               expense_ratio=0.0060, trading_cost_bps=4.0,
               synthetic_mu=0.03, synthetic_vol=0.27, synthetic_equity_loading=0.30),
    Instrument(ticker="PALL", name="abrdn Physical Palladium Shares ETF", asset_class="commodity",
               product_type="commodity_etp", r_level=4, areas=["precious_metals"],
               expense_ratio=0.0060, trading_cost_bps=5.0,
               synthetic_mu=0.03, synthetic_vol=0.35, synthetic_equity_loading=0.25),
    Instrument(ticker="CPER", name="United States Copper Index Fund", asset_class="commodity",
               product_type="commodity_etp", r_level=4, areas=["commodities"],
               expense_ratio=0.0088, trading_cost_bps=5.0,
               synthetic_mu=0.03, synthetic_vol=0.24, synthetic_equity_loading=0.40),
    *[_fund(t, n, ["gold"], er, asset_class="commodity", product_type="commodity_etp", mu=0.04, vol=0.15,
            equity=0.10, rate=0.20) for t, n, er in [
        ("GLD", "SPDR Gold Shares", 0.0040), ("GLDM", "SPDR Gold MiniShares Trust", 0.0010),
        ("SGOL", "abrdn Physical Gold Shares ETF", 0.0017)]],
    _fund("SIVR", "abrdn Physical Silver Shares ETF", ["precious_metals"], 0.0030, r_level=4, asset_class="commodity",
          product_type="commodity_etp", mu=0.04, vol=0.28, equity=0.25, rate=0.15),
    *[_fund(t, n, ["commodities"], er, r_level=4, asset_class="commodity", product_type="commodity_etp",
            trading_cost_bps=3.0, mu=0.03, vol=v, equity=0.30) for t, n, er, v in [
        ("DBC", "Invesco DB Commodity Index Tracking Fund", 0.0085, 0.18),
        ("GSG", "iShares S&P GSCI Commodity-Indexed Trust", 0.0075, 0.22),
        ("DBA", "Invesco DB Agriculture Fund", 0.0093, 0.15),
        ("USO", "United States Oil Fund", 0.0060, 0.35)]],
    # ---- crypto ----
    Instrument(ticker="IBIT", name="iShares Bitcoin Trust ETF", asset_class="crypto",
               product_type="crypto_etp", r_level=5, areas=["crypto"],
               expense_ratio=0.0025, trading_cost_bps=5.0,
               synthetic_mu=0.10, synthetic_vol=0.55, synthetic_equity_loading=0.40),
    *[_fund(t, n, ["crypto"], er, r_level=5, asset_class="crypto", product_type="crypto_etp", trading_cost_bps=5.0,
            mu=0.10, vol=v, equity=0.40) for t, n, er, v in [
        ("FBTC", "Fidelity Wise Origin Bitcoin Fund", 0.0025, 0.55),
        ("GBTC", "Grayscale Bitcoin Trust ETF", 0.0150, 0.55),
        ("ETHA", "iShares Ethereum Trust ETF", 0.0025, 0.75)]],
    # ---- crypto spot pairs, priced from Binance (quoted in USDT, a USD stablecoin) ----
    Instrument(ticker="BTCUSDT", name="Bitcoin / Tether (Binance spot)", asset_class="crypto",
               product_type="crypto_spot", r_level=5, areas=["crypto"], market="crypto_spot",
               aliases=["BTC", "BITCOIN", "BTC/USDT"], expense_ratio=0.0, trading_cost_bps=10.0,
               synthetic_mu=0.10, synthetic_vol=0.60, synthetic_equity_loading=0.40),
    Instrument(ticker="ETHUSDT", name="Ether / Tether (Binance spot)", asset_class="crypto",
               product_type="crypto_spot", r_level=5, areas=["crypto"], market="crypto_spot",
               aliases=["ETH", "ETHEREUM", "ETH/USDT"], expense_ratio=0.0, trading_cost_bps=10.0,
               synthetic_mu=0.10, synthetic_vol=0.75, synthetic_equity_loading=0.40),
    *[Instrument(ticker=t, name=n, asset_class="crypto", product_type="crypto_spot", r_level=5, areas=["altcoins"],
                 market="crypto_spot", aliases=[base, f"{base}/USDT"], expense_ratio=0.0, trading_cost_bps=10.0,
                 synthetic_mu=0.10, synthetic_vol=0.85, synthetic_equity_loading=0.40)
      for t, n, base in [("SOLUSDT", "Solana / Tether (Binance spot)", "SOL"),
                         ("BNBUSDT", "BNB / Tether (Binance spot)", "BNB"),
                         ("XRPUSDT", "XRP / Tether (Binance spot)", "XRP")]],
    # Gold-backed token: gold exposure (counts toward the gold & commodities cap), but held as a crypto
    # token on Binance, with issuer and custody risk on top, so it is rated and restricted like crypto.
    Instrument(ticker="PAXGUSDT", name="PAX Gold / Tether (Binance spot, gold-backed token)",
               asset_class="commodity", product_type="crypto_spot", r_level=5, areas=["gold"],
               market="crypto_spot", aliases=["PAXG", "PAXG/USDT"], expense_ratio=0.0, trading_cost_bps=10.0,
               synthetic_mu=0.04, synthetic_vol=0.16, synthetic_equity_loading=0.10, synthetic_rate_loading=0.20),
]


# Funds tracking (nearly) the same exposure. The first member in CATALOG order that can be priced goes
# to Agent A; the rest are fallbacks (see the module docstring). Single stocks are never grouped.
EXPOSURE_GROUPS: dict[str, list[str]] = {
    "us_treasury_bills": ["BIL", "SGOV", "SHV", "USFR"],
    "us_aggregate_bonds": ["AGG", "BND", "SCHZ"],
    "us_broad_equity": ["VTI", "VOO", "IVV", "SPY", "ITOT", "SCHB"],
    "developed_ex_us_equity": ["IEFA", "VEA", "EFA", "SCHF"],
    "emerging_markets_equity": ["VWO", "IEMG", "EEM", "SCHE"],
    "china_large_cap": ["MCHI", "FXI"],
    "us_small_caps": ["VB", "IWM", "IJR", "SCHA"],
    "us_high_dividend": ["SCHD", "VYM", "DVY"],
    "us_dividend_growth": ["VIG", "DGRO"],
    "us_technology": ["VGT", "XLK"],
    "us_healthcare": ["XLV", "VHT"],
    "us_financials": ["XLF", "VFH"],
    "us_energy": ["XLE", "VDE"],
    "us_consumer_discretionary": ["XLY", "VCR"],
    "us_consumer_staples": ["XLP", "VDC"],
    "us_industrials": ["XLI", "VIS"],
    "us_utilities": ["XLU", "VPU"],
    "us_communication": ["XLC", "VOX"],
    "us_materials": ["XLB", "VAW"],
    "us_reits": ["VNQ", "SCHH", "XLRE", "IYR"],
    "intermediate_treasuries": ["IEF", "VGIT", "GOVT", "IEI"],
    "long_treasuries": ["TLT", "VGLT", "SPTL"],
    "intermediate_corporate_bonds": ["LQD", "VCIT", "IGIB"],
    "tips": ["TIP", "SCHP"],
    "short_tips": ["VTIP", "STIP"],
    "international_bonds_hedged": ["BNDX", "IAGG"],
    "gold_bullion": ["IAU", "GLD", "GLDM", "SGOL"],
    "silver_bullion": ["SLV", "SIVR"],
    "broad_commodities": ["PDBC", "DBC", "GSG"],
    "bitcoin_funds": ["IBIT", "FBTC", "GBTC"],
}


def _with_exposure_groups(catalog: list[Instrument]) -> list[Instrument]:
    group_of = {t: g for g, members in EXPOSURE_GROUPS.items() for t in members}
    unknown = sorted(set(group_of) - {i.ticker for i in catalog})
    if unknown:
        raise ValueError(f"EXPOSURE_GROUPS lists tickers that are not in the catalog: {unknown}")
    return [i.model_copy(update={"exposure_group": group_of[i.ticker]}) if i.ticker in group_of else i
            for i in catalog]


DEFAULT_CATALOG = _with_exposure_groups(DEFAULT_CATALOG)


def normalize_interest_area(raw: str) -> InterestArea:
    """Map one chatbot string to a canonical InterestArea, or raise ValueError listing the options."""
    from typing import get_args

    key = raw.strip().lower().replace("&", " and ").replace("-", " ").replace("/", " ")
    key = "_".join(key.split())
    allowed = get_args(InterestArea)
    if key in allowed:
        return key  # type: ignore[return-value]
    if key in INTEREST_ALIASES:
        return INTEREST_ALIASES[key]
    raise ValueError(f"unknown interest area {raw!r}; allowed: {list(allowed)}")


def resolve_ticker(raw: str, catalog: list[Instrument]) -> str | None:
    """Catalog ticker for something the user typed ("btc", "BTC/USDT", "vti"), or None if unknown."""
    key = raw.strip().upper()
    for inst in catalog:
        if key == inst.ticker or key in inst.aliases:
            return inst.ticker
    return None


def region_of(inst: Instrument) -> str:
    """Where a product's exposure sits, for portfolio method v2 (`AssetInfo.region`): "US", "developed ex-US",
    "emerging" or "global". Deterministic, from the catalog's own interest areas and the name; an approximation (a
    total-international fund counts as developed ex-US)."""
    areas = set(inst.areas)
    if areas & {"emerging_markets", "china"}:
        return "emerging"
    if areas & {"international_stocks", "international_bonds"}:
        return "developed ex-US"
    if inst.asset_class in ("crypto", "commodity"):
        return "global"
    name = inst.name.lower()
    if "world" in name or "global" in name:
        return "global"
    return "US"


def catalog_by_ticker(catalog: list[Instrument]) -> dict[str, Instrument]:
    tickers = [i.ticker for i in catalog]
    if len(set(tickers)) != len(tickers):
        raise ValueError("catalog contains duplicate tickers")
    return {i.ticker: i for i in catalog}
