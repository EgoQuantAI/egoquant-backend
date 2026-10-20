"""Preferences -> Black-Litterman views for portfolio method v2 (egoquant/preferences/views.py, docs/PORTFOLIO_V2.md).

Run with:  pytest -v tests/preferences/test_views.py      (no network, no randomness except seeded `random`)
"""

import random

import pytest

from egoquant.agents.market_intel import DEFAULT_CATALOG, InvestorInterests
from egoquant.agents.market_intel.agent import MarketIntelligenceAgent
from egoquant.demo.personas import PERSONAS, interest_fields
from egoquant.models import AssetInfo, Constraint, PortfolioView
from egoquant.preferences.form import AREA_PRIORITY
from egoquant.preferences.views import (MAX_VIEWS, VIEW_CONFIDENCE, VIEW_EXCESS, assets_for_view, build_views,
                                        build_views_with_notes, normalize_key)


def info(asset_id, asset_class="equity", **meta) -> AssetInfo:
    return AssetInfo(asset_id=asset_id, asset_class=asset_class, **meta)


UNIVERSE = [
    info("VT", area="stocks"),
    info("VTI", area="stocks, us_stocks", region="US"),
    info("VXUS", area="stocks,international_stocks", region="Developed ex-US"),
    info("VWO", area="emerging_markets", region="Emerging"),
    info("QQQ", area="technology", sector="technology"),
    info("AAPL", area="stocks", sector="technology", product_type="stock"),
    info("XLE", area="energy", sector="energy"),
    info("XLV", area="healthcare", sector="healthcare"),
    info("AGG", "bond", area="bonds"),
    info("SHY", "bond", area="government_bonds"),
    info("BIL", "cash", area="cash"),
    info("IAU", "commodity", area="gold"),
    info("ZZCRYPTO", "crypto", area="crypto"),
    info("ZZNOMETA", "crypto"),                                       # no metadata at all
]


def prefs(areas=(), avoid=()):
    return {"interest_areas": list(areas), "avoid_sectors": list(avoid), "instrument_style": "funds",
            "wants_individual_bonds": False}


def keyset(views):
    return [(v.kind, v.key, v.annual_excess, v.confidence) for v in views]


# ==========================================================================
# 1. The rules of the brief
# ==========================================================================
def test_each_chosen_area_is_one_view_of_half_a_percent_at_confidence_030():
    views = build_views(prefs(["stocks", "bonds", "gold"]), [], [], UNIVERSE)
    assert keyset(views) == [("area", "stocks", 0.005, 0.30), ("area", "bonds", 0.005, 0.30),
                             ("area", "gold", 0.005, 0.30)]
    assert (VIEW_EXCESS, VIEW_CONFIDENCE, MAX_VIEWS) == (0.005, 0.30, 20)


def test_an_avoided_industry_is_minus_half_a_percent():
    views = build_views(prefs(["stocks"], avoid=["energy"]), [], [], UNIVERSE)
    assert keyset(views) == [("area", "stocks", 0.005, 0.30), ("sector", "energy", -0.005, 0.30)]


def test_industries_are_sector_views_and_market_areas_use_the_region_label_when_the_area_label_is_missing():
    views = build_views(prefs(["technology", "us_stocks", "emerging_markets"]), [], [], UNIVERSE)
    assert [(v.kind, v.key) for v in views] == [("sector", "technology"), ("area", "us_stocks"),
                                                ("area", "emerging_markets")]
    no_area_label = [info("ZZUS", region="US"), info("ZZEM", region="emerging"), info("ZZBOND", "bond")]
    views = build_views(prefs(["us_stocks", "emerging_markets", "international_stocks"]), [], [], no_area_label)
    assert [(v.kind, v.key) for v in views] == [("region", "us"), ("region", "emerging")]    # no developed ex-US fund


def test_nothing_for_products_the_investor_was_not_offered():
    offered = [a for a in UNIVERSE if a.asset_class in ("bond", "cash")]
    notes = build_views_with_notes(prefs(["stocks", "gold", "crypto", "bonds"], avoid=["energy"]), [], [], offered)[1]
    views = build_views(prefs(["stocks", "gold", "crypto", "bonds"], avoid=["energy"]), [], [], offered)
    assert [(v.kind, v.key) for v in views] == [("area", "bonds")]
    assert any("'gold'" in n for n in notes) and any("'energy'" in n for n in notes)
    assert build_views(prefs(["stocks"]), [], [], []) == []                                  # empty universe


