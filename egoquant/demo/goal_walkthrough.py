"""Goal-based portfolio walkthrough: every step of how Agent A builds and picks the portfolio, made visible.

    python -m egoquant goal-demo <persona> [--goal 180000 --monthly 200 --years 15] [--synthetic]

Runs the real pipeline (questionnaire -> Agent C data -> 100 candidates -> Agent B shortlist -> Monte Carlo
-> choice -> Risk Engine) and writes one HTML page (and JSON) with:
  1. the inputs (goal, start amount, monthly flow, horizon, risk class, interests);
  2. the data: the history window and every short history completed (funds used, R^2);
  3. all candidates, Agent B's decision on each and why;
  4. how the simulation builds a path, decomposed for real paths (which past months it stitches together);
  5. every shortlisted portfolio's outcomes and the chosen portfolio's fan chart with sample paths;
  6. automatic checks that the pick really is the one that best meets the goal.

The page RE-DERIVES the simulation from the same inputs and seed and compares it with Agent A's result, so
the reproducibility check is real, not asserted. Everything shown is hypothetical, based on past data.
"""

from __future__ import annotations

import html
import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from egoquant.agents.market_intel import PriceSource
from egoquant.agents.quant import goal as gq
from egoquant.demo.personas import PERSONAS, behavioral_agent_for
from egoquant.pipeline import PipelineResult, run_pipeline
from egoquant.regime import hook as regime_hook


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class Walkthrough:
    persona: str
    spec: gq.GoalSpec
    run: PipelineResult
    daily: pd.DataFrame | None = None                # the completed daily returns the simulation used
    checks: list[Check] = field(default_factory=list)
    paths: list[dict] = field(default_factory=list)  # decomposed example paths


def default_goal(persona_key: str, goal_amount: float | None = None, monthly_flow: float | None = None,
                 years: float | None = None) -> gq.GoalSpec:
    """The persona's start amount and horizon; a goal of 1.6x the money paid in unless one is given."""
    p = PERSONAS[persona_key]
    years = years or p.horizon_years
    flow = 200.0 if monthly_flow is None else monthly_flow
    paid_in = p.amount_invested + flow * round(years * 12)
    return gq.GoalSpec(start_amount=p.amount_invested, monthly_flow=flow,
                       goal_amount=goal_amount or round(1.6 * paid_in, -3), horizon_years=years)


def run_walkthrough(persona_key: str, as_of: date, spec: gq.GoalSpec, price_source: PriceSource | None = None,
                    fetch_budget: dict[str, int] | None = None) -> Walkthrough:
    p = PERSONAS[persona_key]
    run = run_pipeline(p.answers(), investor_id=p.investor_id, interest_areas=None, preferences=p.preference_answers(),
                       goal_type=p.goal_type, horizon_years=spec.horizon_years, as_of=as_of,
                       trading_platforms=p.trading_platforms, behavioral_agent=behavioral_agent_for(p),
                       price_source=price_source, goal=spec, fetch_budget=fetch_budget,
                       min_cash_buffer=p.min_cash_buffer, liquidity_need=p.liquidity_need)
    walk = Walkthrough(persona=persona_key, spec=spec, run=run)
    if run.goal_analysis is not None:
        walk.daily, rerun = _rederive(run, as_of)
        walk.checks = verify(run, rerun)
        walk.paths = decompose_paths(run, walk.daily)
    return walk


