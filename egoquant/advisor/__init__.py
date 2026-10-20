"""Conversational advisor: grounds the chat LLM in the engine's own results.

The LLM only explains. Every number it may quote comes from `facts` built here out of the deterministic
engine (questionnaire scoring, agents A/B/C, Risk Engine, goal simulation, market briefing).
"""
from egoquant.advisor.context import (
    ContextError,
    build_portfolio_facts,
    build_profile_facts,
    open_context,
    seal_context,
    suggested_questions,
)
from egoquant.advisor.guard import ReplyGuard

__all__ = [
    "ContextError",
    "ReplyGuard",
    "build_portfolio_facts",
    "build_profile_facts",
    "open_context",
    "seal_context",
    "suggested_questions",
]
