"""Market briefing demo.

    python -m egoquant.briefing                     offline: sample (fixture) news + synthetic prices -> HTML report
    python -m egoquant.briefing --live              live RSS + Reddit (+ EDGAR if enabled) for the same investor
    python -m egoquant.briefing --no-llm            template summaries only (no OpenAI calls)
    python -m egoquant.briefing --period month      summarise and compare months instead of weeks

One-off maintenance (writes the committed fixture caches; needs the optional extras):
    python -m egoquant.briefing --refresh-fixture-scores      FinBERT scores for the fixture items (local, free)
    python -m egoquant.briefing --refresh-fixture-summaries   OpenAI summaries for the fixture items (capped)
    python -m egoquant.briefing --refresh-fixture-fund-tones  fund-aware tone labels for the fixture items (capped)

The offline demo makes NO network or OpenAI calls: it reads the committed FinBERT scores and LLM summaries
for the fixture items. The clock is read here, at the edge, and passed in as as_of.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from egoquant.briefing.advice_filter import AdviceFilter
from egoquant.briefing.build import build_briefing, relevant_tickers
from egoquant.briefing.fetch import (EdgarAdapter, FixtureAdapter, RedditAdapter, RSSAdapter, TiingoNewsAdapter,
                                     fetch_all, fixture_as_of)
from egoquant.briefing.fund_tone import FundToneLabeler
from egoquant.briefing.loader import (FIXTURE_FUND_TONES, FIXTURE_LLM_CACHE, FIXTURE_SCORES, BriefingConfig,
                                      briefing_cache_dir,
                                      expand_ticker_feeds, load_config, read_json_cache, write_json_cache)
from egoquant.briefing.models import Briefing
from egoquant.briefing.summarize import OpenAIBackend, Summarizer
from egoquant.briefing.tone import CachedToneScorer, FinBertScorer

DEMO_PERSONA = "growth_investor"
DEMO_EXTRA_AREAS = ["crypto", "bonds"]          # so the demo also shows watchlist items and an R-level note


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def _summarizer(config: BriefingConfig, *, fixture: bool, allow_llm: bool) -> Summarizer:
    backend = OpenAIBackend(config) if allow_llm and OpenAIBackend.key_present() else None
    precomputed = read_json_cache(FIXTURE_LLM_CACHE) if fixture else None
    cache = None if fixture else briefing_cache_dir() / "llm_cache.json"
    return Summarizer(config, backend, AdviceFilter(config.forbidden_patterns), cache_path=cache, precomputed=precomputed)


def _scorer(config: BriefingConfig, *, fixture: bool) -> CachedToneScorer:
    if fixture:   # committed scores only: no model load, no download
        return CachedToneScorer(None, config.tone.model, precomputed=read_json_cache(FIXTURE_SCORES),
                                neutral_gating=config.tone.neutral_gating)
    inner = FinBertScorer(config.tone.model, config.truncation["finbert_max_tokens"])
    return CachedToneScorer(inner, config.tone.model, cache_path=briefing_cache_dir() / "tone_cache.json",
                            neutral_gating=config.tone.neutral_gating)


def fund_tone_labeler(config: BriefingConfig, *, fixture: bool, allow_llm: bool) -> FundToneLabeler:
    """Fixture: the committed labels for the invented sample items, no calls. Live: the shared disk cache plus new
    calls (capped) when allowed; FinBERT for the rest."""
    if fixture:
        return FundToneLabeler(config, precomputed=read_json_cache(FIXTURE_FUND_TONES), allow_calls=False)
    return FundToneLabeler(config, cache_path=briefing_cache_dir() / "fund_tone_cache.json", allow_calls=allow_llm)


def fetch_news(config: BriefingConfig, as_of: datetime, interests, selection, weights, *, live: bool,
               period: str | None):
    """Fixture items, or live RSS + Reddit (+ EDGAR) for this investor's relevant tickers plus the archive."""
    if not live:
        return fetch_all([FixtureAdapter()], as_of)
    from egoquant.agents.market_intel import DEFAULT_CATALOG
    from egoquant.briefing.archive import archive_items, load_archive
    from egoquant.briefing.loader import with_period
    from egoquant.briefing.matching import Matcher
    src = config.live_sources
    # Per-ticker feeds only for this investor's relevant tickers (holdings first), never the whole catalog.
    us_listed = {i.ticker for i in DEFAULT_CATALOG if i.market == "us_listed"}
    tickers = [t for t in relevant_tickers(interests, selection, weights, DEFAULT_CATALOG, src.max_live_tickers)
               if t in us_listed]
    feeds = src.rss + expand_ticker_feeds(src.rss_per_ticker, tickers)
    adapters = [RSSAdapter(feeds, config), RedditAdapter(src.subreddits, config), TiingoNewsAdapter()]
    if src.edgar_enabled and selection is not None:
        adapters.append(EdgarAdapter({i.ticker: i.name for i in selection.instruments}, config))
    fetched = fetch_all(adapters, as_of)
    # News archive: keep every fetched headline (no bodies) so monthly comparisons gain history over time;
    # read it back with the fresh items (fresh first, so they win the dedupe).
    archive_items(fetched.items, Matcher(config))
    lookback = with_period(config, period)[1].lookback_days
    fetched.items = fetched.items + load_archive(since=as_of - timedelta(days=lookback))
    return fetched


