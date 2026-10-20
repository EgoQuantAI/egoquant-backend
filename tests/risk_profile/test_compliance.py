"""Compliance record on the RiskProfile: assessed_at, valid_until, answers_hash; expiry flagged, not blocking."""

import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from egoquant.agents.market_intel import SyntheticPriceSource
from egoquant.pipeline import run_pipeline
from egoquant.report import render_html
from egoquant.risk_profile import answers_hash, build_risk_profile, is_expired
from egoquant.risk_profile.scoring import add_months
from tests.helpers import ASSESSED_AT, BANK, CONFIG, make_answers

ROOT = Path(__file__).resolve().parents[2]


def profile(answers=None, at=ASSESSED_AT, config=CONFIG):
    return build_risk_profile(answers or make_answers(), BANK, config, assessed_at=at)


# ---- answers hash -------------------------------------------------------------------------------
def test_same_answers_give_the_same_hash():
    a = make_answers(gl_score=27, K4=["stocks", "bonds"])
    assert answers_hash(a) == answers_hash(a.model_copy(deep=True)) == profile(a).answers_hash
    assert profile(a).answers_hash.startswith("sha256:") and len(profile(a).answers_hash) == 7 + 64


def test_hash_is_stable_across_processes():
    code = ("import sys; sys.path.insert(0, '.');"
            "from tests.helpers import make_answers; from egoquant.risk_profile import answers_hash;"
            "print(answers_hash(make_answers(gl_score=27, K4=['stocks', 'bonds'])))")
    outs = {subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                           env={"PYTHONHASHSEED": str(seed), "PATH": ""}).stdout.strip() for seed in (1, 2, 3)}
    assert len(outs) == 1 and next(iter(outs)) == answers_hash(make_answers(gl_score=27, K4=["stocks", "bonds"]))


@pytest.mark.parametrize("changed", [
    make_answers(gl_score=28),                       # a different tolerance answer
    make_answers(B4="lt3"),                          # a different capacity answer
    make_answers(invested=50_001),                   # a different amount
    make_answers(needed=1),
    make_answers(K4=["stocks", "crypto"]),           # a different multi-select
])
def test_different_answers_give_a_different_hash(changed):
    assert answers_hash(changed) != answers_hash(make_answers())


def test_questionnaire_version_is_part_of_the_hash():
    a = make_answers()
    assert answers_hash(a) != answers_hash(a.model_copy(update={"questionnaire_version": "eq-rq-9.9.9"}))


def test_normalization_order_and_number_format_do_not_matter():
    assert answers_hash(make_answers(K4=["stocks", "bonds"])) == answers_hash(make_answers(K4=["bonds", "stocks"]))
    assert answers_hash(make_answers(invested=50_000)) == answers_hash(make_answers(invested=50_000.0))


# ---- assessed_at and valid_until ----------------------------------------------------------------
def test_valid_until_is_assessed_at_plus_the_configured_period():
    p = profile()
    assert p.assessed_at == ASSESSED_AT
    assert p.valid_until == ASSESSED_AT.replace(year=2027)                       # 12 months
    six = CONFIG.model_copy(update={"compliance": CONFIG.compliance.model_copy(update={"validity_months": 6})})
    assert profile(config=six).valid_until == datetime(2027, 4, 16, 9, 30, tzinfo=timezone.utc)


def test_month_arithmetic_clamps_to_the_end_of_the_month():
    assert add_months(datetime(2028, 2, 29, tzinfo=timezone.utc), 12) == datetime(2029, 2, 28, tzinfo=timezone.utc)
    assert add_months(datetime(2026, 1, 31, tzinfo=timezone.utc), 1) == datetime(2026, 2, 28, tzinfo=timezone.utc)
    assert add_months(datetime(2026, 11, 15, tzinfo=timezone.utc), 3) == datetime(2027, 2, 15, tzinfo=timezone.utc)


def test_assessed_at_is_required_and_timezone_aware():
    with pytest.raises(TypeError):
        build_risk_profile(make_answers(), BANK, CONFIG)                              # no hidden clock
    with pytest.raises(ValueError, match="timezone-aware"):
        build_risk_profile(make_answers(), BANK, CONFIG, assessed_at=datetime(2026, 10, 16, 9, 30))
    plus4 = timezone(timedelta(hours=4))
    p = profile(at=datetime(2026, 10, 16, 13, 30, tzinfo=plus4))                    # stored in UTC
    assert p.assessed_at == ASSESSED_AT and p.assessed_at.tzinfo == timezone.utc


def test_same_inputs_give_an_identical_profile():
    assert profile().model_dump_json() == profile().model_dump_json()


def test_expiry_boundary():
    p = profile()                                                                   # valid until 16 Oct 2027
    assert not is_expired(p, date(2027, 10, 16)) and is_expired(p, date(2027, 10, 17))


# ---- pipeline and report: an expired profile is flagged, not blocked ------------------------------
def run(assessed_at=None, as_of=date(2026, 10, 16)):
    return run_pipeline(make_answers(gl_score=25), investor_id="t", interest_areas=["stocks", "bonds"],
                        goal_type="growth", horizon_years=10, as_of=as_of, price_source=SyntheticPriceSource(),
                        assessed_at=assessed_at)


def test_fresh_profile_shows_assessment_dates_in_the_report():
    out = run()
    assert not out.profile_expired and not out.report.profile_expired
    assert out.report.profile_assessed_on == date(2026, 10, 16)
    assert out.report.profile_valid_until == date(2027, 10, 16)
    html = render_html(out.report)
    assert "Risk profile assessed on 16 Oct 2026, valid until 16 Oct 2027." in html
    assert not any("expired" in b for b in out.report.banners)


def test_expired_profile_is_flagged_with_a_reassessment_offer_but_not_blocked():
    fresh = run()
    old = run(assessed_at=datetime(2025, 9, 1, tzinfo=timezone.utc))                # valid until 1 Sep 2026
    assert old.profile_expired and old.report.profile_expired
    banner = old.report.banners[0]
    assert "expired on 01 Sep 2026" in banner and "re-assess" in banner and "nothing has been changed or blocked" in banner
    assert "(expired)" in render_html(old.report)
    # the portfolio and the Risk Engine verdict are exactly as for a fresh profile: flagged, not blocked
    assert old.result.final.status != "infeasible" and old.result.final.weights == fresh.result.final.weights
    assert old.review.approved == fresh.review.approved
    assert old.profile.answers_hash == fresh.profile.answers_hash