def _rederive(run: PipelineResult, as_of: date) -> tuple[pd.DataFrame, gq.GoalAnalysis]:
    """The simulation again, from the same data and seed, independently of Agent A's call."""
    cfg = _config(run)
    menu, sc = run.result.menu, run.screening
    held = sorted({a for i in sc.approved for a, w in menu.portfolios[i].weights.items() if w > 1e-9})
    universe = run.agent_c.selection.tickers
    market = next((m for m in gq.MARKET_PROXIES if m in universe), None) if cfg.goal_hindsight_correction else None
    rate_proxy = gq.SHORT_RATE_PROXY if gq.RATE_ANCHOR and gq.SHORT_RATE_PROXY in universe else None
    wanted = sorted(set(held) | ({market} if market else set()) | ({rate_proxy} if rate_proxy else set()))
    prices = run.agent_c.get_price_history(wanted, as_of, cfg.goal_history_days)
    rate = gq.short_rate(prices[rate_proxy]) if rate_proxy in prices.columns else None
    wanted = [a for a in wanted if a in prices.columns and (a != rate_proxy or a in held or a == market)]
    daily, backfilled, note = gq.complete_history(prices[wanted], cfg.goal_block_days, cfg.goal_seed)
    adjustments: list = []
    classes = {i.ticker: i.asset_class for i in run.agent_c.selection.instruments}
    if market in daily.columns:
        daily, adjustments, _ = gq.equilibrium_means(daily, classes, market)
        if adjustments:
            note += (f" Hindsight correction: {len(adjustments)} stock, equity-fund or crypto average(s) "
                     f"set to beta x the {market} market average ({gq.EQUILIBRIUM_METHOD}).")
    rate_anchor = None
    if rate is not None:
        daily, rate_anchor = gq.anchor_rate_means(daily, classes, rate)
        if rate_anchor.assets:
            note += (f" Rate anchor: {len(rate_anchor.assets)} bond or cash average(s) set to the short "
                     f"rate at the decision date ({rate:.2%} a year, from {rate_proxy}; bonds plus "
                     f"{gq.BOND_TERM_PREMIUM:.1%}).")
    fees = {i.ticker: i.expense_ratio for i in run.agent_c.catalog if i.ticker in held}
    candidates = [(i, menu.portfolios[i].method, menu.portfolios[i].equity_weight or 0.0, menu.portfolios[i].weights)
                  for i in sc.approved]
    vols = {i: menu.portfolios[i].risk.ex_ante_vol for i in sc.approved if menu.portfolios[i].risk}
    rerun = gq.analyze_goal(daily, candidates, fees, vols, run.goal_analysis.spec, n_paths=cfg.goal_paths,
                            block_days=cfg.goal_block_days, seed=cfg.goal_seed, tie=cfg.goal_tie_tolerance,
                            backfilled=backfilled, history_note=note, mean_adjustments=adjustments,
                            sampler=_same_sampler(run, daily, as_of), rate_anchor=rate_anchor)
    return daily, rerun


def _same_sampler(run: PipelineResult, daily: pd.DataFrame, as_of: date):
    """The sampler the run itself used (None = the block bootstrap; the regime sampler unless EGOQUANT_REGIME_MC=0)."""
    cfg = _config(run)
    return regime_hook.decide(daily.index, cfg.goal_block_days, as_of, run.result.final.data_source,
                              forced=run.goal_analysis.sampler or {"sampler": regime_hook.SAMPLER_BOOTSTRAP})


def _config(run: PipelineResult):
    """The configuration the pipeline ran with (the demo uses Agent A's defaults)."""
    from egoquant.models import AgentConfig
    return AgentConfig()


