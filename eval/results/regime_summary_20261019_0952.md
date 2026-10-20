# Regime-aware sampler vs block bootstrap (2026-10-19 09:43)

Commit `82552a6`; frozen real-price cache dated 2026-10-18 (offline); model `e881e35058bfb309`.

## A. Goal chances today (15 years, $100,000 start, +$500/month; 10,000 paths)

Regime today: **calm** (probability 99.5%).

| Portfolio | Goal (x paid in) | Old chance | New chance | Change (points) |
|---|---|---|---|---|
| P1 C1 cash and short Treasuries | 1.2x ($228,000) | 29.4% | 26.8% | -2.6 |
| P1 C1 cash and short Treasuries | 1.5x ($285,000) | 0.0% | 0.0% | +0.0 |
| P1 C1 cash and short Treasuries | 2x ($380,000) | 0.0% | 0.0% | +0.0 |
| P2 C2 bonds only (bond-heavy) | 1.2x ($228,000) | 85.0% | 83.6% | -1.4 |
| P2 C2 bonds only (bond-heavy) | 1.5x ($285,000) | 30.8% | 29.7% | -1.1 |
| P2 C2 bonds only (bond-heavy) | 2x ($380,000) | 0.7% | 0.9% | +0.2 |
| P3 C2 bonds with TIPS | 1.2x ($228,000) | 85.6% | 84.4% | -1.2 |
| P3 C2 bonds with TIPS | 1.5x ($285,000) | 21.4% | 20.4% | -1.0 |
| P3 C2 bonds with TIPS | 2x ($380,000) | 0.1% | 0.2% | +0.1 |
| P4 C3 30% stocks | 1.2x ($228,000) | 97.1% | 97.1% | +0.0 |
| P4 C3 30% stocks | 1.5x ($285,000) | 82.6% | 81.0% | -1.6 |
| P4 C3 30% stocks | 2x ($380,000) | 35.8% | 33.9% | -2.0 |
| P5 C3 40% stocks, long bonds | 1.2x ($228,000) | 96.2% | 96.7% | +0.6 |
| P5 C3 40% stocks, long bonds | 1.5x ($285,000) | 83.8% | 83.0% | -0.8 |
| P5 C3 40% stocks, long bonds | 2x ($380,000) | 46.2% | 44.4% | -1.9 |
| P6 C4 60/40 | 1.2x ($228,000) | 94.2% | 94.7% | +0.5 |
| P6 C4 60/40 | 1.5x ($285,000) | 84.2% | 83.1% | -1.1 |
| P6 C4 60/40 | 2x ($380,000) | 57.5% | 56.0% | -1.5 |
| P7 C4 70% stocks (growth) | 1.2x ($228,000) | 93.2% | 93.4% | +0.2 |
| P7 C4 70% stocks (growth) | 1.5x ($285,000) | 83.7% | 82.9% | -0.8 |
| P7 C4 70% stocks (growth) | 2x ($380,000) | 61.7% | 60.1% | -1.6 |
| P8 C5 90% stocks (equity-heavy) | 1.2x ($228,000) | 91.3% | 90.7% | -0.6 |
| P8 C5 90% stocks (equity-heavy) | 1.5x ($285,000) | 82.6% | 81.7% | -0.9 |
| P8 C5 90% stocks (equity-heavy) | 2x ($380,000) | 65.6% | 64.1% | -1.5 |

## B. Crash realism (5-year paths, no contributions)

