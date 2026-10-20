"""Agent C: Market Intelligence. Turns the investor's interest areas into market data for Agent A.

Typical use (the orchestrator does this):

    interests = InvestorInterests(investor_id="u1", interest_areas=["stocks", "Real estate", "crypto"],
                                  trading_platforms=["Interactive Brokers", "Binance"], max_r_level=4)
    agent_c = MarketIntelligenceAgent(interests)                  # REAL data: Tiingo + Binance
    agent_c.fetch_universe(as_of, lookback_days)                  # most relevant first, within the data budget
    agent_a = QuantAgent(agent_c, behavioral_agent, AgentConfig())
    result = agent_a.run(investor_context, as_of)                 # Agent A calls agent_c.get_market_snapshot
    agent_c.selection, agent_c.last_report                        # reasons, exclusions, sources, alerts
    agent_c.get_watchlist_snapshot(as_of)                         # prices of interests the investor may not hold
"""

from egoquant.agents.market_intel.agent import (
    AGENT_A_ASSET_CLASSES,
    CryptoMarketEntry,
    CryptoMarketReport,
    DEFAULT_FETCH_BUDGET,
    FetchReport,
    InvestorInterests,
    MarketDataUnavailable,
    MarketIntelligenceAgent,
    SnapshotReport,
    UniverseSelection,
    select_universe,
)
from egoquant.agents.market_intel.catalog import DEFAULT_CATALOG, Instrument, normalize_interest_area
from egoquant.agents.market_intel.platforms import PLATFORMS, normalize_platform
from egoquant.agents.market_intel.snapshot import SnapshotPriceSource, build_snapshot, load_snapshot
from egoquant.agents.market_intel.sources import (
    BinanceMarket,
    BinancePriceSource,
    CachedPriceSource,
    CsvPriceSource,
    PriceSource,
    PriceSourceError,
    RateLimitError,
    RoutedPriceSource,
    SyntheticPriceSource,
    TiingoPriceSource,
    build_live_source,
)

__all__ = [
    "AGENT_A_ASSET_CLASSES", "BinanceMarket", "BinancePriceSource", "CryptoMarketEntry", "CryptoMarketReport", "CachedPriceSource", "DEFAULT_FETCH_BUDGET", "FetchReport", "PLATFORMS", "RoutedPriceSource", "build_live_source",
    "normalize_platform", "CsvPriceSource", "DEFAULT_CATALOG", "Instrument", "InvestorInterests",
    "MarketDataUnavailable", "MarketIntelligenceAgent", "PriceSource", "PriceSourceError", "RateLimitError",
    "SnapshotPriceSource", "SnapshotReport", "SyntheticPriceSource", "build_snapshot", "load_snapshot", "TiingoPriceSource", "UniverseSelection",
    "normalize_interest_area", "select_universe",
]
