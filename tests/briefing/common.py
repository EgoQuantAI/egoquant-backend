"""Shared helpers for the briefing tests. Everything offline: fake scorer, fake LLM, no network."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone

from egoquant.agents.market_intel import InvestorInterests
from egoquant.briefing.advice_filter import AdviceFilter
from egoquant.briefing.build import build_briefing
from egoquant.briefing.fetch import item_id
from egoquant.briefing.loader import BriefingConfig, load_config
from egoquant.briefing.models import LLMSummary, NewsItem
from egoquant.briefing.summarize import Summarizer
from egoquant.briefing.tone import CachedToneScorer, make_score
from egoquant.models import PortfolioProposal

AS_OF = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def config(**llm) -> BriefingConfig:
    """The real config with the LLM switched off (tests never call OpenAI) unless overridden."""
    cfg = load_config().model_copy(deep=True)
    cfg.llm.enabled = False
    for k, v in llm.items():
        setattr(cfg.llm, k, v)
    return cfg


class FakeScorer:
    """Deterministic stand-in for FinBERT: keyword rules, no model."""

    model_name = "fake-finbert"
    NEG = re.compile(r"\b(fall|falls|fell|slump|weak|decline|losses|outflows|cut|cuts)\b", re.I)
    POS = re.compile(r"\b(gain|gains|rise|rises|strong|record|solid|inflows)\b", re.I)

    def __init__(self):
        self.calls = 0

    def score(self, texts):
        self.calls += 1
        out = []
        for t in texts:
            if self.NEG.search(t):
                out.append(make_score(0.05, 0.85, 0.10, self.model_name))
            elif self.POS.search(t):
                out.append(make_score(0.85, 0.05, 0.10, self.model_name))
            else:
                out.append(make_score(0.10, 0.10, 0.80, self.model_name))
        return out


def scorer() -> CachedToneScorer:
    return CachedToneScorer(FakeScorer(), FakeScorer.model_name, neutral_gating=load_config().tone.neutral_gating)


class FakeBackend:
    """LLM stand-in. Returns `reply` (or raises it if it is an exception) and records every request."""

    def __init__(self, reply=None):
        self.reply = reply or LLMSummary(summary="A neutral summary of the article.", llm_tone=0.1)
        self.requests = []

    def complete(self, request, model):
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def summarizer(cfg: BriefingConfig | None = None, backend=None, **kw) -> Summarizer:
    cfg = cfg or config()
    return Summarizer(cfg, backend, AdviceFilter(cfg.forbidden_patterns), **kw)


def item(title: str, days_ago: float = 1.0, *, source_type: str = "outlet", source_name: str = "Test Wire",
         summary: str = "", url: str | None = None, data_source: str = "fixture") -> NewsItem:
    url = url or "https://test.example.com/" + hashlib.sha1(f"{title}|{days_ago}|{source_name}".encode()).hexdigest()[:12]
    return NewsItem(id=item_id(source_name, url), title=title, summary_text=summary, url=url,
                    published_at=AS_OF - timedelta(days=days_ago), source_name=source_name, source_type=source_type,
                    fetched_at=AS_OF, data_source=data_source)


def interests(*areas: str, max_r_level: int | None = 4, held: list[str] | None = None) -> InvestorInterests:
    return InvestorInterests(investor_id="inv-123", interest_areas=list(areas) or ["real_estate"],
                             max_r_level=max_r_level, held_assets=held or [])


def proposal(weights: dict[str, float]) -> PortfolioProposal:
    return PortfolioProposal(stage="constrained", method="test", as_of=date(2026, 10, 16), status="optimal",
                             weights=weights)


def build(items, *, areas=("real_estate",), weights=None, cfg=None, summ=None, sc=None, max_r_level=4, **kw):
    cfg = cfg or config()
    return build_briefing(items=items, as_of=AS_OF, interests=interests(*areas, max_r_level=max_r_level),
                          horizon_years=5, config=cfg, scorer=sc or scorer(), summarizer=summ or summarizer(cfg),
                          proposal=proposal(weights) if weights is not None else None, **kw)
