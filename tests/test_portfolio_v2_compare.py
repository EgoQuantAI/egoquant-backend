"""eval/portfolio_v2_compare.py: the acceptance checks of docs/PORTFOLIO_V2.md section 5 judge the rows the right way,
the table prints, and the script runs today with v1 only. No network. The real comparison (about 10 minutes for all 24
runs) is not repeated here; one cheap persona is run end to end.
"""

import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from egoquant.demo.personas import PERSONAS

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("portfolio_v2_compare", ROOT / "eval" / "portfolio_v2_compare.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module                 # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


cmp = load()


def row(persona="balanced_saver", c_level="C3", goal="15y", method="v1", **kw):
    base = dict(persona=persona, c_level=c_level, goal=goal, method=method, ok=True, error=None, approved=True,
                status="optimal", n_holdings=6, eff_n=3.6, largest=0.40, stock_share=0.22,
                chance=0.80 if goal != "none" else None, dd90=0.10 if goal != "none" else None, hist_dd=0.02,
                n_universe=11, weights={"A": 0.4, "B": 0.6}, v2=None, seed=None, cpu_s=8.0, wall_s=9.0, peak_mb=300.0)
    base.update(kw)
    return base


def v2_row(persona="balanced_saver", c_level="C3", goal="15y", seed="aa", **kw):
    diag = dict(method="erc", effective_n=7.5, largest=0.2, diversification_floor_met=True, seed=seed)
    return row(persona, c_level, goal, "v2", eff_n=7.5, largest=0.20, n_holdings=20, v2=diag, seed=seed, **kw)


def pairs():
    """v1 and a good v2 for three personas, goal runs and no-goal runs."""
    rows = []
    for i, (persona, level) in enumerate([("balanced_saver", "C3"), ("growth_investor", "C4"), ("crypto_trader", "C5")]):
        for goal in ("none", "15y"):
            rows.append(row(persona, level, goal))
            rows.append(v2_row(persona, level, goal, seed=f"{i}{goal}"))
    return rows


def repeats():
    """The same input run twice, v1 and v2: identical weights."""
    return [row("balanced_saver", "C3", "none"), v2_row("balanced_saver", "C3", "none")]


def status(checks, start):
    matching = [c for c in checks if c.name.startswith(start)]
    assert len(matching) == 1, [c.name for c in checks]
    return matching[0].status


# ==========================================================================
# the checks
# ==========================================================================
def test_with_v1_only_nothing_fails_and_the_v2_checks_say_not_applicable():
    rows = [row(p, "C3", g) for p in ("balanced_saver", "growth_investor") for g in ("none", "15y")]
    checks = cmp.acceptance(rows, [row("balanced_saver", "C3", "none")])
    assert not [c for c in checks if c.status == "FAIL"]
    assert status(checks, "every run finished") == "PASS"
    assert status(checks, "same input twice") == "PASS"
    for name in ("v2 changes the portfolio", "v2_* diagnostics", "Risk Engine approves", "different goal",
                 "effective N", "goal chance", "worst-10% drawdown", "heaviest persona", "peak memory"):
        assert status(checks, name) == "N/A", name
    assert "NOTHING TO ACCEPT YET" in cmp.format_checks(checks)


def test_a_flag_that_changes_nothing_fails_the_active_check_and_the_rest_stay_not_applicable():
    rows = [row(), row(method="v2")]                                    # v2 without diagnostics.method_v2
    checks = cmp.acceptance(rows, [])
    assert status(checks, "v2 changes the portfolio") == "FAIL" and status(checks, "v2_* diagnostics") == "FAIL"
    assert status(checks, "effective N") == "N/A" and status(checks, "peak memory") == "N/A"
    assert "NOT ACCEPTED" in cmp.format_checks(checks)


def test_a_v2_that_changes_the_portfolio_but_has_no_diagnostics_is_measured_and_flagged():
    rows = [row(), row(method="v2", weights={"A": 0.2, "B": 0.3, "C": 0.5}, eff_n=4.0, largest=0.5)]
    checks = cmp.acceptance(rows, [])
    assert status(checks, "v2 changes the portfolio") == "PASS" and status(checks, "v2_* diagnostics") == "FAIL"
    assert status(checks, "effective N") == "FAIL"                  # measured, and it does not meet the floor
    text = cmp.format_table(rows)
    assert "v2?" in text and "differs from v1 but carries no v2_* diagnostics" in text and "n/a" not in text


def test_a_good_v2_passes_every_measurable_check():
    checks = cmp.acceptance(pairs(), repeats())
    assert {c.status for c in checks} == {"PASS", "MANUAL"}, [(c.name, c.status, c.detail) for c in checks]
    assert "ALL MEASURABLE CHECKS PASS" in cmp.format_checks(checks)


def test_a_run_that_crashed_fails_the_first_check():
    rows = pairs() + [{"persona": "x", "goal": "none", "method": "v2", "ok": False, "error": "boom"}]
    checks = cmp.acceptance(rows, repeats())
    assert status(checks, "every run finished") == "FAIL" and "boom" in checks[0].detail


def test_the_risk_engine_rule():
    rows = pairs()
    rows[1]["approved"] = False                                          # v1 approved, v2 not
    assert status(cmp.acceptance(rows, repeats()), "Risk Engine approves") == "FAIL"
    rows = pairs()
    rows[0]["approved"] = False                                          # v1 not approved: nothing to hold v2 to
    rows[1]["approved"] = False
    assert status(cmp.acceptance(rows, repeats()), "Risk Engine approves") == "PASS"
    rows = pairs()
    rows[1]["approved"] = True
    rows[0]["approved"] = False                                          # v2 approves more than v1: fine
    assert status(cmp.acceptance(rows, repeats()), "Risk Engine approves") == "PASS"


def test_the_diversification_rule_applies_to_c3_to_c5_only_and_a_flag_excuses_it():
    for kw in (dict(eff_n=5.9), dict(largest=0.31)):
        rows = pairs()
        rows[1].update(kw)
        assert status(cmp.acceptance(rows, repeats()), "effective N") == "FAIL", kw
        rows[1]["v2"] = {**rows[1]["v2"], "diversification_floor_met": False}         # the universe cannot allow it
        assert status(cmp.acceptance(rows, repeats()), "effective N") == "PASS", kw
    rows = pairs()
    rows[1].update(c_level="C2", eff_n=2.0, largest=0.9)                              # C1 and C2 are exempt
    rows[0]["c_level"] = "C2"
    assert status(cmp.acceptance(rows, repeats()), "effective N") == "PASS"
    rows = pairs()
    rows[1].update(eff_n=6.0, largest=0.30)                                           # exactly at the limits is fine
    assert status(cmp.acceptance(rows, repeats()), "effective N") == "PASS"


def test_the_goal_tolerance_is_the_contracts_tau():
    assert cmp.tolerance(0.80) == pytest.approx(0.03) and cmp.tolerance(0.20) == pytest.approx(0.02)
    assert cmp.tolerance(0.05) == pytest.approx(0.01) and cmp.tolerance(0.0) == pytest.approx(0.01)
    assert cmp.tolerance(0.30) == pytest.approx(0.03) and cmp.tolerance(0.25) == pytest.approx(0.025)


def test_the_goal_chance_rule():
    rows = pairs()                                                                    # v1 80%: tolerance 3 points
    rows[3]["chance"] = 0.77
    assert status(cmp.acceptance(rows, repeats()), "goal chance") == "PASS"
    rows[3]["chance"] = 0.769
    assert status(cmp.acceptance(rows, repeats()), "goal chance") == "FAIL"
    rows[3]["chance"] = 0.95                                                          # higher is always fine
    assert status(cmp.acceptance(rows, repeats()), "goal chance") == "PASS"
    only_none = [r for r in pairs() if r["goal"] == "none"]
    assert status(cmp.acceptance(only_none, repeats()), "goal chance") == "N/A"


def test_the_drawdown_rule():
    rows = pairs()
    rows[3]["dd90"] = 0.10                                                            # equal is fine
    assert status(cmp.acceptance(rows, repeats()), "worst-10% drawdown") == "PASS"
    rows[3]["dd90"] = 0.1001
    assert status(cmp.acceptance(rows, repeats()), "worst-10% drawdown") == "FAIL"


def test_the_time_and_memory_rules():
    rows = pairs()
    rows[1]["cpu_s"] = 45.0
    rows[1]["peak_mb"] = 450.0
    checks = cmp.acceptance(rows, repeats())
    assert status(checks, "heaviest persona") == "PASS" and status(checks, "peak memory") == "PASS"
    rows[1]["cpu_s"], rows[1]["peak_mb"] = 45.1, 450.1
    checks = cmp.acceptance(rows, repeats())
    assert status(checks, "heaviest persona") == "FAIL" and status(checks, "peak memory") == "FAIL"
    rows = pairs()
    rows[0]["cpu_s"] = rows[0]["peak_mb"] = 10_000                                    # v1 is not held to the v2 limits
    assert status(cmp.acceptance(rows, repeats()), "heaviest persona") == "PASS"


def test_the_determinism_and_seed_rules():
    assert status(cmp.acceptance(pairs(), repeats()), "same input twice") == "PASS"
    first = [r for r in pairs() if r["persona"] == "balanced_saver" and r["goal"] == "none"]
    bad = deepcopy(first[1])
    bad["weights"] = {"A": 0.41, "B": 0.59}
    assert status(cmp.acceptance(pairs(), [first[0], bad]), "same input twice") == "FAIL"
    assert status(cmp.acceptance(pairs(), [first[0], deepcopy(first[1])]), "same input twice") == "PASS"
    assert status(cmp.acceptance(pairs(), [{"persona": "balanced_saver", "goal": "none", "method": "v1", "ok": False,
                                            "error": "x"}]), "same input twice") == "FAIL"
    rows = pairs()
    rows[1]["seed"] = rows[3]["seed"]                                                 # two inputs, one seed
    assert status(cmp.acceptance(rows, repeats()), "different goal") == "FAIL"
    rows = pairs()
    rows[1]["seed"] = None                                                            # a v2 run that reports no seed
    checks = cmp.acceptance(rows, repeats())
    assert status(checks, "different goal") == "N/A" and "report no seed" in [c for c in checks if "different goal" in c.name][0].detail


# ==========================================================================
# printing
# ==========================================================================
def test_the_table_lists_every_column_marks_inactive_v2_and_reports_errors():
    text = cmp.format_table([row(), row(method="v2"), {"persona": "x", "goal": "none", "method": "v1", "ok": False,
                                                        "error": "no prices"}])
    for column in ("holdings", "effN", "largest", "stocks", "chance", "dd90", "cpu s", "MB"):
        assert column in text
    assert "same as v1 and carries no v2_* diagnostics" in text and "ERROR: no prices" in text
    inactive_line = [x for x in text.splitlines() if x.startswith("balanced_saver") and " v2 " in x + " "][0]
    assert inactive_line.count("n/a") == 11 and "40%" not in inactive_line          # nothing is shown for a v2 that did not run
    assert "40%" in text and "80%" in text and "10.0%" in text                      # the v1 row
    assert "n/a" not in cmp.format_table([v2_row()])
    assert "n/a" in cmp.markdown_table([row(), row(method="v2")]).splitlines()[3]
    assert "v2?" in cmp.markdown_table([row(method="v2")]).splitlines()[2]               # no v1 to compare with
    assert "balanced_saver" in cmp.format_holdings([row()]) and "B 60%" in cmp.format_holdings([row()])
    md = cmp.markdown_table(pairs())
    assert md.splitlines()[0].startswith("| persona") and len(md.splitlines()) == 2 + len(pairs())


def test_the_personas_and_goal_classes_cover_the_demo_personas():
    assert sorted(cmp.PERSONA_ORDER) == sorted(PERSONAS) and len(cmp.PERSONA_ORDER) == 6
    assert {p.expected_class for p in PERSONAS.values()} <= set(cmp.GOAL_GROWTH)


def test_peak_memory_is_measured():
    mb = cmp.peak_memory_mb()
    assert 20 < mb < 20_000


def test_it_runs_today_with_v1_only_end_to_end(tmp_path, capsys):
    out = tmp_path / "result.json"
    code = cmp.main(["--personas", "no_loss_saver", "--goals", "none", "--methods", "v1", "--no-determinism",
                     "--json", str(out)])
    assert code == 0
    printed = capsys.readouterr().out
    assert "no_loss_saver" in printed and "NOTHING TO ACCEPT YET" in printed and "PASS   every run finished" in printed
    data = json.loads(out.read_text(encoding="utf-8"))
    r = data["rows"][0]
    assert r["ok"] and r["method"] == "v1" and r["c_level"] == "C1" and abs(sum(r["weights"].values()) - 1) < 1e-6
    assert r["eff_n"] >= 1 and r["peak_mb"] > 0 and r["cpu_s"] > 0


# ==========================================================================
# the flat v2_* diagnostics of docs/PORTFOLIO_V2.md section 4, and like-for-like notes
# ==========================================================================
def test_the_v2_information_is_read_from_the_flat_keys_and_from_the_nested_draft():
    flat = {"v2_style": "erc", "v2_effective_n": 9.5, "v2_largest": 0.18, "v2_floor_met": 1.0,
            "v2_worst10_drawdown": 0.081, "v2_risk_contribution": '{"VT": 0.2, "AGG": 0.1}', "goal_probability": 0.8}
    diag = cmp.v2_diagnostics(flat)
    assert diag["style"] == "erc" and diag["effective_n"] == 9.5 and diag["worst10_drawdown"] == 0.081
    assert diag["risk_contribution"] == {"VT": 0.2, "AGG": 0.1} and "goal_probability" not in diag
    assert cmp.v2_diagnostics({"v2_risk_contribution": "not json"})["risk_contribution"] == "not json"
    assert cmp.v2_diagnostics({"method_v2": {"method": "erc", "seed": "ab"}}) == {"method": "erc", "seed": "ab"}
    assert cmp.v2_diagnostics({"method_v2": '{"method": "hrp"}'}) == {"method": "hrp"}
    assert cmp.v2_diagnostics({"goal_probability": 0.8, "method_v2": "garbage"}) == {"raw": "garbage"}
    assert cmp.v2_diagnostics({}) is None and cmp.v2_diagnostics({"goal_probability": 0.8}) is None


def test_a_floor_that_was_not_met_is_flagged_so_the_diversification_check_excuses_it():
    rows = pairs()
    rows[1].update(eff_n=4.0)
    rows[1]["v2"] = {**rows[1]["v2"], "floor_met": 0.0}
    assert status(cmp.acceptance(rows, repeats()), "effective N") == "PASS"
    rows[1]["v2"] = {**rows[1]["v2"], "floor_met": 1.0}
    assert status(cmp.acceptance(rows, repeats()), "effective N") == "FAIL"


def test_a_fail_against_a_v1_portfolio_that_is_itself_outside_the_v2_limits_says_so():
    rows = pairs()                                                   # v1: largest 40%, effN 3.6 (both outside the v2 limits)
    rows[3]["chance"] = 0.60
    rows[3]["dd90"] = 0.20
    checks = cmp.acceptance(rows, repeats())
    detail = {c.name: c.detail for c in checks}
    assert "v1 itself breaches the v2 limits: largest 40%, effN 3.6" in detail["goal chance not lower than v1 by more than the tolerance"]
    assert "v1 itself breaches the v2 limits" in detail["worst-10% drawdown not higher than v1"]
    rows = pairs()
    rows[2].update(largest=0.20, eff_n=7.0)                           # a v1 inside the limits: no note
    rows[3]["chance"] = 0.60
    detail = {c.name: c.detail for c in cmp.acceptance(rows, repeats())}
    assert "breaches" not in detail["goal chance not lower than v1 by more than the tolerance"]


def test_the_drawdown_tie_is_zero_by_contract_and_can_be_widened():
    rows = pairs()
    rows[3]["dd90"] = 0.105                                           # half a point above v1's 10%
    assert status(cmp.acceptance(rows, repeats()), "worst-10% drawdown") == "FAIL"
    assert status(cmp.acceptance(rows, repeats(), drawdown_tie=0.01), "worst-10% drawdown") == "PASS"
    rows[3]["dd90"] = 0.1101
    assert status(cmp.acceptance(rows, repeats(), drawdown_tie=0.01), "worst-10% drawdown") == "FAIL"

