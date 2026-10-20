"""Labeling handouts and the label merge, with tiny made-up files (offline; needs openpyxl)."""

import csv
import importlib.util
import sys
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

from egoquant.agents.market_intel import DEFAULT_CATALOG  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "eval" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


handouts, merge_labels, sheet_mod = load("make_labeling_handouts"), load("merge_labels"), load("make_labeling_sheet")

TINY = [
    {"block": "A", "id": "t1", "headline": "Treasury yields jump", "source": "S", "matched_tickers": "TLT", "label": ""},
    {"block": "B", "id": "t2", "headline": "REITs slump on weak leasing", "source": "S", "matched_tickers": "VNQ", "label": ""},
    {"block": "C", "id": "t3", "headline": "Bitcoin ETF flows split", "source": "S", "matched_tickers": "IBIT BTCUSDT", "label": ""},
    {"block": "D", "id": "t4", "headline": "Gold slides as dollar firms", "source": "S", "matched_tickers": "IAU", "label": ""},
]


def make_files(tmp_path, labels: dict[str, dict[str, str]], sheet=TINY) -> dict[str, Path]:
    """Write one handout per person, then fill the label column like a labeler would."""
    files = {}
    for person in sheet_mod.ASSIGNMENT:
        path = tmp_path / f"{person}.xlsx"
        handouts.write_handout(path, handouts.person_rows(sheet, person))
        wb = openpyxl.load_workbook(path)
        ws = wb["Labels"]
        for row in ws.iter_rows(min_row=2):
            rid = row[4].value
            row[3].value = labels.get(person, {}).get(rid)
        wb.save(path)
        files[person] = path
    return files


GOOD = {"person1": {"t1": "negative", "t2": "negative"}, "person2": {"t2": "negative", "t3": "neutral"},
        "person3": {"t3": "positive", "t4": "Negative"}, "person4": {"t4": "negative", "t1": "negative"}}


# ---- merge ------------------------------------------------------------------------------------------
def test_merge_pairs_labels_and_reports_disagreements(tmp_path):
    merged, problems = merge_labels.merge(TINY, make_files(tmp_path, GOOD))
    assert problems == []
    assert [(r["id"], r["label_a"], r["label_b"], r["agree"]) for r in merged] == [
        ("t1", "negative", "negative", "yes"),     # person1 + person4
        ("t2", "negative", "negative", "yes"),     # person1 + person2
        ("t3", "neutral", "positive", "no"),       # person2 + person3: a disagreement
        ("t4", "negative", "negative", "yes"),     # labels are case-insensitive
    ]


def test_merge_rejects_missing_or_invalid_labels(tmp_path):
    bad = {**GOOD, "person2": {"t2": "positive!", "t3": None}}
    _, problems = merge_labels.merge(TINY, make_files(tmp_path, bad))
    assert any("person2.xlsx" in p and "'positive!'" in p for p in problems)
    assert any("person2.xlsx" in p and "None" in p for p in problems)


def test_merge_rejects_edited_ids_and_missing_files(tmp_path):
    files = make_files(tmp_path, GOOD)
    wb = openpyxl.load_workbook(files["person3"])
    wb["Labels"]["E2"].value = "edited"
    wb.save(files["person3"])
    files["person4"] = tmp_path / "nope.xlsx"
    _, problems = merge_labels.merge(TINY, files)
    assert any("person3.xlsx" in p and "missing" in p for p in problems)
    assert any("person3.xlsx" in p and "edited" in p for p in problems)
    assert any("nope.xlsx: file not found" in p for p in problems)


