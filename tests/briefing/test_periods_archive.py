"""briefing_period presets (week / month) and the local news archive. Offline."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from egoquant.briefing import archive
from egoquant.briefing.__main__ import main, run_demo
from egoquant.briefing.loader import load_config, with_period
from egoquant.briefing.matching import Matcher
from tests.briefing.common import build, config, item

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("EGOQUANT_CACHE_DIR", str(tmp_path / "cache"))


# ---- presets --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("period, days, lookback, half_life", [("week", 7, 14, 3), ("month", 30, 60, 10)])
def test_presets_set_window_and_half_life(period, days, lookback, half_life):
    base = config()
    cfg, preset = with_period(base, period)
    assert (cfg.window.period_days, cfg.window.lookback_days, cfg.tone.recency_half_life_days) == \
        (days, lookback, half_life)
    assert cfg.tone.min_items_for_aggregate == base.tone.min_items_for_aggregate == 3        # min items unchanged
    assert base.window.period_days == 7                                          # the input config is not changed
    assert preset.this == f"this {period}" and preset.last == f"last {period}"


def test_default_period_is_week_and_unknown_is_rejected():
    c = config()
    assert c.default_briefing_period == "week" and with_period(c, None)[1].period_days == 7
    with pytest.raises(ValueError):
        with_period(c, "year")


REIT_NOW = [item(t, 2 + i, source_name="Reuters") for i, t in enumerate([
    "REITs fall as office vacancies climb", "Real estate funds see weak demand from buyers",
    "REIT shares slump as rates climb", "Real estate trusts post losses on empty towers"])]
REIT_LAST_MONTH = [item(t, 35 + 3 * i, source_name="Reuters") for i, t in enumerate([
    "REITs gain on strong leasing", "Real estate funds draw record inflows",
    "Apartment REITs post solid rent growth", "Real estate stocks rise after upbeat earnings"])]


def test_monthly_view_compares_this_month_with_last_month():
    b = build(REIT_NOW + REIT_LAST_MONTH, weights={"VNQ": 0.2, "AGG": 0.8}, briefing_period="month")
    vnq = next(a for a in b.instrument_aggregates if a.subject == "VNQ")
    assert b.briefing_period == "month" and vnq.n_items == 4 and vnq.n_items_previous == 4
    assert "this month" in vnq.text and "last month:" in vnq.text and "change" in vnq.text
    assert any("this month" in a.message for a in b.alerts)


def test_weekly_view_ignores_items_older_than_two_weeks():
    b = build(REIT_NOW + REIT_LAST_MONTH, weights={"VNQ": 0.2, "AGG": 0.8}, briefing_period="week")
    vnq = next(a for a in b.instrument_aggregates if a.subject == "VNQ")
    assert b.briefing_period == "week" and "this week" in vnq.text
    assert "too little news last week to compare" in vnq.text and vnq.change is None and not b.alerts


def test_thin_previous_month_says_too_little_news_and_never_alerts():
    b = build(REIT_NOW + REIT_LAST_MONTH[:2], weights={"VNQ": 0.2, "AGG": 0.8}, briefing_period="month")
    vnq = next(a for a in b.instrument_aggregates if a.subject == "VNQ")
    assert "too little news last month to compare" in vnq.text and vnq.change is None and not b.alerts


# ---- offline fixture: monthly view has a real comparison --------------------------------------------------------
def test_offline_fixture_monthly_view_compares(tmp_path):
    b = run_demo(live=False, allow_llm=False, out_dir=tmp_path, period="month")
    compared = [a for a in b.instrument_aggregates if a.change is not None]
    assert b.briefing_period == "month" and compared
    assert all("this month" in a.text and "last month:" in a.text for a in compared)
    assert b.metadata.n_tone_llm_calls == 0 and b.metadata.n_fund_tones_finbert == 0
    assert (tmp_path / "briefing_demo_month.html").exists()
    assert "DEMO" in " ".join(b.banners)


def test_period_flag_on_the_cli(tmp_path, capsys):
    assert main(["--no-llm", "--period", "month", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "briefing_demo_month.html").exists()
    assert json.loads((tmp_path / "briefing_demo_month.json").read_text())["briefing_period"] == "month"
    with pytest.raises(SystemExit):
        main(["--period", "year", "--out", str(tmp_path)])


# ---- archive -----------------------------------------------------------------------------------------------------
def test_archive_dedupes_by_id_and_keeps_no_bodies(tmp_path):
    path = tmp_path / "news_archive.json"
    m = Matcher(load_config())
    a = item("REITs fall as office vacancies climb", 3, summary="The full article body must not be kept.")
    b = item("Treasury yields ease after jobs data", 5, summary="Another body.")
    assert archive.archive_items([a, b], m, path) == 2
    assert archive.archive_items([a, b], m, path) == 0                       # same ids: nothing added
    stored = json.loads(path.read_text())
    assert len(stored) == 2 and set(stored[a.id]) == set(archive.ARCHIVE_FIELDS)
    assert "body" not in path.read_text() and "summary" not in path.read_text()
    assert stored[a.id]["matched_tickers"] == ["VNQ"]
    loaded = archive.load_archive(path)
    assert [i.id for i in loaded] == [b.id, a.id]                            # oldest first
    assert all(i.summary_text == "" for i in loaded)
    assert [i.id for i in archive.load_archive(path, since=a.published_at)] == [a.id]


def test_archive_never_overwrites_an_existing_entry(tmp_path):
    path = tmp_path / "news_archive.json"
    m = Matcher(load_config())
    a = item("REITs fall as office vacancies climb", 3, url="https://test.example.com/same")
    archive.archive_items([a], m, path)
    first = json.loads(path.read_text())[a.id]
    archive.archive_items([a.model_copy(update={"title": "Changed title"})], m, path)
    assert json.loads(path.read_text())[a.id] == first


def test_fresh_items_win_the_dedupe_over_archived_copies():
    fresh = item("REITs fall as office vacancies climb", 2, summary="Fresh feed summary.")
    stale = fresh.model_copy(update={"summary_text": ""})
    b = build([fresh, stale, *REIT_LAST_MONTH[:3]], weights={"VNQ": 1.0}, briefing_period="month")
    assert len([i for i in b.items if i.item.id == fresh.id]) == 1
    assert next(i for i in b.items if i.item.id == fresh.id).item.summary_text == "Fresh feed summary."


def test_archive_lives_in_the_cache_dir_outside_git(tmp_path):
    assert archive.archive_path() == tmp_path / "cache" / "briefing" / "news_archive.json"
    if shutil.which("git") and (ROOT / ".git").exists():
        in_repo = ROOT / "briefing_cache" / "briefing" / "news_archive.json"
        r = subprocess.run(["git", "check-ignore", "-q", str(in_repo)], cwd=ROOT)
        assert r.returncode == 0                                                # gitignored if kept in the repo