def test_no_preferences_and_no_assets_give_no_views():
    assert build_views(None, [], [], UNIVERSE) == []
    assert build_views(prefs(), None, None, UNIVERSE) == []
    assert build_views(prefs(["stocks"]), ["VT"], [], []) == []


def test_at_most_twenty_views_and_the_avoided_industries_are_kept_first():
    many = [info(f"T{i}", area=a) for i, a in enumerate(AREA_PRIORITY)]
    many += [info(f"S{i}", sector=s, area=s) for i, s in enumerate(
        ["technology", "healthcare", "financials", "energy", "consumer", "industrials", "utilities",
         "communication", "materials", "real_estate"])]
    chosen = [a for a in AREA_PRIORITY if a not in ("energy", "materials")]            # 27 areas
    views = build_views(prefs(chosen, avoid=["energy", "materials"]), [], [], many)
    assert len(views) == MAX_VIEWS
    assert {v.key for v in views if v.annual_excess < 0} == {"energy", "materials"}
    assert views[-1].annual_excess < 0 and all(v.annual_excess > 0 for v in views[:-2])    # chosen first, avoided last
    assert len(build_views(prefs(chosen), [], [], many, max_views=5)) == 5
    assert build_views(prefs(chosen), [], [], many, max_views=0) == []


def test_the_numbers_are_keyword_options():
    views = build_views(prefs(["gold"], avoid=["energy"]), [], [], UNIVERSE, excess=0.01, confidence=0.5)
    assert keyset(views) == [("area", "gold", 0.01, 0.5), ("sector", "energy", -0.01, 0.5)]


# ==========================================================================
# 2. Interest assets
# ==========================================================================
def test_interest_assets_that_no_area_view_reaches_get_an_asset_view_sorted_by_ticker():
    views = build_views(prefs(["crypto"]), ["ZZNOMETA", "VT", "ZZCRYPTO"], [], UNIVERSE)
    # ZZCRYPTO is reached by the crypto area view, VT and ZZNOMETA are not (VT's "stocks" was not chosen).
    assert [(v.kind, v.key) for v in views] == [("area", "crypto"), ("asset", "VT"), ("asset", "ZZNOMETA")]
    assert all(v.annual_excess == 0.005 and v.confidence == 0.30 for v in views)


def test_interest_assets_that_are_not_offered_are_ignored():
    assert build_views(prefs(), ["NOT_OFFERED", "ZZNOMETA", "ZZNOMETA"], [], UNIVERSE) == [
        PortfolioView(kind="asset", key="ZZNOMETA", annual_excess=0.005, confidence=0.30)]


def test_an_avoided_industry_is_not_given_back_through_an_asset_view():
    views = build_views(prefs(avoid=["energy"]), ["XLE"], [], UNIVERSE)
    assert [(v.kind, v.key) for v in views] == [("sector", "energy")]


# ==========================================================================
# 3. Agent B only tightens
# ==========================================================================
def behavioral(type_, value, **kw) -> Constraint:
    return Constraint(type=type_, value=value, source="behavioral", **kw)


def test_agent_b_never_creates_a_view_and_never_raises_one():
    base = build_views(prefs(["stocks", "gold", "crypto"], avoid=["energy"]), ["ZZNOMETA"], [], UNIVERSE)
    tightened = [behavioral("max_equity", 0.3), behavioral("max_stress_loss", 0.08, scenario="equity_down_30_bond_down_5"),
                 behavioral("max_class_weight", 0.01, asset_class="crypto"), behavioral("max_turnover", 0.1),
                 behavioral("min_cash", 0.2), behavioral("max_vol", 0.05), behavioral("max_asset_weight", 0.05)]
    assert build_views(prefs(["stocks", "gold", "crypto"], avoid=["energy"]), ["ZZNOMETA"], tightened, UNIVERSE) == base


