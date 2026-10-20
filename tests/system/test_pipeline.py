"""Tests for the end-to-end pipeline: questionnaire -> Agent C -> Agent A (+ Agent B) -> Risk Engine -> report.

Run with:  pytest -v tests/system/test_pipeline.py      (SYNTHETIC prices; no internet needed)
"""

from datetime import date

import pytest

from egoquant.agents.behavioral import BehavioralTwinAgent
from egoquant.agents.market_intel import SyntheticPriceSource
from egoquant.demo.synthetic_investors import synthetic_behavioral_provider
from egoquant.models import DEFAULT_RISK_CLASS_POLICIES
from egoquant.pipeline import run_pipeline
from egoquant.risk_engine import RiskEngine
from tests.helpers import make_answers

AS_OF = date(2026, 9, 30)
R_LEVELS = RiskEngine().r_levels
# Grable & Lytton score per tolerance level (bands 13-18, 19-22, 23-28, 29-32, 33-47); capacity stays 5
CLASS_CASES = {"C1": dict(gl_score=15), "C2": dict(gl_score=20), "C3": dict(gl_score=25),
               "C4": dict(gl_score=30), "C5": dict(gl_score=40)}


def run(answers, **kw):
    kw.setdefault("interest_areas", ["stocks", "bonds"])
    return run_pipeline(answers, investor_id=kw.pop("investor_id", "t"), goal_type="growth", horizon_years=10,
                        as_of=AS_OF, price_source=SyntheticPriceSource(), min_cash_buffer=0.05, **kw)


@pytest.mark.parametrize("c_level", list(CLASS_CASES))
def test_every_class_gets_an_approved_portfolio_within_its_limits(c_level):
    out = run(make_answers(**CLASS_CASES[c_level]))
    final = out.result.final
    policy = DEFAULT_RISK_CLASS_POLICIES[c_level]
    assert out.profile.c_level == c_level
    assert final.status == "optimal" and out.approved, out.review.violations
    held = {a for a, w in final.weights.items() if w > 1e-6}
    assert all(R_LEVELS[a] <= out.profile.max_r_level for a in held)
    assert (final.equity_weight or 0.0) <= policy.max_equity + 1e-6
    # even the harshest hypothetical scenario stays within the class loss limit
    assert -min(final.stress.values()) <= policy.max_stress_loss + 1e-6, final.stress
    assert out.report.risk_review_approved is True
    assert not any("NOT APPROVED" in b for b in out.report.banners)


def test_c1_and_c2_get_different_products():
    c1 = run(make_answers(**CLASS_CASES["C1"])).selection.tickers
    c2 = run(make_answers(**CLASS_CASES["C2"])).selection.tickers
    assert set(c1) < set(c2)
    assert all(R_LEVELS[t] == 1 for t in c1) and any(R_LEVELS[t] == 2 for t in c2)


def test_no_loss_gate_investor_holds_only_assets_that_do_not_fall():
    out = run(make_answers(gl_score=30, D1="no"))
    assert out.profile.no_loss_gate and out.approved
    assert out.result.final.stress["equity_down_30_bond_down_5"] >= -1e-6


def test_behavioral_limit_reaches_the_report_and_is_rechecked():
    # sara (synthetic): one panic sale + cautious game -> soft crash-loss limit from Agent B
    out = run(make_answers(gl_score=25), investor_id="sara",
              behavioral_agent=BehavioralTwinAgent(synthetic_behavioral_provider(seed=11)))
    limit = next(c for c in out.result.behavioral_constraints if c.type == "max_stress_loss")
    assert out.approved
    assert out.result.final.stress[limit.scenario] >= -limit.value - 1e-6
    assert any(c.name == "Loss in a severe fall, behavioural limit" for c in out.report.limits)
    lowered = next(r for r in out.report.reasons if r.text.startswith("We lowered the share in stocks"))
    assert lowered.kind == "behavioral" and "past choices" in lowered.text


def test_risk_engine_checks_suitability_class_and_hard_behavioral_limits():
    out = run(make_answers(gl_score=25))
    sources = {c.source for c in out.review_constraints}
    assert {"suitability", "risk_class"} <= sources
    assert all(c.hard for c in out.review_constraints)


def test_caller_cannot_loosen_suitability_fields():
    with pytest.raises(ValueError, match="risk questionnaire"):
        run(make_answers(gl_score=25), max_acceptable_loss=0.9)
    with pytest.raises(ValueError, match="risk questionnaire"):
        run(make_answers(gl_score=15), risk_class="C5")


