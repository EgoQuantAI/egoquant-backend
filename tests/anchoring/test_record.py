"""The record builder: every field from a saved report, exact ppm, no personal data, rejected reports refused."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from egoquant.anchoring.canonical import canonical_text, report_sha256
from egoquant.anchoring.record import NotSealable, allocation_ppm, build_record, code_versions

SAVED = json.loads((Path(__file__).parents[2] / "egoquant/anchoring/demo_saved_report.json").read_text(encoding="utf-8"))
RID = "00000000-0000-4000-8000-0000000000aa"
WHEN = datetime(2026, 10, 20, 8, 15, 30, 999999, tzinfo=timezone.utc)
LOG = [
    {"at": "2026-10-20T08:10:09.5+00:00", "action": "report_generated", "details": {"report_id": RID}},
    {"at": "2026-10-20T08:10:09.1+00:00", "action": "questionnaire_submitted", "details": {"report_id": RID}},
    {"at": "2026-10-20T08:12:44+00:00", "action": "report_viewed", "details": {"report_id": RID}},
    {"at": "2026-10-20T08:15:31+00:00", "action": "plan_confirmed", "details": {"report_id": RID}},
    {"at": "2026-10-20T07:00:00+00:00", "action": "report_generated", "details": {"report_id": "other"}},
    {"at": "2026-10-20T07:00:00+00:00", "action": "consent_given", "details": {"scope": "ibkr_data"}},
]


def build(saved=SAVED, **kw):
    return build_record(saved, report_id=RID, log_rows=LOG, consent_version="2026-10-20", confirmed_at=WHEN, **kw)


def test_fields():
    r = build()
    assert r["schema"] == "egoq-record-1" and r["report_id"] == RID and r["record_version"] == 1
    assert r["confirmed_at"] == "2026-10-20T08:15:30Z" and r["consent_version"] == "2026-10-20"
    assert r["investor"] == {"risk_class": "C3", "questionnaire_version": "eq-rq-0.1.0",
                             "answers": {"choices": {"G1": "b", "G2": "c", "G3": "a"}, "multi_choices": {},
                                         "amount_invested_cents": 1_000_000, "amount_needed_cents": 0}}
    assert r["allocation"] == [{"ticker": "BIL", "weight_ppm": 300_000}, {"ticker": "SHY", "weight_ppm": 300_000},
                               {"ticker": "VT", "weight_ppm": 400_000}]
    assert r["risk_engine"]["approved"] is True and r["risk_engine"]["violations"] == []
    names = [c["name"] for c in r["risk_engine"]["checks"]]
    assert names == sorted(names) and set(r["risk_engine"]["checks"][0]) == {"name", "passed", "detail"}
    v = r["versions"]
    assert v["engine"] == "0.3.0" and v["input_hash"] == "demo0000c3000001"
    assert v["report_sha256"] == report_sha256(SAVED)
    assert set(v) == {"engine", "input_hash", "risk_engine", "risk_profile_config", "agent_d_prompt_sha256",
                      "agent_d_model", "report_sha256"}
    assert v["agent_d_prompt_sha256"] == code_versions()["agent_d_prompt_sha256"]


def test_the_risk_game_is_part_of_the_record_only_when_it_was_played():
    assert "risk_game" not in build()["investor"]["answers"]               # a report without a game is byte-identical to before
    played = copy.deepcopy(SAVED)
    played["inputs"]["answers"]["risk_game"] = {
        "tolerance": 62, "loss_acceptance": 40, "completed_at": "2026-10-20T08:00:00+00:00",
        "crash_test": {"sold_at_drawdown": 0.125, "sold_fraction": 0.5, "max_drawdown_seen": 0.2}}
    game = build(played)["investor"]["answers"]["risk_game"]
    assert game == {"tolerance": 62, "loss_acceptance": 40,
                    "crash_test": {"max_drawdown_seen_ppm": 200_000, "sold_fraction_ppm": 500_000, "sold_at_drawdown_ppm": 125_000}}
    assert "completed_at" not in canonical_text(build(played))             # when it was played is not an input
    assert canonical_text(build(played)) != canonical_text(build())        # so the fingerprint covers the game
    held = copy.deepcopy(SAVED)
    held["inputs"]["answers"]["risk_game"] = {"tolerance": 10, "completed_at": "2026-10-20T08:00:00+00:00",
                                              "crash_test": {"sold_at_drawdown": None, "sold_fraction": 0.0, "max_drawdown_seen": 0.3}}
    assert build(held)["investor"]["answers"]["risk_game"] == {
        "tolerance": 10, "crash_test": {"max_drawdown_seen_ppm": 300_000, "sold_fraction_ppm": 0, "sold_at_drawdown_ppm": None}}


def test_operation_log_is_this_reports_rows_sorted_with_one_plan_confirmed():
    assert build()["operation_log"] == [
        {"at": "2026-10-20T08:10:09Z", "action": "questionnaire_submitted"},
        {"at": "2026-10-20T08:10:09Z", "action": "report_generated"},
        {"at": "2026-10-20T08:12:44Z", "action": "report_viewed"},
        {"at": "2026-10-20T08:15:30Z", "action": "plan_confirmed"}]


def test_no_personal_data_and_no_score():
    text = canonical_text(build())
    for banned in ("user_id", "email", "username", "tolerance_score", "score", "investor_id", "demo-investor",
                   "headline", "Vanguard"):
        assert banned not in text


def test_record_is_canonical_and_deterministic():
    assert canonical_text(build()) == canonical_text(build())


@pytest.mark.parametrize("weights,expected", [
    ([0.333333, 0.333333, 0.333334], [333_333, 333_333, 333_334]),
    ([1 / 3, 1 / 3, 1 / 3], [333_334, 333_333, 333_333]),            # ties: the remainder goes by ticker order
    ([0.5000004, 0.2999998, 0.1999998], [500_000, 300_000, 200_000]),
    ([0.22000000000000006, 0.78], [220_000, 780_000]),
])
def test_ppm_sums_exactly(weights, expected):
    rows = [{"asset_id": t, "weight": w} for t, w in zip("CBA", weights)]
    out = allocation_ppm(rows)
    assert sum(r["weight_ppm"] for r in out) == 1_000_000
    assert [r["ticker"] for r in out] == sorted("CBA"[:len(weights)])
    assert {r["ticker"]: r["weight_ppm"] for r in out} == dict(zip("CBA", expected))


def test_largest_remainder_goes_to_largest_weight():
    out = allocation_ppm([{"asset_id": "BIG", "weight": 0.6000005}, {"asset_id": "SMALL", "weight": 0.3999995}])
    assert out == [{"ticker": "BIG", "weight_ppm": 600_001}, {"ticker": "SMALL", "weight_ppm": 399_999}]


def test_zero_weights_dropped_and_bad_sums_refused():
    assert allocation_ppm([{"asset_id": "A", "weight": 1.0}, {"asset_id": "B", "weight": 0.0}]) == \
        [{"ticker": "A", "weight_ppm": 1_000_000}]
    with pytest.raises(NotSealable):
        allocation_ppm([{"asset_id": "A", "weight": 0.5}])
    with pytest.raises(NotSealable):
        allocation_ppm([{"asset_id": "A", "weight": 0.5}, {"asset_id": "A", "weight": 0.5}])


@pytest.mark.parametrize("change", [
    lambda s: s.update(approved=False),
    lambda s: s.update(violations=["equity 55% > 40%"]),
    lambda s: s["report"].update(risk_review_approved=False),
    lambda s: s["report"].update(status="infeasible"),
    lambda s: s["report"].update(allocation=[]),
    lambda s: s.pop("inputs"),
    lambda s: s["report"].update(risk_class="C9"),
])
def test_rejected_or_incomplete_reports_raise(change):
    saved = copy.deepcopy(SAVED)
    change(saved)
    with pytest.raises(NotSealable):
        build(saved)


def test_one_changed_report_value_changes_report_sha256():
    saved = copy.deepcopy(SAVED)
    saved["report"]["worst_loss"] = 0.143
    assert build(saved)["versions"]["report_sha256"] != build()["versions"]["report_sha256"]


def test_building_never_changes_the_report():
    saved = copy.deepcopy(SAVED)
    build(saved)
    assert saved == SAVED
