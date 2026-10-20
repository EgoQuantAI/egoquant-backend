"""The backend never needs hmmlearn: it is a dev/eval dependency of scripts/fit_regime_model.py only."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_no_runtime_module_imports_hmmlearn():
    offenders = []
    for path in (ROOT / "egoquant").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            if any(n.split(".")[0] == "hmmlearn" for n in names):
                offenders.append(str(path.relative_to(ROOT)))
    for top in ("main.py", "chat.py", "egoquant_routes.py"):
        assert "hmmlearn" not in (ROOT / top).read_text(encoding="utf-8")
    assert offenders == []


def test_hmmlearn_is_not_a_runtime_requirement():
    assert "hmmlearn" not in (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    assert "hmmlearn" not in pyproject
    assert "hmmlearn" in (ROOT / "eval" / "regime" / "requirements-fit.txt").read_text(encoding="utf-8")


def test_egoquant_imports_and_samples_with_hmmlearn_blocked():
    code = """
import sys, importlib.abc
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name == "hmmlearn" or name.startswith("hmmlearn."):
            raise ImportError("hmmlearn is blocked in this test")
sys.meta_path.insert(0, Block())
import os
os.environ["EGOQUANT_REGIME_MC"] = "1"
import pandas as pd
from datetime import date
import egoquant.regime as r, egoquant.agents.quant.goal, egoquant.agents.what_if, egoquant.report
dates = pd.bdate_range("2008-01-02", "2026-10-16")
dec = r.decide(dates, 21, date(2026, 10, 16), "historical")
assert dec.active, dec.record()
assert dec.draws(len(dates) - 20, 500, 24, 1).shape == (500, 24)
assert "hmmlearn" not in sys.modules
print("ok")
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT), timeout=300)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stderr[-2000:]
