# Portfolio method v2: decision sheet for Savas (written 20 Oct, ~02:30 Dubai; numbers measured on `oct19` 05c360b)

**Decision needed (planned for Tuesday 09:00):** keep the default engine (**v1**, what runs today) or switch to **v2**. The contract
in `docs/PORTFOLIO_V2.md` section 5 says: *if any acceptance item fails, the flag stays v1.* Two items fail, so **the recommendation is v1**
(details and a middle way below). Nothing has to be done to stay on v1: the flag is off by default and v2 ships in the code, tested.

## What v2 is, in plain words
v1 builds a portfolio from about 11 to 100 products that were picked for you. v2 looks at **every product you are allowed to hold**
(up to 292) and keeps one fund from each group of look-alike funds. It then compares many ways of splitting your money (for example "give
every fund the same share", "spread the risk evenly", "spread across things that do not move together") and picks the safest one
that still gives about the same chance of reaching your goal. (The method names, for the technical reader: risk parity, hierarchical
risk parity, maximum diversification, Black-Litterman, resampled efficiency, equal weight, inverse volatility, interest tilt.) No fund
may exceed **30%** (categories C3 to C5), at most 20 positions, and among the near-best candidates the one with the **smallest drawdown
in a bad stretch** is chosen. Same input, same portfolio (fixed seed). The Risk Engine still has the last word; no AI picks anything.

**Words used below.** *Effective holdings*: how many equal-sized funds the portfolio is as spread out as (20 funds with one huge one can
count as 3). *Drawdown*: the biggest fall from a peak. *1-point tie*: drawdowns within one percentage point count as equal. *Goal
chance*: in how many of 100 simulated futures the goal is reached (a simulation on past data, not a forecast).

## The acceptance table (6 demo personas, with and without a goal; harness `eval/portfolio_v2_compare.py --drawdown-tie 0.01`)
| Check | Result | Note |
|---|---|---|
| Risk Engine approves every v2 portfolio | **PASS** 12/12 | |
| Same input twice gives the same portfolio | **PASS** | v1 and v2 |
| Effective number of holdings >= 6 and largest <= 30% (C3 to C5) | **PASS** | v1 put up to 40% in one fund (C3 to C5) |
| Worst-10% drawdown not higher than v1 | **PASS** | with the agreed 1-point tie |
| Peak memory <= 450 MB for a goal 30 years away (the cloud server is small, so memory is budgeted) | **PASS** | 286 to 301 MB |
| Our automatic tests (about 3,150) | **PASS** | all pass except 3 that are known and unrelated (a news screen test, a timing test of it, and a Windows file-permission test) |
| Goal chance not lower than v1 by more than 3 points | **FAIL** | balanced_saver (C3) 86 -> 76; eager_beginner (C5) 56 -> 48 |
| Heaviest demo investor builds in <= 45 s | **FAIL, borderline** | goal 30 years away, first build on a quiet PC: growth_investor 58 s, crypto_trader 44 s, eager_beginner 28 s, balanced_saver 32 s; the same builds when the server is warm: about 26 s; v1: 11 to 39 s (goal 15 years away, busy PC) |

## Why the two items fail (so nobody argues about the wrong thing)
* **eager_beginner:** v1's own portfolio breaks v2's rules (40% in one fund, 3 effective holdings). v2 must refuse that, and the extra
  diversification costs 8 points of simulated chance. This is the rule working, not a defect.
* **balanced_saver:** in our simulation all bond funds earn about the same, cash earns a little less, and gold earns its own past
  average. v1's small hand-picked list puts the maximum allowed gold (10%) and four intermediate bond funds into every candidate. v2
  spreads the money over many bond and cash-like funds (which earn the least in the simulation) and holds gold only where it lowers risk.
  So the simulation gives v2 a lower chance (76% against 86%) although v2 is more spread out (15.6 effective holdings against 6.2). That
  is a feature of how the simulation treats these funds, not a worse portfolio. (Checked on all 38 approved candidates, not only 20.)
* **time:** one heavy build runs about 320 small optimisations. Damla tried two savings (a repeated price lookup, and skipping the
  optimiser when a candidate already fits every limit): the first saves about 2 seconds, the second almost never applies, so neither
  was merged. A heavy build takes 44 to 58 seconds the first time after a restart and about 26 seconds afterwards.

## What v2 gives (same table, the other side)
| Persona (goal 15 years) | v1 holdings / effective / largest | v2 | v1 chance | v2 chance |
|---|---|---|---|---|
| growth_investor C4 | 69 / 42.8 / 6% | 21 / 15.6 / 13% | 55% | **78%** |
| crypto_trader C5 | 37 / 31.3 / 4% | 21 / 16.6 / 10% | 58% | **76%** |
| balanced_saver C3 | 11 / 6.2 / 19% | 20 / 15.6 / 10% | **86%** | 76% |
| eager_beginner C5 | 4 / 2.9 / 40% | 20 / 18.1 / 10% | **56%** | 48% |
growth_investor: v1 holds 69 positions (mostly dust), v2 caps at 20 on purpose.
Without a goal (C3 to C5) v2 is clearly more spread: effective holdings 8.6 to 16.5 against 3.4 to 10.4, largest holding 12 to 19% against 20 to 40%:
the "40% in one broad fund" complaint is gone. The demo pack exists in both variants (`docs/DEMO_SCRIPT_v1.md` / `_v2.md`).

## Options
1. **Stay on v1 (recommended).** Zero risk and zero work. Present v2 as built, tested and switchable (flag), with this table.
2. **v2 for builds without a goal only.** Fast (no simulation of futures is needed), fixes v1's "up to 40% in one fund" complaint, and the two failing items do not
   apply (they are goal-chance and goal-build time). About one hour of work and one more test run; say if you want it.
3. **v2 everywhere.** Only with a written waiver of the two failing items. Then: set `EGOQUANT_PORTFOLIO_METHOD=v2` on Railway,
   copy the two v2 demo files over the default pair (`cp docs/demo_data/demo_numbers_v2.json docs/demo_data/demo_numbers.json;
   cp docs/DEMO_SCRIPT_v2.md docs/DEMO_SCRIPT.md`), restart, and re-run the deploy check as `python eval/deploy_check.py https://<backend> --method v2` (it then compares with `demo_numbers_v2.json`; after the file copy the suite stays green). Goal builds then take 30 to 60 s
   against a 120 s proxy limit.

## How to switch (any time, reversible)
Railway variable `EGOQUANT_PORTFOLIO_METHOD` = `v2` (anything else or unset = v1); `GET /data/status` shows the active `portfolio_method`.
The report names the method it used ("+pv2" in the engine version), so a saved report is always labelled.
