"""Data contracts for Agent D, the speculation bot. Module-specific, so they live here, not in egoquant/models.py.

Agent D is a REPORT SECTION ONLY: it never changes weights, limits or approvals, and nothing here is an
input to Agent A, Agent B or the Risk Engine. Only C4/C5 investors are eligible. Ideas are picked by
deterministic code from allowed instruments in the news; an LLM may only rewrite the thesis text.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Direction = Literal["watch_up", "watch_down"]
Confidence = Literal["low", "medium"]                       # never "high": these are speculative by definition
ThesisKind = Literal["llm", "template"]
DataSource = Literal["live", "fixture", "mixed", "none"]    # "none" = no briefing/news available

MAX_IDEAS = 3
DISCLAIMER = (
    "Speculative ideas to watch, not recommendations. They are picked by simple rules from recent news and "
    "price momentum, can be wrong, and do not change your portfolio. Only invest money you can afford to lose."
)


class IdeaSource(BaseModel):
    """One news item behind an idea (from the market briefing)."""

    title: str
    url: str
    source_name: str
    published_at: datetime


class IdeaSignal(BaseModel):
    """The deterministic inputs behind an idea. Weights live in config and are PROVISIONAL."""

    score: float                                 # combined score used for ranking
    tone_change: float | None = None             # None = tone unavailable; the score then uses momentum + volume only
    momentum_3m: float | None = None             # 3-month price return, e.g. 0.12 = +12%
    news_volume: int = Field(0, ge=0)            # matched news items in the window
    tone_available: bool
    basis: str                                   # plain words, e.g. "momentum + news volume (tone unavailable)"


class SpeculativeIdea(BaseModel):
    ticker: str
    name: str
    r_level: int = Field(ge=1, le=5)
    direction: Direction
    thesis: str = Field(min_length=1)
    thesis_kind: ThesisKind
    signal: IdeaSignal
    facts: list[str] = Field(default_factory=list)          # numbers stated plainly, from code, not from the LLM
    sources: list[IdeaSource] = Field(min_length=1)
    confidence: Confidence
    risk_warning: str = Field(min_length=1)
    already_held_weight: float = Field(0.0, ge=0, le=1)     # share of the proposed portfolio already in this ticker


class ExcludedInstrument(BaseModel):
    ticker: str
    reason: str                                  # e.g. "crypto blocked by your complex-products answer"


class SpeculationReport(BaseModel):
    eligible: bool                               # False for C1-C3, or when the no-loss gate applies
    ideas: list[SpeculativeIdea] = Field(default_factory=list, max_length=MAX_IDEAS)
    excluded: list[ExcludedInstrument] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER
    data_source: DataSource
    as_of: datetime
    input_hash: str                              # hash of the inputs, so a report can be reproduced
    version: str

    @model_validator(mode="after")
    def _no_ideas_when_ineligible(self) -> "SpeculationReport":
        if not self.eligible and self.ideas:
            raise ValueError("an ineligible investor must get no speculative ideas")
        if not self.disclaimer.strip():
            raise ValueError("the disclaimer is always shown and cannot be empty")
        return self
