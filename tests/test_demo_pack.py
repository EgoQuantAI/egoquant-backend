"""The demo pack stays honest: the document is the template filled from the committed run, the invented IBKR samples
parse, and the run's own claims (categories, verdicts) are the ones the document promises. No network, no AI.

The run itself (`python eval/demo_pack.py`) takes about 30 seconds and is not repeated here; regenerate the numbers
and the document together after a change that moves them:
    python eval/demo_pack.py --out docs/demo_data/demo_numbers.json && python eval/build_demo_doc.py
"""

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

from egoquant.agents.behavioral.ibkr import parse_ibkr_report

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "demo_data"


def build_module():
    spec = importlib.util.spec_from_file_location("build_demo_doc", ROOT / "eval" / "build_demo_doc.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The pack comes in two variants, one per portfolio method, so that making one the default is copying two files:
#   demo_numbers_v1.json + DEMO_SCRIPT_v1.md, demo_numbers_v2.json + DEMO_SCRIPT_v2.md; the active pair is
#   demo_numbers.json + DEMO_SCRIPT.md (a copy of one of them).
VARIANTS = {"v1": (DATA / "demo_numbers_v1.json", ROOT / "docs" / "DEMO_SCRIPT_v1.md"),
            "v2": (DATA / "demo_numbers_v2.json", ROOT / "docs" / "DEMO_SCRIPT_v2.md")}


def test_the_document_is_the_template_filled_from_the_committed_numbers():
    built = build_module().build().splitlines()
    committed = (ROOT / "docs" / "DEMO_SCRIPT.md").read_text(encoding="utf-8").splitlines()
    assert built == committed, "docs/DEMO_SCRIPT.md is stale: run python eval/build_demo_doc.py"
    joined = "\n".join(committed)
    assert "{{" not in joined and "<!--v" not in joined


@pytest.mark.parametrize("method", sorted(VARIANTS))
def test_each_variant_document_is_the_template_filled_from_its_own_numbers(method):
    numbers, document = VARIANTS[method]
    built = build_module().build(numbers).splitlines()
    assert built == document.read_text(encoding="utf-8").splitlines(), (
        f"{document.name} is stale: python eval/build_demo_doc.py --numbers docs/demo_data/{numbers.name} "
        f"--out docs/{document.name}")
    assert json.loads(numbers.read_text(encoding="utf-8"))["_meta"].get("method", "v1") == method


def test_the_default_pack_is_a_copy_of_one_variant_so_that_flipping_it_is_a_file_copy():
    active_numbers = json.loads((DATA / "demo_numbers.json").read_text(encoding="utf-8"))
    active_text = (ROOT / "docs" / "DEMO_SCRIPT.md").read_text(encoding="utf-8")
    matches = [m for m, (numbers, document) in VARIANTS.items()
               if json.loads(numbers.read_text(encoding="utf-8")) == active_numbers and document.read_text(encoding="utf-8") == active_text]
    assert len(matches) == 1, "demo_numbers.json and DEMO_SCRIPT.md must be a copy of the v1 pair or of the v2 pair"


def test_the_two_variants_differ_in_the_wording_that_depends_on_the_method():
    v1, v2 = (VARIANTS[m][1].read_text(encoding="utf-8") for m in ("v1", "v2"))
    assert "Portfolio method: the default one (v1)" in v1 and "Portfolio method: v2" in v2
    assert "in five steps" in v1 and "in seven steps" in v2 and "in seven steps" not in v1
    assert "EGOQUANT_PORTFOLIO_METHOD=v2" in v2
    for text in (v1, v2):
        assert "<!--" not in text and "{{" not in text and "Why not just buy the cheapest fund" in text


@pytest.mark.parametrize("name", ["ibkr_cautious_saver", "ibkr_balanced_investor", "ibkr_eager_beginner"])
def test_the_invented_ibkr_samples_parse_and_say_they_are_invented(name):
    text = (DATA / f"{name}.xml").read_text(encoding="utf-8")
    report = parse_ibkr_report(text, today=date(2026, 10, 19), filename=f"{name}.xml")
    assert report.trades and report.currency == "USD"
    assert "INVENTED" in text and "U0000000" in text


@pytest.mark.parametrize("method", sorted(VARIANTS))
def test_the_committed_run_promises_what_the_document_says(method):
    numbers = json.loads(VARIANTS[method][0].read_text(encoding="utf-8"))
    assert numbers["_meta"]["prices_file"] == "prices_snapshot_yahoo.json.gz" and "no OpenAI call" in numbers["_meta"]["network"]
    assert [numbers[k]["profile"]["c_level"] for k in ("saver", "balanced", "beginner")] == ["C2", "C3", "C5"]     # questionnaire alone
    with_game = {k: numbers[k]["profile_with_game"] for k in ("saver", "balanced", "beginner")}
    assert [with_game[k]["c_level"] for k in with_game] == ["C2", "C3", "C4"]                                      # after the Risk Game
    assert all(p["game_played"] for p in with_game.values())
    assert [p["questionnaire_class"] for p in with_game.values()] == ["C2", "C3", "C5"]
    assert [p["binding_dimension"] for p in with_game.values()] == ["tolerance", "tolerance", "game"]              # only the beginner moved
    assert [numbers[k][v]["c_level"] for v in ("without_ibkr", "with_ibkr") for k in with_game] == ["C2", "C3", "C4"] * 2
    assert numbers["_meta"].get("risk_game") == "played in all three scenarios"
    for key in ("saver", "balanced", "beginner"):
        for variant in ("without_ibkr", "with_ibkr"):
            assert numbers[key][variant]["approved"] is True and numbers[key][variant]["data_source"] == "historical"
    verdicts = {key: [t["verdict"] for t in numbers[key]["trade_checks"]] for key in ("saver", "balanced", "beginner")}
    assert verdicts == {"saver": ["not_advisable", "allowed_with_warnings"],
                        "balanced": ["allowed_with_warnings", "not_advisable"],
                        "beginner": ["not_advisable", "advisable"]}
    assert numbers["balanced"]["what_ifs"][0]["status"] == "blocked"                # the Risk Engine's final say
    behavioural = [l for l in numbers["balanced"]["with_ibkr"]["limits"] if "behavioural" in l["name"].lower()]
    assert behavioural and behavioural[0]["limit"] < 0.20                           # Agent B tightened, never loosened
    assert numbers["balanced"]["with_ibkr"]["equity_weight"] <= numbers["balanced"]["without_ibkr"]["equity_weight"]
    assert "crypto" in " ".join(n["option"] for p in numbers["beginner"]["preferences"] for n in p["not_offered"]).lower() \
        or any("Bitcoin" in n["option"] for p in numbers["beginner"]["preferences"] for n in p["not_offered"])


# ---- the demo video script (eval/demo_video.template.md): same numbers, same variants, same flip ------------------------------------
VIDEO_TEMPLATE = ROOT / "eval" / "demo_video.template.md"
VIDEO_VARIANTS = {"v1": (DATA / "demo_numbers_v1.json", ROOT / "docs" / "DEMO_VIDEO_SCRIPT_v1.md"),
                  "v2": (DATA / "demo_numbers_v2.json", ROOT / "docs" / "DEMO_VIDEO_SCRIPT_v2.md")}


@pytest.mark.parametrize("method", sorted(VIDEO_VARIANTS))
def test_each_video_script_is_the_template_filled_from_its_own_numbers(method):
    numbers, document = VIDEO_VARIANTS[method]
    built = build_module().build(numbers, VIDEO_TEMPLATE).splitlines()
    assert built == document.read_text(encoding="utf-8").splitlines(), (
        f"{document.name} is stale: python eval/build_demo_doc.py --template eval/demo_video.template.md "
        f"--numbers docs/demo_data/{numbers.name} --out docs/{document.name}")


def test_the_default_video_script_is_a_copy_of_the_variant_of_the_default_pack():
    active_numbers = json.loads((DATA / "demo_numbers.json").read_text(encoding="utf-8"))
    active_video = (ROOT / "docs" / "DEMO_VIDEO_SCRIPT.md").read_text(encoding="utf-8")
    matches = [m for m, (numbers, document) in VIDEO_VARIANTS.items()
               if json.loads(numbers.read_text(encoding="utf-8")) == active_numbers and document.read_text(encoding="utf-8") == active_video]
    assert len(matches) == 1, ("docs/DEMO_VIDEO_SCRIPT.md must be a copy of the variant that demo_numbers.json is a copy of: "
                               "flipping the demo pack to v2 also means cp docs/DEMO_VIDEO_SCRIPT_v2.md docs/DEMO_VIDEO_SCRIPT.md")


def spoken(text: str) -> list[str]:
    import re
    return re.findall(r'\*\*Say:\*\* "(.*?)"\n', text, flags=re.S)


@pytest.mark.parametrize("method", sorted(VIDEO_VARIANTS))
def test_the_video_script_has_ten_scenes_a_sensible_length_and_says_nothing_that_reads_as_advice(method):
    import re
    text = VIDEO_VARIANTS[method][1].read_text(encoding="utf-8")
    assert "{{" not in text and "<!--v" not in text
    assert re.findall(r"^## Scene (\d+)", text, flags=re.M) == [str(n) for n in range(1, 11)]
    words = [len(s.split()) for s in spoken(text)]
    assert len(words) == 10 and 650 <= sum(words) <= 850, words                        # about 5 to 6 minutes at 140 words a minute
    for narration in spoken(text):
        low = narration.lower()
        for phrase in ("we recommend", "you should", "guaranteed", "you could buy", "you could sell", "consider buying",
                       "consider selling", "will go up", "go ahead"):
            assert phrase not in low, (phrase, narration[:80])
    for scene in re.findall(r"## Scene 5.*?(?=## Scene 6)", text, flags=re.S):
        assert "hypothetical" in scene and "forecast" in scene                         # the scene that says the chance says what it is

