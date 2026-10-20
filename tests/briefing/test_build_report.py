"""Whole briefing on the sample (fixture) items, read-only guarantees, and the client report section."""

import copy
import html
import re
from datetime import date

import pytest

from egoquant.agents.market_intel import InvestorInterests, SyntheticPriceSource
from egoquant.briefing.advice_filter import AdviceFilter
from egoquant.briefing.build import build_briefing
from egoquant.briefing.fetch import FixtureAdapter, fixture_as_of
from egoquant.briefing.loader import FIXTURE_SCORES, read_json_cache
from egoquant.briefing.models import DEMO_BANNER, LABELS, NO_AI_SUMMARIES_LABEL

TEMPLATE_LABELS = [NO_AI_SUMMARIES_LABEL, *LABELS[1:]]   # these tests use template summaries only
from egoquant.briefing.render import briefing_lines, briefing_section_html
from egoquant.briefing.tone import CachedToneScorer
from egoquant.demo.personas import PERSONAS
from egoquant.pipeline import run_pipeline
from egoquant.report import attach_briefing, render_html
from tests.briefing.common import config, summarizer

CFG = config()
AREAS = ["stocks", "technology", "real_estate", "gold", "commodities", "crypto", "bonds"]


@pytest.fixture(scope="module")
def run():
    """The demo investor's portfolio on SYNTHETIC prices, computed before and independently of the briefing."""
    p = PERSONAS["growth_investor"]
    return run_pipeline(p.answers(), investor_id=p.investor_id, interest_areas=AREAS, goal_type=p.goal_type,
                        horizon_years=p.horizon_years, as_of=date(2026, 10, 16),
                        trading_platforms=p.trading_platforms, price_source=SyntheticPriceSource())


def fixture_briefing(run, cfg=CFG):
    interests = InvestorInterests(investor_id=run.context.investor_id, interest_areas=AREAS,
                                  max_r_level=run.profile.max_r_level)
    scorer = CachedToneScorer(None, cfg.tone.model, precomputed=read_json_cache(FIXTURE_SCORES),
                              neutral_gating=cfg.tone.neutral_gating)
    return build_briefing(items=FixtureAdapter().fetch(fixture_as_of()).items, as_of=fixture_as_of(),
                          interests=interests, horizon_years=15, config=cfg, scorer=scorer, summarizer=summarizer(cfg),
                          proposal=run.result.final, selection=run.selection)


def test_fixture_briefing_is_deterministic(run):
    assert fixture_briefing(run).model_dump_json() == fixture_briefing(run).model_dump_json()


def test_fixture_briefing_content(run):
    b = fixture_briefing(run)
    assert b.data_source == "fixture" and b.banners[0] == DEMO_BANNER and b.labels == TEMPLATE_LABELS
    assert b.metadata.n_fetched == 71 and b.metadata.n_llm_calls == 0
    assert b.metadata.n_tone_unavailable == 0                     # committed FinBERT scores cover every item
    assert all(a.action == "offer_risk_profile_recheck" for a in b.alerts)
    assert "VNQ" in {a.subject for a in b.alerts}                 # the real-estate story in the sample data
    assert any(n.area == "crypto" for n in b.suitability_notes)  # crypto (R5) above this investor's R4
    assert any(i.summary_kind == "withheld" for i in b.items)    # the "strong buy" headline
    held = [i for i in b.items if i.exposure]
    assert held and held[0].exposure == max(i.exposure for i in held)


def test_briefing_never_mutates_its_inputs(run):
    before = (copy.deepcopy(run.result.final.model_dump()), copy.deepcopy(run.context.model_dump()),
              copy.deepcopy(run.selection.model_dump()), copy.deepcopy(run.review.model_dump()))
    fixture_briefing(run)
    after = (run.result.final.model_dump(), run.context.model_dump(), run.selection.model_dump(), run.review.model_dump())
    assert before == after


# ---- report ---------------------------------------------------------------------------------------
def test_report_renders_without_a_briefing(run):
    page = render_html(run.report)
    assert "Agent D" not in page and "Market Briefing" not in page and run.report.market_briefing is None


def test_report_with_briefing_keeps_every_number(run):
    with_b = attach_briefing(fixture_briefing(run), run.report)
    a, b = run.report.model_dump(), with_b.model_dump()
    for k in ("market_briefing", "banners"):
        a.pop(k), b.pop(k)
    assert a == b
    assert with_b.banners[0] == DEMO_BANNER
    page = render_html(with_b)
    # the download leaves the news and its banner out (Bideci, 20 Oct: they live on the News tab)
    assert "Market Briefing" not in page and "Agent D" not in page and DEMO_BANNER not in page
    section = briefing_section_html(with_b.market_briefing)
    assert "<h2>Market Briefing (read-only)</h2>" in section and "Agent D" not in section
    for label in TEMPLATE_LABELS:
        assert html.escape(label) in section


def test_external_text_is_escaped_and_links_are_safe(run):
    b = fixture_briefing(run)
    first = b.items[0]
    evil_item = first.item.model_copy(update={"source_name": "<script>alert(1)</script>",
                                              "url": "javascript:alert(1)"})
    b.items[0] = first.model_copy(update={"item": evil_item, "summary": "<img src=x onerror=alert(1)>"})
    html = briefing_section_html(b)
    assert "<script>" not in html and "<img" not in html and "javascript:" not in html
    assert "&lt;script&gt;" in html and "href='#'" in html


def test_final_rendered_text_never_contains_forbidden_words(run):
    b = fixture_briefing(run)
    b.items[0] = b.items[0].model_copy(update={"summary": "Readers should buy this fund; price target 120."})
    b.alerts[0] = b.alerts[0].model_copy(update={"message": "Prices will rise. We recommend selling."})
    html = briefing_section_html(b)
    text = re.sub(r"<[^>]+>", " ", html)
    f = AdviceFilter(CFG.forbidden_patterns)
    assert f.is_clean(text), f.hits(text)
    assert all(f.is_clean(line) for line in briefing_lines(fixture_briefing(run)))
