"""Agent B: the Behavioral Twin. See README.md in this folder.

    agent.py    BehavioralTwinAgent: trading logs + risk game + Agent A's menu -> numeric limits
    core.py     pure behavioral math (returns, drawdowns, panic episodes, disposition, chasing)
    models.py   Agent B's own data contracts (TradingHistory, GameResult, BehaviorConfig, report)
"""

from __future__ import annotations

from egoquant.agents.behavioral.agent import BehavioralTwinAgent
from egoquant.agents.behavioral.models import (
    BehavioralData,
    BehavioralDataProvider,
    BehavioralReport,
    BehaviorConfig,
    GameResult,
    LossAcceptance,
    ScreeningResult,
    TradingHistory,
)
from egoquant.models import Constraint, PortfolioMenu


class NoBehavioralEvidence:
    """Agent B for an investor with no trading history and no risk game yet: no limits.

    The pipeline uses it when no behavioral agent is given. "No evidence, no constraint."
    """

    def evaluate_menu(self, investor_id: str, menu: PortfolioMenu) -> list[Constraint]:
        return []


class NoBehavioralData:
    """Data provider with nothing on file. BehavioralTwinAgent(NoBehavioralData()) still screens goal-path
    candidates with the comfort band from the questionnaire class (the prior), but adds no limits."""

    def get_behavioral_data(self, investor_id: str) -> BehavioralData | None:
        return None


__all__ = [
    "BehavioralData", "BehavioralDataProvider", "BehavioralReport", "BehavioralTwinAgent", "BehaviorConfig",
    "GameResult", "LossAcceptance", "NoBehavioralData", "NoBehavioralEvidence", "ScreeningResult", "TradingHistory",
]