# --------------------------------------------------------------------------
# Checks: is the chosen portfolio really the one that best meets the goal?
# --------------------------------------------------------------------------
def verify(run: PipelineResult, rerun: gq.GoalAnalysis) -> list[Check]:
    cfg = _config(run)
    g, sc, menu, final = run.goal_analysis, run.screening, run.result.menu, run.result.final
    checks: list[Check] = []
    n = len(menu.portfolios)
    cap = min(run.context.suitability_max_equity, cfg.risk_class_policies[run.context.risk_class].max_equity)
    over = [i for i, p in enumerate(menu.portfolios) if (p.equity_weight or 0.0) > cap + 1e-6]
    checks.append(Check("Candidates built inside the limits", n > 0 and not over,
                        f"{n} candidates (target {cfg.candidate_target}); none above the {cap:.0%} stock limit"
                        if not over else f"{len(over)} candidates above the stock limit: {over[:5]}"))
    k = len(sc.approved)
    checks.append(Check("Agent B shortlist size", 1 <= k <= 20 and (k >= 5 or k == sc.counts.get("candidates", k)),
                        f"{k} of {n} kept (target 5-20; fewer only if fewer pass); counts {sc.counts}"))
    best = max(o.prob_goal for o in g.outcomes)
    contenders = [o for o in g.outcomes if o.prob_goal >= best - cfg.goal_tie_tolerance - 1e-12]
    expected = min(contenders, key=lambda o: (o.worst5_shortfall, (menu.portfolios[o.menu_index].risk.ex_ante_vol
                                                                   if menu.portfolios[o.menu_index].risk else 0.0),
                                              o.menu_index))
    checks.append(Check("Chosen = highest chance of reaching the goal", expected.menu_index == g.chosen_index,
                        f"best chance {best:.2%}; {len(contenders)} within the {cfg.goal_tie_tolerance:.0%} tie band; "
                        f"among them the smallest worst-5% shortfall is #{expected.menu_index}; chosen #{g.chosen_index}"))
    chosen_w = menu.portfolios[g.chosen_index].weights
    same = all(abs(final.weights.get(a, 0.0) - w) < 1e-9 for a, w in chosen_w.items())
    checks.append(Check("Final portfolio = the chosen candidate", same,
                        "the proposal carries exactly the simulated weights" if same else "weights differ"))
    identical = [o.model_dump() for o in rerun.outcomes] == [o.model_dump() for o in g.outcomes]
    checks.append(Check("Reproducible (same data + seed -> same result)", identical and rerun.chosen_index == g.chosen_index,
                        f"re-ran {rerun.n_paths:,} paths for {len(rerun.outcomes)} portfolios with seed {rerun.seed}: "
                        + ("identical outcomes" if identical else "DIFFERENT outcomes")))
    p = g.outcomes[0].prob_goal
    se = math.sqrt(max(p * (1 - p), 1e-12) / g.n_paths)
    checks.append(Check("Simulation precision", se < cfg.goal_tie_tolerance,
                        f"standard error of the chosen chance {se:.2%} (95% interval +/-{1.96 * se:.2%}), below the "
                        f"{cfg.goal_tie_tolerance:.0%} tie band used to separate portfolios"))
    checks.append(Check("Enough history", g.history_days >= cfg.goal_min_history_days,
                        f"{g.history_days} trading days ({g.history_start} to {g.history_end}); minimum "
                        f"{cfg.goal_min_history_days}; {len(g.backfilled)} younger fund(s) completed"))
    checks.append(Check("Risk Engine approved the chosen portfolio", run.approved,
                        "independent re-check passed" if run.approved else "; ".join(run.review.violations)))
    return checks