def make_builder(*, live: bool, allow_llm: bool, config: BriefingConfig | None = None,
                 as_of: datetime | None = None):
    """A briefing builder for run_pipeline (briefing_builder=...). live=False: the DEMO-labeled sample news
    (its own as_of, no network, no OpenAI). live=True: live sources, the archive, cached + capped LLM calls when
    allow_llm. The builder only reads the proposal it is given (None for a rejected proposal).
    as_of: the news clock (default: now when live, the sample's own date otherwise), not the price date."""
    news_as_of = as_of

    def build(*, interests, selection, proposal, horizon_years, briefing_period=None, **_hook):
        cfg = config or load_config()
        when = news_as_of or (datetime.now(timezone.utc) if live else fixture_as_of())
        weights = dict(proposal.weights) if proposal is not None else None
        fetched = fetch_news(cfg, when, interests, selection, weights, live=live, period=briefing_period)
        return build_briefing(items=fetched.items, as_of=when, interests=interests, horizon_years=horizon_years,
                              config=cfg, scorer=_scorer(cfg, fixture=not live),
                              summarizer=_summarizer(cfg, fixture=not live, allow_llm=allow_llm),
                              proposal=proposal, selection=selection, sources_skipped=fetched.skipped,
                              fund_tone=fund_tone_labeler(cfg, fixture=not live, allow_llm=allow_llm),
                              briefing_period=briefing_period)
    return build


def run_demo(*, live: bool, allow_llm: bool, out_dir: Path, period: str = "week") -> Briefing:
    from egoquant.agents.market_intel import InvestorInterests, SyntheticPriceSource
    from egoquant.demo.personas import PERSONAS, interest_fields
    from egoquant.pipeline import run_pipeline
    from egoquant.report import attach_briefing, render_html

    config = load_config()
    persona = PERSONAS[DEMO_PERSONA]
    as_of = datetime.now(timezone.utc) if live else fixture_as_of()
    areas = list(dict.fromkeys([*interest_fields(persona)["interest_areas"], *DEMO_EXTRA_AREAS]))

    # The portfolio is computed first and independently (synthetic prices, offline). The briefing only reads it.
    run = run_pipeline(persona.answers(), investor_id=persona.investor_id, interest_areas=areas,
                       goal_type=persona.goal_type, horizon_years=persona.horizon_years, as_of=as_of.date(),
                       trading_platforms=persona.trading_platforms, price_source=SyntheticPriceSource())
    interests = InvestorInterests(investor_id=persona.investor_id, interest_areas=areas,
                                  max_r_level=run.profile.max_r_level)

    briefing = make_builder(live=live, allow_llm=allow_llm, config=config, as_of=as_of)(
        interests=interests, selection=run.selection, proposal=run.result.final, as_of=as_of.date(),
        horizon_years=persona.horizon_years, briefing_period=period)
    report = attach_briefing(briefing, run.report)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = ("briefing_live" if live else "briefing_demo") + ("" if period == "week" else f"_{period}")
    (out_dir / f"{stem}.html").write_text(render_html(report), encoding="utf-8")
    (out_dir / f"{stem}.json").write_text(briefing.model_dump_json(indent=1), encoding="utf-8")
    return briefing


def refresh_fixture_scores() -> int:
    config = load_config()
    items = FixtureAdapter().fetch(fixture_as_of()).items
    scorer = FinBertScorer(config.tone.model, config.truncation["finbert_max_tokens"])
    if not scorer.available():
        print("FinBERT is not available (pip install -e '.[nlp]'); nothing written.", file=sys.stderr)
        return 1
    cached = CachedToneScorer(scorer, config.tone.model, neutral_gating=config.tone.neutral_gating)
    gated = cached.score_items(items)
    write_json_cache(FIXTURE_SCORES, {cached.key(it): s.model_dump() for it, s in zip(items, gated) if s})
    print(f"wrote {len(cached.cache)} FinBERT scores to {FIXTURE_SCORES}")
    return 0


