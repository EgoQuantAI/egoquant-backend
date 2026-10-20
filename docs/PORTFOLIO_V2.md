# Portfolio method v2: contract (Onur, Mon 19 Oct 2026, decided by Savas)

Status: built, tested and **off by default** (the optional method v2; the outcome and the switch are in `docs/V2_DECISION_SHEET.md`). This file keeps the original plan and contract.
Flag: `EGOQUANT_PORTFOLIO_METHOD` = `v1` (default, today's engine, untouched) or `v2` (this document). It is read ONCE in Agent A
(`egoquant/agents/quant/`). Until Savas accepts the before/after table (Tue 20 Oct 09:00 Dubai) the default stays `v1`.

## 1. Why
Measured on `oct19` (6 demo personas, committed Yahoo snapshot): 5 to 7 holdings, effective number of holdings (1/sum w^2) 3.4 to 5.6,
largest holding 26 to 40%. Cause: candidates are minimum-variance based (corner solutions: the calmest fund of each kind fills to the
per-fund cap of 40%), expected returns are not used to build candidates, near-duplicate funds (VTI, VT, VIG, SCHD) look different to the
optimizer, and Agent C sends at most 100 of the 292 catalog products. Without a goal the stock share is simply the class target
(C3 40%, C4 60%, C5 80%); with a goal the choice is the highest simulated chance.

## 2. Decisions (Savas, 19 Oct)
1. Per-fund cap for C3 to C5: **30%** (`RiskClassPolicy.v2_max_fund_weight`, models.py; only used when the flag is `v2`). Single stocks keep 10%. C1/C2 unchanged.
2. Universe: **all eligible products (up to the full catalog, 292 today), in two stages**: Agent C sends everything eligible with metadata; Agent A screens the
   universe down to representatives, then optimizes. The old 100-instrument cap, "25 per interest cluster, 50 stocks, 30 crypto" trimming is not applied under `v2`.
3. Selection: among the candidates whose simulated goal chance is within **tau = max(1 point, min(3 points, 10% of the best chance))** of the best chance,
   choose the one with the **lowest worst-10% drawdown**. Definition: for each simulated path take its maximum peak-to-trough drawdown over the horizon; the
   worst-10% drawdown of a candidate is the 90th percentile of that distribution (the drawdown exceeded in the worst 10% of paths). Ties: higher effective N, then lower volatility, then lower menu index.
4. Noise: **seeded resampling only** (Michaud). Same inputs always give the same portfolio. No account-level seed, no random choice among equals.
5. Rollout: behind the flag; default `v1` until the acceptance table is accepted.

## 3. The method (all deterministic; no LLM anywhere in the choice)
Stage 0, eligibility (Agent C, existing): risk level R1 to R5, knowledge gating, exclusions, the investor's platform. Output: every eligible product.
Stage 1, universe screening (Agent A):
- Daily returns over the existing window; products with less history than `MIN_HISTORY_DAYS` (default 750) are handled as today (backfill note) or dropped, never silently.
- Correlation distance d(i,j) = sqrt(0.5 * (1 - rho_ij)); hierarchical clustering (Ward or average linkage), cut so that at most `MAX_REPRESENTATIVES` (default 120) clusters remain.
- One representative per cluster: lowest expense ratio, then longest history, then highest liquidity tier, then ticker (deterministic). The investor's interest assets are always kept.
  Clusters are also the "groups of overlapping funds" used by the group cap below.
Stage 2, inputs:
- Covariance: unchanged (Ledoit-Wolf / EWMA, positive-definite repair).
- Expected returns: Black-Litterman (1992). Prior pi = delta * Sigma * w_ref (reverse optimisation of a reference allocation = the class's baseline mix spread over its clusters),
  anchored to the explicit asset-class assumptions already in `InvestorContext.expected_returns`; NEVER the historical mean. Views (deterministic, from Bartu's mapping):
  a small absolute view per chosen interest area / preference, each with a confidence. Posterior mu_BL, then minus the fund's expense ratio.
Stage 3, candidates (every one satisfies sum w = 1, long only, per-fund cap 30% for C3 to C5, single-stock 10%, class caps, volatility and stress limits, group cap, turnover rules):
- Along the same stock-share axis as today (up to 20 levels). At each level: equal risk contribution (Spinu 2013 convex form), hierarchical risk parity (Lopez de Prado 2016,
  within the stock sleeve and within the other sleeve, then projected into the bounds), maximum diversification (Choueifaty and Coignard 2008), Black-Litterman mean-variance
  (risk aversion delta from the class; its target return is the return the goal needs when a goal exists), and resampled efficiency (Michaud): the average of `N_RESAMPLE` (default 20)
  Black-Litterman mean-variance solutions on resampled inputs. The old five styles stay as candidates.
- Resampling: mu_tilde ~ N(mu_BL, Sigma / T_eff) and Sigma_tilde from a block bootstrap of the return window, both from a PRNG seeded with SHA-256(input_hash || "resample" || level).
  The constraints use the base Sigma, so the average of feasible solutions is feasible (convex set). Different clients differ in input_hash, so they get different draws.
- Group cap: no correlation cluster above 25% of the portfolio, and none above 50% of the stock sleeve (C3 to C5). Effective-N floor: 6 for C3 to C5 (via the existing ridge term, increased in a fixed
  number of deterministic steps); if it cannot be met the candidate is kept and flagged `diversification_floor_met = false` (never an error).
Stage 4, selection (section 2.3). With no goal (`goal_amount` absent): among candidates whose stock share is within 2 points of the class target pick the highest effective N, then the lowest historical-window worst-10% drawdown, then lower volatility.
Risk Engine: unchanged and final. Agent B: unchanged (tighten only). Monte Carlo and regime sampler: unchanged (the drawdown is tracked inside the streamed month loop: a running peak and a running maximum drawdown per path, no stored matrices).

## 4. Interfaces (names are fixed so five people can work in parallel)
Agent C -> Agent A (existing `MarketSnapshot.assets: list[AssetInfo]`, nothing renamed): under `v2` Agent C sends all eligible products. Additive metadata, all optional with safe defaults:
`AssetInfo.area: str | None`, `AssetInfo.region: str | None`, `AssetInfo.sector: str | None`, `AssetInfo.history_start: date | None`, `AssetInfo.product_type: str | None` (existing `expense_ratio`, `liquidity_tier`, `max_weight` are used as they are; the five new fields are already in models.py, added by Onur). The flag helper `egoquant.settings.portfolio_method() -> "v1"|"v2"` (anything other than "v2" is "v1") is written by Damla in `settings.py`; Agent A imports it, nobody reads the environment variable directly.
Bartu -> Agent A: `InvestorContext.views: list[PortfolioView]` (added by Onur, default empty, excluded from the JSON dump so v1 fingerprints do not change; v2 mixes the views into its own seed), `PortfolioView(kind: "area"|"sector"|"region"|"asset", key: str, annual_excess: float within +-2%, confidence: float in (0,1])`; built from interests, preferences and Agent B outputs by `egoquant/preferences/views.py` (pure function, tested).
Agent A -> report/UI/seal: `PortfolioProposal.diagnostics` is `dict[str, float | str]` (flat), so the v2 information travels as flat keys, all prefixed `v2_`, on the chosen portfolio
(absent under `v1`): `v2_style` (str: erc, hrp, max_div, bl_mv, resampled, diversified, min_variance, equal_weight, inverse_vol, interest_tilt), `v2_level` (stock share axis), `v2_effective_n`, `v2_largest`,
`v2_n_holdings`, `v2_n_universe` (eligible products sent by Agent C), `v2_n_representatives` (after screening), `v2_group_cap_binding` (0/1), `v2_group_caps_dropped` (0/1: the soft group caps made the level infeasible and were dropped),
`v2_resample_n`, `v2_risk_contribution` (str: JSON object ticker -> share of portfolio risk, top 10), and, on the goal path, `v2_tolerance_points`, `v2_n_contenders`, `v2_worst10_drawdown`, `v2_floor_met` (0/1).
The saved `engine_version` ends with `+pv2`. Onur's `report.py` shows them in the Method and Why tabs; Umut renders the same keys (they arrive in `report.final_diagnostics`, see report.py).

## 5. Acceptance (a table for the six demo personas, v1 vs v2, produced by `eval/portfolio_v2_compare.py`)
- Risk Engine approves every v2 portfolio that v1 approved; no hard limit ever violated (unit test on 200 random investors).
- Same input twice = byte-identical portfolio (test); different goal/amount/answers = different seed.
- Effective N >= 6 and largest holding <= 30% for C3 to C5 (except where the universe cannot allow it, then flagged).
- Goal chance (where a goal exists) not lower than v1 by more than the tolerance in 2.3; worst-10% drawdown not higher than v1.
- Time: heaviest persona <= 45 s on this PC, measured in WORK not wall-clock (cap on solves, so results never depend on machine speed); peak memory <= 450 MB for a 30-year goal.
- Full offline suite green (known Agent D failures only). Docs updated. Report shows the method.
- If any item fails, the flag stays `v1`; v1 remains the demo engine.

## 6. Lanes and branches (REVISED 19 Oct evening: Zerya is away, so Agent A v2 is built by the other four)
All new v2 math lives in NEW files under `egoquant/agents/quant/v2/` (package `__init__.py` by Onur). Zerya's v1 code (`core.py`, `agent.py`, `goal.py`, `egoquant/regime/**`) is NOT edited
except by Onur at three marked hook points, each guarded by `portfolio_method() == "v2"`, so v1 is byte-identical when the flag is off (golden tests prove it). Zerya reviews when she is back.
| Who | Branch | Files |
|---|---|---|
| Damla | `portfolio-v2-agent-c` | `egoquant/settings.py` (`portfolio_method()`, FIRST), `egoquant/agents/market_intel/**` (full eligible universe + metadata + cache), `egoquant/agents/quant/v2/screening.py`, `tests/agent_c/**`, `tests/agent_a/v2/test_screening.py` |
| Bartu | `portfolio-v2-inputs` | `egoquant/preferences/views.py`, `egoquant/agents/quant/v2/returns_bl.py`, `eval/portfolio_v2_compare.py`, demo pack and docs, their tests |
| Umut | `portfolio-v2-ui` | `egoquant/agents/quant/v2/targets.py` (ERC, HRP, maximum diversification) and `tests/agent_a/v2/test_targets.py`, THEN the frontend Method/Why/Holdings display |
| Onur | `portfolio-v2` | `v2/candidates.py`, `v2/selection.py`, `v2/diagnostics.py`, the three hooks in `agent.py`/`goal.py`, `pipeline.py`, `report.py`, docs, merge gate |
Merge: each branch is pushed, never `oct19`; Onur merges into `portfolio-v2` after the module's tests are green and runs the full suite. The flag defaults to `v1` in every branch.

## 7. Module APIs (fixed so four people can code in parallel; pure functions, numpy/pandas/scipy only, deterministic, no I/O, no randomness except seeded `np.random.default_rng`)
```python
# v2/screening.py (Damla)
@dataclass(frozen=True)
class ScreenResult:
    kept: list[str]                  # representatives, in the INPUT column order
    cluster_of: dict[str, int]       # every input asset id -> cluster id 0..K-1
    clusters: list[list[str]]        # members per cluster, representative(s) first
    dropped_short_history: list[str]
def screen_universe(returns: pd.DataFrame, assets: list[AssetInfo], forced: set[str] | None = None,
                    max_representatives: int = 120, min_history_days: int = 750) -> ScreenResult: ...
# distance sqrt(0.5*(1-rho)), average linkage, fcluster(maxclust). Representative of a cluster = min by (expense_ratio, -history_days,
# liquidity_tier, asset_id). Every forced asset is kept (so a cluster may keep several). len(kept) <= max_representatives + len(forced).

# v2/returns_bl.py (Bartu)
def reference_weights(asset_ids: list[str], asset_classes: list[str], class_mix: dict[str, float],
                      cluster_of: dict[str, int] | None = None) -> np.ndarray: ...
def black_litterman_returns(cov: np.ndarray, asset_ids: list[str], asset_classes: list[str],
                            class_assumptions: dict[str, float] | None, w_ref: np.ndarray, views: list[PortfolioView],
                            assets: list[AssetInfo], delta: float = 3.0, tau: float = 0.05) -> np.ndarray: ...
# annual, NET of expense_ratio. No views => pi (= delta*cov@w_ref, shifted per class to the class assumption when given). With views:
# He-Litterman posterior; P has one row per view (equal weights over the matching assets), q = P@pi + annual_excess, omega = diag(P (tau cov) P') * (1/confidence - 1).

# v2/targets.py (Umut)  -- each returns a RAW target weight vector (sums to 1) that Onur then projects onto the feasible set with the existing solve_portfolio(target_weights=...)
def erc_target(cov: np.ndarray, allowed: np.ndarray, sleeves: list[tuple[np.ndarray, float]], upper: np.ndarray) -> np.ndarray | None: ...
def hrp_target(cov: np.ndarray, allowed: np.ndarray, sleeves: list[tuple[np.ndarray, float]], upper: np.ndarray) -> np.ndarray | None: ...
def max_diversification_target(cov: np.ndarray, allowed: np.ndarray, sleeves: list[tuple[np.ndarray, float]], upper: np.ndarray) -> np.ndarray | None: ...
# `sleeves` = [(boolean mask, share), ...] exactly like `target()` in QuantAgent.build_candidates (stock sleeve gets `level`, the rest 1-level). Inside a sleeve
# apply the method to the sub-covariance of the allowed assets; respect `upper` by clip-and-redistribute (deterministic loop); None if a sleeve with share > 0 has no allowed asset.
```
Onur-side (not for the others): `v2/candidates.py` (`build_v2_candidates(model, context, constraints, screen, mu, views) -> PortfolioMenu`), BL mean-variance and Michaud resampling via `solve_portfolio(expected_returns=...)`, `v2/selection.py`.

## 8. Status board (each owner appends one line when a module is pushed and green; newest at the bottom)
| UTC | Who | Module | Branch | Commit | Tests |
|---|---|---|---|---|---|
| 21:35 | Damla | settings.portfolio_method + screening.py | portfolio-v2-agent-c | 806d292 | 251 + 31 tests |
| 21:24 | Umut | targets.py (ERC, HRP, max diversification) | portfolio-v2-ui | 131a1d2 | 88 tests |
| 22:15 | Onur | universe, solve, candidates, selection, final, diag, 5 hooks, report text, pipeline views hand-over; merged Damla + Umut | portfolio-v2 | 608fe27 | full suite 2298 passed, 1 known Agent D failure |
