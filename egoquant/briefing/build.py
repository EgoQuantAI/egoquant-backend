"""build_briefing: items -> matched, scored, summarized, aggregated Briefing. Deterministic, no hidden clock.

Inputs are only READ. The investor's interests, Agent C's selection and Agent A's proposal are never
modified, and nothing produced here is fed back into Agent A, Agent B or the Risk Engine.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from egoquant.agents.market_intel import DEFAULT_CATALOG, Instrument, InvestorInterests, UniverseSelection
from egoquant.briefing import aggregate as agg
from egoquant.briefing.fetch import Prepared, prepare
from egoquant.briefing.fund_tone import FundToneLabeler
from egoquant.briefing.loader import BriefingConfig, with_period
from egoquant.briefing.matching import Matcher, about_one_product, named_funds
from egoquant.briefing.models import (AI_SUMMARIES_LABEL, LABELS, NO_AI_SUMMARIES_LABEL, Aggregate, AreaInstrument,
                                      Briefing, BriefingItem, BriefingMetadata, FundTone, HoldingGroup, LargestMove,
                                      NewsItem, TierLegend, ToneScore)
from egoquant.briefing.sources import SINGLE_TIER_WORDS, tier_of
from egoquant.briefing.themes import RegionSignals, ThemeMatcher, fund_themes, theme_label
from egoquant.briefing.summarize import Summarizer
from egoquant.briefing.tone import CachedToneScorer, top_class
from egoquant.models import PortfolioProposal


def select_tickers(interests: InvestorInterests, selection: UniverseSelection | None,
                   weights: dict[str, float] | None, catalog: list[Instrument],
                   max_tickers: int | None = None, small_position: float | None = None) -> tuple[list[str], list[str]]:
    """(the tickers this briefing covers in priority order, the tickers left out by the cap).

      1. funds with weight > 0 in the final proposal, largest weight first;
      2. other held assets (InvestorInterests.held_assets), then holdings below 2% of the portfolio: lower
         priority than the larger holdings, but every holding stays inside the cap (a held fund is never "quiet"
         only because the cap left it out);
      3. the investor's interest areas (including Agent C's watchlist), filled ROUND-ROBIN: one instrument per
         area in the investor's order, then a second per area, and so on. Within an area: funds before single
         stocks, catalog order. So every area with at least one instrument gets a slot when the cap allows, and
         a few dozen tech stocks cannot crowd out "gold". Watchlist tickers outside the investor's areas form one
         last bucket.

    The rest of Agent C's universe (e.g. core funds the investor neither holds nor asked about) is NOT
    included, so the briefing scales with the portfolio and the investor's interests, not the catalog.
    """
    known = {i.ticker for i in catalog}
    held = [t for t, w in sorted((weights or {}).items(), key=lambda kv: (-kv[1], kv[0])) if w > 1e-9]
    small_cut = 0.02 if small_position is None else small_position
    big = [t for t in held if (weights or {}).get(t, 0) >= small_cut]
    small = [t for t in held if t not in big]          # small positions: after the larger holdings, before interests
    first = [t for t in dict.fromkeys([*big, *interests.held_assets, *small]) if t in known]
    watch = set(selection.watchlist_tickers) if selection is not None else set()
    areas = list(dict.fromkeys(interests.interest_areas))
    buckets: dict[str, list[str]] = {a: [] for a in areas}
    extra: list[str] = []
    ordered_catalog = ([i for i in catalog if i.product_type != "stock"]
                       + [i for i in catalog if i.product_type == "stock"])      # funds first, catalog order
    for inst in ordered_catalog:
        mine = [a for a in areas if a in inst.areas]
        for a in mine:
            buckets[a].append(inst.ticker)
        if not mine and inst.ticker in watch:
            extra.append(inst.ticker)
    queues = [b for b in [*buckets.values(), extra] if b]
    chosen = list(first)
    taken = set(chosen)
    depth = 0
    while any(depth < len(q) for q in queues):
        for q in queues:
            # each area's next instrument not already covered (an instrument can sit in several areas)
            while depth < len(q) and q[depth] in taken:
                q.pop(depth)
            if depth < len(q):
                chosen.append(q[depth])
                taken.add(q[depth])
        depth += 1
    if max_tickers:
        return chosen[:max_tickers], chosen[max_tickers:]
    return chosen, []


def relevant_tickers(interests: InvestorInterests, selection: UniverseSelection | None,
                     weights: dict[str, float] | None, catalog: list[Instrument],
                     max_tickers: int | None = None) -> list[str]:
    """The covered tickers of select_tickers, in priority order, at most max_tickers."""
    return select_tickers(interests, selection, weights, catalog, max_tickers)[0]


def relate_items(items: list[NewsItem], as_of: datetime, config: BriefingConfig, period_name: str, matcher: Matcher,
                 relevant: set[str], holdings: list[str], catalog: list[Instrument]
                 ) -> list[tuple[NewsItem, list[str], list[str], dict[str, str]]]:
    """(item, funds it is ABOUT, their areas, {held fund: exposure theme}) for every item that concerns the investor.

    Exposure: a headline matching one of a HELD fund's exposure themes ("Fed holds rates" -> US T-bills & Fed
    policy -> BIL) is attached to that fund unless it is already about it. Per theme and period at most
    exposure.cap_per_theme headlines (newest first); each headline counts once (its first matching theme).
    Low-reliability (tier3) pages get no exposure attachments."""
    by = {i.ticker: i for i in catalog}
    holdings = [t for t in holdings if t in relevant]          # a fund left out by the ticker cap gets no news at all
    themes_of = {t: fund_themes(by[t], config) for t in holdings if t in by}
    wanted = {th for ths in themes_of.values() for th in ths}
    tm = ThemeMatcher(config)
    region = RegionSignals(config)
    order = list(config.exposure_themes)
    cap = config.exposure.cap_per_theme.get(period_name, 15)
    p = timedelta(days=config.window.period_days)
    pools: dict[tuple[str, int], int] = {}
    out = []
    for it in sorted(items, key=lambda i: (-i.published_at.timestamp(), i.id)):
        direct = [t for t in matcher.match_item(it).tickers if t in relevant]
        exp: dict[str, str] = {}
        # no exposure from a headline about ANOTHER named fund ("Is the ... ETF (DGS) a strong ETF?")
        # ... nor from a write-up about one product ("The S&P 500 ETF You Know Charges 0.0945%")
        other = named_funds(it.title) - set(holdings)
        if wanted and not other and not about_one_product(it.title) and tier_of(it, config)[0] != "tier3":
            th = tm.themes_for_text(f"{it.title}\n{it.summary_text}") & wanted
            if th:
                primary = next(t for t in order if t in th)
                key = (primary, 0 if it.published_at > as_of - p else 1)
                if pools.get(key, 0) < cap:
                    pools[key] = pools.get(key, 0) + 1
                    foreign = region.foreign_only(f"{it.title}\n{it.summary_text}")
                    for h in holdings:
                        if h in direct or (foreign and region.is_us_fund(by[h])):   # "Canadian dividend stocks"
                            continue
                        hit = next((t for t in themes_of.get(h, []) if t in th), None)
                        if hit:
                            exp[h] = hit
        if direct or exp:
            areas = list(dict.fromkeys(a for t in direct for a in by[t].areas))
            out.append((it, direct, areas, exp))
    return out


def briefing_pairs(items: list[NewsItem], as_of: datetime, config: BriefingConfig, interests: InvestorInterests,
                   selection: UniverseSelection | None, weights: dict[str, float] | None,
                   catalog: list[Instrument] | None = None, period_name: str = "month") -> list[tuple[NewsItem, str]]:
    """Every (headline, fund) pair the briefing will rate: funds it is about AND held funds it concerns through an
    exposure theme. Low-reliability pages excluded. (config: already set to the period.)"""
    catalog = list(catalog if catalog is not None else DEFAULT_CATALOG)
    covered = set(select_tickers(interests, selection, weights, catalog, config.live_sources.max_live_tickers,
                                 config.exposure.small_position)[0])
    holdings = [t for t, w in sorted((weights or {}).items(), key=lambda kv: -kv[1]) if w > 1e-9]
    prepared = prepare(items, as_of, config)
    rel = relate_items(prepared.items, as_of, config, period_name, Matcher(config, catalog), covered, holdings, catalog)
    return [(it, t) for it, direct, _, exp in rel if tier_of(it, config)[0] != "tier3" for t in [*direct, *exp]]


def build_briefing(
    *,
    items: list[NewsItem],
    as_of: datetime,
    interests: InvestorInterests,
    horizon_years: float,
    config: BriefingConfig,
    scorer: CachedToneScorer,
    summarizer: Summarizer,
    proposal: PortfolioProposal | None = None,
    selection: UniverseSelection | None = None,
    sources_skipped: list[str] | None = None,
    catalog: list[Instrument] | None = None,
    fund_tone: FundToneLabeler | None = None,
    briefing_period: str | None = None,
) -> Briefing:
    """fund_tone: the fund-aware labeler (config tone.method "llm_fund_aware"). None = FinBERT only, exactly as
    before; callers opt in by passing one (cache-only labelers make no calls).
    briefing_period: "week" | "month" (config briefing_periods); None = config.default_briefing_period (week).
    It sets the window the briefing summarises and compares; cadence (how often it is refreshed) is separate."""
    config, preset = with_period(config, briefing_period)
    catalog = list(catalog if catalog is not None else DEFAULT_CATALOG)
    weights = dict(proposal.weights) if proposal is not None else None          # a copy; never written back
    matcher = Matcher(config, catalog)
    covered, left_out = select_tickers(interests, selection, weights, catalog, config.live_sources.max_live_tickers,
                                       config.exposure.small_position)
    relevant = set(covered)
    holdings = [t for t, w in sorted((weights or {}).items(), key=lambda kv: (-kv[1], kv[0])) if w > 1e-9]
    by_ticker = {i.ticker: i for i in catalog}

    prepared: Prepared = prepare(items, as_of, config)
    meta = BriefingMetadata(n_fetched=prepared.n_fetched, n_non_news=prepared.n_non_news,
                            n_after_dedupe=prepared.n_after_dedupe,
                            n_outside_window=prepared.n_outside_window, fallback_tone_model=scorer.model_name,
                            tone_llm_model=config.tone.llm_model,
                            llm_model=summarizer.model, prompt_version=config.llm.prompt_version,
                            sources_skipped=list(sources_skipped or []), tickers_left_out_by_cap=left_out,
                            cadence=agg.cadence(horizon_years, config), config_version=config.version)

    # 1. match to the investor's instruments (deterministic)
    # (funds it is ABOUT, plus held funds it concerns through an exposure theme)
    period_name = briefing_period or config.default_briefing_period
    related = relate_items(prepared.items, as_of, config, period_name, matcher, relevant, holdings, catalog)
    exposure_by_id = {it.id: exp for it, _, _, exp in related}
    matched = [(it, tickers, areas) for it, tickers, areas, _ in related]
    meta.n_unmatched = len(prepared.items) - len(matched)
    meta.n_matched = len(matched)

    # 2. tone: fund-aware LLM label per (headline, fund), FinBERT (cached) as fallback;
    # 3. summaries (LLM, cached and capped; template fallback)
    # tier3 (low-reliability) items are listed but never scored, labeled by the LLM or summarized by the LLM.
    tiers = [tier_of(it, config) for it, _, _ in matched]
    usable = [i for i, (tier, _) in enumerate(tiers) if tier != "tier3"]
    scored = scorer.score_items([matched[i][0] for i in usable])
    tones: list[ToneScore | None] = [None] * len(matched)
    for i, t in zip(usable, scored):
        tones[i] = t
    meta.n_tone_unavailable = sum(tones[i] is None for i in usable)
    use_llm = config.tone.method == "llm_fund_aware" and fund_tone is not None
    meta.tone_method = config.tone.method
    b_items: list[BriefingItem] = []
    for (it, tickers, areas), tone, (tier, reason) in zip(matched, tones, tiers):
        if tier == "tier3":
            fund_tones: list[FundTone] = []
            audit = ["low-reliability source: not used for tone, no LLM call"]
            summary, kind = summarizer._template(it, audit)
            llm_tone = None
        else:
            fund_tones = [ft for t in [*tickers, *exposure_by_id.get(it.id, {})]
                          if (ft := _fund_tone(it, t, tone, fund_tone if use_llm else None, config))]
            summary, kind, llm_tone, audit = summarizer.summarize(it)
        exp = exposure_by_id.get(it.id, {})
        methods = {ft.method for ft in fund_tones}
        b_items.append(BriefingItem(
            item=it, matched_tickers=tickers, matched_areas=areas,
            exposure=agg.exposure_of(weights, tickers or list(exp)),
            exposure_text=(agg.exposure_text(weights, tickers) if tickers else _exposure_text(weights, exp, config)),
            tone=tone, fund_tones=fund_tones, exposure_of=exp,
            exposure_labels={t: theme_label(th, config) for t, th in exp.items()},
            tone_by_fund=fund_tones, publisher=it.source_name, source_tier=tier, tier_reason=reason,
            tone_method=(methods.pop() if len(methods) == 1 else "mixed") if methods else "unavailable",
            summary=summary, summary_kind=kind, llm_tone=llm_tone, audit=audit))
        if tier == "tier3":
            continue
        meta.n_fund_tones_llm += sum(ft.method == "llm_fund_aware" for ft in fund_tones)
        meta.n_fund_tones_finbert += sum(ft.method == "finbert" for ft in fund_tones)
        meta.n_fund_tones_unavailable += len(tickers) + len(exp) - len(fund_tones)
    if use_llm:
        fund_tone.flush()
        meta.n_tone_llm_calls = fund_tone.calls
        meta.tone_fallback_reasons = sorted(fund_tone.fallback_reasons) if meta.n_fund_tones_finbert or \
            meta.n_fund_tones_unavailable else []
    elif config.tone.method == "llm_fund_aware":
        meta.tone_fallback_reasons = ["no fund-aware labeler given"]
    summarizer.flush()
    meta.n_llm_calls, meta.n_llm_cache_hits = summarizer.calls, summarizer.cache_hits
    meta.n_filtered, meta.n_withheld = summarizer.filtered, summarizer.withheld

    # 4. sort by exposure, then recency
    b_items.sort(key=lambda b: (-(b.exposure or 0.0), -b.item.published_at.timestamp(), b.item.id))

    # 5. aggregates: one card per holding (weight > 0, even with no news), the other covered instruments that had
    # news, and one card per area the investor CHOSE (even with no news). Tone counts every relevant headline;
    # display assigns each headline to ONE card (see _assign_cards).
    holdings = [t for t in holdings if t in by_ticker]
    tickers_seen = [t for t in by_ticker if any(t in b.matched_tickers for b in b_items)]
    inst_order = list(dict.fromkeys([*holdings, *tickers_seen]))
    inst_aggs = []
    for t in inst_order:
        a = agg.aggregate(t, "instrument", [b for b in b_items if t in b.matched_tickers or t in b.exposure_of],
                          as_of, agg.exposure_of(weights, [t]), config, tickers=[t], this=preset.this, last=preset.last)
        inst = by_ticker[t]
        ths = fund_themes(inst, config)
        inst_aggs.append(a.model_copy(update=dict(
            name=inst.name, held=t in holdings, weight=(weights or {}).get(t) if t in holdings else None,
            r_level=inst.r_level, areas=list(inst.areas), themes=ths,
            theme_labels=[theme_label(th, config) for th in ths])))
    chosen = [a for a in dict.fromkeys(interests.interest_areas)]
    area_aggs = []
    for a in chosen:
        ag = agg.aggregate(a, "area", [b for b in b_items if a in b.matched_areas], as_of,
                           agg.exposure_of(weights, agg.area_tickers(a, catalog)), config,
                           tickers=agg.area_tickers(a, catalog), this=preset.this, last=preset.last)
        area_aggs.append(ag.model_copy(update=dict(
            name=agg.label(a), instruments=_area_instruments(a, catalog, holdings, relevant, interests.max_r_level),
            parent_area=_parent_area(a, chosen, catalog))))
    inst_aggs, area_aggs = _assign_cards(b_items, inst_aggs, area_aggs, holdings, chosen, catalog, weights or {},
                                         as_of, timedelta(days=config.window.period_days))

    # 6. alerts and suitability notes (the old "balance your sources" suggestions are retired: see sources tiers)
    alerts = agg.alerts_for(inst_aggs + area_aggs, config, weights, catalog, this=preset.this, last=preset.last)
    notes = agg.suitability_notes(list(interests.interest_areas), interests.max_r_level, catalog, config)
    diversity: list = []

    # 7. top-level summaries for the UI
    start = as_of - timedelta(days=config.window.period_days)
    current = [b for b in b_items if b.item.date_status == "unknown" or start < b.item.published_at <= as_of]
    tier_counts = {t: sum(b.source_tier == t for b in current) for t in ("tier1", "tier2", "unrated", "tier3")}
    method_counts = {"llm_fund_aware": meta.n_fund_tones_llm, "finbert": meta.n_fund_tones_finbert,
                     "not_yet_assessed": meta.n_fund_tones_unavailable}
    low = [b.item.id for b in current if b.source_tier == "tier3"]
    single = []
    for t in holdings:
        mine = [b for b in current if t in b.matched_tickers and agg.countable(b)]
        kinds_t = {b.source_tier for b in mine}
        if len(mine) >= 2 and len(kinds_t) == 1:
            single.append(f"All {len(mine)} headlines about {t} {preset.this} come from {SINGLE_TIER_WORDS[kinds_t.pop()]}.")
    # "What changed" uses HOLDINGS only: the largest move of a holding that can be compared, and for the others why
    # not ("VT: too little news last week to compare").
    held_aggs = [x for x in inst_aggs if x.held]
    largest = max((x for x in held_aggs if x.change is not None), key=lambda a: abs(a.change), default=None)
    compare_notes = []
    for x in held_aggs:
        if x.change is not None:
            continue
        if x.coverage_status and x.coverage_status.state == "quiet":
            compare_notes.append(f"{x.subject}: quiet {preset.this.split()[-1]}, nothing to compare.")
        elif x.compare_note:
            compare_notes.append(f"{x.subject}: {x.compare_note}.")
    groups = []
    if len(held_aggs) > config.exposure.group_above_holdings:
        by_theme: dict[str, list[str]] = {}
        for x in held_aggs:
            by_theme.setdefault(x.themes[0] if x.themes else "single_stocks", []).append(x.subject)
        for th, members in by_theme.items():
            glabel = theme_label(th, config) if th != "single_stocks" else "Individual companies"
            g = agg.aggregate(th, "group", [b for b in b_items if any(t in b.matched_tickers or t in b.exposure_of
                                                                         for t in members)],
                              as_of, agg.exposure_of(weights, members), config, tickers=members,
                              this=preset.this, last=preset.last, display_name=glabel)
            groups.append(HoldingGroup(theme=th, label=glabel, tickers=members,
                                       weight=round(sum((weights or {}).get(t, 0) for t in members), 6), aggregate=g))
        groups.sort(key=lambda g: -g.weight)
    small = [t for t in holdings if 0 < (weights or {}).get(t, 0) < config.exposure.small_position]
    legend = [TierLegend(tier=t, label=config.sources.tier_labels[t],
                         explanation=config.sources.tier_explanations.get(t, ""),
                         weight=None if t == "tier3" else config.sources.tier_weights[t])
              for t in ("tier1", "tier2", "unrated", "tier3")]
    ui = dict(period_words={"this": preset.this, "last": preset.last},
              alert_threshold=config.alerts.tone_change_threshold, holdings_order=holdings, chosen_areas=chosen,
              tier_counts=tier_counts, method_counts=method_counts, low_reliability_items=low,
              single_tier_notes=single, tier_legend=legend,
              largest_move=LargestMove(subject=largest.subject, kind=largest.kind, change=largest.change)
              if largest else None,
              n_date_unknown=sum(b.item.date_status == "unknown" for b in b_items),
              compare_notes=compare_notes, holding_groups=groups, small_positions=small)

    kinds = {b.item.data_source for b in b_items} or {it.data_source for it in items} or {"live"}
    data_source = kinds.pop() if len(kinds) == 1 else "mixed"
    # Header and labels describe what actually scored / wrote THESE items (no contradictory model line).
    llm_scored = config.tone.method == "llm_fund_aware" and (meta.n_fund_tones_llm or not meta.n_fund_tones_finbert)
    note = config.tone.header_note if llm_scored else config.tone.finbert_header_note
    labels = list(LABELS)
    if not any(b.summary_kind == "llm" for b in b_items):
        labels = [NO_AI_SUMMARIES_LABEL if x == AI_SUMMARIES_LABEL else x for x in labels]
    return Briefing(as_of=as_of, investor_id=interests.investor_id, data_source=data_source, tone_note=note,
                    labels=labels,
                    briefing_period=briefing_period or config.default_briefing_period,
                    period_start=as_of - timedelta(days=config.window.period_days), period_end=as_of,
                    items=b_items, instrument_aggregates=inst_aggs, area_aggregates=area_aggs, alerts=alerts,
                    suitability_notes=notes, diversity_suggestions=diversity, metadata=meta, **ui)


def _area_instruments(area: str, catalog: list[Instrument], holdings: list[str], relevant: set[str],
                      max_r_level: int | None, limit: int = 12) -> list[AreaInstrument]:
    """The instruments behind an area card: holdings first, then funds (catalog order), then covered single stocks."""
    members = [i for i in catalog if area in i.areas]
    picked = ([i for i in members if i.ticker in holdings]
              + [i for i in members if i.ticker not in holdings and i.product_type != "stock"]
              + [i for i in members if i.ticker not in holdings and i.product_type == "stock" and i.ticker in relevant])
    out = []
    unique = list({i.ticker: i for i in picked}.values())
    for i in unique[:limit]:
        status = ("held" if i.ticker in holdings else
                  "above_risk_level" if max_r_level is not None and i.r_level > max_r_level else "not_held")
        out.append(AreaInstrument(ticker=i.ticker, name=i.name, status=status, r_level=i.r_level))
    return out


def _fund_set(area: str, catalog: list[Instrument]) -> set[str]:
    return {i.ticker for i in catalog if area in i.areas and i.product_type != "stock"}


def _parent_area(area: str, chosen: list[str], catalog: list[Instrument]) -> str | None:
    """The smallest OTHER chosen area whose funds include all of this area's funds (us_stocks under stocks)."""
    mine = _fund_set(area, catalog)
    if not mine:
        return None
    parents = [a for a in chosen if a != area and mine < _fund_set(a, catalog)]
    return min(parents, key=lambda a: (len(_fund_set(a, catalog)), chosen.index(a)), default=None)