# --------------------------------------------------------------------------
# How one path is built
# --------------------------------------------------------------------------
def decompose_paths(run: PipelineResult, daily: pd.DataFrame, months_shown: int = 24) -> list[dict]:
    """Rebuild the chosen portfolio's paths and explain a median one and a bad one, month by month."""
    cfg = _config(run)
    g, menu = run.goal_analysis, run.result.menu
    spec = g.spec
    blocks = gq.block_returns(daily, cfg.goal_block_days)
    regime = _same_sampler(run, daily, run.result.final.as_of)
    draws = regime.draws(len(blocks), cfg.goal_paths, spec.months, cfg.goal_seed) if regime is not None else None
    if draws is None:
        draws = np.random.default_rng(cfg.goal_seed).integers(0, len(blocks), size=(cfg.goal_paths, spec.months))
    weights = menu.portfolios[g.chosen_index].weights
    assets = list(daily.columns)
    w = np.array([weights.get(a, 0.0) for a in assets])
    fees = np.array([next((i.expense_ratio for i in run.agent_c.catalog if i.ticker == a), 0.0) for a in assets])
    wealth = gq.simulate_wealth(blocks, w, fees, spec, draws)
    final = wealth[:, -1]
    order = np.argsort(final)
    picks = {"a median path": int(order[len(order) // 2]), "a bad path (5th percentile)": int(order[len(order) // 20])}
    monthly = blocks @ w * (1.0 - float(w @ fees) / 12.0)
    out = []
    for label, path in picks.items():
        rows = []
        for m in range(min(months_shown, spec.months)):
            b = int(draws[path, m])
            start, end = daily.index[b], daily.index[min(b + cfg.goal_block_days - 1, len(daily) - 1)]
            rows.append({"month": m + 1, "block_start": str(start.date()), "block_end": str(end.date()),
                         "return": float(monthly[b] - 1.0), "wealth": float(wealth[path, m + 1])})
        worst = int(np.argmin(monthly[draws[path]]))
        wb = int(draws[path, worst])
        out.append({"label": label, "path": path, "final": float(final[path]), "reached": bool(final[path] >= spec.goal_amount),
                    "rows": rows, "worst_month": {"month": worst + 1, "block_start": str(daily.index[wb].date()),
                                                  "return": float(monthly[wb] - 1.0)}})
    return out


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------
STYLE = """
.viz-root{color-scheme:light;--surface-1:#fcfcfb;--surface-2:#f3f2ef;--text-primary:#0b0b0b;--text-secondary:#52514e;
--text-muted:#8a8984;--grid:#e4e3df;--series-1:#2a78d6;--series-2:#eb6834;--neutral:#b9b8b2;
--band-1:#d6e6f8;--band-2:#a9cbf0;--good:#008300;--bad:#e34948}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;--surface-1:#1a1a19;
--surface-2:#252523;--text-primary:#f0efec;--text-secondary:#c3c2b7;--text-muted:#8e8d86;--grid:#3a3a37;--series-1:#3987e5;
--series-2:#d95926;--neutral:#5f5e59;--band-1:#1f3550;--band-2:#2b4d78;--good:#2fa32f;--bad:#e66767}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface-1:#1a1a19;--surface-2:#252523;--text-primary:#f0efec;
--text-secondary:#c3c2b7;--text-muted:#8e8d86;--grid:#3a3a37;--series-1:#3987e5;--series-2:#d95926;--neutral:#5f5e59;
--band-1:#1f3550;--band-2:#2b4d78;--good:#2fa32f;--bad:#e66767}
body{margin:0;background:var(--surface-1)} .viz-root{background:var(--surface-1);color:var(--text-primary);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:26px;margin:0 0 4px} h2{font-size:19px;margin:40px 0 8px;border-top:2px solid var(--text-primary);padding-top:16px}
.muted{color:var(--text-secondary)} .small{font-size:13px} table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:5px 8px;border-bottom:1px solid var(--grid);text-align:right} th:first-child,td:first-child{text-align:left}
th{color:var(--text-secondary);font-weight:600} tr.chosen td{font-weight:700} .scroll{overflow-x:auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:12px 0}
.card{background:var(--surface-2);padding:12px 14px;border-radius:6px} .card b{display:block;font-size:22px}
.ok{color:var(--good);font-weight:700} .fail{color:var(--bad);font-weight:700} .legend span{margin-right:16px}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle}
svg text{fill:var(--text-secondary);font-size:11px} details{margin:8px 0}
"""


def _money(x: float) -> str:
    return f"${x:,.0f}"


def _scale(v: float, lo: float, hi: float, a: float, b: float) -> float:
    return a if hi <= lo else a + (v - lo) / (hi - lo) * (b - a)


def _scatter(run: PipelineResult) -> str:
    """Every candidate: severe-fall loss (x) vs expected volatility (y); role by colour + shape + legend."""
    menu, sc, g = run.result.menu, run.screening, run.goal_analysis
    pts = []
    for i, p in enumerate(menu.portfolios):
        loss = max(0.0, -p.stress.get(sc.stress_scenario, 0.0))
        vol = p.risk.ex_ante_vol if p.risk else 0.0
        role = "chosen" if i == g.chosen_index else "kept" if i in sc.approved else "rejected"
        pts.append((i, loss, vol, role, p.message.split(" | ")[0], sc.rejected.get(i, "")))
    w, h, l, b = 760, 340, 56, 40
    xs, ys = [p[1] for p in pts], [p[2] for p in pts]
    x0, x1, y0, y1 = 0.0, max(xs + [sc.band_upper]) * 1.05 or 0.01, 0.0, max(ys) * 1.08 or 0.01
    X = lambda v: _scale(v, x0, x1, l, w - 12)
    Y = lambda v: _scale(v, y0, y1, h - b, 12)
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Candidate portfolios">']
    for t in np.linspace(y0, y1, 5):
        parts.append(f'<line x1="{l}" x2="{w-12}" y1="{Y(t):.1f}" y2="{Y(t):.1f}" stroke="var(--grid)"/>'
                     f'<text x="{l-6}" y="{Y(t)+4:.1f}" text-anchor="end">{t:.0%}</text>')
    for t in np.linspace(x0, x1, 6):
        parts.append(f'<text x="{X(t):.1f}" y="{h-b+16}" text-anchor="middle">{t:.0%}</text>')
    for edge, name in ((sc.band_lower, "band from"), (sc.band_upper, "band to")):
        if edge > 0:
            parts.append(f'<line x1="{X(edge):.1f}" x2="{X(edge):.1f}" y1="12" y2="{h-b}" stroke="var(--text-muted)" '
                         f'stroke-dasharray="4 4"><title>Agent B comfort band {name} {edge:.1%}</title></line>')
    parts.append(f'<text x="{(l+w)/2}" y="{h-6}" text-anchor="middle">loss in the severe-fall scenario</text>'
                 f'<text x="14" y="{h/2}" transform="rotate(-90 14 {h/2})" text-anchor="middle">expected volatility</text>')
    for role in ("rejected", "kept", "chosen"):
        for i, loss, vol, r, label, why in pts:
            if r != role:
                continue
            tip = html.escape(f"#{i} {label}: loss {loss:.1%}, volatility {vol:.1%}"
                              + (f" - rejected: {why}" if why else " - shortlisted" if r == "kept" else " - CHOSEN"))
            if r == "rejected":
                parts.append(f'<circle cx="{X(loss):.1f}" cy="{Y(vol):.1f}" r="4" fill="var(--neutral)"><title>{tip}</title></circle>')
            elif r == "kept":
                parts.append(f'<circle cx="{X(loss):.1f}" cy="{Y(vol):.1f}" r="5" fill="var(--series-1)" '
                             f'stroke="var(--surface-1)" stroke-width="2"><title>{tip}</title></circle>')
            else:
                parts.append(f'<rect x="{X(loss)-7:.1f}" y="{Y(vol)-7:.1f}" width="14" height="14" rx="3" fill="var(--series-2)" '
                             f'stroke="var(--surface-1)" stroke-width="2"><title>{tip}</title></rect>')
    parts.append("</svg>")
    legend = ('<p class="legend small"><span><i class="dot" style="background:var(--neutral)"></i>rejected by Agent B</span>'
              '<span><i class="dot" style="background:var(--series-1)"></i>shortlisted</span>'
              '<span><i class="dot" style="background:var(--series-2);border-radius:3px"></i>chosen</span>'
              '<span class="muted">dashed: Agent B comfort band</span></p>')
    return legend + "".join(parts)


def _fan(g: gq.GoalAnalysis) -> str:
    """Chosen portfolio: 10-90 and 25-75 bands, median, sample paths, goal line (one y-axis, money)."""
    w, h, l, b = 760, 340, 76, 34
    months = g.spec.months
    top = max([f.p90 for f in g.fan] + [g.spec.goal_amount]) * 1.08
    X = lambda m: _scale(m, 0, months, l, w - 12)
    Y = lambda v: _scale(v, 0, top, h - b, 12)
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Range of simulated outcomes">']
    for t in np.linspace(0, top, 5):
        parts.append(f'<line x1="{l}" x2="{w-12}" y1="{Y(t):.1f}" y2="{Y(t):.1f}" stroke="var(--grid)"/>'
                     f'<text x="{l-6}" y="{Y(t)+4:.1f}" text-anchor="end">{_money(t)}</text>')
    for f in g.fan:
        parts.append(f'<text x="{X(f.year*12):.1f}" y="{h-b+16}" text-anchor="middle">{f.year:g}y</text>')
    for path in g.sample_paths:
        pts = " ".join(f"{X(m):.1f},{Y(v):.1f}" for m, v in enumerate(path))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="var(--neutral)" stroke-width="1" opacity="0.6"/>')
    def band(lo: str, hi: str, color: str, name: str) -> None:
        up = " ".join(f"{X(f.year*12):.1f},{Y(getattr(f, hi)):.1f}" for f in g.fan)
        down = " ".join(f"{X(f.year*12):.1f},{Y(getattr(f, lo)):.1f}" for f in reversed(g.fan))
        parts.append(f'<polygon points="{up} {down}" fill="{color}" opacity="0.75"><title>{name}</title></polygon>')
    band("p10", "p90", "var(--band-1)", "10th to 90th percentile")
    band("p25", "p75", "var(--band-2)", "25th to 75th percentile")
    med = " ".join(f"{X(f.year*12):.1f},{Y(f.p50):.1f}" for f in g.fan)
    parts.append(f'<polyline points="{med}" fill="none" stroke="var(--series-1)" stroke-width="2"/>')
    for f in g.fan:
        parts.append(f'<circle cx="{X(f.year*12):.1f}" cy="{Y(f.p50):.1f}" r="6" fill="transparent">'
                     f'<title>year {f.year:g}: median {_money(f.p50)}, 10-90% {_money(f.p10)} - {_money(f.p90)}</title></circle>')
    gy = Y(g.spec.goal_amount)
    parts.append(f'<line x1="{l}" x2="{w-12}" y1="{gy:.1f}" y2="{gy:.1f}" stroke="var(--text-primary)" stroke-dasharray="6 4"/>'
                 f'<text x="{w-14}" y="{gy-6:.1f}" text-anchor="end" style="fill:var(--text-primary)">goal {_money(g.spec.goal_amount)}</text>')
    parts.append("</svg>")
    legend = ('<p class="legend small"><span><i class="dot" style="background:var(--band-1)"></i>10th-90th percentile</span>'
              '<span><i class="dot" style="background:var(--band-2)"></i>25th-75th</span>'
              '<span><i class="dot" style="background:var(--series-1)"></i>median</span>'
              f'<span><i class="dot" style="background:var(--neutral)"></i>{len(g.sample_paths)} of the '
              f'{g.n_paths:,} simulated paths</span></p>')
    return legend + "".join(parts)


def render_html(walk: Walkthrough) -> str:
    run, spec = walk.run, walk.spec
    e = html.escape
    sec: list[str] = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
                      f"content='width=device-width,initial-scale=1'><title>Goal Walkthrough</title><style>{STYLE}</style>"
                      f"</head><body><div class='viz-root'>"]
    sec.append(f"<h1>How the portfolio is built and chosen: {e(walk.persona)}</h1>"
               f"<p class='muted'>Prices as of {run.result.final.as_of} &middot; {e(run.report.data_source)} data &middot; "
               f"{e(gq.LABEL)}</p>")
    # 1. inputs
    flow = spec.monthly_flow
    sec.append("<h2>1. The goal</h2><div class='cards'>"
               f"<div class='card'>Goal<b>{_money(spec.goal_amount)}</b></div>"
               f"<div class='card'>Starting amount<b>{_money(spec.start_amount)}</b></div>"
               f"<div class='card'>Every month<b>{'+' if flow >= 0 else '-'}{_money(abs(flow))}</b></div>"
               f"<div class='card'>Horizon<b>{spec.horizon_years:g} years</b></div>"
               f"<div class='card'>Risk class<b>{e(run.profile.c_level)}</b></div></div>"
               f"<p class='small muted'>Interests (Agent C's clusters): {e(', '.join(run.selection.interest_areas))}. "
               f"Universe Agent C priced: {len(run.selection.tickers)} instruments.</p>")
    g = run.goal_analysis
    if g is None:
        sec.append(f"<h2>Goal simulation not run</h2><p>{e(run.goal_unavailable or 'no goal given')}</p></div></body></html>")
        return "".join(sec)
    sc, menu = run.screening, run.result.menu
    # 2. data
    rows = "".join(f"<tr><td>{e(b.ticker)}</td><td>{b.own_start}</td><td>{b.filled_days:,}</td>"
                   f"<td>{e(', '.join(f'{f} ({beta:+.2f})' for f, beta in zip(b.factors, b.betas)))}</td>"
                   f"<td>{b.r_squared:.0%}</td></tr>" for b in g.backfilled)
    sec.append(f"<h2>2. The data the simulation resamples</h2><p>{e(g.history_note)}</p>"
               + (f"<details open><summary>{len(g.backfilled)} younger fund(s) completed ({e(gq.BACKFILL_METHOD)})</summary>"
                  "<div class='scroll'><table><tr><th>Fund</th><th>Real prices from</th><th>Days rebuilt</th>"
                  f"<th>Regressed on (beta)</th><th>R&sup2;</th></tr>{rows}</table></div>"
                  "<p class='small muted'>Each missing day = intercept + betas &middot; the long funds' actual returns that day "
                  "+ one of the fund's own residuals, drawn in blocks (no intercept: a short window's alpha is noise). "
                  "Real crashes reach the younger fund through its betas.</p>"
                  "</details>" if g.backfilled else ""))
    if g.mean_adjustments:
        arow = "".join(f"<tr><td>{e(m.ticker)}</td><td>{m.beta:.2f}</td><td>{m.sample_return:+.1%}</td>"
                       f"<td>{m.adjusted_return:+.1%}</td></tr>"
                       for m in sorted(g.mean_adjustments, key=lambda m: -(m.sample_return - m.adjusted_return)))
        sec.append(f"<details open><summary>Hindsight correction: {len(g.mean_adjustments)} stock, equity-fund or crypto "
                   f"average(s) set to beta x the {e(g.mean_adjustments[0].market)} market average</summary>"
                   "<p class='small muted'>These were picked from today's most traded list, so their own past returns are "
                   "those of known winners (survivorship / look-ahead bias). Each keeps its real day-to-day moves, "
                   "volatility, correlations and crashes; only its average return is replaced by what its market "
                   f"sensitivity earned over the same years ({e(gq.EQUILIBRIUM_METHOD)}).</p>"
                   "<div class='scroll'><table><tr><th>Asset</th><th>Beta</th><th>Sample average a year</th>"
                   f"<th>Used in the simulation</th></tr>{arow}</table></div></details>")
    # 3. candidates
    crow = []
    for i, p in enumerate(menu.portfolios):
        role = "chosen" if i == g.chosen_index else "shortlisted" if i in sc.approved else "rejected"
        crow.append(f"<tr class='{'chosen' if role == 'chosen' else ''}'><td>#{i} {e(p.message.split(' | ')[0])}</td>"
                    f"<td>{(p.equity_weight or 0):.0%}</td><td>{(p.risk.ex_ante_vol if p.risk else 0):.1%}</td>"
                    f"<td>{max(0.0, -p.stress.get(sc.stress_scenario, 0.0)):.1%}</td><td>{role}</td>"
                    f"<td style='text-align:left'>{e(sc.rejected.get(i, ''))}</td></tr>")
    sec.append(f"<h2>3. {len(menu.portfolios)} candidate portfolios &rarr; Agent B keeps {len(sc.approved)}</h2>"
               "<p>Agent A builds candidates along the risk axis in five styles (lowest risk, diversified, equal weight, "
               "risk-balanced, interest tilt); every one is inside the risk class and suitability limits. Agent B removes "
               "those that break its behavioural limits or fall outside the investor's comfort band.</p>"
               + _scatter(run)
               + "".join(f"<p class='small'>{e(f)}</p>" for f in sc.facts)
               + f"<p class='small muted'>Counts: {e(json.dumps(sc.counts))}</p>"
               "<details><summary>All candidates (table view)</summary><div class='scroll'><table><tr><th>Candidate</th>"
               "<th>Stocks</th><th>Volatility</th><th>Severe-fall loss</th><th>Decision</th><th>Why</th></tr>"
               + "".join(crow) + "</table></div></details>")
    # 4. method
    sec.append(f"<h2>4. How the Monte Carlo simulation builds a path</h2>"
               f"<p>{g.n_paths:,} paths per portfolio. Each month of a path is one block of {g.block_days} consecutive "
               f"real trading days, drawn at random (seed {g.seed}) from {g.history_start} to {g.history_end}; whole "
               "blocks keep crashes and rebounds clustered as they happened. Each month: grow by the block's return "
               "(rebalanced to the target weights), take off fund fees, add the monthly contribution (or withdrawal). "
               "Every portfolio runs on the SAME paths, so differences come from the portfolios, not luck.</p>")
    for d in walk.paths:
        body = "".join(f"<tr><td>{r['month']}</td><td>{r['block_start']} &rarr; {r['block_end']}</td>"
                       f"<td>{r['return']:+.2%}</td><td>{_money(r['wealth'])}</td></tr>" for r in d["rows"])
        wm = d["worst_month"]
        sec.append(f"<details><summary>Path #{d['path']}: {e(d['label'])}, ends at {_money(d['final'])} "
                   f"({'reaches' if d['reached'] else 'misses'} the goal); its worst month (month {wm['month']}) replays "
                   f"the block starting {wm['block_start']}: {wm['return']:+.1%}</summary><div class='scroll'><table>"
                   "<tr><th>Month</th><th>Past days it replays</th><th>Portfolio return</th><th>Wealth</th></tr>"
                   f"{body}</table></div><p class='small muted'>First {len(d['rows'])} months shown.</p></details>")
    # 5. results
    srow = []
    for rank, o in enumerate(g.outcomes, 1):
        p = menu.portfolios[o.menu_index]
        srow.append(f"<tr class='{'chosen' if o.menu_index == g.chosen_index else ''}'><td>{rank}. #{o.menu_index} "
                    f"{e(p.message.split(' | ')[0])}{' &larr; chosen' if o.menu_index == g.chosen_index else ''}</td>"
                    f"<td>{o.prob_goal:.1%}</td><td>{_money(o.p10)}</td><td>{_money(o.p50)}</td><td>{_money(o.p90)}</td>"
                    f"<td>{_money(o.worst5_shortfall)}</td><td>{o.drawdown_p90:.0%}</td><td>{o.prob_depleted:.1%}</td></tr>")
    c = next(o for o in g.outcomes if o.menu_index == g.chosen_index)
    sec.append("<h2>5. Results and the choice</h2><div class='cards'>"
               f"<div class='card'>Chance of reaching the goal<b>{c.prob_goal:.0%}</b></div>"
               f"<div class='card'>Median outcome<b>{_money(c.p50)}</b></div>"
               f"<div class='card'>10th-90th percentile<b style='font-size:17px'>{_money(c.p10)} - {_money(c.p90)}</b></div>"
               f"<div class='card'>Worst-5% shortfall<b>{_money(c.worst5_shortfall)}</b></div></div>"
               f"<p><b>{e(g.chosen_reason)}</b></p>" + _fan(g)
               + "<div class='scroll'><table><tr><th>Shortlisted portfolio (best first)</th><th>Chance</th><th>p10</th>"
               "<th>p50</th><th>p90</th><th>Worst-5% shortfall</th><th>Bad-case fall</th><th>Money runs out</th></tr>"
               + "".join(srow) + "</table></div>")
    holdings = sorted(run.result.final.weights.items(), key=lambda kv: -kv[1])
    sec.append("<details><summary>The chosen portfolio's holdings</summary><div class='scroll'><table><tr><th>Asset</th>"
               "<th>Weight</th></tr>" + "".join(f"<tr><td>{e(a)}</td><td>{w:.1%}</td></tr>" for a, w in holdings if w > 0.001)
               + "</table></div></details>")
    # 6. checks
    sec.append("<h2>6. Checks</h2><div class='scroll'><table><tr><th>Check</th><th>Result</th><th style='text-align:left'>"
               "Detail</th></tr>" + "".join(
                   f"<tr><td>{e(ch.name)}</td><td class='{'ok' if ch.passed else 'fail'}'>{'pass' if ch.passed else 'FAIL'}"
                   f"</td><td style='text-align:left'>{e(ch.detail)}</td></tr>" for ch in walk.checks) + "</table></div>")
    sec.append("</div></body></html>")
    return "".join(sec)


def to_json(walk: Walkthrough) -> dict:
    g = walk.run.goal_analysis
    return {"persona": walk.persona, "spec": walk.spec.model_dump(), "goal_unavailable": walk.run.goal_unavailable,
            "analysis": g.model_dump(mode="json") if g else None,
            "screening": {"approved": walk.run.screening.approved, "counts": walk.run.screening.counts,
                          "rejected": walk.run.screening.rejected} if walk.run.screening else None,
            "checks": [c.__dict__ for c in walk.checks], "example_paths": walk.paths}


def save(walk: Walkthrough, out_dir: str | Path, as_of: date) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"goal_walkthrough_{walk.persona}_{as_of}"
    html_path, json_path = stem.with_suffix(".html"), stem.with_suffix(".json")
    html_path.write_text(render_html(walk), encoding="utf-8")
    json_path.write_text(json.dumps(to_json(walk), indent=1, default=str), encoding="utf-8")
    return html_path, json_path
