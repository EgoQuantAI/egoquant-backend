"""Market intelligence briefing: news about the investor's instruments, shown ALONGSIDE the portfolio.

It never changes Agent A's inputs or weights, Agent B's limits or the Risk Engine's decision. Its only
action is an alert that OFFERS a risk-profile re-check. Exposure, counts, dates and aggregates come from
deterministic code. The LLM writes neutral summary sentences and, as the primary tone, labels each headline
positive / negative / neutral for holders of one fund (fixed evaluated prompt, cached; FinBERT fallback). See
README.md.
"""

from egoquant.briefing.build import build_briefing
from egoquant.briefing.loader import BriefingConfig, load_config
from egoquant.briefing.models import DEMO_BANNER, LABELS, Briefing, BriefingAlert, BriefingItem, NewsItem

__all__ = ["DEMO_BANNER", "LABELS", "Briefing", "BriefingAlert", "BriefingConfig", "BriefingItem", "NewsItem",
           "build_briefing", "load_config"]
