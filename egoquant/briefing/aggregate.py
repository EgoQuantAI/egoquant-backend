"""Deterministic aggregation: tone per instrument and area, exposure, alerts, suitability, diversity, cadence.

The per-headline, per-fund tone comes from fund_tone.py (fund-aware LLM label: +1 / 0 / -1) or FinBERT (fallback:
gated P(pos) - P(neg)). Everything here is deterministic code: weights, decay, coverage minimums, exposure from
Agent A's weights, R-levels from Agent C's catalog, thresholds from config.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from egoquant.agents.market_intel import Instrument
from egoquant.briefing.loader import BriefingConfig
from egoquant.briefing.models import (Aggregate, BriefingAlert, BriefingItem, CoverageStatus, DiversitySuggestion,
                                      SuitabilityNote)
from egoquant.briefing.sources import tier_weight


# --------------------------------------------------------------------------
# Tone aggregation
# --------------------------------------------------------------------------
def item_weight(it: BriefingItem, period_end: datetime, config: BriefingConfig) -> float:
    """Source-tier weight (tier1 1.0, tier2 0.7, unrated 0.4, forums 0.4, tier3 0) x the source type's own weight
    x exponential recency decay (half-life in days), measured from the period end."""
    age_days = max(0.0, (period_end - it.item.published_at).total_seconds() / 86_400)
    decay = 0.5 ** (age_days / config.tone.recency_half_life_days)
    type_w = 1.0 if it.item.source_type == "forum" else config.tone.source_weights[it.item.source_type]
    return tier_weight(it.source_tier, it.item, config) * type_w * decay


def countable(it: BriefingItem) -> bool:
    """Counts for tone windows: not a tier3 (low-reliability) source and a known date."""
    return it.source_tier != "tier3" and it.item.date_status != "unknown"


def item_value(it: BriefingItem, tickers: list[str] | None = None) -> float | None:
    """The item's tone for these funds: the mean of its per-fund tones for them (all its funds when None).
    Items built without per-fund tones (older callers) use their FinBERT tone."""
    if it.fund_tones:
        vals = [ft.value for ft in it.fund_tones if tickers is None or ft.ticker in tickers]
        return sum(vals) / len(vals) if vals else None
    return it.tone.tone if it.tone is not None else None


def relation_factor(it: BriefingItem, tickers: list[str] | None, config: BriefingConfig) -> float:
    """1.0 when the item is ABOUT one of these funds (or for areas); exposure.factor when it is only about what they
    hold (an exposure theme)."""
    if tickers is None or any(t in it.matched_tickers for t in tickers):
        return 1.0
    if any(t in it.exposure_of for t in tickers):
        return config.exposure.factor
    return 1.0


def tone_stats(items: list[BriefingItem], period_end: datetime, config: BriefingConfig,
               tickers: list[str] | None = None) -> tuple[float | None, float]:
    """(damped score, evidence). score = sum(w*s) / (sum(w) + k): thin evidence is pulled toward 0 instead of being
    hidden. None when no item has a rating. evidence = sum(w) (tier x recency x relation)."""
    scored = [(it, v) for it in items if (v := item_value(it, tickers)) is not None]
    if not scored:
        return None, 0.0
    weights = [item_weight(it, period_end, config) * relation_factor(it, tickers, config) for it, _ in scored]
    total = sum(weights)
    if total <= 0:
        return None, 0.0
    return sum(w * v for w, (_, v) in zip(weights, scored)) / (total + config.confidence.k), total


def weighted_tone(items: list[BriefingItem], period_end: datetime, config: BriefingConfig,
                  tickers: list[str] | None = None) -> float | None:
    """The damped score only (see tone_stats)."""
    return tone_stats(items, period_end, config, tickers)[0]


def confidence_of(evidence: float, config: BriefingConfig) -> str | None:
    if evidence <= 0:
        return None
    c = config.confidence
    return "high" if evidence >= c.high else "medium" if evidence >= c.medium else "low"


def comparable(confidence: str | None) -> bool:
    return confidence in ("medium", "high")


def tone_words(value: float, config: BriefingConfig) -> str:
    """Fixed, symmetric bands (config tone.display): mostly / slightly positive, mixed or neutral, slightly /
    mostly negative."""
    d = config.tone.display
    if value >= d.strong:
        return "mostly positive"
    if value >= d.slight:
        return "slightly positive"
    if value > -d.slight:
        return "mixed or neutral"
    if value > -d.strong:
        return "slightly negative"
    return "mostly negative"


def describe(value: float, n: int, config: BriefingConfig) -> str:
    """'mostly negative (-0.40, 5 headlines)'."""
    return f"{tone_words(value, config)} ({_fmt(value)}, {n} headline{'s' if n != 1 else ''})"


def _in(it: BriefingItem, start: datetime, end: datetime) -> bool:
    return start < it.item.published_at <= end


def _fmt(t: float) -> str:
    return f"{t:+.2f}"


def label(subject: str) -> str:
    """Readable name: interest areas use underscores in code ("real_estate" -> "real estate")."""
    return subject.replace("_", " ")


def _noun(words: str) -> str:
    """'this week' -> 'week'."""
    return words.split()[-1] if words else "period"


def aggregate(subject: str, kind: str, items: list[BriefingItem], as_of: datetime, exposure: float | None,
              config: BriefingConfig, tickers: list[str] | None = None,
              this: str = "this period", last: str = "last period", display_name: str | None = None) -> Aggregate:
    """tickers: the funds whose tones count for this subject (the instrument itself, or an area's funds).

    Final states only: a damped score with a confidence label; "quiet" (no news about the fund or what it holds);
    "unrated" (news found, no rating available). A change is computed only when BOTH periods have medium or high
    confidence; otherwise compare_note says why."""
    p = timedelta(days=config.window.period_days)
    usable = [it for it in items if countable(it)]
    cur = [it for it in usable if _in(it, as_of - p, as_of)]
    prev = [it for it in usable if _in(it, as_of - 2 * p, as_of - p)]
    tone, ev = tone_stats(cur, as_of, config, tickers)
    tone_prev, ev_prev = tone_stats(prev, as_of - p, config, tickers)
    conf, conf_prev = confidence_of(ev, config), confidence_of(ev_prev, config)
    name = display_name or label(subject)
    n_cur = sum(item_value(it, tickers) is not None for it in cur)
    n_prev = sum(item_value(it, tickers) is not None for it in prev)
    waiting = len(cur) - n_cur
    change, note = None, None
    if tone is not None and tone_prev is not None and comparable(conf) and comparable(conf_prev):
        change = tone - tone_prev
    elif tone is not None:
        note = (f"too little news {last} to compare" if not comparable(conf_prev)
                else f"too little news {this} to compare")
    if tone is not None:
        status = CoverageStatus(state="tone", n=n_cur, needed=0, n_waiting=waiting)
        text = f"{name}: {describe(tone, n_cur, config)}, {conf} confidence {this}"
        text += (f"; {last}: {describe(tone_prev, n_prev, config)}, change {_fmt(change)}" if change is not None
                 else f"; {note}")
    elif cur:
        status = CoverageStatus(state="unrated", n=0, needed=0, n_waiting=waiting)
        text = f"{name}: {waiting} headline{'s' if waiting != 1 else ''} {this}, not rated (no rating available)"
    else:
        status = CoverageStatus(state="quiet", n=0, needed=0, n_waiting=0)
        text = f"{name}: quiet {_noun(this)}, no news about {name} or what it holds"
    ids = sorted(it.item.id for it in cur + prev if item_value(it, tickers) is not None)
    vals = [item_value(it, tickers) for it in cur]
    counts = dict(n_positive=sum(v is not None and v > 1e-9 for v in vals),
                  n_negative=sum(v is not None and v < -1e-9 for v in vals),
                  n_neutral=sum(v is not None and abs(v) <= 1e-9 for v in vals),
                  n_unrated=sum(v is None for v in vals))
    n_exp = sum(1 for it in cur if tickers is not None and not any(t in it.matched_tickers for t in tickers)
                and any(t in it.exposure_of for t in tickers))
    return Aggregate(subject=subject, kind=kind, n_items=len(cur), tone=tone, n_items_previous=len(prev),
                     tone_previous=tone_prev, change=change, exposure=exposure, text=text, item_ids=ids,
                     coverage_status=status, confidence=conf, confidence_previous=conf_prev,
                     evidence=round(ev, 3), evidence_previous=round(ev_prev, 3), compare_note=note,
                     n_exposure=n_exp, **counts)


# --------------------------------------------------------------------------
# Exposure (Agent A's final weights; never the LLM)
# --------------------------------------------------------------------------
def held(weights: dict[str, float] | None, tickers: list[str]) -> list[str]:
    return [t for t in tickers if weights and weights.get(t, 0.0) > 1e-9]


def exposure_of(weights: dict[str, float] | None, tickers: list[str]) -> float | None:
    if weights is None:
        return None
    return sum(weights[t] for t in held(weights, tickers))


def exposure_text(weights: dict[str, float] | None, tickers: list[str]) -> str:
    if weights is None:
        return ""
    h = held(weights, tickers)
    if not h:
        return "On your watchlist; not in your portfolio."
    return f"This concerns {share_text(exposure_of(weights, tickers))} of your portfolio ({', '.join(h)})."


def share_text(share: float | None) -> str:
    """'22%', or 'under 1%' for a holding below 1% (never '0%' for something that is held)."""
    if share is None:
        return ""
    return "under 1%" if 0 < share < 0.01 else f"{share:.0%}"


def area_tickers(area: str, catalog: list[Instrument]) -> list[str]:
    return [i.ticker for i in catalog if area in i.areas]


# --------------------------------------------------------------------------
# Alerts: the only action is offering a risk-profile re-check
# --------------------------------------------------------------------------
def _fires(a: Aggregate, config: BriefingConfig) -> bool:
    # A HOLDING only (interests never alert); a change exists only with medium/high confidence in both periods.
    return (a.kind == "instrument" and a.change is not None and a.exposure is not None
            and comparable(a.confidence) and comparable(a.confidence_previous)
            and abs(a.change) >= config.alerts.tone_change_threshold and a.exposure > config.alerts.min_exposure)


RECHECK_OFFER = "Would you like to re-check your risk profile? Nothing changes unless you confirm."


def alert_summary(a: Aggregate, config: BriefingConfig, this: str = "this period", last: str = "last period") -> str:
    """'News tone about VNQ moved from mostly positive (+0.82) last week to mostly negative (-1.00) this week
    (change -1.82, 7 headlines). It concerns 7% of your portfolio. Tone is an assessment of the headlines, not a
    forecast of prices.' Every number comes from the aggregate."""
    return (f"News tone about {label(a.subject)} moved from {tone_words(a.tone_previous, config)} "
            f"({_fmt(a.tone_previous)}) {last} to {tone_words(a.tone, config)} ({_fmt(a.tone)}) {this} "
            f"(change {_fmt(a.change)}, {a.n_items} headlines). It concerns {a.exposure:.0%} of your portfolio. "
            "Tone is an assessment of the headlines, not a forecast of prices.")


def alerts_for(aggregates: list[Aggregate], config: BriefingConfig, weights: dict[str, float] | None = None,
               catalog: list[Instrument] | None = None, this: str = "this period",
               last: str = "last period") -> list[BriefingAlert]:
    """Alerts for HOLDINGS only (interest areas give context, never alerts), each needing medium or high
    confidence in both periods (see _fires). A move of -threshold or more downward carries the re-check offer; an
    equal move upward is reported without it."""
    out = []
    fired = [a for a in aggregates if a.kind == "instrument" and _fires(a, config)]
    alerted = {a.subject for a in fired}
    alerted_items = {tuple(a.item_ids) for a in fired}
    for a in aggregates:
        if not _fires(a, config):
            continue
        if a.kind == "area":
            if tuple(a.item_ids) in alerted_items:
                continue
            if catalog is not None:
                held_in_area = held(weights, area_tickers(a.subject, catalog))
                if held_in_area and set(held_in_area) <= alerted:
                    continue
        summary = alert_summary(a, config, this, last)
        offer = a.change <= -config.alerts.tone_change_threshold        # negative moves only get the offer
        out.append(BriefingAlert(subject=a.subject, kind=a.kind, change=a.change, exposure=a.exposure,
                                 tone=a.tone, tone_previous=a.tone_previous, n_items=a.n_items, summary=summary,
                                 message=f"{summary} {RECHECK_OFFER}" if offer else summary,
                                 action="offer_risk_profile_recheck" if offer else None))
    return out


# --------------------------------------------------------------------------
# Suitability notes (Agent C's R-levels)
# --------------------------------------------------------------------------
def suitability_notes(areas: list[str], max_r_level: int | None, catalog: list[Instrument],
                      config: BriefingConfig) -> list[SuitabilityNote]:
    if max_r_level is None:
        return []
    out = []
    for area in areas:
        above = [i.ticker for i in catalog if area in i.areas and i.r_level > max_r_level]
        if above:
            out.append(SuitabilityNote(area=area, max_r_level=max_r_level, tickers_above=above,
                                       text=config.suitability["note"].format(area=area.replace("_", " "),
                                                                             max_r=max_r_level)))
    return out


# --------------------------------------------------------------------------
# Source diversity: suggestions ONLY from the curated config list
# --------------------------------------------------------------------------
def diversity_suggestions(items: list[BriefingItem], area_aggs: list[Aggregate],
                          config: BriefingConfig) -> list[DiversitySuggestion]:
    cfg = config.diversity
    tone_of = {a.subject: a.tone for a in area_aggs}
    out: list[DiversitySuggestion] = []
    areas = sorted({a for it in items for a in it.matched_areas})
    for area in areas:
        group = [it for it in items if area in it.matched_areas]
        if len(group) < cfg.min_items:
            continue
        counts = Counter(it.item.source_type for it in group)
        top_type, top_n = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        share = top_n / len(group)
        tone = tone_of.get(area)
        reason = None
        if share > cfg.max_share_one_type:
            reason = f"{share:.0%} of the items about {area.replace('_', ' ')} come from {top_type} sources"
        elif tone is not None and abs(tone) >= cfg.one_sided_tone and share >= 0.5:
            reason = (f"the tone about {area.replace('_', ' ')} is one-sided ({_fmt(tone)}) and mostly from "
                      f"{top_type} sources")
        if reason:
            for src in cfg.curated_sources:
                if src.type != top_type:
                    out.append(DiversitySuggestion(area=area, reason=reason, name=src.name, url=src.url,
                                                   source_type=src.type))
    return out


# --------------------------------------------------------------------------
# Cadence
# --------------------------------------------------------------------------
def cadence(horizon_years: float, config: BriefingConfig) -> str:
    for rule in config.cadence_rules:
        if horizon_years >= rule.min_horizon_years:
            extra = ", with alerts between briefings" if config.cadence.alerts_between_briefings else ""
            return f"{rule.frequency} briefing{extra}"
    return ""
