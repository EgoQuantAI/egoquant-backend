"""Command line entry point:  python -m egoquant <command>  (or `egoquant <command>` once installed)

    personas                       list the demo trader profiles
    options <persona>              the investment-preference form that persona's risk profile allows
    advise <persona>               full pipeline for one persona on REAL data (Tiingo + Binance), writes
                                   the client report (HTML + JSON) to reports/
           --synthetic             invented prices instead (offline)
           --csv PATH              replay prices saved with `fetch` (offline)
           --briefing              add the Agent D market briefing + "What changed" (read-only; sample
                                   news with --synthetic/--csv, live news otherwise)
           --period week|month     the briefing's window (default week; needs --briefing)
           --as-of YYYY-MM-DD      decision date (default: the last weekday before today)
    fetch <persona|all> --out CSV  save the real prices those personas need, for offline replay
    crypto-market <persona>        the whole Binance spot market (crypto-eligible personas only)
    directory [--refresh]          Agent C's investment directory: every US stock and ETF by cluster
    warm-cache [--per-cluster 10] [--wait]   fetch the ~20-year history of every cluster's most traded funds
                                   ahead of time (Tiingo: 50 requests/hour; waits and resumes)
    goal-demo <persona>            the goal-based path step by step (candidates, Agent B, Monte Carlo, choice,
              [--goal --monthly --years] [--synthetic]   checks) -> reports/goal_walkthrough_*.html
    behavior [profile ...]         Agent A + Agent B on the SYNTHETIC behavioral investors
    backtest                       walk-forward backtest of Agent A against simple baselines (SYNTHETIC)
             --seeds N --risk-class C3
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from egoquant.agents.behavioral import BehavioralTwinAgent, NoBehavioralEvidence
from egoquant.agents.market_intel import (
    PLATFORMS,
    CsvPriceSource,
    InvestorInterests,
    MarketIntelligenceAgent,
    PriceSourceError,
    SyntheticPriceSource,
)
from egoquant.agents.quant import QuantAgent
from egoquant.agents.quant.backtest import (
    HistoricalMarketProvider,
    make_agent_strategy,
    make_equal_weight_strategy,
    make_fixed_mix_strategy,
    make_inverse_vol_strategy,
    make_vol_target_overlay_strategy,
    run_backtest,
)
from egoquant.demo.personas import PERSONAS, Persona, interest_fields, run_persona
from egoquant.demo.stubs import SyntheticMarketAgent
from egoquant.demo.synthetic_investors import SYNTHETIC_PROFILES, synthetic_behavioral_provider
from egoquant.demo.synthetic_market import make_synthetic_history, synthetic_asset_infos
from egoquant.models import RISK_CLASS_ORDER, AgentConfig, InvestorContext
from egoquant.pipeline import PipelineResult
from egoquant.preferences import build_form, load_preference_bank, to_interest_fields
from egoquant.report import save_report
from egoquant.risk_profile import build_risk_profile, load_all, to_agent_c_limits


def last_weekday_before(day: date) -> date:
    """The last Monday-Friday strictly before `day`: the most recent day whose close is final."""
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


# ==========================================================================
# personas / advise / fetch
# ==========================================================================
def cmd_personas(_: argparse.Namespace) -> None:
    for p in PERSONAS.values():
        print(f"{p.key:18s} {p.expected_class}  {p.description}")
        print(f"{'':18s}     picks: {', '.join(p.preferences['P1'])} | trades on: {', '.join(p.trading_platforms)}")


def cmd_options(args: argparse.Namespace) -> None:
    persona = PERSONAS[args.persona]
    bank, config = load_all()
    profile = build_risk_profile(persona.answers(), bank, config)
    form = build_form(load_preference_bank(), profile, config, persona.trading_platforms)
    print(f"{persona.key}: risk class {profile.c_level}, products up to R{profile.max_r_level}, "
          f"knowledge {profile.product_access}, trades on {', '.join(persona.trading_platforms)}\n")
    for q in form.questions:
        when = f"  (asked if {', '.join(f'{k}={v}' for k, v in q.requires.items())})" if q.requires else ""
        print(f"{q.id} [{q.type}] {q.text}{when}")
        picked = set(persona.preferences.get(q.id, []))
        for o in q.options:
            mark = "x" if o.id in picked else " "
            print(f"   [{mark}] {o.text}" if o.available else f"   [-] {o.text}  -- not offered: {o.reason}")
    print("\nPicks sent to Agent C (priority order):",
          to_interest_fields(persona.preference_answers(), form, load_preference_bank()))


def cmd_crypto_market(args: argparse.Namespace) -> None:
    persona = PERSONAS[args.persona]
    as_of = args.as_of or last_weekday_before(date.today())
    run = run_persona(persona, as_of)
    try:
        market = run.agent_c.get_crypto_market(as_of)
    except ValueError as exc:
        raise SystemExit(f"{persona.key}: {exc}")
    pct = lambda x: "   n/a" if x is None else f"{x:+6.1%}"
    print(f"Binance spot market on {as_of}: {market.n_pairs_listed} coins listed, {len(market.entries)} priced "
          f"(historical, not a forecast)\n")
    print("rank  coin        last close      1 day   30 days  90 days  from 90d high  24h volume (USDT)")
    for e in market.entries[:args.top]:
        volume = f"{e.quote_volume_24h:,.0f}" if e.quote_volume_24h is not None else "n/a"
        print(f"{e.volume_rank:4d}  {e.ticker:10s} {e.last_close:>12,.4f}  {pct(e.change_1d)}   {pct(e.change_30d)}   "
              f"{pct(e.change_90d)}   {e.drawdown_from_peak:+6.1%}       {volume}")
    print(f"\nIn this investor's universe (sent to Agent A): "
          f"{', '.join(t for t in run.selection.tickers if run.agent_c.instruments[t].asset_class == 'crypto')}")
    for a in market.alerts:
        print(f"  ALERT: {a.message}")


def _price_source(args: argparse.Namespace):
    if args.csv:
        return CsvPriceSource(args.csv)
    return SyntheticPriceSource() if args.synthetic else None


def cmd_advise(args: argparse.Namespace) -> None:
    persona = PERSONAS[args.persona]
    as_of = args.as_of or last_weekday_before(date.today())
    if args.period is not None and not args.briefing:
        raise SystemExit("--period needs --briefing")
    builder = None
    if args.briefing:
        from egoquant.briefing.__main__ import make_builder
        # Invented prices (--synthetic) or a replay (--csv): the DEMO-labeled sample news, offline, no OpenAI.
        # Real prices: live news, the archive, cached + capped fund-aware tone calls.
        offline = bool(args.synthetic or args.csv)
        builder = make_builder(live=not offline, allow_llm=not offline)
    try:
        run = run_persona(persona, as_of, price_source=_price_source(args), briefing_builder=builder,
                          briefing_period=args.period or "week")
    except PriceSourceError as exc:
        raise SystemExit(f"\nCannot get market data: {exc}\n")
    print_run(persona, run)
    if run.report.market_briefing is not None:
        b = run.report.market_briefing
        m = b.metadata
        print(f"\nAGENT D briefing ({b.briefing_period}, {b.data_source}): {len(b.items)} headlines, "
              f"{len(b.alerts)} tone alert(s); tone per (headline, fund): {m.n_fund_tones_llm} AI, "
              f"{m.n_fund_tones_finbert} FinBERT, {m.n_fund_tones_unavailable} not yet assessed; "
              f"OpenAI calls: {m.n_llm_calls} summaries, {m.n_tone_llm_calls} tone")
    if run.report.what_changed:
        print("\nWHAT CHANGED (only action: offer_risk_profile_recheck):")
        for line in run.report.what_changed:
            print(f"   - {line}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    html_path, json_path = out / f"{persona.key}_{as_of}.html", out / f"{persona.key}_{as_of}.json"
    save_report(run.report, str(html_path), str(json_path))
    print(f"\nClient report: {html_path} (and {json_path.name} for the app / Advisor)")


def print_run(persona: Persona, run: PipelineResult) -> None:
    p, sel, final = run.profile, run.selection, run.result.final
    print(f"{persona.key} ({persona.investor_id}): {persona.description}")
    print(f"\n1. QUESTIONNAIRE -> {p.c_level} (tolerance level {p.tolerance_level}, capacity level "
          f"{p.capacity_level}, bound by {p.binding_dimension}); products up to R{p.max_r_level}; "
          f"knowledge: {p.product_access}; loss capacity {p.loss_capacity:.0%}")
    print(f"\n2. AGENT C universe ({', '.join(PLATFORMS[k].name for k in sel.trading_platforms) or 'no platform given'}):")
    print(f"   picks (priority order): {', '.join(sel.interest_areas)} | style: {run.agent_c.interests.instrument_style}"
          + (f" | avoids: {', '.join(run.agent_c.interests.avoid_sectors)}" if run.agent_c.interests.avoid_sectors else ""))
    for i in sel.instruments:
        print(f"     {i.ticker:8s} {i.asset_class:9s} R{i.r_level}  {', '.join(i.reasons):38s} {i.name}")
    if sel.deprioritized:
        print(f"     left out to stay within budget: {len(sel.deprioritized)} lower-priority instrument(s), e.g. "
              f"{', '.join(d.ticker for d in sel.deprioritized[:6])}")
    for e in sel.excluded:
        print(f"     excluded {e.ticker}: {e.reason}")
    if sel.watchlist:
        print(f"     watchlist (briefings only): {', '.join(sel.watchlist_tickers)}")
    for f in sel.suitability_flags:
        print(f"     SUITABILITY FLAG: {f}")
    m = run.market_report
    if m is not None:
        print(f"   data: {m.data_source} from {m.source}, {m.n_rows} rows {m.first_date} .. {m.last_date}; "
              f"risk-free {m.risk_free_rate_annual:.2%} ({m.risk_free_method})")
        for d in m.dropped:
            print(f"     dropped {d.ticker}: {d.reason}")
        for a in m.alerts:
            print(f"     ALERT: {a.message}")
    print("\n3. AGENT B limits:", "none" if not run.result.behavioral_constraints else "")
    for c in run.result.behavioral_constraints:
        print(f"     {c.label} = {c.value:.1%} (hard={c.hard}, confidence {c.confidence})")
    print(f"\n4. AGENT A proposal: {final.status}")
    for asset, w in sorted(final.weights.items(), key=lambda kv: -kv[1]):
        if w > 0.0005:
            print(f"     {asset:8s} {w:6.1%}")
    print(f"     binding: {', '.join(final.binding_constraints) or 'none'}")
    print(f"\n5. RISK ENGINE: {'APPROVED' if run.approved else 'NOT APPROVED'}")
    for v in run.review.violations:
        print(f"     - {v}")
    print(f"\n{run.report.headline}")
    for note in run.report.interest_notes:
        print(f"  - {note}")


def cmd_directory(args: argparse.Namespace) -> None:
    """Build (or load) the investment directory and show each cluster's most traded members."""
    from egoquant.agents.market_intel.directory import directory_path, load_directory

    directory = load_directory(refresh=args.refresh)
    if directory is None:
        print("The directory could not be built (no network?). Agent C uses its curated catalog instead.")
        return
    stocks = sum(e.kind == "stock" for e in directory.entries)
    print(f"Directory of {directory.built_on}: {len(directory.entries)} instruments ({stocks} stocks, "
          f"{len(directory.entries) - stocks} ETFs) from {', '.join(directory.sources)}. Saved at {directory_path()}.")
    print("Left out: " + ", ".join(f"{n} {reason}" for reason, n in
                                   sorted(directory.excluded.items(), key=lambda kv: -kv[1])))
    by_ticker = {e.ticker: e for e in directory.entries}
    clusters = directory.clusters()
    for area in sorted(clusters, key=lambda a: -len(clusters[a])):
        top = " ".join(clusters[area][: args.top])
        kinds = {by_ticker[t].kind for t in clusters[area]}
        print(f"  {area:22s} {len(clusters[area]):5d} ({'/'.join(sorted(kinds))})  {top}")


