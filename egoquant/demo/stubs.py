"""Offline stand-ins for Agent C and Agent B, for unit tests and demos of Agent A on its own.

EVERYTHING here is SYNTHETIC. The data comes from egoquant/demo/synthetic_market.py and its
parameters are invented for testing. The real agents implement the same methods (see the Protocols
in egoquant/models.py): MarketIntelligenceAgent (Agent C) and BehavioralTwinAgent (Agent B).
"""

from __future__ import annotations

from datetime import date

from egoquant.demo.synthetic_market import make_synthetic_history, synthetic_asset_infos
from egoquant.models import Constraint, MarketSnapshot, PortfolioMenu
from egoquant.scenarios import CAPACITY_SCENARIO


class SyntheticMarketAgent:
    """Fake Agent C: a seeded synthetic history that ends exactly at `as_of` (calm regime)."""

    def __init__(self, seed: int = 7):
        self.seed = seed

    def get_market_snapshot(self, asset_ids: list[str] | None, as_of: date,
                            lookback_days: int) -> MarketSnapshot:
        prices = make_synthetic_history(self.seed, lookback_days + 1, as_of, asset_ids)
        return MarketSnapshot(as_of=as_of, prices=prices, assets=synthetic_asset_infos(asset_ids),
                              risk_free_rate_annual=0.03, data_source="synthetic")


class StubBehavioralAgent:
    """Fake Agent B: returns fixed numeric limits. It does NOT analyze real trading history."""

    def __init__(self, max_equity: float = 0.50, confidence: float = 0.80,
                 max_stress_loss: float | None = None, scenario: str = CAPACITY_SCENARIO):
        self.max_equity = max_equity
        self.confidence = confidence
        self.max_stress_loss = max_stress_loss
        self.scenario = scenario

    def evaluate_menu(self, investor_id: str, menu: PortfolioMenu) -> list[Constraint]:
        constraints = [Constraint(type="max_equity", value=self.max_equity, source="behavioral",
                                  confidence=self.confidence, evidence_ref=f"stub-evidence:{investor_id}")]
        if self.max_stress_loss is not None:
            constraints.append(Constraint(type="max_stress_loss", value=self.max_stress_loss,
                                          source="behavioral", confidence=self.confidence,
                                          scenario=self.scenario, evidence_ref=f"stub-evidence:{investor_id}"))
        return constraints
