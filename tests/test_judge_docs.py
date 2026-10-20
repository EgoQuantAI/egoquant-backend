"""The judges' documents (docs/judges/*.md) name files and tests as the evidence for their claims. Every name must exist.

A claim that points at a file or a test that is not there is worse than a claim without evidence, so this reads each backticked
reference in the documents and checks it against the repository: paths (`egoquant/risk_profile/scoring.py`, `tests/...::test_name`),
bare file names (`scoring.py`: somewhere in the repository), and test function names (`test_...`: defined in tests/).
Commands, environment variables, code fragments and anything with a space or a wildcard are not references and are skipped.
Names of frontend files (.ts, .tsx) and the frontend repository's docs/UI_DECISIONS.md and docs/screens are not in this repository
and are not checked.

Documents written by Bartu (OWNED) must pass. For the others the same problems are reported as warnings, so that their authors see
them; set JUDGE_DOCS_STRICT=1 (the release check) to make them fail too.
"""

import os
import re
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JUDGES = ROOT / "docs" / "judges"
DOCS = sorted(JUDGES.glob("*.md")) if JUDGES.is_dir() else []
OWNED = {"03_METHODS_AND_MODELS.md", "05_USER_GUIDE_AND_DEMO.md"}
PATH_ROOTS = ("tests/", "egoquant/", "eval/", "docs/", "supabase/", "scripts/")
OTHER_REPOSITORY = ("docs/UI_DECISIONS.md", "docs/screens")
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}


def repository_names() -> set[str]:
    return {p.name for p in ROOT.rglob("*") if p.is_file() and not (SKIP_DIRS & set(p.relative_to(ROOT).parts))}


def defined_tests() -> set[str]:
    names = set()
    for path in (ROOT / "tests").rglob("*.py"):
        names |= set(re.findall(r"^\s*(?:async\s+)?def (test_\w+)\(", path.read_text(encoding="utf-8", errors="replace"), flags=re.M))
    return names


def references(text: str) -> list[str]:
    return [m for m in re.findall(r"`([^`\n]+)`", text) if " " not in m and "*" not in m and "<" not in m and "=" not in m]


def missing_references(document: Path) -> list[str]:
    names, tests = repository_names(), defined_tests()
    missing = []
    for ref in references(document.read_text(encoding="utf-8")):
        target, _, test = ref.partition("::")
        if target.startswith(OTHER_REPOSITORY):
            continue
        if target.startswith(PATH_ROOTS):
            target = target.split("[")[0].rstrip("/")
            exists = (ROOT / target).exists() or bool(list((ROOT / target).parent.glob(Path(target).name + "*")))
            if not exists or (test and test not in tests):
                missing.append(ref)
        elif re.fullmatch(r"test_\w+", target):
            if target not in tests:
                missing.append(ref)
        elif re.fullmatch(r"[\w\-]+\.(py|yaml|json|md|xml)", target):
            if target not in names and not (JUDGES / target).exists():
                missing.append(ref)
    return sorted(set(missing))


@pytest.mark.skipif(not DOCS, reason="no judges' documents in this checkout")
@pytest.mark.parametrize("document", DOCS, ids=lambda p: p.name)
def test_every_file_and_test_a_judges_document_names_exists(document):
    missing = missing_references(document)
    message = f"{document.name} names things that do not exist: {missing}"
    if document.name in OWNED or os.environ.get("JUDGE_DOCS_STRICT") == "1":
        assert not missing, message
    elif missing:
        warnings.warn(message)