def _assign_cards(items: list[BriefingItem], inst_aggs: list[Aggregate], area_aggs: list[Aggregate],
                  holdings: list[str], chosen: list[str], catalog: list[Instrument], weights: dict[str, float],
                  as_of: datetime, period: timedelta) -> tuple[list[Aggregate], list[Aggregate]]:
    """Each headline of this period is SHOWN on one card only: the largest holding it concerns; else the most
    specific chosen area (fewest funds); low-reliability items go to the footer list instead. Every other card
    it concerns counts it as "+N shown under ...". Tone aggregates are not changed."""
    size = {a: len(_fund_set(a, catalog)) or 10_000 for a in chosen}
    shown: dict[str, list[BriefingItem]] = {}
    elsewhere: dict[str, dict[str, int]] = {}
    under_holdings: dict[str, int] = {}
    current = [b for b in items if b.source_tier != "tier3"
               and (b.item.date_status == "unknown" or as_of - period < b.item.published_at <= as_of)]
    for b in sorted(current, key=lambda b: (b.item.date_status == "unknown", -b.item.published_at.timestamp())):
        held_here = sorted((t for t in dict.fromkeys([*b.matched_tickers, *b.exposure_of]) if t in holdings),
                           key=lambda t: (t not in b.matched_tickers, -weights.get(t, 0), t))
        areas_here = sorted((a for a in b.matched_areas if a in chosen), key=lambda a: (size[a], chosen.index(a)))
        if held_here:
            home = held_here[0]
            for t in held_here[1:]:
                elsewhere.setdefault(t, {}).setdefault(home, 0)
                elsewhere[t][home] += 1
            for a in areas_here:
                under_holdings[a] = under_holdings.get(a, 0) + 1
        elif areas_here:
            home = areas_here[0]
            for a in areas_here[1:]:
                elsewhere.setdefault(a, {}).setdefault(home, 0)
                elsewhere[a][home] += 1
        else:
            continue
        shown.setdefault(home, []).append(b)

    def done(a: Aggregate) -> Aggregate:
        return a.model_copy(update=dict(headline_ids=[b.item.id for b in shown.get(a.subject, [])],
                                        shown_under=elsewhere.get(a.subject, {}),
                                        n_shown_under_holdings=under_holdings.get(a.subject, 0)))
    return [done(a) for a in inst_aggs], [done(a) for a in area_aggs]