def test_agent_b_can_only_remove_a_positive_view_whose_products_it_shuts_completely():
    base = keyset(build_views(prefs(["stocks", "gold", "crypto"], avoid=["energy"]), ["ZZNOMETA"], [], UNIVERSE))
    shut_crypto = [behavioral("max_class_weight", 0.0, asset_class="crypto")]
    after = keyset(build_views(prefs(["stocks", "gold", "crypto"], avoid=["energy"]), ["ZZNOMETA"], shut_crypto,
                               UNIVERSE))
    assert set(after) < set(base)
    assert not any(k[1] in ("crypto", "ZZNOMETA") for k in after)
    assert ("sector", "energy", -0.005, 0.30) in after                    # the investor's own avoid view stays
    # a view with one product still open stays: only ZZCRYPTO is shut, the area "crypto" has ZZCRYPTO only, so it goes ...
    one_asset = [behavioral("max_asset_weight", 0.0, asset_id="ZZCRYPTO")]
    assert not any(k[1] == "crypto" for k in keyset(build_views(prefs(["crypto"]), [], one_asset, UNIVERSE)))
    # ... but "stocks" (VT, VTI, VXUS, AAPL ...) survives when VT alone is shut
    shut_vt = [behavioral("max_asset_weight", 0.0, asset_id="VT")]
    assert ("area", "stocks", 0.005, 0.30) in keyset(build_views(prefs(["stocks"]), [], shut_vt, UNIVERSE))
    equity_shut = [behavioral("max_equity", 0.0)]
    assert build_views(prefs(["stocks", "technology"]), [], equity_shut, UNIVERSE) == []


def test_limits_below_the_confidence_floor_soft_limits_and_other_sources_are_ignored():
    shut = dict(asset_class="crypto")
    ignored = [behavioral("max_class_weight", 0.0, confidence=0.4, **shut),
               behavioral("max_class_weight", 0.0, hard=False, **shut),
               Constraint(type="max_class_weight", value=0.0, source="risk_class", asset_class="crypto")]
    assert [v.key for v in build_views(prefs(["crypto"]), [], ignored, UNIVERSE)] == ["crypto"]
    counted = [behavioral("max_class_weight", 0.0, confidence=0.5, **shut)]
    assert build_views(prefs(["crypto"]), [], counted, UNIVERSE) == []
    assert build_views(prefs(["crypto"]), [], counted, UNIVERSE, min_behavioral_confidence=0.9) != []


# ==========================================================================
# 4. Inputs
# ==========================================================================
def test_preferences_can_be_the_dict_or_agent_cs_investor_interests():
    fields = prefs(["gold", "stocks"], avoid=["energy"])
    from_dict = build_views(fields, [], [], UNIVERSE)
    interests = InvestorInterests(investor_id="x", interest_areas=["gold", "stocks"], avoid_sectors=["energy"])
    assert build_views(interests, [], [], UNIVERSE) == from_dict
    assert [v.key for v in from_dict] == ["stocks", "gold", "energy"]                  # preference order, not input order


def test_something_that_is_not_interest_fields_is_refused_with_a_clear_error():
    with pytest.raises(TypeError, match="to_interest_fields"):
        build_views("gold", [], [], UNIVERSE)
    with pytest.raises(TypeError):
        build_views(object(), [], [], UNIVERSE)


def test_unknown_areas_and_industries_are_left_out_with_a_note_and_aliases_are_understood():
    views, notes = build_views_with_notes(prefs(["bitcoin", "no_such_area", "Gold"], avoid=["no_such_sector"]),
                                          [], [], UNIVERSE)
    assert [v.key for v in views] == ["gold", "crypto"]                                  # "bitcoin" is crypto
    assert any("no_such_area" in n for n in notes) and any("no_such_sector" in n for n in notes)


def test_an_area_that_is_both_chosen_and_avoided_is_avoided():
    views, notes = build_views_with_notes(prefs(["energy", "gold"], avoid=["energy"]), [], [], UNIVERSE)
    assert [(v.key, v.annual_excess) for v in views] == [("gold", 0.005), ("energy", -0.005)]
    assert any("both chosen and avoided" in n for n in notes)


def test_a_view_is_never_built_from_a_ticker_longer_than_the_view_key_allows():
    long_id = "X" * 41
    assert build_views(prefs(), [long_id], [], [info(long_id)]) == []