def cmd_warm_cache(args: argparse.Namespace) -> None:
    """Warm the price cache with the long history the goal simulation needs (see market_intel/warm.py)."""
    from egoquant.agents.market_intel.directory import load_directory
    from egoquant.agents.market_intel.warm import warm_cache, warm_plan
    from egoquant.models import AgentConfig

    as_of = args.as_of or last_weekday_before(date.today())
    start = MarketIntelligenceAgent.window_start(as_of, AgentConfig().goal_history_days)
    areas = [a.strip() for a in args.areas.split(",")] if args.areas else None
    plan = warm_plan(load_directory(), args.per_cluster, areas)
    print(f"Warming {len(plan)} tickers ({args.per_cluster} per cluster, core funds first), {start} to {as_of}")
    if args.dry_run:
        print(" ".join(plan))
        return
    report = warm_cache(plan, as_of, start, wait=args.wait, max_requests=args.max_requests)
    print(f"\nDone: {len(report.fetched)} fetched, {len(report.already_cached)} already cached, "
          f"{len(report.failed)} failed, {report.waits} wait(s)" + (f"; stopped: {report.stopped}" if report.stopped else ""))
    if report.failed:
        print("Failed:", ", ".join(f"{t} ({why[:40]})" for t, why in list(report.failed.items())[:15]))


