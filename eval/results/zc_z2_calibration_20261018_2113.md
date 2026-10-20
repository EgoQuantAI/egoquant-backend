# Z2: Monte Carlo calibration backtest. Can we trust "a 69% chance"? (ZC, Oct 18)

**Short answer.** Overall it looks calibrated, and per asset mix it is not:

- **Overall (5-year goals):** the average prediction was 47%, and 50% of goals were really reached. The real outcome
  fell inside the predicted 10–90% band 78% of the time (80% expected).
- **Why that overall figure misleads:** two opposite errors cancel out.
  - **Bond-only portfolios (C2):** predicted too optimistically. The real outcome fell below the predicted median
    every time.
  - **Equity-heavy portfolios (C4/C5):** predicted too pessimistically. They beat the median in 73–93% of cases.
  - **Mixed portfolios (C3):** close to calibrated.
- **What this means for the "69%":** treat it as a rough guide (± about 20 points), which is how the report already
  words it. The bond-side error has a clear cause, and the fix is proposed below.

## What was tested

**Model.** At each past quarter-end t, the production goal path ran on prices up to t only:

- Agent C, then Agent A's 100 candidates, then Agent B's shortlist;
- 10,000 paths of 21-day blocks, fixed seed, hindsight correction on;
- the portfolio with the highest chance chosen.

The chosen portfolio was then replayed on the prices that actually followed, using the simulation's own rules:
21-trading-day months, monthly rebalancing, fund fees, and $500 a month added to a $100,000 start.

**No look-ahead.**
- A hard price cutoff at t. A fund with no prices before t does not exist at t.
- The universe is Agent C's curated broad funds, with interests "stocks, bonds, gold" ("bonds, cash" for C2).
- Not used: today's most-traded list or single stocks (survivorship bias in the real outcome), and crypto (Binance
  history starts in 2017).

**Goals.** Three per date and type, at 1.05×, 1.25× and 1.5× the money paid in for 5-year goals, and 1.1×, 1.5× and
2.0× for 10-year goals. This gives a spread of predicted chances.

**History limit.** Cached prices start on 2007-05-23. At these dates the simulation therefore saw **5–14 years of
history, against ~20 in production**. This calibration tests a shorter-memory version of the model.

**Run size and timing.** Commit `96b94e8`, offline on the frozen cache. 1,080 runs on 10 workers in 8 minutes.

**Files.**
- `zc_z2_calibration_20261018_2113.csv`: every run, with prediction, percentiles, real outcome and weights.
- `zc_z2_calibration_20261018_2113.json`: the same, plus the summary.
- Script: `eval/zc/z2_calibration.py`.

## Main run (production settings): 37 quarter-ends, 2012-06-30 to 2021-06-30

At 2012-03-31 there were only 1,220 days of history (under the 5-year minimum), so the simulation correctly declined.
Those 24 cases are not counted.

| | 5-year goals | 10-year goals |
|---|---|---|
| Cases (start dates) | 444 (37) | 204 (17) |
| Mean predicted chance / real share reached | 47% / 50% | 47% / 53% |
| Brier score (lower is better) / always predicting the base rate | 0.167 / 0.250 | 0.235 / 0.249 |
| Brier skill vs base rate | **+33%** | +6% |
| Real outcome inside predicted 10–90% band (80% expected) | **78%** | 56% |
| Real outcome below predicted median (50% expected) | 48% | 43% |

**Reliability, 5-year goals (all types):**

| Predicted | Cases | Mean predicted | Really reached (95% CI) |
|---|---|---|---|
| 0–10% | 57 | 4% | 0% (0–6%) |
| 10–20% | 42 | 16% | 24% (14–39%) |
| 20–30% | 66 | 24% | 14% (7–24%) |
| 30–40% | 22 | 37% | 59% (39–77%) |
| 40–50% | 70 | 46% | 59% (47–69%) |
| 50–60% | 41 | 54% | 68% (53–80%) |
| 60–70% | 26 | 65% | 81% (62–92%) |
| 70–80% | 34 | 75% | 91% (77–97%) |
| 80–90% | 17 | 85% | 100% (82–100%) |
| 90–100% | 69 | 95% | **77%** (66–85%) |

Of the 69 cases in the 90–100% bin, the 32 C3 cases were all reached; only 21 of the 37 C2 cases were.

**Per investor type, 5-year goals.** This is where the truth is:

