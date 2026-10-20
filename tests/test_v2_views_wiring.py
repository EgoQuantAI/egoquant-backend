"""Portfolio method v2: the investor's views really reach Agent A through the pipeline.

Found in the integration probe of 19 Oct night: `pipeline._v2_views` passed the investor's PreferenceAnswers and Agent C's
selected instruments to build_views, which only understood other shapes, and the pipeline's own `except` turned the
TypeError into "no views" (a warning in the log, nothing in the result). A portfolio built without the investor's choices
looks fine and is wrong, so this pins the whole path. Offline, synthetic prices, no network.
"""

import logging
from datetime import date

import pytest

from egoquant.agents.market_intel import SyntheticPriceSource
from egoquant.demo.personas import PERSONAS, behavioral_agent_for
from egoquant.models import AssetInfo
from egoquant.pipeline import run_pipeline
from egoquant.preferences.views import assets_for_view


def run(persona_key: str):
    p = PERSONAS[persona_key]
    return run_pipeline(p.answers(), investor_id=p.investor_id, interest_areas=None, preferences=p.preference_answers(),
                        goal_type=p.goal_type, horizon_years=p.horizon_years, as_of=date(2026, 10, 16),
                        trading_platforms=p.trading_platforms, behavioral_agent=behavioral_agent_for(p),
                        price_source=SyntheticPriceSource(), min_cash_buffer=p.min_cash_buffer,
                        liquidity_need=p.liquidity_need, current_holdings=p.current_holdings, directory=None)


def test_under_v2_the_pipeline_gives_agent_a_the_investors_views(monkeypatch, caplog):
    monkeypatch.setenv("EGOQUANT_PORTFOLIO_METHOD", "v2")
    with caplog.at_level(logging.WARNING, logger="egoquant.pipeline"):
        result = run("growth_investor")                       # likes stocks, technology, healthcare, real estate, gold; avoids energy
    assert "views unavailable" not in caplog.text, caplog.text
    views = {(v.kind, v.key): v.annual_excess for v in result.context.views}
    assert views[("sector", "technology")] == pytest.approx(0.005)
    assert ("area", "gold") in views and ("area", "stocks") in views
    # the avoided industry (energy): Agent C already leaves its products out of the universe, so there is nothing to tilt
    offered_energy = assets_for_view("sector", "energy", result.selection.instruments)
    assert (("sector", "energy") in views) == bool(offered_energy)
    if offered_energy:
        assert views[("sector", "energy")] == pytest.approx(-0.005)
    assert len(views) <= 20


def test_every_view_of_the_pipeline_applies_to_a_product_agent_a_really_has(monkeypatch):
    """The views are built from Agent C's selection but matched against Agent A's AssetInfo: both must agree."""
    monkeypatch.setenv("EGOQUANT_PORTFOLIO_METHOD", "v2")
    result = run("crypto_trader")
    agent_a_assets = [AssetInfo(asset_id=i.ticker, asset_class=i.asset_class) for i in result.selection.instruments]
    assert result.context.views
    for v in result.context.views:
        assert assets_for_view(v.kind, v.key, agent_a_assets), (v.kind, v.key)


def test_under_v1_there_are_no_views_and_nothing_changes(monkeypatch):
    monkeypatch.delenv("EGOQUANT_PORTFOLIO_METHOD", raising=False)
    result = run("growth_investor")
    assert result.context.views == []
