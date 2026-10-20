"""Agent D: the speculation bot. A report section only; never changes the portfolio.

    models.py   SpeculationReport / SpeculativeIdea contracts (consumed by egoquant.models.ClientReport)
"""

from __future__ import annotations

from egoquant.agents.speculation.models import (
    DISCLAIMER,
    MAX_IDEAS,
    ExcludedInstrument,
    IdeaSignal,
    IdeaSource,
    SpeculationReport,
    SpeculativeIdea,
)

__all__ = [
    "DISCLAIMER", "MAX_IDEAS", "ExcludedInstrument", "IdeaSignal", "IdeaSource", "SpeculationReport",
    "SpeculativeIdea",
]