def test_main_writes_nothing_when_checks_fail(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(merge_labels, "ROOT", tmp_path)
    (tmp_path / "eval").mkdir()
    with open(tmp_path / "eval" / "labeling_sheet.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(TINY[0]))
        w.writeheader()
        w.writerows(TINY)
    folder = tmp_path / "returned"
    folder.mkdir()
    make_files(folder, {**GOOD, "person1": {"t1": "negative"}})          # t2 left empty
    assert merge_labels.main([str(folder)]) == 1
    assert not (tmp_path / "eval" / "labels_merged.csv").exists()
    make_files(folder, GOOD)
    assert merge_labels.main([str(folder)]) == 0
    out = capsys.readouterr().out
    assert "agreement: 3/4 = 75.0%" in out and "t3" in out
    rows = list(csv.DictReader(open(tmp_path / "eval" / "labels_merged.csv")))
    assert list(rows[0]) == ["id", "headline", "label_a", "label_b", "agree"] and len(rows) == 4


# ---- handouts -------------------------------------------------------------------------------------------
def test_handout_layout_dropdown_and_instructions(tmp_path):
    path = tmp_path / "p.xlsx"
    handouts.write_handout(path, handouts.person_rows(TINY, "person1"))
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["Labels", "Instructions"]
    ws = wb["Labels"]
    assert [c.value for c in ws[1]] == ["row", "headline", "fund", "label", "id (do not edit)"]
    assert ws.freeze_panes == "A2" and ws["B2"].alignment.wrap_text
    assert ws["E2"].fill.fgColor.rgb.endswith("EDEDED")                     # greyed id column
    assert {ws.cell(row=r, column=3).value for r in (2, 3)} == {"TLT - long-term US Treasury bonds",
                                                                 "VNQ - US real estate (REITs)"}
    dv = ws.data_validations.dataValidation[0]
    assert dv.type == "list" and dv.formula1 == '"positive,negative,neutral"'
    assert dv.showErrorMessage and dv.errorStyle == "stop" and str(dv.sqref) == "D2:D3"
    text = "\n".join(str(c.value or "") for c in wb["Instructions"]["A"])
    for phrase in ("holds the fund", "positive", "negative", "neutral", "Do not open links", "unsure",
                   "Label alone", "No AI tools", "Treasury yields jump", "TLT", "BIL",
                   "Send this file back to Bartu by Monday 14:00 Abu Dhabi time."):
        assert phrase in text, phrase
    assert "eval/" not in text and ".py" not in text                       # no repository paths


def test_several_funds_are_joined():
    assert handouts.fund_text("IBIT BTCUSDT") == "IBIT - bitcoin fund (ETF); BTCUSDT - bitcoin"


def test_plain_names_cover_the_labeling_sheet_and_fall_back_to_the_catalog():
    """Agent C's catalog grew (single stocks, more ETFs): every ticker in the labeling sheet must have a plain
    name; any other catalog ticker falls back to its catalog name; no plain name for an unknown ticker."""
    catalog = {i.ticker: i.name for i in DEFAULT_CATALOG}
    assert set(handouts.PLAIN_NAMES) <= set(catalog)
    sheet = list(csv.DictReader(open(ROOT / "eval" / "labeling_sheet.csv", encoding="utf-8")))
    assert {t for r in sheet for t in r["matched_tickers"].split()} <= set(handouts.PLAIN_NAMES)
    other = next(t for t in catalog if t not in handouts.PLAIN_NAMES)
    assert handouts.fund_text(other) == f"{other} - {catalog[other]}"


def test_real_sheet_gives_every_headline_to_exactly_two_people():
    sheet = list(csv.DictReader(open(ROOT / "eval" / "labeling_sheet.csv", encoding="utf-8")))
    assert len(sheet) == 120 and {b: sum(r["block"] == b for r in sheet) for b in "ABCD"} == {b: 30 for b in "ABCD"}
    seen = {}
    for person in sheet_mod.ASSIGNMENT:
        rows = handouts.person_rows(sheet, person)
        assert len(rows) == 60
        assert rows == handouts.person_rows(sheet, person)                  # fixed seed per person
        assert [r["block"] for r in rows] != sorted(r["block"] for r in rows)   # shuffled: blocks not visible
        for r in rows:
            seen[r["id"]] = seen.get(r["id"], 0) + 1
    assert set(seen.values()) == {2} and len(seen) == 120


# ---- typo fixes, kappa, adjudication ------------------------------------------------------------------------
def test_obvious_typos_are_fixed_and_reported(tmp_path):
    merge_labels.FIXES.clear()
    labels = {**GOOD, "person4": {"t4": "Netural ", "t1": "negative"}}
    merged, problems = merge_labels.merge(TINY, make_files(tmp_path, labels))
    assert problems == []
    assert any("person4.xlsx" in f and "'Netural ' -> 'neutral'" in f for f in merge_labels.FIXES)
    assert {r["id"]: (r["label_a"], r["label_b"]) for r in merged}["t4"] == ("negative", "neutral")


def test_unfixable_values_are_still_rejected(tmp_path):
    _, problems = merge_labels.merge(TINY, make_files(tmp_path, {**GOOD, "person1": {"t1": "maybe", "t2": "negative"}}))
    assert any("'maybe'" in p for p in problems)


def test_cohen_kappa_and_pairs():
    assert merge_labels.cohen_kappa(["positive", "positive", "neutral", "neutral"],
                                    ["positive", "neutral", "neutral", "neutral"]) == pytest.approx(0.5)
    assert merge_labels.cohen_kappa(["neutral"] * 3, ["neutral"] * 3) is None          # undefined, not 1/0
    per_person = {p: dict(GOOD[p]) for p in GOOD}
    per_person["person3"]["t4"] = "negative"
    pairs = {(x, y): (block, n, k) for x, y, block, n, k in merge_labels.pair_kappas(TINY, per_person)}
    assert set(pairs) == {("person1", "person2"), ("person1", "person4"), ("person2", "person3"), ("person3", "person4")}
    assert pairs[("person1", "person2")][:2] == ("B", 1) and pairs[("person2", "person3")][:2] == ("C", 1)


def adjudication_setup(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "eval").mkdir(parents=True)
    with open(root / "eval" / "labeling_sheet.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(TINY[0]))
        w.writeheader()
        w.writerows(TINY)
    monkeypatch.setattr(merge_labels, "ROOT", root)
    folder = root / "eval" / "labeling"
    folder.mkdir()
    make_files(folder, GOOD)                    # one disagreement: t3 (neutral vs positive)
    return root, folder


def fill_final(path, value):
    wb = openpyxl.load_workbook(path)
    wb["Disagreements"]["F2"].value = value
    wb.save(path)


def test_adjudication_flow(tmp_path, monkeypatch, capsys):
    root, folder = adjudication_setup(tmp_path, monkeypatch)
    labeled = root / "eval" / "headlines_labeled.csv"
    dis = folder / "disagreements.xlsx"

    # 1st run: creates the adjudication workbook, writes no labels yet
    assert merge_labels.main([str(folder)]) == 0
    assert dis.exists() and not labeled.exists()
    wb = openpyxl.load_workbook(dis)
    ws = wb["Disagreements"]
    assert wb.sheetnames == ["Disagreements", "Instructions"]
    assert [c.value for c in ws[1]] == merge_labels.DIS_HEADERS
    assert [c.value for c in ws[2]][1:5] == ["Bitcoin ETF flows split", "IBIT - bitcoin fund (ETF); BTCUSDT - bitcoin",
                                             "neutral", "positive"]
    assert ws["F2"].value is None and ws["G2"].value == "t3"
    dv = ws.data_validations.dataValidation[0]
    assert dv.formula1 == '"positive,negative,neutral"' and str(dv.sqref) in ("F2", "F2:F2") and dv.errorStyle == "stop"
    assert not any("person" in str(v) for row in ws.iter_rows(values_only=True) for v in row if v)   # no names
    text = "\n".join(str(c.value or "") for c in wb["Instructions"]["A"])
    assert "holds the listed fund" in text and "headline only" in text and "unsure, choose neutral" in text

    # 2nd run with the final still empty: nothing written
    assert merge_labels.main([str(folder)]) == 1 and not labeled.exists()

    # an invalid final is rejected
    fill_final(dis, "maybe")
    assert merge_labels.main([str(folder)]) == 1 and not labeled.exists()

    # filled: headlines_labeled.csv is written; the workbook is never overwritten
    fill_final(dis, "positive")
    before = dis.read_bytes()
    assert merge_labels.main([str(folder)]) == 0
    assert dis.read_bytes() == before
    rows = list(csv.DictReader(open(labeled)))
    assert list(rows[0]) == ["id", "headline", "label", "labeler"]
    assert [(r["id"], r["label"], r["labeler"]) for r in rows] == [
        ("t1", "negative", "agreed"), ("t2", "negative", "agreed"),
        ("t3", "positive", "adjudicated"), ("t4", "negative", "agreed")]
    assert "example" not in labeled.read_text()                                    # no example rows
    assert "wrote 4 labeled headlines" in capsys.readouterr().out


def test_no_disagreements_writes_labels_directly(tmp_path, monkeypatch):
    root, folder = adjudication_setup(tmp_path, monkeypatch)
    make_files(folder, {**GOOD, "person3": {"t3": "neutral", "t4": "negative"}})
    assert merge_labels.main([str(folder)]) == 0
    assert not (folder / "disagreements.xlsx").exists()
    assert {r["labeler"] for r in csv.DictReader(open(root / "eval" / "headlines_labeled.csv"))} == {"agreed"}


def test_real_label_file_is_gitignored_and_examples_moved_to_tests():
    gitignore = (ROOT / ".gitignore").read_text()
    assert "eval/headlines_labeled.csv" in gitignore and "eval/labeling/" in gitignore
    example = ROOT / "tests" / "briefing" / "data" / "headlines_labeled_example.csv"
    assert sum(1 for _ in csv.DictReader(open(example))) == 10


def test_disagreements_path_option(tmp_path, monkeypatch):
    root, folder = adjudication_setup(tmp_path, monkeypatch)
    assert merge_labels.main([str(folder)]) == 0                      # creates disagreements.xlsx
    named = folder / "disagreements_labeled.xlsx"
    (folder / "disagreements.xlsx").rename(named)
    fill_final(named, "neutral")
    assert merge_labels.main([str(folder), "--disagreements", str(named)]) == 0
    assert not (folder / "disagreements.xlsx").exists()               # the user's file is used, nothing renamed
    rows = list(csv.DictReader(open(root / "eval" / "headlines_labeled.csv")))
    assert {(r["id"], r["label"], r["labeler"]) for r in rows if r["labeler"] == "adjudicated"} == {("t3", "neutral", "adjudicated")}
