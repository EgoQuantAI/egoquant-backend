"""Source reliability tiers (config `sources`, PROVISIONAL). Deterministic, by publisher name and headline pattern.

tier1 established (weight 1.0) · tier2 commentary (0.7) · unrated (0.4) · tier3 automated / low quality: listed
("low-reliability sources, not used for tone") but never counted for tone or alerts, and never sent to the LLM.
Forums keep their source-type weight (0.4) unless they are tier3. A tier3 headline pattern wins over any source.
"""

from __future__ import annotations

import re
from functools import lru_cache

from egoquant.briefing.loader import BriefingConfig
from egoquant.briefing.models import NewsItem

SINGLE_TIER_WORDS = {"tier1": "established sources",
                     "tier2": "opinion sites, so read them as opinions",
                     "unrated": "sites we haven't reviewed yet, so treat them with care"}


@lru_cache(maxsize=8)
def _compiled(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


def tier_of(item: NewsItem, config: BriefingConfig) -> tuple[str, str]:
    """(tier, one-line reason)."""
    cfg = config.sources
    for p in _compiled(tuple(cfg.tier3_title_patterns)):
        if p.search(item.title):
            return "tier3", "automated or template page (headline pattern)"
    for tier in ("tier3", "tier1", "tier2"):
        reason = cfg.tiers.get(tier, {}).get(item.source_name)
        if reason is not None:
            return tier, reason
    if item.source_type == "forum":
        return "unrated", "forum post"
    return "unrated", "not reviewed yet"


def tier_weight(tier: str, item: NewsItem, config: BriefingConfig) -> float:
    """Tone weight for one item: 0 for tier3; forums keep their source-type weight; otherwise the tier weight."""
    if tier == "tier3":
        return 0.0
    if item.source_type == "forum":
        return config.tone.source_weights["forum"]
    return config.sources.tier_weights.get(tier, config.sources.tier_weights["unrated"])