def test_caller_exclusions_are_added_to_the_suitability_exclusions():
    out = run(make_answers(gl_score=25), excluded_assets=["VT"])
    assert "VT" in out.context.excluded_assets
    assert out.result.final.weights.get("VT", 0.0) < 1e-9


# --------------------------------------------------------------------------
# Expanded universe: gold, commodities, crypto and the class caps (C1..C5)
# --------------------------------------------------------------------------
ALL_AREAS = ["stocks", "bonds", "gold", "precious_metals", "commodities", "crypto", "real_estate"]


@pytest.mark.parametrize("c_level", list(CLASS_CASES))
def test_every_class_respects_its_commodity_crypto_and_cash_caps(c_level):
    out = run(make_answers(**CLASS_CASES[c_level], K4=["stocks", "crypto"]), interest_areas=ALL_AREAS,
              trading_platforms=["IBKR", "Binance"])
    policy = DEFAULT_RISK_CLASS_POLICIES[c_level]
    final = out.result.final
    assert final.status == "optimal" and out.approved, out.review.violations
    classes = {a.ticker: a.asset_class for a in out.selection.instruments}
    weight = lambda cls: sum(w for a, w in final.weights.items() if classes[a] == cls)
    assert weight("commodity") <= policy.max_commodity + 1e-6
    assert weight("crypto") <= policy.max_crypto + 1e-6
    assert weight("cash") <= max(policy.max_cash, 0.05) + 1e-6
    reviewed = {c.label for c in out.review_constraints if c.type == "max_class_weight"}
    assert {f"risk_class:max_class_weight[{cls}]" for cls in ("commodity", "crypto")} <= reviewed or c_level in ("C1", "C2")


def test_what_each_class_may_trade():
    tickers = {c: set(run(make_answers(**CLASS_CASES[c]), interest_areas=ALL_AREAS,
                          trading_platforms=["IBKR"]).selection.tickers) for c in CLASS_CASES}
    assert not {"IAU", "SLV", "PDBC", "IBIT"} & (tickers["C1"] | tickers["C2"])   # no gold below C3
    assert "IAU" in tickers["C3"] and not {"SLV", "PDBC", "IBIT"} & tickers["C3"]   # gold (R3) from C3
    assert {"IAU", "SLV", "PDBC"} <= tickers["C4"] and "IBIT" not in tickers["C4"]  # silver/commodities R4
    assert {"IAU", "SLV", "PDBC", "IBIT"} <= tickers["C5"]                          # crypto R5: C5 only


def test_crypto_needs_product_knowledge_even_in_c5():
    out = run(make_answers(gl_score=40, K1="dk", K2="dk", K3="dk", K5="none", K4=["none"]),
              interest_areas=["stocks", "crypto"], trading_platforms=["IBKR"])
    assert out.profile.c_level == "C5" and out.profile.product_access == "blocked_complex"
    assert "IBIT" not in out.selection.tickers and "IBIT" in out.selection.watchlist_tickers
    assert any("crypto" in n and "not available" in n for n in out.report.interest_notes)


def test_investor_cash_need_overrides_the_class_cash_cap():
    # C5 caps cash at 15%, but this investor needs 40% in cash: liquidity comes first.
    out = run_pipeline(make_answers(gl_score=40), investor_id="t", interest_areas=["stocks"], goal_type="growth",
                       horizon_years=10, as_of=AS_OF, price_source=SyntheticPriceSource(), min_cash_buffer=0.40)
    assert out.approved and out.result.final.weights["BIL"] >= 0.40 - 1e-6
    cash_cap = next(c for c in out.review_constraints if c.label == "risk_class:max_class_weight[cash]")
    assert cash_cap.value == pytest.approx(0.40)


def test_report_explains_every_interest_area():
    out = run(make_answers(gl_score=25), interest_areas=["stocks", "gold", "crypto"], trading_platforms=["IBKR"])
    notes = out.report.interest_notes
    assert len(notes) == 3 and all(n.startswith("You asked about") for n in notes)
    assert "R5" in next(n for n in notes if "crypto" in n)              # C3: crypto is above R3