| Portfolio | Sampler | Max drawdown p50 / p90 / p99 | Fall >= 20% | Fall >= 10% lasting > 3 months |
|---|---|---|---|---|
| P2 C2 bonds only (bond-heavy) | old | 6.2% / 11.0% / 16.8% | 0.2% | 14.2% |
| P2 C2 bonds only (bond-heavy) | new (today: calm) | 6.1% / 10.8% / 16.5% | 0.1% | 13.3% |
| P2 C2 bonds only (bond-heavy) | new (stationary start) | 6.2% / 10.9% / 16.2% | 0.2% | 14.2% |
| P2 C2 bonds only (bond-heavy) | history | 4.5% / 16.8% / 16.8% | 0.0% | 31.2% |
| P6 C4 60/40 | old | 14.3% / 26.6% / 40.2% | 26.4% | 75.8% |
| P6 C4 60/40 | new (today: calm) | 14.0% / 26.5% / 40.4% | 25.4% | 72.6% |
| P6 C4 60/40 | new (stationary start) | 14.3% / 26.8% / 40.5% | 26.6% | 74.5% |
| P6 C4 60/40 | history | 13.2% / 19.3% / 32.9% | 9.8% | 54.3% |
| P8 C5 90% stocks (equity-heavy) | old | 22.3% / 39.3% / 56.2% | 58.5% | 95.5% |
| P8 C5 90% stocks (equity-heavy) | new (today: calm) | 22.0% / 38.9% / 55.9% | 57.1% | 94.2% |
| P8 C5 90% stocks (equity-heavy) | new (stationary start) | 22.2% / 39.7% / 56.2% | 58.5% | 95.1% |
| P8 C5 90% stocks (equity-heavy) | history | 20.5% / 24.8% / 48.5% | 55.5% | 100.0% |