def cmd_goal_demo(args: argparse.Namespace) -> None:
    """Run the goal-based path for one persona and write the step-by-step walkthrough page."""
    from egoquant.demo.goal_walkthrough import default_goal, run_walkthrough, save

    as_of = args.as_of or last_weekday_before(date.today())
    spec = default_goal(args.persona, args.goal, args.monthly, args.years)
    print(f"{args.persona}: reach ${spec.goal_amount:,.0f} in {spec.horizon_years:g} years from "
          f"${spec.start_amount:,.0f}, {spec.monthly_flow:+,.0f} a month")
    try:
        walk = run_walkthrough(args.persona, as_of, spec, price_source=_price_source(args),
                               fetch_budget={"us_listed": args.budget, "crypto_spot": 1000})
    except PriceSourceError as exc:
        raise SystemExit(f"\nCannot get market data: {exc}\n")
    g = walk.run.goal_analysis
    if g is None:
        print(f"Goal simulation not run: {walk.run.goal_unavailable}")
    else:
        sc = walk.run.screening
        chosen = next(o for o in g.outcomes if o.menu_index == g.chosen_index)
        print(f"  {len(walk.run.result.menu.portfolios)} candidates -> Agent B kept {len(sc.approved)} -> "
              f"{g.n_paths:,} paths each over {g.history_days} days of history ({len(g.backfilled)} fund(s) completed)")
        print(f"  chosen #{g.chosen_index}: {chosen.prob_goal:.0%} chance, median ${chosen.p50:,.0f}; {g.chosen_reason}")
        for c in walk.checks:
            print(f"  [{'pass' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
    html_path, json_path = save(walk, args.out, as_of)
    print(f"\nWalkthrough: {html_path} (and {json_path.name})")


def cmd_fetch(args: argparse.Namespace) -> None:
    """Save the real prices the chosen personas need (universe + watchlist) to one CSV.

    Each persona's universe is fetched within Agent C's data budget first (most relevant first), so
    one run stays inside Tiingo's free tier; run it again later to fill in what was left out.
    """
    keys = list(PERSONAS) if args.persona == "all" else [args.persona]
    bank, config = load_all()
    as_of = args.as_of or last_weekday_before(date.today())
    frames = []
    for key in keys:
        persona = PERSONAS[key]
        profile = build_risk_profile(persona.answers(), bank, config, assessed_at=datetime.now(timezone.utc))
        interests = InvestorInterests(investor_id=persona.investor_id, trading_platforms=persona.trading_platforms,
                                      held_assets=sorted(persona.current_holdings or {}),
                                      **interest_fields(persona), **to_agent_c_limits(profile, config))
        agent_c = MarketIntelligenceAgent(interests)
        lookback = max(1, int((args.days - 30) * 5 / 7))     # trading days whose window covers --days
        agent_c.fetch_universe(as_of, lookback)
        frames.append(agent_c.export_prices(agent_c.window_start(as_of, lookback), as_of))
    prices = pd.concat(frames, axis=1)
    prices = prices.loc[:, ~prices.columns.duplicated()]
    prices.rename_axis("date").reset_index().to_csv(args.out, index=False)
    print(f"Saved {prices.shape[1]} tickers, {len(prices)} rows to {args.out}. "
          f"Replay offline with: python -m egoquant advise <persona> --csv {args.out}")


# ==========================================================================
# behavior (Agent B on its synthetic investors)
# ==========================================================================
_PATTERNS = ["revealed_equity_share", "annual_turnover", "concentration_p90", "cost_of_panic",
             "disposition_effect", "return_chasing_share", "hype_buy_share", "implied_holding_years", "game_level"]


def cmd_behavior(args: argparse.Namespace) -> None:
    as_of = date(2026, 9, 30)
    agent_b = BehavioralTwinAgent(synthetic_behavioral_provider(seed=11))
    for key in args.profiles or list(SYNTHETIC_PROFILES):
        if key not in SYNTHETIC_PROFILES:
            raise SystemExit(f"unknown profile '{key}'. Choose from: {', '.join(SYNTHETIC_PROFILES)}")
        p = SYNTHETIC_PROFILES[key]
        context = InvestorContext(
            investor_id=p.investor_id, risk_class=p.risk_class, goal_type="growth",
            horizon_years=p.stated_horizon_years or 10, liquidity_need=0.05, min_cash_buffer=0.05,
            max_acceptable_loss=0.25, suitability_max_equity=0.80, max_asset_weight=0.40, **p.context_overrides)
        result = QuantAgent(SyntheticMarketAgent(seed=7), agent_b, AgentConfig()).run(context, as_of)
        report = agent_b.analyze(context.investor_id, result.menu)
        print("\n" + "=" * 86)
        print(f"{key.upper()}  [{report.data_source}]  questionnaire class {report.risk_class}")
        print(f"  {p.description}")
        print("=" * 86)
        print(f"  status: {report.status} | flags: {', '.join(report.flags) or 'none'}")
        for f in report.features:
            if f.name in _PATTERNS:
                shown = "n/a" if f.value is None else f"{f.value:.2f}"
                print(f"    {f.name:24s} {shown:>6s}   {f.note}")
        print("  limits sent to Agent A:" if report.constraints else "  limits sent to Agent A: none")
        for c in report.constraints:
            print(f"    {c.label:54s} {c.value:.3f}  hard={c.hard}  confidence={c.confidence}")
        print("  facts for the Advisor:")
        for fact in report.advisor_facts or ["(none)"]:
            print(f"    - {fact}")
        final = result.final
        print(f"  FINAL: {final.status} | equity {final.equity_weight:.1%} (class target {result.menu.target_equity:.0%})"
              f" | crash scenario {final.stress[report.stress_scenario]:+.1%} (hypothetical)")
        print(f"         ignored {final.ignored_constraints or 'none'} | relaxed {final.relaxed_constraints or 'none'}")


# ==========================================================================
# backtest (Agent A vs simple baselines on SYNTHETIC paths)
# ==========================================================================
def _backtest_one_path(seed: int, risk_class: str):
    prices = make_synthetic_history(seed=seed, n_days=1260, end_date=date(2026, 9, 30), crisis_start=800, crisis_len=70)
    assets = synthetic_asset_infos()
    provider = HistoricalMarketProvider(prices, assets)
    ids = list(prices.columns)
    context = InvestorContext(investor_id="backtest", risk_class=risk_class, goal_type="growth",
                              horizon_years=10, min_cash_buffer=0.05, max_acceptable_loss=0.30)
    full = AgentConfig(run_sensitivity=False)
    no_budget = AgentConfig(run_sensitivity=False, dynamic_risk_budget=False)
    policy = full.risk_class_policies[risk_class]
    strategies = {
        "Agent A (full)": make_agent_strategy(QuantAgent(provider, NoBehavioralEvidence(), full), context, ids),
        "Agent A (no dynamic budget)": make_agent_strategy(QuantAgent(provider, NoBehavioralEvidence(), no_budget),
                                                           context, ids),
        "Fixed mix (class target)": make_fixed_mix_strategy(assets, policy),
        "Inverse-vol within mix": make_inverse_vol_strategy(assets, policy),
        "Vol-target overlay on mix": make_vol_target_overlay_strategy(assets, policy),
        "Equal weight (1/N)": make_equal_weight_strategy(len(ids)),
    }
    return run_backtest(prices, assets, strategies, rebalance_days=21, first_rebalance_row=252, risk_free_annual=0.03)


def cmd_backtest(args: argparse.Namespace) -> None:
    results = [_backtest_one_path(100 + k, args.risk_class) for k in range(args.seeds)]
    mean = sum(r.metrics for r in results) / len(results)
    print(f"Walk-forward backtest, class {args.risk_class}, {args.seeds} SYNTHETIC path(s), monthly rebalancing,")
    print("costs charged on every change, first allocation free. Averages over paths where more than one.\n")
    table = mean.copy()
    for column in ["ann_return", "ann_vol", "max_drawdown", "cvar95_daily", "annual_turnover", "total_cost",
                   "avg_equity_weight"]:
        table[column] = table[column].map("{:.1%}".format)
    table["sharpe"] = mean["sharpe"].map("{:.2f}".format)
    print(table.rename(columns={"ann_return": "return/yr", "ann_vol": "vol/yr", "max_drawdown": "max DD",
                                "cvar95_daily": "CVaR95 (1d)", "annual_turnover": "turnover/yr",
                                "total_cost": "total cost", "avg_equity_weight": "avg equity"}).to_string())
    print("\nVolatility forecast check for Agent A (realized / predicted, 1.00 = unbiased):")
    for name in ["Agent A (full)", "Agent A (no dynamic budget)"]:
        ratios = [r.calibration[name]["realized_over_predicted"] for r in results if name in r.calibration]
        corr = pd.Series([r.calibration[name]["correlation"] for r in results if name in r.calibration]).mean()
        print(f"  {name:28s} ratio {sum(ratios) / len(ratios):.2f}   correlation with next-month realized vol {corr:+.2f}")
    print("\nCaveats: synthetic data, one crisis shape, simple baselines, no taxes, no market impact.")


# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="egoquant", description="EgoQuant wealth-advisory engine")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("personas", help="list the demo trader profiles").set_defaults(func=cmd_personas)

    options = sub.add_parser("options", help="the preference form a persona's risk profile allows")
    options.add_argument("persona", choices=sorted(PERSONAS))
    options.set_defaults(func=cmd_options)

    crypto = sub.add_parser("crypto-market", help="the whole Binance spot market for a crypto-eligible persona")
    crypto.add_argument("persona", choices=sorted(PERSONAS))
    crypto.add_argument("--as-of", type=date.fromisoformat, default=None)
    crypto.add_argument("--top", type=int, default=25, help="how many coins to list")
    crypto.set_defaults(func=cmd_crypto_market)

    advise = sub.add_parser("advise", help="full pipeline for one persona (real data by default)")
    advise.add_argument("persona", choices=sorted(PERSONAS))
    advise.add_argument("--as-of", type=date.fromisoformat, default=None)
    source = advise.add_mutually_exclusive_group()
    source.add_argument("--synthetic", action="store_true", help="INVENTED prices (offline)")
    source.add_argument("--csv", help="replay prices saved with `fetch`")
    advise.add_argument("--out", default="reports", help="directory for the client report")
    advise.add_argument("--briefing", action="store_true",
                        help="add the Agent D market briefing (sample news with --synthetic/--csv, else live)")
    advise.add_argument("--period", choices=["week", "month"], default=None,
                        help="briefing window (with --briefing; default week)")
    advise.set_defaults(func=cmd_advise)

    warm = sub.add_parser("warm-cache", help="fetch the long price history of every cluster's top funds ahead of time")
    warm.add_argument("--per-cluster", type=int, default=10, help="most traded funds per cluster to warm")
    warm.add_argument("--areas", default=None, help="comma-separated clusters (default: all)")
    warm.add_argument("--wait", action="store_true", help="on Tiingo's hourly limit, wait and resume")
    warm.add_argument("--max-requests", type=int, default=None, help="stop after this many new requests")
    warm.add_argument("--dry-run", action="store_true", help="show the plan only")
    warm.add_argument("--as-of", type=date.fromisoformat, default=None)
    warm.set_defaults(func=cmd_warm_cache)

    goal_demo = sub.add_parser("goal-demo", help="the goal-based path step by step, as an HTML walkthrough")
    goal_demo.add_argument("persona", choices=sorted(PERSONAS))
    goal_demo.add_argument("--goal", type=float, default=None, help="goal amount (default: 1.6x the money paid in)")
    goal_demo.add_argument("--monthly", type=float, default=None, help="monthly contribution (+) or withdrawal (-)")
    goal_demo.add_argument("--years", type=float, default=None, help="horizon (default: the persona's)")
    goal_demo.add_argument("--budget", type=int, default=40, help="most new Tiingo requests")
    goal_demo.add_argument("--synthetic", action="store_true", help="invented prices instead (offline)")
    goal_demo.add_argument("--csv", help="replay prices saved with `fetch`")
    goal_demo.add_argument("--as-of", type=date.fromisoformat, default=None)
    goal_demo.add_argument("--out", default="reports")
    goal_demo.set_defaults(func=cmd_goal_demo)

    directory = sub.add_parser("directory", help="Agent C's directory of every US stock and ETF, by cluster")
    directory.add_argument("--refresh", action="store_true", help="rebuild it now (Nasdaq + Tiingo)")
    directory.add_argument("--top", type=int, default=10, help="members shown per cluster")
    directory.set_defaults(func=cmd_directory)

    fetch = sub.add_parser("fetch", help="save real prices for offline replay")
    fetch.add_argument("persona", choices=sorted(PERSONAS) + ["all"])
    fetch.add_argument("--out", required=True)
    fetch.add_argument("--as-of", type=date.fromisoformat, default=None)
    fetch.add_argument("--days", type=int, default=900, help="calendar days of history")
    fetch.set_defaults(func=cmd_fetch)

    behavior = sub.add_parser("behavior", help="Agent B on its synthetic investors")
    behavior.add_argument("profiles", nargs="*")
    behavior.set_defaults(func=cmd_behavior)

    backtest = sub.add_parser("backtest", help="walk-forward backtest on synthetic paths")
    backtest.add_argument("--seeds", type=int, default=1)
    backtest.add_argument("--risk-class", default="C3", choices=RISK_CLASS_ORDER)
    backtest.set_defaults(func=cmd_backtest)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)