# --------------------------------------------------------------------------
# Preference form -> Agent C -> Agent A
# --------------------------------------------------------------------------
def _prefs(**choices):
    from egoquant.preferences import PreferenceAnswers, load_preference_bank

    return PreferenceAnswers(preferences_version=load_preference_bank().preferences_version, choices=choices)


def run_with_prefs(answers, prefs, **kw):
    return run_pipeline(answers, investor_id="t", interest_areas=None, preferences=prefs, goal_type="growth",
                        horizon_years=10, as_of=AS_OF, price_source=SyntheticPriceSource(), min_cash_buffer=0.05, **kw)


def test_preferences_decide_what_agent_c_sends_to_agent_a():
    out = run_with_prefs(make_answers(gl_score=30), _prefs(P1=["stocks", "gold"], P2=["technology"], P4=["mix"]))
    assert out.profile.c_level == "C4" and out.approved, out.review.violations
    assert out.preference_form is not None and out.selection.interest_areas == ["stocks", "technology", "gold"]
    assert {"QQQ", "AAPL", "IAU"} <= set(out.selection.tickers)
    held_stock = [a for a, w in out.result.final.weights.items() if a in {"AAPL", "MSFT", "NVDA", "AVGO", "ORCL"}]
    assert all(out.result.final.weights[a] <= 0.10 + 1e-6 for a in held_stock)


def test_an_option_the_profile_does_not_offer_is_refused_before_any_data_is_fetched():
    with pytest.raises(ValueError, match="not available"):
        run_with_prefs(make_answers(gl_score=25), _prefs(P1=["stocks", "crypto"]))     # C3: no crypto


def test_interest_areas_or_preferences_exactly_one():
    with pytest.raises(ValueError, match="exactly one"):
        run_pipeline(make_answers(gl_score=25), investor_id="t", interest_areas=["stocks"],
                     preferences=_prefs(P1=["stocks"]), goal_type="growth", horizon_years=10, as_of=AS_OF,
                     price_source=SyntheticPriceSource())


def _fixture_briefing(*, interests, selection, proposal, as_of, horizon_years):
    from egoquant.briefing.build import build_briefing
    from egoquant.briefing.fetch import FixtureAdapter, fixture_as_of
    from egoquant.briefing.loader import FIXTURE_SCORES, read_json_cache
    from egoquant.briefing.tone import CachedToneScorer
    from tests.briefing.common import config, summarizer
    cfg = config()
    scorer = CachedToneScorer(None, cfg.tone.model, precomputed=read_json_cache(FIXTURE_SCORES))
    return build_briefing(items=FixtureAdapter().fetch(fixture_as_of()).items, as_of=fixture_as_of(),
                          interests=interests, horizon_years=horizon_years, config=cfg, scorer=scorer,
                          summarizer=summarizer(cfg), proposal=proposal, selection=selection)


def test_briefing_builder_attaches_a_briefing_and_keeps_every_number():
    answers = make_answers(**CLASS_CASES["C4"])
    plain = run(answers)
    with_briefing = run(answers, briefing_builder=_fixture_briefing)
    assert plain.report.market_briefing is None
    assert with_briefing.report.market_briefing is not None
    assert with_briefing.result.final.weights == plain.result.final.weights
    assert with_briefing.approved == plain.approved


def test_a_failing_briefing_builder_never_breaks_the_portfolio():
    def broken(**_):
        raise RuntimeError("news feed down")
    out = run(make_answers(**CLASS_CASES["C3"]), briefing_builder=broken)
    assert out.approved and out.report.market_briefing is None


def test_an_area_whose_prices_failed_says_so_not_that_the_platform_lacks_it():
    from egoquant.agents.market_intel.agent import ExcludedInstrument
    from egoquant.report import _nothing_offered
    sel = run(make_answers(**CLASS_CASES["C3"])).selection
    failed = sel.model_copy(update={"queues": {**sel.queues, "gold": ["IAU"]},
                                    "not_fetched": [ExcludedInstrument(ticker="IAU", reason="rate limit reached")]})
    assert "could not load prices" in _nothing_offered(failed, "gold") and "rate limit" in _nothing_offered(failed, "gold")
    empty = sel.model_copy(update={"queues": {}, "not_fetched": [], "trading_platforms": []})
    assert _nothing_offered(empty, "gold") == "nothing in our product list matches it"
    on_platform = empty.model_copy(update={"trading_platforms": ["interactive_brokers"]})
    assert "trading platform" in _nothing_offered(on_platform, "gold")