# ==========================================================================
# 5. The matching rule (shared with returns_bl)
# ==========================================================================
def test_labels_are_compared_without_case_or_punctuation_except_tickers():
    assert normalize_key("Developed ex-US") == "developed_ex_us" == normalize_key("developed_ex_us")
    ids = lambda kind, key: [a.asset_id for a in assets_for_view(kind, key, UNIVERSE)]          # noqa: E731
    assert ids("area", "us_stocks") == ["VTI"] and ids("area", "STOCKS") == ["VT", "VTI", "VXUS", "AAPL"]
    assert ids("region", "developed-ex-US") == ["VXUS"] and ids("sector", "Technology") == ["QQQ", "AAPL"]
    assert ids("asset", "vt") == [] and ids("asset", "VT") == ["VT"]
    assert ids("area", "") == [] and ids("sector", "nothing") == []
    with pytest.raises(ValueError):
        assets_for_view("colour", "red", UNIVERSE)


# ==========================================================================
# 6. Determinism and properties
# ==========================================================================
def test_the_same_inputs_give_the_same_list_whatever_the_order_of_the_inputs():
    a = build_views(prefs(["gold", "stocks", "crypto", "bonds"], avoid=["healthcare", "energy"]),
                    ["ZZNOMETA", "VT"], [], UNIVERSE)
    b = build_views(prefs(["bonds", "crypto", "stocks", "gold"], avoid=["energy", "healthcare"]),
                    ["VT", "ZZNOMETA"], [], list(UNIVERSE))
    assert a == b and a == build_views(prefs(["gold", "stocks", "crypto", "bonds"], avoid=["healthcare", "energy"]),
                                       ["ZZNOMETA", "VT"], [], UNIVERSE)


def test_random_preferences_always_give_valid_ordered_views_inside_the_universe():
    rng = random.Random(20261019)
    areas = list(AREA_PRIORITY)
    industries = ["technology", "healthcare", "financials", "energy", "consumer", "industrials", "utilities",
                  "communication", "materials", "real_estate"]
    classes = ["equity", "bond", "cash", "commodity", "crypto"]
    for _ in range(300):
        universe = [info(f"A{i}", rng.choice(classes), area=rng.choice([None, *areas]),
                         sector=rng.choice([None, *industries]), region=rng.choice([None, "US", "emerging"]))
                    for i in range(rng.randint(0, 40))]
        chosen = rng.sample(areas, rng.randint(0, 12))
        avoid = [s for s in rng.sample(industries, rng.randint(0, 4)) if s not in chosen]
        interest = [a.asset_id for a in universe if rng.random() < 0.3]
        limits = [behavioral("max_class_weight", rng.choice([0.0, 0.1]), asset_class=rng.choice(classes))
                  for _ in range(rng.randint(0, 2))]
        views = build_views(prefs(chosen, avoid), interest, limits, universe)
        assert views == build_views(prefs(chosen, avoid), interest, limits, universe)
        assert len(views) <= MAX_VIEWS
        assert len({(v.kind, v.key) for v in views}) == len(views)
        for v in views:
            assert abs(v.annual_excess) == pytest.approx(VIEW_EXCESS) and v.confidence == VIEW_CONFIDENCE
            assert assets_for_view(v.kind, v.key, universe)                           # offered
        negatives = [v for v in views if v.annual_excess < 0]
        assert all(v.kind in ("sector", "area") and v.key in avoid for v in negatives)
        n_asset = sum(v.kind == "asset" for v in views)
        order = ["positive"] * (len(views) - len(negatives) - n_asset) + ["negative"] * len(negatives) \
            + ["asset"] * n_asset
        assert order == [("negative" if v.annual_excess < 0 else "asset" if v.kind == "asset" else "positive")
                         for v in views]                               # chosen areas, then avoided, then assets
        without_b = build_views(prefs(chosen, avoid), interest, [], universe)
        assert set(map(repr, views)) <= set(map(repr, without_b))                    # Agent B never adds a view


# ==========================================================================
# 7. The six demo personas on the real catalog
# ==========================================================================
def catalog_assets() -> list[AssetInfo]:
    """What Agent C's metadata will say for every catalog product: the areas (comma separated) and the industry."""
    out = []
    for inst in DEFAULT_CATALOG:
        base = MarketIntelligenceAgent._asset_info(inst)
        out.append(base.model_copy(update={"area": ",".join(inst.areas), "sector": inst.sector,
                                           "product_type": inst.product_type}))
    return out