def _exposure_text(weights: dict[str, float] | None, exp: dict[str, str], config: BriefingConfig) -> str:
    """'About what BIL holds: US T-bills & Fed policy. This concerns 30% of your portfolio.'"""
    if not exp or weights is None:
        return ""
    funds = list(exp)
    labels = list(dict.fromkeys(theme_label(th, config) for th in exp.values()))
    verb = "holds" if len(funds) == 1 else "hold"
    return (f"About what {', '.join(funds)} {verb}: {'; '.join(labels)}. "
            f"This concerns {agg.share_text(agg.exposure_of(weights, funds))} of your portfolio.")


def _fund_tone(item: NewsItem, ticker: str, finbert: ToneScore | None, labeler: FundToneLabeler | None,
               config: BriefingConfig) -> FundTone | None:
    """The fund-aware LLM label when available; otherwise FinBERT's (gated) tone; otherwise None."""
    if labeler is not None:
        label = labeler.label(item, ticker)
        if label is not None:
            return FundTone(ticker=ticker, label=label, value=config.tone.llm_values[label], method="llm_fund_aware")
    if finbert is not None:
        return FundTone(ticker=ticker, label=top_class(finbert.positive, finbert.negative, finbert.neutral),
                        value=finbert.tone, method="finbert")
    return None