def refresh_fixture_fund_tones(max_calls: int = 450) -> int:
    """Fund-aware labels for every (fixture item, fund) pair: the funds it is about and every fund it concerns through
    an exposure theme. Invented items only; cached, capped."""
    from egoquant.briefing.matching import Matcher
    config = load_config()
    if not OpenAIBackend.key_present():
        print("OPENAI_API_KEY is not set; nothing written.", file=sys.stderr)
        return 1
    config.tone.llm_max_calls_per_run = max_calls                       # explicit cap for the invented items
    items = {it.id: it for it in FixtureAdapter().fetch(fixture_as_of()).items}.values()
    matcher = Matcher(config)
    from egoquant.briefing.sources import tier_of
    from egoquant.agents.market_intel import DEFAULT_CATALOG
    from egoquant.briefing.themes import ThemeMatcher, fund_themes
    themes = ThemeMatcher(config)
    funds = [(i.ticker, set(fund_themes(i, config))) for i in DEFAULT_CATALOG if i.product_type != "stock"]
    pairs = []
    for it in items:
        if tier_of(it, config)[0] == "tier3":                            # low-reliability items: never labeled
            continue
        direct = list(matcher.match_item(it).tickers)
        hit = themes.themes_for_text(f"{it.title}\n{it.summary_text}")  # exposure pairs: every fund that could hold it
        pairs += [(it, t) for t in dict.fromkeys([*direct, *(t for t, th in funds if th & hit)])]
    labeler = FundToneLabeler(config, precomputed=read_json_cache(FIXTURE_FUND_TONES))
    pending = sum(labeler.key(it.id, t) not in labeler.cache for it, t in pairs)
    print(f"{len(pairs)} (item, fund) pairs; {pending} need a call (cap {config.tone.llm_max_calls_per_run})")
    if pending > config.tone.llm_max_calls_per_run:
        print("More calls than the cap; nothing sent.", file=sys.stderr)
        return 1
    labels = [labeler.label(it, t) for it, t in pairs]
    keep = {labeler.key(it.id, t) for it, t in pairs}
    write_json_cache(FIXTURE_FUND_TONES, {k: v for k, v in labeler.cache.items() if k in keep})
    cost = labeler.input_tokens / 1e6 * 0.40 + labeler.output_tokens / 1e6 * 1.60   # gpt-4.1-mini list prices
    print(f"OpenAI calls: {labeler.calls}; tokens in/out: {labeler.input_tokens}/{labeler.output_tokens}; "
          f"cost about ${cost:.4f}; unscored: {sum(x is None for x in labels)}; errors: {labeler.errors}")
    return 0


def refresh_fixture_summaries(max_calls: int | None = None) -> int:
    """Summaries for the INVENTED fixture items only. Cached, so items already in the file are not sent again."""
    config = load_config()
    if not OpenAIBackend.key_present():
        print("OPENAI_API_KEY is not set; nothing written.", file=sys.stderr)
        return 1
    if max_calls is not None:
        config.llm.max_llm_calls_per_run = min(config.llm.max_llm_calls_per_run, max_calls)
    items = FixtureAdapter().fetch(fixture_as_of()).items
    backend = OpenAIBackend(config)
    summarizer = Summarizer(config, backend, AdviceFilter(config.forbidden_patterns), cache_path=FIXTURE_LLM_CACHE)
    pending = sum(summarizer.key(it) not in summarizer.cache for it in items)
    print(f"{pending} fixture items need a summary (cap {config.llm.max_llm_calls_per_run} calls per run)")
    results = [summarizer.summarize(it) for it in items]
    summarizer.flush()
    kinds = {k: sum(r[1] == k for r in results) for k in ("llm", "template", "withheld")}
    print(f"OpenAI calls made: {summarizer.calls}; cache hits: {summarizer.cache_hits}; "
          f"rejected by the advice filter: {summarizer.filtered}; result kinds: {kinds}; "
          f"model: {summarizer.model}; tokens in/out: {backend.input_tokens}/{backend.output_tokens}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m egoquant.briefing", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="live RSS/Reddit sources instead of the sample items")
    ap.add_argument("--no-llm", action="store_true", help="template summaries only")
    ap.add_argument("--period", choices=["week", "month"], default="week",
                    help="the window the briefing summarises and compares (default week)")
    ap.add_argument("--out", default="reports", help="output folder (gitignored)")
    ap.add_argument("--refresh-fixture-scores", action="store_true")
    ap.add_argument("--refresh-fixture-summaries", action="store_true")
    ap.add_argument("--max-calls", type=int, default=None, help="lower the OpenAI call cap for this run")
    ap.add_argument("--refresh-fixture-fund-tones", action="store_true")
    args = ap.parse_args(argv)
    _load_dotenv()
    if args.refresh_fixture_scores:
        return refresh_fixture_scores()
    if args.refresh_fixture_fund_tones:
        return refresh_fixture_fund_tones(args.max_calls or 450)
    if args.refresh_fixture_summaries:
        return refresh_fixture_summaries(args.max_calls)
    b = run_demo(live=args.live, allow_llm=not args.no_llm, out_dir=Path(args.out), period=args.period)
    m = b.metadata
    print("\n".join(b.banners + b.labels))
    print(f"\n{len(b.items)} items ({m.n_fetched} fetched, {m.n_unmatched} not relevant), "
          f"{len(b.alerts)} alerts, {len(b.suitability_notes)} suitability notes, "
          f"{len(b.diversity_suggestions)} source suggestions; OpenAI calls this run: {m.n_llm_calls} summaries, "
          f"{m.n_tone_llm_calls} tone; tone per (headline, fund): {m.n_fund_tones_llm} AI, {m.n_fund_tones_finbert} "
          f"FinBERT; "
          f"cadence: {m.cadence}")
    for a in b.alerts:
        print(f"ALERT ({a.action}): {a.message}")
    stem = ("briefing_live" if args.live else "briefing_demo") + ("" if args.period == "week" else f"_{args.period}")
    print(f"\nreport: {Path(args.out) / (stem + '.html')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
