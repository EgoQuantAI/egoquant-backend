# Regime model v2: results

Protocol fixed before any result: `eval/regime/PROTOCOL_v2.md`. Frozen real-price cache (2026-10-18), 10,000 paths, models refitted at every date on earlier data only.

## Calibration backtest (Brier score, lower is better; difference vs the block bootstrap with a 95% date-bootstrap interval)

| Model | Period | 5-year Brier | vs block bootstrap | 10-year Brier | vs block bootstrap |
|---|---|---|---|---|---|
| v1 | all | 0.1821 | +0.0030 (+0.0005 to +0.0059) | 0.2302 | +0.0074 (+0.0016 to +0.0137) |
| v1 | selection | 0.1866 | +0.0044 (-0.0002 to +0.0092) | 0.2302 | +0.0074 (+0.0016 to +0.0137) |
| v1 | confirmation | 0.1774 | +0.0016 (-0.0007 to +0.0039) | - | - |
| spy | all | 0.1785 | -0.0006 (-0.0028 to +0.0016) | 0.2207 | -0.0021 (-0.0048 to +0.0005) |
| spy | selection | 0.1794 | -0.0028 (-0.0056 to +0.0002) | 0.2207 | -0.0021 (-0.0048 to +0.0005) |
| spy | confirmation | 0.1776 | +0.0018 (-0.0012 to +0.0046) | - | - |
| joint | all | 0.1723 | -0.0068 (-0.0100 to -0.0034) | 0.2117 | -0.0112 (-0.0149 to -0.0075) |
| joint | selection | 0.1705 | -0.0117 (-0.0152 to -0.0079) | 0.2117 | -0.0112 (-0.0149 to -0.0075) |
| joint | confirmation | 0.1741 | -0.0017 (-0.0057 to +0.0034) | - | - |

Block bootstrap: 5-year 0.1791, 10-year 0.2229 (all dates); 5-year 0.1822 (selection), 0.1758 (confirmation).

**Selection** (lower 5-year Brier on dates <= 2016-12-31 (ties: spy)): spy 0.1794, joint 0.1705 -> **joint** becomes regime_hmm_v2.

## Per portfolio (5-year, all dates): Brier and mean predicted chance vs share reached

| Portfolio | Reached | Block bootstrap | v1 | v2 spy | v2 joint |
|---|---|---|---|---|---|
| P1 C1 cash and short Treasuries | 14% | 0.1137 (18%) | 0.1135 (17%) | 0.1087 (17%) | 0.1029 (16%) |
| P2 C2 bonds only (bond-heavy) | 18% | 0.2026 (47%) | 0.2000 (46%) | 0.1971 (46%) | 0.1913 (46%) |
| P3 C2 bonds with TIPS | 20% | 0.1481 (40%) | 0.1463 (39%) | 0.1456 (39%) | 0.1424 (39%) |
| P4 C3 30% stocks | 46% | 0.0952 (51%) | 0.0926 (50%) | 0.0945 (50%) | 0.0941 (50%) |
| P5 C3 40% stocks, long bonds | 68% | 0.1053 (54%) | 0.1065 (53%) | 0.1051 (54%) | 0.1018 (54%) |
| P6 C4 60/40 | 77% | 0.2100 (46%) | 0.2165 (45%) | 0.2113 (46%) | 0.2043 (47%) |
| P7 C4 70% stocks (growth) | 85% | 0.2560 (46%) | 0.2651 (45%) | 0.2575 (45%) | 0.2478 (47%) |
| P8 C5 90% stocks (equity-heavy) | 90% | 0.3019 (44%) | 0.3166 (42%) | 0.3080 (43%) | 0.2935 (45%) |

States chosen by BIC at the walk-forward dates: spy: K=4 x37; joint: K=4 x37

## Goal chances today with v2 (joint), 15 years, goal 1.5x paid in

| Portfolio | Block bootstrap | v2 |
|---|---|---|
| P1 C1 cash and short Treasuries | 0.0% | 0.0% |
| P2 C2 bonds only (bond-heavy) | 30.8% | 29.8% |
| P3 C2 bonds with TIPS | 21.4% | 20.8% |
| P4 C3 30% stocks | 82.6% | 81.6% |
| P5 C3 40% stocks, long bonds | 83.8% | 83.5% |
| P6 C4 60/40 | 84.2% | 83.1% |
| P7 C4 70% stocks (growth) | 83.7% | 82.8% |
| P8 C5 90% stocks (equity-heavy) | 82.6% | 81.5% |

Today's regime (v2): normal (100.0%); states ['calm', 'normal', 'stressed', 'crisis'].

## Runtime and peak memory of analyze_goal (v2)

| Horizon | Code | Time old -> new | tracemalloc peak old -> new |
|---|---|---|---|
| 15 y | current | 1.79 -> 2.15 s (+20%) | 350.41 -> 348.13 MB (-2337 KiB) |
| 15 y | z1_lean | 1.75 -> 1.84 s (+5%) | 87.87 -> 85.59 MB (-2337 KiB) |
| 30 y | current | 2.95 -> 3.80 s (+29%) | 689.40 -> 689.40 MB (+2 KiB) |
| 30 y | z1_lean | 3.13 -> 3.36 s (+7%) | 165.77 -> 165.77 MB (+6 KiB) |
| 60 y | current | 6.44 -> 7.65 s (+19%) | 1376.35 -> 1376.35 MB (+5 KiB) |
| 60 y | z1_lean | 7.06 -> 8.26 s (+17%) | 330.56 -> 330.57 MB (+7 KiB) |

## Limit checks with v2 (every persona x 3 goals x flag off/on, HTTP layer)

- requests: 36, all answered 200: True
- Risk Engine inconsistencies: []; approved with a broken limit: []
- regime runs: 12; What-If baseline mismatches: []

## Decision (PROTOCOL_v2.md)

- 5-year Brier not worse (all dates): PASS
- 10-year Brier not worse (all dates): PASS
- 5-year Brier not worse (confirmation period): PASS
- runtime +25% at most: FAIL
- peak memory not higher (strict, tracemalloc): FAIL
- limit checks and What-If consistency: PASS

Note: in the confirmation period the change is -0.0017 (-0.0057 to +0.0034): the interval includes 0, so the confirmation period alone neither proves nor rules out a gain. The test suite is checked separately.

**Recommendation:** keep `EGOQUANT_REGIME_MC` off under the protocol: runtime +25% at most, peak memory not higher (strict, tracemalloc) failed

## Shipping decision (outside the protocol)

The flag is ON by default at the product owner's request (`EGOQUANT_REGIME_MC=0` is the kill switch and gives the old output byte for byte). The protocol's strict memory rule is left as it is and still reported above. Peak memory new - old: 15 y current -2337 KiB, 15 y z1_lean -2337 KiB, 30 y current +2 KiB, 30 y z1_lean +6 KiB, 60 y current +5 KiB, 60 y z1_lean +7 KiB. The remaining few KiB at long horizons are the sampler record itself; any regime sampler keeps one. The held-out (confirmation) gain is within noise. Merging changes live goal chances and can change the chosen portfolio (see the limit checks).
