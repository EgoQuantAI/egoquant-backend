"""Tone math and aggregation (fake scorer), exposure, alerts, suitability notes, diversity, cadence."""

import pytest

from egoquant.briefing import aggregate as agg
from egoquant.briefing.models import Aggregate, BriefingAlert, BriefingItem
from egoquant.briefing.tone import make_score
from tests.briefing.common import AS_OF, build, config, item

CFG = config()


def bi(title, days_ago, tone, source_type="outlet", tier="tier1"):
    s = None if tone is None else make_score(max(tone, 0), max(-tone, 0), 1 - abs(tone), "t")
    return BriefingItem(item=item(title, days_ago, source_type=source_type), matched_tickers=["VNQ"],
                        matched_areas=["real_estate"], tone=s, summary="x", summary_kind="template",
                        source_tier=tier)


# ---- tone math --------------------------------------------------------------------------------
def test_weighted_tone_uses_source_weights_and_half_life():
    items = [bi("a", 0, 0.9), bi("b", 3, -0.5, "forum", tier="unrated"), bi("c", 6, 0.3)]
    # weights: tier1 outlet 1.0 x 1; forum 0.4 x 0.5 (one half-life); tier1 outlet 1.0 x 0.25 (two half-lives)
    # damped: sum(w*s) / (sum(w) + k), k = confidence.k
    expected = (1.0 * 0.9 + 0.2 * -0.5 + 0.25 * 0.3) / (1.0 + 0.2 + 0.25 + CFG.confidence.k)
    assert agg.weighted_tone(items, AS_OF, CFG) == pytest.approx(expected)
    assert agg.tone_stats(items, AS_OF, CFG)[1] == pytest.approx(1.45)


def test_tier_weights_and_tier3_never_counts():
    items = [bi("a", 0, 1.0, tier="tier1"), bi("b", 0, -1.0, tier="tier2"), bi("c", 0, -1.0, tier="unrated")]
    assert agg.weighted_tone(items, AS_OF, CFG) == pytest.approx((1.0 - 0.7 - 0.4) / (1.0 + 0.7 + 0.4 + CFG.confidence.k))
    low = [bi(str(i), 1, -1.0, tier="tier3") for i in range(5)]
    a = agg.aggregate("VNQ", "instrument", items + low, AS_OF, 0.2, CFG)
    assert a.n_items == 3 and a.tone == pytest.approx(agg.weighted_tone(items, AS_OF, CFG), abs=0.2)
    assert all(i.item.id not in a.item_ids for i in low)


def test_tone_is_p_positive_minus_p_negative():
    assert make_score(0.7, 0.2, 0.1, "m").tone == pytest.approx(0.5)


def test_thin_evidence_is_damped_and_labelled_low_confidence_not_hidden():
    a = agg.aggregate("VNQ", "instrument", [bi("a", 1, 0.5), bi("b", 2, 0.5)], AS_OF, None, CFG)
    ev = agg.tone_stats([bi("a", 1, 0.5), bi("b", 2, 0.5)], AS_OF, CFG)[1]
    assert a.tone == pytest.approx(0.5 * ev / (ev + CFG.confidence.k)) and a.tone < 0.5     # pulled toward 0
    assert a.confidence == "low" and a.coverage_status.state == "tone" and a.change is None
    assert "low confidence" in a.text and "not enough coverage" not in a.text


def test_only_final_states_rated_quiet_or_unrated():
    a = agg.aggregate("VNQ", "instrument", [bi("a", 1, 0.5), bi("b", 2, None), bi("c", 3, None)], AS_OF, None, CFG)
    assert a.coverage_status.state == "tone" and a.coverage_status.n == 1 and a.coverage_status.n_waiting == 2
    w = agg.aggregate("VNQ", "instrument", [bi("a", 1, None), bi("b", 2, None)], AS_OF, None, CFG)
    assert w.coverage_status.state == "unrated" and w.coverage_status.n_waiting == 2
    q = agg.aggregate("VNQ", "instrument", [], AS_OF, None, CFG, this="this week")
    assert q.coverage_status.state == "quiet" and q.text == "VNQ: quiet week, no news about VNQ or what it holds"
    for x in (a, w, q):
        assert x.coverage_status.state not in ("not_enough_coverage", "not_yet_assessed")