| Type (stocks) | Mean predicted | Really reached | Inside 10–90% band | Below predicted median | Below p10 / above p90 |
|---|---|---|---|---|---|
| C2 (0%, bonds and cash) | 45% | **19%** | **33%** | **100%** | **67%** / 0% |
| C3 (~30%) | 51% | 45% | 95% | 59% | 5% / 0% |
| C4 (~60%) | 48% | 64% | 94% | 27% | 0% / 6% |
| C5 (~85%) | 44% | 73% | 89% | 7% | 0% / 11% |

For 10-year goals the same pattern is stronger: C2 ended below p10 in 100% of cases, C4/C5 above the median in 100%.

## Findings

1. **Bond and cash portfolios are over-optimistic.**
   - **Cause:** bonds and cash keep their own historical average return (the hindsight correction covers only stocks,
     equity funds and crypto). At a 2012–2021 start, that average came from 2007–2012, when yields and cash rates
     were far higher and falling yields gave bonds price gains.
   - **What happened instead:** later returns were close to the low starting yields, then 2022 brought the worst bond
     year in decades.
   - **Result:** a bond-only portfolio's predicted range sat above what happened, every time.
   - **Proposed (quant, needs Zerya's OK; not written yet):** anchor bond and cash averages to the yield at t instead
     of the sample average. The standard approach: for bonds the starting yield is the best predictor of the next
     5–10 years' return (Leibowitz 1986; Vanguard / AQR capital-market assumptions). A cash proxy from the cache: BIL's
     trailing return. Bond yields are not in the cache, so a proxy is needed. I can test it in this same backtest
     before anyone touches the code.

2. **Equity-heavy portfolios were under-confident in 2012–2026.**
   - **Driver:** the hindsight correction sets equity averages to beta × the world market's average over the
     available history. At 2012–2016 starts, that history is dominated by 2008.
   - **What followed:** an unusually strong decade for stocks.
   - **Interpretation:** this is mostly one historical draw, not proof of bias. Overlapping windows mean only about
     3 independent 5-year periods per type (see the caveat below). It is the conservative direction, which suits a
     suitability tool.

3. **The chance itself is informative.**
   - **Ranking:** where the model says low, goals were rarely reached (0–10%: 0 of 57); where it says high, mostly
     reached (70–90%: 47 of 51).
   - **Brier skill:** +33% over always guessing the base rate (5-year goals).
   - **Mixed portfolios (C3):** the 10–90% band held the real outcome 95% of the time, so it was slightly too wide.

4. **10-year goals are weaker** (band coverage 56%). All 17 start dates fall in 2012–2016 and end in 2022–2026, so
   this is close to a single 10-year realization of each portfolio.

## Sensitivity run: NOT THE PRODUCTION MODEL (minimum history lowered to 1 year)

| | 5-year goals | 10-year goals |
|---|---|---|
| Cases (start dates 2008-06-30 to 2012-03-31) | 156 (16) | 156 (16) |
| Brier skill vs base rate | +14% | −13% (worse than the base rate) |
| Inside 10–90% band (80% expected) | 64% | 50% |

- **What was lost:** before 2009 Q2 the core fund VT (listed June 2008) had under a year of prices, so for C3–C5
  Agent C could not build a snapshot. 96 of 408 cases are missing, and C3–C5 start only in 2009 Q2.
- **Result:** with 1–4 years of history, which mostly means the 2008 crash, the predictions have almost no skill. This
  supports the production rule of at least 5 years of history, and ideally 20.

## Caveats (read before quoting any number)

- **Overlap.** Quarterly starts with 5- and 10-year horizons overlap heavily, and the three goals per date share one
  real outcome path. The confidence intervals above treat cases as independent, so they are **too narrow**. Effective
  independent periods are about 3 per type (5-year) and about 1–2 (10-year).
- **Non-overlapping check.** Five pairs of non-overlapping 5-year starts (2012/2017 … 2016/2021, 24 cases each) give
  Brier 0.13–0.18. That is consistent with the full run, but tiny samples.
- **Short memory.** The model here saw 5–14 years of history, not production's 20. Production's longer history
  includes 2006–2007 and would likely change the bond error little, since it is a yield-level effect, not a sample
  length effect.
- **Narrow universe.** The curated broad funds only. The directory's single stocks and crypto are not tested, on
  purpose (survivorship bias in the real outcome).
- **One period.** One market history (2007–2026). A different decade could reverse the equity finding.
