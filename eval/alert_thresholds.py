"""How many tone alerts would fire for different alerts.tone_change_threshold values? (No API calls.)

    python eval/alert_thresholds.py

(a) The offline sample briefing (fixture items, the demo investor's real weights, fund-aware labels from the
    committed cache): alerts that would fire (change >= threshold, enough coverage, exposure > minimum).
(b) The 120 hand-labeled evaluation headlines, grouped by fund and publication date into this week vs last week
    (as of the newest headline), as if every fund were held: funds whose tone change crosses the threshold.
    Fund-aware labels come from the evaluation cache; FinBERT runs locally for comparison. Dates come from
    eval/headlines_to_label.csv (gitignored, local).
Nothing is written; per-headline data is not printed.
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

THRESHOLDS = (0.3, 0.4, 0.5, 0.6)


def count_alerts(aggregates, config, weights, catalog, threshold: float) -> int:
    from egoquant.briefing import aggregate as agg
    cfg = config.model_copy(deep=True)
    cfg.alerts.tone_change_threshold = threshold
    return len(agg.alerts_for(aggregates, cfg, weights, catalog))


def fixture_table() -> list[tuple[float, int]]:
    from egoquant.agents.market_intel import DEFAULT_CATALOG, InvestorInterests, SyntheticPriceSource
    from egoquant.briefing.__main__ import DEMO_EXTRA_AREAS, DEMO_PERSONA, fund_tone_labeler
    from egoquant.briefing.advice_filter import AdviceFilter
    from egoquant.briefing.build import build_briefing
    from egoquant.briefing.fetch import FixtureAdapter, fixture_as_of
    from egoquant.briefing.loader import FIXTURE_SCORES, load_config, read_json_cache
    from egoquant.briefing.summarize import Summarizer
    from egoquant.briefing.tone import CachedToneScorer
    from egoquant.demo.personas import PERSONAS, interest_fields
    from egoquant.pipeline import run_pipeline

    config = load_config()
    config.llm.enabled = False
    persona = PERSONAS[DEMO_PERSONA]
    as_of = fixture_as_of()
    areas = list(dict.fromkeys([*interest_fields(persona)["interest_areas"], *DEMO_EXTRA_AREAS]))
    run = run_pipeline(persona.answers(), investor_id=persona.investor_id, interest_areas=areas,
                       goal_type=persona.goal_type, horizon_years=persona.horizon_years, as_of=as_of.date(),
                       trading_platforms=persona.trading_platforms, price_source=SyntheticPriceSource())
    interests = InvestorInterests(investor_id=persona.investor_id, interest_areas=areas, max_r_level=run.profile.max_r_level)
    b = build_briefing(items=FixtureAdapter().fetch(as_of).items, as_of=as_of, interests=interests,
                       horizon_years=persona.horizon_years, config=config,
                       scorer=CachedToneScorer(None, config.tone.model, precomputed=read_json_cache(FIXTURE_SCORES),
                                               neutral_gating=config.tone.neutral_gating),
                       summarizer=Summarizer(config, None, AdviceFilter(config.forbidden_patterns)),
                       proposal=run.result.final, selection=run.selection,
                       fund_tone=fund_tone_labeler(config, fixture=True, allow_llm=False))
    weights = dict(run.result.final.weights)
    aggs = b.instrument_aggregates + b.area_aggregates
    return [(t, count_alerts(aggs, config, weights, DEFAULT_CATALOG, t)) for t in THRESHOLDS]


def eval_table() -> dict[str, list[tuple[float, int]]] | None:
    from egoquant.briefing import aggregate as agg
    from egoquant.briefing.fetch import item_id
    from egoquant.briefing.loader import load_config, read_json_cache
    from egoquant.briefing.models import BriefingItem, FundTone, NewsItem
    from egoquant.briefing.tone import FinBertScorer, top_class
    import run_fund_aware_llm as fa

    dated = ROOT / "eval" / "headlines_to_label.csv"
    labeled = ROOT / "eval" / "headlines_labeled.csv"
    if not dated.exists() or not labeled.exists():
        print("(b) skipped: eval/headlines_to_label.csv or eval/headlines_labeled.csv not found (local, gitignored)")
        return None
    config = load_config()
    dates = {r["id"]: r for r in csv.DictReader(open(dated, encoding="utf-8"))}
    rows = [r for r in csv.DictReader(open(labeled, encoding="utf-8")) if r["id"] in dates]
    fa_pred = fa.predictions(rows, read_json_cache(fa.CACHE_PATH), fa.load_prompt()[2])
    fb = FinBertScorer(config.tone.model).score([r["headline"] for r in rows])
    as_of = max(datetime.combine(datetime.fromisoformat(dates[r["id"]]["date"]).date(), time(12), tzinfo=timezone.utc)
                for r in rows) + timedelta(hours=1)
    out: dict[str, list[tuple[float, int]]] = {}
    for method in ("fund-aware LLM", "FinBERT"):
        items: dict[str, list[BriefingItem]] = {}
        for r, s in zip(rows, fb):
            tickers = dates[r["id"]]["matched_tickers"].split()
            when = datetime.combine(datetime.fromisoformat(dates[r["id"]]["date"]).date(), time(12), tzinfo=timezone.utc)
            news = NewsItem(id=r["id"], title=r["headline"], url=f"https://eval.invalid/{item_id('eval', r['id'])}",
                            published_at=when, source_name=dates[r["id"]]["source"], source_type="outlet",
                            fetched_at=as_of, data_source="live")
            for t in tickers:
                if method == "fund-aware LLM":
                    lab = fa_pred[r["id"]]
                    if lab is None:
                        continue
                    ft = FundTone(ticker=t, label=lab, value=config.tone.llm_values[lab], method="llm_fund_aware")
                else:
                    ft = FundTone(ticker=t, label=top_class(s.positive, s.negative, s.neutral), value=s.tone,
                                  method="finbert")
                items.setdefault(t, []).append(BriefingItem(item=news, matched_tickers=[t], matched_areas=[],
                                                            fund_tones=[ft], summary="", summary_kind="template"))
        aggs = [agg.aggregate(t, "instrument", its, as_of, 1.0, config, tickers=[t]) for t, its in items.items()]
        comparable = [a for a in aggs if a.change is not None]
        out[method] = [(th, sum(abs(a.change) >= th for a in comparable)) for th in THRESHOLDS]
        out[method + " (funds comparable)"] = [(0, len(comparable))]
    return out


def main() -> int:
    print("(a) offline sample briefing, demo investor (alerts that would fire):")
    for t, n in fixture_table():
        print(f"    threshold {t:.1f}: {n} alert(s)")
    table = eval_table()
    if table:
        print("(b) 120 evaluation headlines by fund, this week vs last week, every fund treated as held:")
        for method in ("fund-aware LLM", "FinBERT"):
            comparable = table[method + " (funds comparable)"][0][1]
            print(f"    {method} ({comparable} funds with enough coverage in both weeks): "
                  + ", ".join(f"{t:.1f} -> {n}" for t, n in table[method]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