def test_unavailable_tone_is_labeled_not_guessed():
    a = agg.aggregate("VNQ", "instrument", [bi(str(i), i, None) for i in range(3)], AS_OF, None, CFG)
    assert a.tone is None and "not rated" in a.text and a.coverage_status.state == "unrated"


def test_change_versus_previous_period():
    cur = [bi(f"c{i}", 1 + i, -0.8) for i in range(3)]
    prev = [bi(f"p{i}", 8 + i, 0.2) for i in range(3)]
    a = agg.aggregate("real_estate", "area", cur + prev, AS_OF, 0.1, CFG)
    assert (a.n_items, a.n_items_previous) == (3, 3) and a.confidence == a.confidence_previous == "medium"
    t, ev = agg.tone_stats(cur, AS_OF, CFG)
    assert t == pytest.approx(-0.8 * ev / (ev + CFG.confidence.k))
    assert a.change == pytest.approx(a.tone - a.tone_previous)
    assert a.text.startswith("real estate: slightly negative (-0.45, 3 headlines), medium confidence this period; "
                             "last period: mixed or neutral (+0.11, 3 headlines), change -0.56")


def test_no_change_unless_both_periods_have_medium_or_high_confidence():
    cur = [bi(f"c{i}", 1 + i, -0.8) for i in range(3)]
    prev = [bi("p0", 8, 0.9)]
    a = agg.aggregate("VNQ", "instrument", cur + prev, AS_OF, 0.3, CFG, this="this week", last="last week")
    assert a.confidence == "medium" and a.confidence_previous == "low" and a.change is None
    assert a.compare_note == "too little news last week to compare"


# ---- exposure -----------------------------------------------------------------------------------
def test_exposure_sums_held_tickers_and_names_them():
    w = {"VNQ": 0.27, "QQQ": 0.10, "AGG": 0.0}
    assert agg.exposure_of(w, ["VNQ", "QQQ", "AGG"]) == pytest.approx(0.37)
    assert agg.exposure_text(w, ["VNQ"]) == "This concerns 27% of your portfolio (VNQ)."
    assert agg.exposure_text(w, ["AGG", "IBIT"]) == "On your watchlist; not in your portfolio."
    assert agg.exposure_text(None, ["VNQ"]) == "" and agg.exposure_of(None, ["VNQ"]) is None


def test_items_sorted_by_exposure_then_recency():
    items = [item("REITs fall again", 1), item("Nasdaq-100 gains", 2), item("REITs slump", 0.5)]
    b = build(items, areas=("real_estate", "technology"), weights={"VNQ": 0.1, "QQQ": 0.3})
    assert [x.item.title for x in b.items] == ["Nasdaq-100 gains", "REITs slump", "REITs fall again"]


# ---- alerts -------------------------------------------------------------------------------------
NEG = ["REITs fall as office vacancies climb", "Real estate trusts slump on refinancing costs",
       "Commercial property values decline again", "REIT dividends cut after weak leasing"]
POS = ["REITs post solid gains on rent growth", "Apartment REITs report strong occupancy",
       "Real estate funds see record inflows", "REIT earnings rise for a third quarter"]


def story(n_cur=3, n_prev=3):
    """Distinct headlines (near-identical ones would be deduplicated as one story)."""
    return ([item(t, 1 + i * 0.5, source_name="Reuters") for i, t in enumerate(NEG[:n_cur])]
            + [item(t, 8 + i * 0.5, source_name="Reuters") for i, t in enumerate(POS[:n_prev])])


def test_alert_fires_when_threshold_count_and_exposure_are_met():
    b = build(story(), weights={"VNQ": 0.27})
    assert [(a.subject, a.kind) for a in b.alerts] == [("VNQ", "instrument")]   # holdings only, never areas
    assert all(a.action == "offer_risk_profile_recheck" for a in b.alerts)
    assert "re-check your risk profile" in b.alerts[0].message and "Nothing changes unless you confirm" in b.alerts[0].message


@pytest.mark.parametrize("kw, why", [
    (dict(weights={"VNQ": 0.05}), "exposure must be strictly above the minimum"),
    (dict(weights=None), "no portfolio, no exposure"),
    (dict(weights={"QQQ": 0.5}), "not held"),
])
def test_alert_needs_exposure(kw, why):
    assert build(story(), **kw).alerts == [], why


def test_alert_needs_medium_confidence_in_both_periods():
    assert build(story(n_cur=1), weights={"VNQ": 0.27}).alerts == []      # one headline: low confidence


