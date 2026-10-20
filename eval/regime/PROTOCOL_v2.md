# Regime model v2: test protocol (written and committed BEFORE any v2 result exists)

Why this file exists: v1 (3 states, SPY only, one EM run) did not meet the acceptance criteria. Trying several
variants on the same 37 backtest dates and keeping the best would find a "winner" by chance: the backtest holds only
about 3 non-overlapping 5-year periods. So the variants, the selection rule and the decision rule are fixed here
first, and every variant is reported, whatever it scores.

## What v2 changes (the fixable caveats of v1)

1. **Number of states is chosen by the data, not by us.** At every fit (the deployed model and each walk-forward
   refit), K = 2, 3 and 4 states are fitted, each with 10 EM restarts (random_state = 20261017 + i, i = 0..9;
   the restart with the highest log-likelihood is kept). K is chosen by **BIC on that fit's own training data**
   (lower is better; parameters = (K-1) start + K(K-1) transition + K*d means + K*d(d+1)/2 covariances).
   The backtest's Brier score is never used to choose K.
2. **One EM run → 10 restarts** (local optima of EM).
3. **Bonds get a say in the regimes** (variant `joint`): features are SPY's AND AGG's daily log return and 21-day
   realised volatility (4 features). AGG (US aggregate bonds) is cached from 2007-05-23, like SPY. States are still
   sorted by SPY volatility (highest = crisis).

Pre-registered variants (exactly two; no others will be tried):

| Variant | Features | K | Restarts |
|---|---|---|---|
| `spy` | SPY return, SPY 21-day vol | BIC over {2,3,4} | 10 |
| `joint` | SPY + AGG return and 21-day vol | BIC over {2,3,4} | 10 |

v1 (3 states, SPY, one run) is reported alongside, from its existing walk-forward models.

## Not changed, and why

- **Longer history (1993):** every fund in the test portfolios (and VT) is cached only from 2007, so SPY from 1993
  would only sharpen the HMM's parameters: no extra bootstrap blocks and no extra backtest dates. Not fetched.
- **Small sample:** the backtest cannot start before 2012-06 (5 years of history are needed) or end after 2021-06
  (5 years of outcomes are needed). This is a limit of the data, not of the method.
- **Grounding gap:** pre-existing, in the advisor's module (Umut), unrelated to prediction quality.
- **Platform:** no Docker or WSL on this machine; timing and memory stay Windows measurements.

## Selection and confirmation

- **Selection (2012-06-30 .. 2016-12-31, 19 quarter-ends):** the variant (`spy` or `joint`) with the lower 5-year
  Brier score on these dates becomes `regime_hmm_v2`. Ties: `spy` (simpler).
- **Confirmation (2017-03-31 .. 2021-06-30, 18 quarter-ends, never used for selection):** the chosen variant's
  5-year Brier vs the block bootstrap, with a 95% date-bootstrap interval. (10-year goals have dates only up to
  2016-06, so they have no confirmation period; they are reported over all their dates.)

## Decision rule (unchanged from v1)

The flag may be recommended only if ALL hold, over the full period: 5-year and 10-year Brier not worse than the
block bootstrap; runtime +25% at most; peak memory not higher (strict, as for v1); limit checks and What-If
consistency pass; the test suite passes (except the known failure). Otherwise `EGOQUANT_REGIME_MC` stays off and
the result is reported as it is.

Things to watch, whatever the numbers say: a variant that "wins" only by being more optimistic for equity-heavy
portfolios is fitting the 2012-2026 bull market, not predicting better. Results are reported per portfolio.