Long crises (3+ consecutive crisis months) in 5 years: regime chains 72.0% (starting from today's calm regime), 74.3% from the stationary distribution; history 75.0% of 172 overlapping 5-year windows. history windows use the same daily returns as the simulation (hindsight-corrected averages), consecutive 21-day months, monthly rebalancing and fees; they overlap heavily.

## C. Calibration backtest (walk-forward regime models; real outcomes)

**5y goals:** 888 cases, 37 start dates, reached 52%.

| | Mean predicted | Brier | Brier skill vs base rate |
|---|---|---|---|
| Old (block bootstrap) | 43.1% | 0.1791 | +28.2% |
| New (regime) | 42.2% | 0.1821 | +27.0% |
| Base rate only | 52.1% | 0.2495 | 0 |

Brier new - old: +0.0030 (95% interval resampling start dates: +0.0005 to +0.0059).

| Portfolio | Reached | Mean pred old | Mean pred new | Brier old | Brier new |
|---|---|---|---|---|---|
| P1 C1 cash and short Treasuries | 14% | 18% | 17% | 0.114 | 0.114 |
| P2 C2 bonds only (bond-heavy) | 18% | 47% | 46% | 0.203 | 0.200 |
| P3 C2 bonds with TIPS | 20% | 40% | 39% | 0.148 | 0.146 |
| P4 C3 30% stocks | 46% | 51% | 50% | 0.095 | 0.093 |
| P5 C3 40% stocks, long bonds | 68% | 54% | 53% | 0.105 | 0.107 |
| P6 C4 60/40 | 77% | 46% | 45% | 0.210 | 0.216 |
| P7 C4 70% stocks (growth) | 85% | 46% | 45% | 0.256 | 0.265 |
| P8 C5 90% stocks (equity-heavy) | 90% | 44% | 42% | 0.302 | 0.317 |

| Predicted | Old: cases / reached | New: cases / reached |
|---|---|---|
| 0%-10% | 170 / 0% | 175 / 1% |
| 10%-20% | 113 / 27% | 121 / 28% |
| 20%-30% | 85 / 41% | 87 / 40% |
| 30%-40% | 49 / 51% | 42 / 62% |
| 40%-50% | 110 / 75% | 116 / 73% |
| 50%-60% | 89 / 80% | 85 / 81% |
| 60%-70% | 55 / 85% | 48 / 83% |
| 70%-80% | 58 / 90% | 56 / 91% |
| 80%-90% | 43 / 88% | 53 / 92% |
| 90%-100% | 116 / 72% | 105 / 69% |

**10y goals:** 408 cases, 17 start dates, reached 55%.

| | Mean predicted | Brier | Brier skill vs base rate |
|---|---|---|---|
| Old (block bootstrap) | 44.3% | 0.2229 | +9.9% |
| New (regime) | 43.2% | 0.2302 | +6.9% |
| Base rate only | 55.1% | 0.2474 | 0 |

Brier new - old: +0.0074 (95% interval resampling start dates: +0.0016 to +0.0137).

| Portfolio | Reached | Mean pred old | Mean pred new | Brier old | Brier new |
|---|---|---|---|---|---|
| P1 C1 cash and short Treasuries | 16% | 23% | 22% | 0.178 | 0.181 |
| P2 C2 bonds only (bond-heavy) | 18% | 54% | 54% | 0.278 | 0.278 |
| P3 C2 bonds with TIPS | 25% | 45% | 45% | 0.134 | 0.134 |
| P4 C3 30% stocks | 45% | 55% | 53% | 0.115 | 0.108 |
| P5 C3 40% stocks, long bonds | 67% | 57% | 56% | 0.061 | 0.063 |
| P6 C4 60/40 | 76% | 43% | 42% | 0.223 | 0.234 |
| P7 C4 70% stocks (growth) | 94% | 41% | 40% | 0.355 | 0.374 |
| P8 C5 90% stocks (equity-heavy) | 100% | 36% | 34% | 0.439 | 0.469 |

| Predicted | Old: cases / reached | New: cases / reached |
|---|---|---|
| 0%-10% | 82 / 5% | 89 / 7% |
| 10%-20% | 68 / 40% | 68 / 44% |
| 20%-30% | 20 / 60% | 20 / 60% |
| 30%-40% | 28 / 89% | 22 / 100% |
| 40%-50% | 33 / 85% | 40 / 82% |
| 50%-60% | 33 / 61% | 34 / 56% |
| 60%-70% | 31 / 81% | 30 / 80% |
| 70%-80% | 30 / 77% | 25 / 76% |
| 80%-90% | 11 / 45% | 7 / 57% |
| 90%-100% | 72 / 78% | 73 / 77% |

## D. Runtime and peak memory of analyze_goal (20 portfolios, 10,000 paths)

| Horizon | Code | Old time | New time | Change | tracemalloc peak old / new | Process peak WS old / new |
|---|---|---|---|---|---|---|
| 15 y | current | 1.00 s | 1.13 s | +13% | 350.41 / 350.44 MB | 486 / 487 MB |
| 15 y | z1_lean | 1.07 s | 1.12 s | +5% | 87.87 / 87.90 MB | 223 / 225 MB |
| 30 y | current | 1.59 s | 1.79 s | +13% | 689.39 / 689.42 MB | 825 / 827 MB |
| 30 y | z1_lean | 1.60 s | 1.75 s | +9% | 165.77 / 165.80 MB | 301 / 303 MB |
| 60 y | current | 3.49 s | 3.89 s | +11% | 1376.35 / 1376.38 MB | 1513 / 1514 MB |
| 60 y | z1_lean | 3.75 s | 4.02 s | +7% | 330.56 / 330.59 MB | 466 / 468 MB |

## E. Limit checks (/portfolio through the HTTP layer, every persona x 3 goals x flag off/on)

- 36 requests, all answered 200: **True**; approved: 36.
- Risk Engine approved a portfolio with a failed check or broken limit: **0** cases.
- Regime sampler ran in 12 requests; fallbacks with the flag on: none.
- Chosen portfolio changed by the flag: none.
- What-If baseline different from the report's chance: 0.
- Median request time: flag off 4.0 s, flag on 3.9 s.

## Acceptance criteria

- brier_not_worse: **FAIL**
- runtime_increase_below_25pct: **PASS**
- peak_memory_not_higher: **FAIL** (largest increase +34 KiB)
- limit_checks_and_what_if: **PASS**
- all tests pass: checked separately (pytest), see the PR description.

**Recommendation: keep EGOQUANT_REGIME_MC off (see the failed criteria)**