def test_alert_needs_the_threshold():
    flat = [item(t, 1 + i) for i, t in enumerate(NEG[:3])] + [item(t, 8 + i) for i, t in enumerate(NEG[1:4])]
    assert build(flat, weights={"VNQ": 0.27}).alerts == []


def test_only_action_is_offer_risk_profile_recheck():
    with pytest.raises(ValueError):
        BriefingAlert(subject="VNQ", kind="instrument", change=-1, exposure=0.2, message="m", action="sell")
    from egoquant.briefing.models import AlertAction
    import typing
    assert set(typing.get_args(AlertAction)) == {"offer_risk_profile_recheck"}        # the only action there is
    assert set(typing.get_args(BriefingAlert.model_fields["action"].annotation)) == {AlertAction, type(None)}


# ---- suitability, diversity, cadence -------------------------------------------------------------
def test_suitability_note_for_instruments_above_max_r_level():
    b = build([item("Bitcoin ETF inflows rise")], areas=("crypto", "technology"), max_r_level=4)
    assert [(n.area, n.tickers_above) for n in b.suitability_notes] == [("crypto", ["IBIT", "FBTC", "GBTC", "ETHA", "BTCUSDT", "ETHUSDT"])]
    assert b.suitability_notes[0].text == ("Some instruments in crypto are above your risk level (R4) and are "
                                           "shown for information only.")


def test_diversity_suggestions_are_retired():
    # the old "balance your sources" box repeated SEC EDGAR once per area; source tiers replace it
    titles = ["Bitcoin gains again", "Ether rally thread", "Spot bitcoin ETF inflows rise", "Crypto prices climb"]
    forum = [item(t, 1 + i, source_type="forum", source_name=f"r/x{i}") for i, t in enumerate(titles)]
    assert build(forum, areas=("crypto",), max_r_level=5).diversity_suggestions == []


def test_no_diversity_suggestion_for_mixed_sources():
    mixed = [item("Bitcoin gains", 1, source_type=t, source_name=t) for t in ("outlet", "analyst", "forum", "filing")]
    assert build(mixed, areas=("crypto",), max_r_level=5).diversity_suggestions == []


@pytest.mark.parametrize("years, freq", [(5, "weekly"), (0.25, "weekly"), (0.1, "daily")])
def test_cadence(years, freq):
    assert agg.cadence(years, CFG).startswith(freq)


def test_aggregate_counts_ratings_per_card():
    items = [bi("a", 1, 0.9), bi("b", 2, -0.6), bi("c", 3, 0.0), bi("d", 3, None), bi("e", 4, 0.4)]
    a = agg.aggregate("VNQ", "instrument", items, AS_OF, None, CFG)
    assert (a.n_positive, a.n_neutral, a.n_negative, a.n_unrated) == (2, 1, 1, 1)
    assert a.n_positive + a.n_neutral + a.n_negative == a.coverage_status.n


def test_negative_labels_flow_through_to_a_negative_score():
    items = [bi(str(i), 1 + i, -1.0) for i in range(3)]
    a = agg.aggregate("VNQ", "instrument", items, AS_OF, 0.2, CFG)
    ev = agg.tone_stats(items, AS_OF, CFG)[1]
    assert a.n_negative == 3 and a.tone == pytest.approx(-ev / (ev + CFG.confidence.k))
    assert agg.tone_words(a.tone, CFG) == "mostly negative"


def test_positive_move_alerts_without_the_offer():
    from egoquant.briefing.aggregate import RECHECK_OFFER, alerts_for
    from egoquant.briefing.models import Aggregate
    cfg = config()
    base = dict(kind="instrument", n_items=8, n_items_previous=8, exposure=0.3, text="", confidence="high",
                confidence_previous="high")
    up = Aggregate(subject="VNQ", tone=0.4, tone_previous=-0.3, change=0.7, **base)
    down = Aggregate(subject="AGG", tone=-0.4, tone_previous=0.3, change=-0.7, **base)
    small = Aggregate(subject="BIL", tone=-0.1, tone_previous=0.3, change=-0.4, **base)
    got = {a.subject: a for a in alerts_for([up, down, small], cfg)}
    assert set(got) == {"VNQ", "AGG"}
    assert got["AGG"].action == "offer_risk_profile_recheck" and got["AGG"].message.endswith(RECHECK_OFFER)
    assert got["VNQ"].action is None and RECHECK_OFFER not in got["VNQ"].message
