"""Build eval/labeling_sheet.csv from eval/headlines_to_label.csv. No scoring, no labels, no model.

    python eval/make_labeling_sheet.py

Rows are shuffled with a fixed seed (eval.random_seed, 42) and dealt to blocks A-D in turn, so the blocks
are equal in size (+-1) and the result is reproducible (120 rows -> 4 blocks of 30). Columns: block,id,headline,source,matched_tickers,label
(label left empty). See eval/LABELING_GUIDE.md for who labels which blocks.
"""

from __future__ import annotations

import csv
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egoquant.briefing.loader import load_config  # noqa: E402

BLOCKS = ["A", "B", "C", "D"]
# Who labels which blocks: every block (so every headline) is labeled by exactly two people.
ASSIGNMENT = {"person1": ["A", "B"], "person2": ["B", "C"], "person3": ["C", "D"], "person4": ["D", "A"]}
COLUMNS = ["block", "id", "headline", "source", "matched_tickers", "label"]


def make_sheet(rows: list[dict], seed: int) -> list[dict]:
    ordered = sorted(rows, key=lambda r: r["id"])            # independent of the input order
    random.Random(seed).shuffle(ordered)
    out = [{"block": BLOCKS[i % len(BLOCKS)], "id": r["id"], "headline": r["headline"], "source": r["source"],
            "matched_tickers": r["matched_tickers"], "label": ""} for i, r in enumerate(ordered)]
    order = {b: n for n, b in enumerate(BLOCKS)}
    return sorted(out, key=lambda r: order[r["block"]])     # stable: keeps the shuffled order within a block


def main() -> int:
    src = ROOT / "eval" / "headlines_to_label.csv"
    with open(src, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    sheet = make_sheet(rows, load_config().eval.random_seed)
    out = ROOT / "eval" / "labeling_sheet.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(sheet)
    counts = Counter(r["block"] for r in sheet)
    print(f"wrote {len(sheet)} rows to {out}; per block: " + ", ".join(f"{b} {counts[b]}" for b in BLOCKS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
