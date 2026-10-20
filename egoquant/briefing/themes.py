"""Exposure themes: what each fund HOLDS (US T-bills & Fed policy, US stock market, Gold, ...) and region signals.

Themes come from Agent C's catalog fields (asset class, areas, sector, duration, exposure group) through config
`theme_rules`, with `theme_overrides` for the common funds; single stocks have none (company news suffices). A
headline belongs to a theme when it matches one of the theme's phrases; for US themes it must not name a foreign
government, currency, central bank or bill code unless it also carries a US signal (config `region_signals`).
Deterministic code; no LLM.
"""

from __future__ import annotations

import re
from functools import lru_cache

from egoquant.agents.market_intel import DEFAULT_CATALOG, Instrument
from egoquant.briefing.loader import BriefingConfig


# --------------------------------------------------------------------------
# Region signals
# --------------------------------------------------------------------------
class RegionSignals:
    def __init__(self, config: BriefingConfig):
        rs = config.region_signals
        self.us = [re.compile(p) for p in rs.us]
        self.foreign = [re.compile(p) for p in rs.foreign]
        self.crypto = [re.compile(p) for p in rs.crypto]
        self.us_areas = set(rs.us_region_areas)

    def has_crypto(self, text: str) -> bool:
        return any(p.search(text) for p in self.crypto)

    def has_us(self, text: str) -> bool:
        return any(p.search(text) for p in self.us)

    def has_foreign(self, text: str) -> bool:
        return any(p.search(text) for p in self.foreign)

    def foreign_only(self, text: str) -> bool:
        """Names a foreign government/currency/central bank/bill code and carries no US signal."""
        return self.has_foreign(text) and not self.has_us(text)

    def is_us_fund(self, inst: Instrument) -> bool:
        """US-region funds: US Treasury/T-bill, US bond, US stock and US sector funds (not international/EM/crypto)."""
        if inst.product_type == "stock" or inst.market != "us_listed":
            return False
        if set(inst.areas) & {"international_stocks", "emerging_markets", "china", "international_bonds"}:
            return False
        if inst.asset_class in ("commodity", "crypto"):
            return False
        return bool(set(inst.areas) & self.us_areas) or inst.sector is not None


# --------------------------------------------------------------------------
# Fund -> themes
# --------------------------------------------------------------------------
def _rule_matches(when: dict, inst: Instrument) -> bool:
    for key, want in when.items():
        if key == "duration_max":
            if inst.duration_years is None or inst.duration_years > float(want):
                return False
        elif key == "duration_min":
            if inst.duration_years is None or inst.duration_years <= float(want):
                return False
        elif key == "areas":
            if not set(inst.areas) & set(want):
                return False
        else:
            if getattr(inst, key, None) not in want:
                return False
    return True


def fund_themes(inst: Instrument, config: BriefingConfig) -> list[str]:
    """Theme ids for one catalog instrument, in priority order (the first is its primary theme)."""
    if inst.ticker in config.theme_overrides:
        return list(config.theme_overrides[inst.ticker])
    out: list[str] = []
    for rule in config.theme_rules:
        if _rule_matches(rule.when, inst):
            out += rule.themes
            if rule.stop:
                break
    return list(dict.fromkeys(out))


def theme_map(config: BriefingConfig, catalog: list[Instrument] | None = None) -> dict[str, list[str]]:
    return {i.ticker: fund_themes(i, config) for i in (catalog or DEFAULT_CATALOG)}


# --------------------------------------------------------------------------
# Headline -> themes
# --------------------------------------------------------------------------
class ThemeMatcher:
    def __init__(self, config: BriefingConfig):
        self.config = config
        self.region = RegionSignals(config)
        self.patterns = {tid: [re.compile(p, re.IGNORECASE) for p in t.match]
                         for tid, t in config.exposure_themes.items()}
        # "Fed" must be the capitalised word (not "fed up"); the match list is case-insensitive otherwise
        self._fed = re.compile(r"\bFed\b")

    def themes_for_text(self, text: str) -> set[str]:
        out: set[str] = set()
        foreign_only = self.region.foreign_only(text)
        crypto = self.region.has_crypto(text)
        for tid, pats in self.patterns.items():
            if crypto and self.config.exposure_themes[tid].not_with_crypto:
                continue
            hit = False
            for p in pats:
                if p.pattern == r"\bFed\b":
                    hit = bool(self._fed.search(text))
                else:
                    hit = bool(p.search(text))
                if hit:
                    break
            if not hit:
                continue
            if self.config.exposure_themes[tid].region == "us" and foreign_only:
                continue
            out.add(tid)
        return out


def theme_label(theme: str, config: BriefingConfig) -> str:
    t = config.exposure_themes.get(theme)
    return t.label if t else theme.replace("_", " ")


__all__ = ["RegionSignals", "ThemeMatcher", "fund_themes", "theme_label", "theme_map"]
