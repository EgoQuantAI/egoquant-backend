# Z2 addendum: anchoring bond and cash average returns to the rate at t. EXPERIMENT, NOT THE PRODUCTION MODEL

**Question (Z2 finding 1).** Bond-only portfolios were predicted too optimistically: the real outcome fell below the
predicted median 100% of the time. Does replacing the bonds' and cash's historical average return with the rate the
market paid at t fix that?

**Variant.**
- **Cash:** average return = BIL's trailing 63-day return at t, annualized (the short rate).
- **Bonds:** the short rate + a term premium X. X is an assumption, tested at 0.5%, 1.0% and 1.5%.
- **What changes:** only each bond or cash column's mean daily return is shifted, just before the simulation. Every
  real daily move is kept. Equity, the hindsight correction and everything else are unchanged.
- **Test design:** the same 37 start dates, investor types, goals and seeds as the main run (648 cases per variant;
  1,944 runs in total; 24 per variant at 2012-03-31 again had too little history). No repository change: done by
  wrapping the simulation in `eval/zc/z2_calibration.py --anchor-tp`.

**Files.**
- `zc_z2_calibration_20261018_2205.csv` / `.json`: the anchored runs.
- The production baseline is `zc_z2_calibration_20261018_2113.*`.

## Results (Brier: lower is better; band: the real outcome inside the predicted 10–90% range, 80% expected)

**5-year goals:**

| | Production | X = 0.5% | X = 1.0% | X = 1.5% |
|---|---|---|---|---|
| All: Brier / inside band | 0.167 / 78% | 0.174 / 82% | 0.171 / 84% | **0.168 / 86%** |
| C2 bonds: predicted vs real | 45% vs 19% | 17% vs 18% | 20% vs 17% | 24% vs 19% |
| C2: below predicted median (50% expected) | **100%** | 37% | 42% | **54%** |
| C2: Brier | 0.183 | 0.147 | 0.146 | **0.144** |
| C3 mixed: predicted vs real, Brier | 51% vs 45%, **0.098** | 35% vs 50%, 0.128 | 37% vs 50%, 0.121 | 39% vs 50%, 0.113 |
| C4 / C5: Brier | 0.164 / 0.222 | 0.185 / 0.235 | 0.181 / 0.235 | 0.178 / 0.239 |

**10-year goals:**

| | Production | X = 0.5% | X = 1.0% | X = 1.5% |
|---|---|---|---|---|
| All: Brier / inside band | 0.235 / 56% | 0.194 / 74% | 0.189 / 76% | **0.187 / 78%** |
| C2: inside band / Brier | **0% / 0.294** | 80% / 0.073 | 92% / 0.075 | **100% / 0.084** |
| C3: Brier | 0.122 | 0.123 | 0.112 | **0.102** |
| C4 / C5: Brier | 0.184 / 0.340 | 0.259 / 0.321 | 0.252 / 0.319 | 0.246 / 0.317 |

## What it shows

1. **Anchoring fixes the bond-only error.**
   - **5-year goals, C2:** the predicted chance falls from 45% to about 20% against 17–19% reached. The share of
     outcomes below the predicted median goes from 100% to 42–54%.
   - **10-year goals, C2:** band coverage goes from 0% to 92–100%. The cause found in Z2 (historical bond averages
     from a high-yield period) is confirmed.
2. **It makes everything holding bonds a little more pessimistic.**
   - **Why:** in 2012–2026 the stock side was already under-confident (Z2 finding 2), so lowering the bond side adds
     to that.
   - **C3 (~30% stocks):** worse on 5-year goals (Brier 0.098 → 0.113 at 1.5%) and better on 10-year goals
     (0.122 → 0.102).
   - **C4/C5:** slightly worse on 5-year goals. On 10-year goals C4 gets worse (0.184 → 0.246) and C5 slightly
     better (0.340 → 0.317).
3. **Overall effect.** With X = 1.5%:
   - **5-year goals:** Brier unchanged (0.168 vs 0.167), band coverage closer to 80% (86% vs 78%).
   - **10-year goals:** clearly better (Brier 0.187 vs 0.235, coverage 78% vs 56%).
   - X = 1.5% is the best of the three values on both horizons.

## Caveats

- **The premium was picked on this same period.** X was chosen on the data it is scored on (in-sample), and the
  window has about 3 independent 5-year periods. Expect less benefit out of sample.
- **The rate is a proxy.** Real bond yields are not in the cache. Bond funds of different durations (SHY vs TLT) all
  get the same short rate + X; a proper version would use each fund's yield or duration.
- **The stock-side under-confidence in this window is untouched.** It is one strong decade and is not corrected
  here, deliberately.

## Recommendation (Zerya decides; it is quant code)

Worth adopting, as a PROVISIONAL config option. In `run_goal_based`, after the hindsight correction:

- set cash's average return to the short rate at t, taken from BIL, which is a core fund and always priced;
- set bonds' average return to the short rate + `goal_bond_term_premium` (PROVISIONAL 1.0–1.5%);
- record each change in `mean_adjustments`, so the report's "Corrections against over-optimism" step names it.

**Effect for users:** bond-heavy investors would see lower, more honest chances. I can write this as a proposed diff
with tests if you say yes; nothing has been changed.

## Update: written as a proposed change (Zerya said yes)

**Part 1, quant (Zerya's code): `zc_z2_proposed_rate_anchor_part1_quant.diff`.**
- **What it adds:**
  - `goal.short_rate` and `goal.anchor_rate_means`;
  - a `RateAnchor` record on `GoalAnalysis`, holding the rate, the premium and each fund's before/after;
  - PROVISIONAL constants `RATE_ANCHOR = True`, `BOND_TERM_PREMIUM = 0.015`, `SHORT_RATE_PROXY = "BIL"` (63-day window).
- **Where it runs:** wired into `run_goal_based`. BIL is fetched for the rate and kept out of the simulated history
  unless it is held.
- **Tests:** `tests/agent_a/test_goal_rate_anchor.py`, 7 tests.
- **Settings placement:** the constants sit in `goal.py`, not in the shared `models.py`. Moving them into
  `AgentConfig` is a team decision.

**Part 2, outside quant (routed by Onur): `zc_z2_proposed_rate_anchor_part2_outside_quant.diff`.**
- `report.py`: one sentence under "Corrections against over-optimism", so the investor is told.
- `demo/goal_walkthrough.py`: the independent re-derivation does the same, so the reproducibility check stays green.
- `CLAUDE.md`: a note under decision 19.

**Verification.**
- **On `96b94e8`:** the memory-fix diff, then part 1, then part 2 apply cleanly. The full offline suite gives
  892 passed, 1 skipped.
- **Production code vs the experiment:** the Z2 main calibration re-run with the real code
  (`zc_z2_calibration_20261018_2222.*`) is identical, run for run, to the 1.5% experiment: 672 of 672 runs with the
  same prediction, outcome and weights.
- **On `origin/october18` @ `608eff2`:** all three diffs apply cleanly and give 1,015 passed. The same 34 failures
  occur WITHOUT the diffs (1,008 passed): the new IBKR / accounts tests need `defusedxml`, which is not installed in
  this machine's venv. I did not install it, because the venv is shared.
- **Not done:** none of the diffs is committed (Umut commits).
