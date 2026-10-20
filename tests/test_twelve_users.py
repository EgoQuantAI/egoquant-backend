"""The twelve-user check (eval/twelve_users.py) stays runnable and clean: two investors whose category is set by their finances
(a bold game against a low cap, a short horizon with strong finances) go through every route in a separate process (the script
blocks the network for the process it runs in, so it must not be imported here). No network, no AI, a few seconds.

The whole run (13 investors and the error sentences, about 4 minutes) is a manual check:
    python eval/twelve_users.py --out twelve.json --texts twelve.md
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_two_investors_pass_every_rule_of_the_twelve_user_check(tmp_path):
    out, texts = tmp_path / "run.json", tmp_path / "run.md"
    done = subprocess.run([sys.executable, str(ROOT / "eval" / "twelve_users.py"), "--only", "bold_low_cap", "short_horizon",
                           "--out", str(out), "--texts", str(texts)], cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-800:]
    run = json.loads(out.read_text(encoding="utf-8"))
    assert run["findings"] == [], run["findings"]
    results = run["results"]
    # a bold game does not lift a low cap; a horizon under a year limits the capacity however strong the finances are
    assert results["bold_low_cap"]["profile"]["c_level"] == "C2" and results["bold_low_cap"]["profile"]["binding_dimension"] == "capacity"
    short = results["short_horizon"]["profile"]
    assert (short["c_level"], short["capacity_level"], short["binding_dimension"]) == ("C2", 2, "capacity")
    assert short["tolerance_level"] >= 4                                                  # the attitude alone would have allowed more
    text = texts.read_text(encoding="utf-8")
    assert "because of your investment horizon (less than 1 year)" in text and "weighs the least" not in text
    for key in ("bold_low_cap", "short_horizon"):
        for built in results[key]["builds"].values():
            report = built["built"]["report"]
            assert built["built"]["approved"] and (report["equity_weight"] or 0) == 0     # C2: no stocks at all
