"""Trading platforms: where the investor trades decides what they can buy and where Agent C gets prices.

The chatbot asks "where do you trade?" and passes the answer as InvestorInterests.trading_platforms.

    Binance                -> crypto spot pairs; prices come from Binance's own public market-data API
    a US-market broker     -> US-listed ETFs; prices come from an end-of-day vendor (Tiingo or CSV),
                              because brokers only give price data to logged-in account holders and a
                              US-listed ETF has one exchange price whichever broker you use.

Adding a platform = one entry in PLATFORMS (+ aliases). Adding a new market (e.g. Hong Kong or
A-shares for Futu or Eastmoney users) also needs instruments in catalog.py and a data source in
sources.py; that is not built yet.
"""

from __future__ import annotations

from pydantic import BaseModel

from egoquant.agents.market_intel.catalog import Market


class Platform(BaseModel):
    key: str
    name: str
    kind: str                              # "crypto_exchange" | "stock_broker"
    markets: list[Market]                  # what can be bought there (within Agent C's catalog)
    price_source_note: str                 # shown to the user / judges: where the prices come from


_US_NOTE = "US-listed prices from an end-of-day market data vendor (Tiingo), same for every US broker"

PLATFORMS: dict[str, Platform] = {
    "binance": Platform(key="binance", name="Binance", kind="crypto_exchange", markets=["crypto_spot"],
                        price_source_note="Binance's own public market-data API (data-api.binance.vision)"),
    "interactive_brokers": Platform(key="interactive_brokers", name="Interactive Brokers", kind="stock_broker",
                                    markets=["us_listed"], price_source_note=_US_NOTE),
    "charles_schwab": Platform(key="charles_schwab", name="Charles Schwab", kind="stock_broker",
                               markets=["us_listed"], price_source_note=_US_NOTE),
    "fidelity": Platform(key="fidelity", name="Fidelity", kind="stock_broker", markets=["us_listed"],
                         price_source_note=_US_NOTE),
    "robinhood": Platform(key="robinhood", name="Robinhood", kind="stock_broker", markets=["us_listed"],
                          price_source_note=_US_NOTE),
    "futu_moomoo": Platform(key="futu_moomoo", name="Futu / moomoo (US market)", kind="stock_broker",
                            markets=["us_listed"], price_source_note=_US_NOTE),
    "tiger_brokers": Platform(key="tiger_brokers", name="Tiger Brokers (US market)", kind="stock_broker",
                              markets=["us_listed"], price_source_note=_US_NOTE),
    "other_us_broker": Platform(key="other_us_broker", name="Another broker with US market access",
                                kind="stock_broker", markets=["us_listed"], price_source_note=_US_NOTE),
}

PLATFORM_ALIASES: dict[str, str] = {
    "binance_com": "binance", "binance_exchange": "binance",
    "ibkr": "interactive_brokers", "interactive_broker": "interactive_brokers", "ib": "interactive_brokers",
    "schwab": "charles_schwab", "td_ameritrade": "charles_schwab",
    "fidelity_investments": "fidelity",
    "futu": "futu_moomoo", "moomoo": "futu_moomoo", "futubull": "futu_moomoo", "futu_niuniu": "futu_moomoo",
    "tiger": "tiger_brokers", "tiger_trade": "tiger_brokers",
    "other": "other_us_broker", "us_broker": "other_us_broker",
}


def normalize_platform(raw: str) -> str:
    """Map one chatbot string to a PLATFORMS key, or raise ValueError listing the supported ones."""
    key = raw.strip().lower().replace("&", " and ").replace("-", " ").replace("/", " ").replace(".", " ")
    key = "_".join(key.split())
    if key in PLATFORMS:
        return key
    if key in PLATFORM_ALIASES:
        return PLATFORM_ALIASES[key]
    raise ValueError(f"unsupported trading platform; supported: {', '.join(sorted(PLATFORMS))}")     # the typed text is not repeated