@pytest.mark.parametrize("key", sorted(PERSONAS))
def test_every_demo_persona_gets_valid_views_for_exactly_what_they_chose(key):
    fields = interest_fields(key)
    universe = catalog_assets()
    views, notes = build_views_with_notes(fields, [], [], universe)
    assert len(views) <= MAX_VIEWS
    assert views == build_views(fields, [], [], universe)
    chosen = set(fields["interest_areas"])
    for v in views:
        assert assets_for_view(v.kind, v.key, universe)
        if v.annual_excess > 0:
            assert v.key in chosen
        else:
            assert v.key in fields["avoid_sectors"]
    # the catalog names every area of every product, so each chosen area and each avoided industry has a view (<= 20)
    expected = len(chosen) + len(fields["avoid_sectors"])
    assert len(views) == min(expected, MAX_VIEWS), notes


# ==========================================================================
# 8. What the pipeline really hands over (found in the integration probe of 19 Oct night)
# ==========================================================================
def test_the_investors_preference_answers_work_and_count_only_the_explicit_picks():
    from egoquant.preferences import PreferenceAnswers, load_preference_bank
    bank = load_preference_bank()
    answers = PreferenceAnswers(preferences_version=bank.preferences_version, choices={
        "P1": ["stocks", "gold"], "P2": ["technology"], "P3": ["us_stocks"], "P4": ["individual"], "P5": ["energy"],
        "P6": ["no_preference"], "P7": ["funds"]})
    views = build_views(answers, [], [], UNIVERSE)
    # the explicit picks in the preference order; "individual" stocks do not add the nine other industries
    assert [(v.kind, v.key, v.annual_excess) for v in views] == [
        ("area", "stocks", 0.005), ("sector", "technology", 0.005), ("area", "us_stocks", 0.005),
        ("area", "gold", 0.005), ("sector", "energy", -0.005)]
    assert views == build_views(answers, [], [], UNIVERSE, bank=bank)
    nothing = PreferenceAnswers(preferences_version=bank.preferences_version, choices={})
    assert build_views(nothing, [], [], UNIVERSE) == []


def test_assets_can_be_agent_cs_selected_instruments_or_catalog_instruments():
    from egoquant.agents.market_intel import DEFAULT_CATALOG
    from egoquant.agents.market_intel.agent import SelectedInstrument
    by_ticker = {i.ticker: i for i in DEFAULT_CATALOG}
    tickers = ["VT", "VTI", "QQQ", "XLE", "AGG", "IAU", "BTCUSDT"]
    selected = [SelectedInstrument(ticker=t, name=by_ticker[t].name, asset_class=by_ticker[t].asset_class,
                                   product_type=by_ticker[t].product_type, r_level=by_ticker[t].r_level, reasons=["core"])
                for t in tickers]
    catalog = [by_ticker[t] for t in tickers]
    wanted = prefs(["stocks", "us_stocks", "technology", "gold", "crypto"], avoid=["energy"])
    views = build_views(wanted, ["QQQ"], [], selected)
    assert [(v.kind, v.key) for v in views] == [("area", "stocks"), ("sector", "technology"), ("area", "us_stocks"),
                                                 ("area", "gold"), ("area", "crypto"), ("sector", "energy")]
    assert views == build_views(wanted, ["QQQ"], [], catalog)
    assert [a.ticker for a in assets_for_view("area", "us_stocks", selected)] == ["VTI"]
    with pytest.raises(TypeError, match="neither asset_id nor ticker"):
        build_views(wanted, [], [], [object()])


def test_all_the_catalogs_areas_count_even_when_agent_c_writes_only_the_first_one():
    """Agent C's AssetInfo.area holds one area (VTI: "stocks"), the investor chose "us_stocks"."""
    first_area_only = [info("VTI", area="stocks", region="US"), info("VWO", area="emerging_markets", region="emerging"),
                       info("MCHI", area="china", region="emerging"), info("AGG", "bond", area="bonds", region="US")]
    views = build_views(prefs(["us_stocks", "china", "emerging_markets"]), [], [], first_area_only)
    assert [(v.kind, v.key) for v in views] == [("area", "us_stocks"), ("area", "emerging_markets"), ("area", "china")]
    assert [a.asset_id for a in assets_for_view("area", "china", first_area_only)] == ["MCHI"]
    assert [a.asset_id for a in assets_for_view("area", "emerging_markets", first_area_only)] == ["VWO", "MCHI"]

