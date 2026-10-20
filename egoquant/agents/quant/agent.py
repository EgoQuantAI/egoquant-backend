"""Agent A: the Quantitative Portfolio Engine (version 2).

Agent A is a deterministic function, not a chatbot. The flow it implements:

  Risk questionnaire -> risk_class (C1..C5) --+
  Advisor chat ------> InvestorContext -------+--> Agent A
  Agent A --asks--> Agent C --MarketSnapshot--> Agent A        (prepare_market_model)
  Agent A: baseline + candidate menu                           (build_candidate_menu)
  Agent A --PortfolioMenu--> Agent B --list[Constraint]--> Agent A
  Agent A: re-optimize with B's numeric limits                 (finalize_proposal)
  Agent A --PortfolioProposal--> Risk Engine (independent approval)

The Risk Engine has the final say. Agent A only proposes.

How the C1..C5 class is used: RiskClassPolicy turns the class into HARD limits (max equity, max
volatility, max loss in a named stress scenario, caps on commodities, crypto and cash) plus a
default equity level (target_equity). The menu runs from 0% equity up to the class cap; the final
proposal starts at the class target.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import numpy as np
import pandas as pd

from egoquant.agents.quant import core as qc
from egoquant.agents.quant import goal as gq
from egoquant.agents.quant import v2
from egoquant.agents.quant.v2 import candidates as v2_cand
from egoquant.agents.quant.v2 import diag as v2_diag
from egoquant.agents.quant.v2 import final as v2_final
from egoquant.agents.quant.v2 import selection as v2_selection
from egoquant.agents.quant.v2 import solve as v2_solve
from egoquant.agents.quant.v2 import universe as v2_universe
from egoquant.models import (
    CLASS_CAP_FIELDS,
    AgentAResult,
    AgentConfig,
    AssetInfo,
    BehavioralAdvisor,
    Constraint,
    DataQualityReport,
    ImplementationPlan,
    InvestorContext,
    MarketDataProvider,
    PortfolioMenu,
    PortfolioProposal,
    RiskClassPolicy,
    RiskMetrics,
    SensitivityRow,
    StressScenario,
)
from egoquant.regime import hook as regime_hook
from egoquant.scenarios import DEFAULT_STRESS_SCENARIOS

CANDIDATE_STYLES = ("min_variance", "diversified", "equal_weight", "inverse_vol", "interest_tilt")
STYLE_LABELS = {
    "min_variance": "lowest risk", "diversified": "diversified", "equal_weight": "equal weight",
    "inverse_vol": "risk-balanced", "interest_tilt": "interest tilt",
}


class CandidateScreener(Protocol):
    """Agent B's goal-path role: keep the candidates that fit the investor (see BehavioralTwinAgent)."""

    def screen_candidates(self, investor_id: str, menu: PortfolioMenu, min_confidence: float): ...


@dataclass
class GoalBasedResult:
    """Goal-based run: candidates -> Agent B's screening -> simulation -> chosen portfolio."""

    result: AgentAResult           # menu = all candidates, final = the chosen portfolio
    screening: object              # Agent B's ScreeningResult
    analysis: gq.GoalAnalysis


class DataValidationError(Exception):
    """Raised when Agent C's data fails validation. Carries the full report."""

    def __init__(self, report: DataQualityReport):
        super().__init__("; ".join(report.issues))
        self.report = report


@dataclass
class MarketModel:
    """Everything estimated from Agent C's data for one run (internal, not a contract)."""

    as_of: date
    asset_ids: list[str]
    assets: list[AssetInfo]
    asset_classes: list[str]
    equity_mask: np.ndarray            # True where the asset is equity
    cash_mask: np.ndarray              # True where the asset is cash-like
    returns_all: pd.DataFrame          # full lookback, used by the sensitivity analysis
    returns_window: pd.DataFrame       # last window_days rows, used for the main estimates
    cov_annual: np.ndarray             # Sigma (annualized), main window
    cov_long_run: np.ndarray           # Sigma over the full lookback (the 'normal conditions' reference)
    cov_stressed: np.ndarray           # Sigma with risky-asset correlations raised (hypothetical)
    shrinkage_intensity: float         # how much the covariance was pulled toward a calm target
    cond_number: float
    scenario_shocks: dict[str, np.ndarray]
    regime_ratio: float                # EWMA vol / long-run vol of the equity market proxy
    regime_label: str                  # "low", "normal" or "high"
    risk_budget_scale: float           # in (0, 1]; multiplies the class volatility cap
    data_quality: DataQualityReport
    data_source: str
    data_hash: str


class QuantAgent:
    """Agent A. Needs a market-data provider (Agent C); Agent B is optional for `run`."""

    def __init__(
        self,
        market_provider: MarketDataProvider,
        behavioral_advisor: BehavioralAdvisor | None = None,
        config: AgentConfig | None = None,
        scenarios: list[StressScenario] | None = None,
    ):
        self.market_provider = market_provider
        self.behavioral_advisor = behavioral_advisor
        self.config = config or AgentConfig()
        self.scenarios = scenarios or DEFAULT_STRESS_SCENARIOS
        if self.config.objective == "mean_variance" and not self.config.expected_returns:
            raise ValueError("objective 'mean_variance' needs config.expected_returns (explicit assumptions)")
        if self.config.capacity_scenario not in {s.name for s in self.scenarios}:
            raise ValueError(f"capacity_scenario '{self.config.capacity_scenario}' is not a defined scenario")

    # ======================================================================
    # Public API
    # ======================================================================
    def run(self, context: InvestorContext, as_of: date) -> AgentAResult:
        """Whole loop: data -> menu -> Agent B -> constrained proposal."""
        if self.behavioral_advisor is None:
            raise ValueError("run() needs a behavioral_advisor; call the steps separately without one")
        model = self.prepare_market_model(context, as_of)
        menu = self.build_candidate_menu(context, model)
        behavioral_constraints = self.behavioral_advisor.evaluate_menu(context.investor_id, menu)
        final = self.finalize_proposal(context, model, menu, behavioral_constraints)
        return AgentAResult(menu=menu, behavioral_constraints=behavioral_constraints, final=final)

    # ----------------------------------------------------------------------
    # Step 1: ask Agent C for data, validate it, estimate risk and the volatility regime
    # ----------------------------------------------------------------------
    def prepare_market_model(self, context: InvestorContext, as_of: date) -> MarketModel:
        cfg = self.config
        snapshot = self.market_provider.get_market_snapshot(cfg.universe, as_of, cfg.lookback_days)
        asset_ids = [a.asset_id for a in snapshot.assets]

        report = qc.validate_prices(snapshot.prices, asset_ids, as_of, min_rows=cfg.window_days + 1)
        data_quality = DataQualityReport(
            passed=not report.issues, issues=report.issues, warnings=report.warnings,
            n_observations=report.n_observations, first_date=report.first_date, last_date=report.last_date,
        )
        if not data_quality.passed:
            raise DataValidationError(data_quality)

        unknown = (set(context.excluded_assets) | set(context.current_holdings or {})) - set(asset_ids)
        if unknown:
            raise ValueError(f"investor context refers to assets outside the universe: {sorted(unknown)}")

        prices = snapshot.prices[asset_ids]  # enforce a consistent column order
        returns_all = qc.compute_returns(prices)
        returns_window = returns_all.tail(cfg.window_days)
        cov_annual, intensity = qc.estimate_annual_covariance(
            returns_window, cfg.cov_estimator, cfg.ewma_decay, cfg.shrinkage, cfg.periods_per_year
        )
        cov_long_run, _ = qc.estimate_annual_covariance(
            returns_all, cfg.cov_estimator, cfg.ewma_decay, cfg.shrinkage, cfg.periods_per_year
        )

        asset_classes = [a.asset_class for a in snapshot.assets]
        equity_mask = np.array([c == "equity" for c in asset_classes])
        cash_mask = np.array([c == "cash" for c in asset_classes])
        scenario_shocks = {
            s.name: qc.scenario_shock_vector(
                asset_classes, [a.duration_years for a in snapshot.assets],
                [a.convexity for a in snapshot.assets], s.asset_class_shocks, s.rate_shock_bp)
            for s in self.scenarios
        }

        # Volatility regime of an equal-weight equity "market proxy": recent EWMA vs long-run.
        proxy_columns = np.flatnonzero(equity_mask) if equity_mask.any() else np.arange(len(asset_ids))
        proxy_all = returns_all.to_numpy()[:, proxy_columns].mean(axis=1)
        proxy_window = proxy_all[-len(returns_window):]
        long_run_vol = float(np.std(proxy_all, ddof=1) * np.sqrt(cfg.periods_per_year))
        recent_vol = qc.ewma_volatility(proxy_window, cfg.ewma_decay, cfg.periods_per_year)
        regime_ratio, regime_label, budget_scale = qc.volatility_regime(
            recent_vol, long_run_vol, cfg.regime_low, cfg.regime_high, cfg.min_risk_budget_scale)
        if not cfg.dynamic_risk_budget:
            budget_scale = 1.0

        data_hash = _hash_parts(
            pd.util.hash_pandas_object(prices, index=True).to_numpy().tobytes(),
            snapshot.model_dump_json(exclude={"prices"}),
            as_of.isoformat(),
        )
        model = MarketModel(
            as_of=as_of, asset_ids=asset_ids, assets=snapshot.assets, asset_classes=asset_classes,
            equity_mask=equity_mask, cash_mask=cash_mask,
            returns_all=returns_all, returns_window=returns_window, cov_annual=cov_annual,
            cov_long_run=cov_long_run, cov_stressed=qc.stressed_covariance(cov_annual, ~cash_mask, cfg.stress_correlation),
            shrinkage_intensity=intensity, cond_number=qc.condition_number(cov_annual),
            scenario_shocks=scenario_shocks, regime_ratio=regime_ratio, regime_label=regime_label,
            risk_budget_scale=budget_scale, data_quality=data_quality,
            data_source=snapshot.data_source, data_hash=data_hash,
        )
        if v2.portfolio_method() == "v2":        # portfolio method v2: screened universe (docs/PORTFOLIO_V2.md)
            model = v2_universe.screen_model(self, model, context)
        return model

    # ----------------------------------------------------------------------
    # Step 2: baseline + candidate menu (sent to Agent B)
    # ----------------------------------------------------------------------
    def build_candidate_menu(self, context: InvestorContext, model: MarketModel) -> PortfolioMenu:
        """Portfolios spaced evenly along ONE axis: the equity weight, from 0% to the equity cap.

        Each point is the lowest-risk (or best mean-variance) portfolio at that exact equity weight,
        under class, suitability and investor-preference constraints only (no behavioral input yet).
        """
        cfg = self.config
        policy = self._policy(context)
        base_constraints = self._base_constraints(context, model)
        equity_cap = min(c.value for c in base_constraints if c.type == "max_equity")
        target_equity = min(policy.target_equity, equity_cap)

        # Naive fixed-weight reference. It is NOT constraint-aware: violations are reported honestly.
        baseline_weights = qc.build_baseline_weights(
            model.asset_classes, [a in context.excluded_assets for a in model.asset_ids],
            self._baseline_mix(policy), equity_cap,
        )
        baseline = self._make_proposal(
            model, context, baseline_weights, stage="baseline", method="fixed_weight",
            status="optimal", message="naive fixed class mix, equal weight within each class; constraints not enforced",
            active_constraints=base_constraints, input_hash=_hash_parts(model.data_hash, "baseline"),
        )

        portfolios: list[PortfolioProposal] = []
        infeasible_levels: list[float] = []
        for level in np.linspace(cfg.menu_min_equity, equity_cap,
                                 min(cfg.menu_size, 6) if v2.portfolio_method() == "v2" else cfg.menu_size):
            result, relaxed = self._solve_with_fallback(model, context, base_constraints, float(level))
            if result.weights is None:
                infeasible_levels.append(round(float(level), 6))
                continue
            portfolios.append(self._make_proposal(
                model, context, result.weights, stage="optimized", method=cfg.objective,
                status="fallback" if relaxed else "optimal", active_constraints=base_constraints,
                binding=result.binding, relaxed=relaxed, message=f"equity target {level:.0%}",
                input_hash=_hash_parts(model.data_hash, f"menu:{level:.6f}"),
            ))

        menu_hash = _hash_parts(model.data_hash, context.model_dump_json(), cfg.model_dump_json(), "menu")
        return PortfolioMenu(
            as_of=model.as_of, risk_class=context.risk_class, target_equity=target_equity,
            baseline=baseline, portfolios=portfolios,
            default_index=self._pick_default_index(target_equity, portfolios),
            infeasible_levels=infeasible_levels, data_quality=model.data_quality, input_hash=menu_hash,
        )

    # ----------------------------------------------------------------------
    # Goal-based path: many distinct candidates -> Agent B screens -> simulate -> choose
    # ----------------------------------------------------------------------
    def build_candidates(self, context: InvestorContext, model: MarketModel) -> PortfolioMenu:
        """Up to candidate_levels x 5 DISTINCT portfolios, every one inside the class, suitability and
        investor limits (no behavioral input yet).

        Levels run along the risk axis: the stock share from menu_min_equity to the equity cap, or, for a
        profile allowed no stocks, the bond (non-cash) share from 0% to 100%. At each level five styles:
        lowest risk; diversified (lowest risk plus a penalty on concentration); equal weight; risk-balanced
        (inverse volatility); and interest tilt (extra weight on the investor's interest assets). The last
        three are the feasible portfolios nearest to their target mix. Near-duplicates are dropped.
        """
        if v2.portfolio_method() == "v2":        # portfolio method v2: other candidate styles and limits
            return v2_cand.build_menu(self, context, model)
        cfg = self.config
        policy = self._policy(context)
        constraints = self._base_constraints(context, model)
        equity_cap = min(c.value for c in constraints if c.type == "max_equity")
        bond_mask = np.array([c == "bond" for c in model.asset_classes])
        if equity_cap > 1e-9:
            axis, mask = "stocks", model.equity_mask
            levels = np.linspace(cfg.menu_min_equity, equity_cap, cfg.candidate_levels)
        else:
            axis, mask = "bonds", bond_mask
            levels = np.linspace(0.0, 1.0, cfg.candidate_levels)

        allowed = np.array([a not in context.excluded_assets for a in model.asset_ids])
        vols = np.sqrt(np.diag(model.cov_annual))
        interest_set = set(context.interest_assets)
        interest = np.array([a in interest_set for a in model.asset_ids]) & allowed
        ridge = cfg.candidate_ridge * float(np.mean(np.diag(model.cov_annual)))

        def target(level: float, per_asset: np.ndarray) -> np.ndarray | None:
            """Split `level` over the axis assets and the rest over the others, in proportion to per_asset."""
            t = np.zeros(len(model.asset_ids))
            for group, share in ((mask & allowed, level), (~mask & allowed, 1.0 - level)):
                if share <= 1e-12:
                    continue
                if not group.any() or per_asset[group].sum() <= 0:
                    return None
                t[group] = share * per_asset[group] / per_asset[group].sum()
            return t

        ones = np.ones(len(model.asset_ids))
        inverse_vol = 1.0 / np.maximum(vols, 1e-6)
        tilt = np.where(interest, cfg.candidate_interest_tilt, 1.0)
        portfolios: list[PortfolioProposal] = []
        kept: list[np.ndarray] = []
        tried: set[float] = set()

        def add_level(level: float) -> None:
            """Every style at one point of the risk axis; near-duplicates of earlier candidates are dropped."""
            tried.add(round(level, 9))
            plans: dict[str, dict | None] = {
                "min_variance": {},
                "diversified": {"ridge": ridge},
                "equal_weight": {"target_weights": target(level, ones)},
                "inverse_vol": {"target_weights": target(level, inverse_vol)},
                "interest_tilt": {"target_weights": target(level, tilt)} if interest.any() else None,
            }
            for style in CANDIDATE_STYLES:
                options = plans[style]
                if options is None or ("target_weights" in options and options["target_weights"] is None):
                    continue
                if axis == "bonds":
                    options = {**options, "equity_mask": bond_mask.astype(float)}
                result, relaxed = self._solve_with_fallback(model, context, constraints, level, **options)
                if result.weights is None:
                    continue
                w = result.weights
                if any(np.max(np.abs(w - other)) <= cfg.candidate_min_distance for other in kept):
                    continue
                kept.append(w)
                portfolios.append(self._make_proposal(
                    model, context, w, stage="optimized", method=style,
                    status="fallback" if relaxed else "optimal", active_constraints=constraints,
                    binding=result.binding, relaxed=relaxed,
                    message=f"{STYLE_LABELS[style]} · {axis} {level:.0%}",
                    input_hash=_hash_parts(model.data_hash, f"candidate:{style}:{axis}:{level:.6f}"),
                    diagnostics={"style": style, "axis": axis, "level": level},
                ))

        for raw_level in levels:
            add_level(float(raw_level))
        # Aim for candidate_target distinct portfolios: while there are too few (near-duplicates dropped,
        # infeasible levels), add the midpoints between the levels tried so far; at most three rounds.
        for _ in range(3):
            if len(portfolios) >= cfg.candidate_target:
                break
            points = sorted(tried)
            before = len(portfolios)
            for low, high in zip(points, points[1:]):
                add_level((low + high) / 2.0)
            if len(portfolios) == before:
                break

        scenario = cfg.capacity_scenario
        portfolios.sort(key=lambda p: (p.equity_weight or 0.0, -p.stress.get(scenario, 0.0), p.method))
        if len(portfolios) > cfg.candidate_target:   # thin evenly along the risk axis to exactly the target
            keep = np.linspace(0, len(portfolios) - 1, cfg.candidate_target).round().astype(int)
            portfolios = [portfolios[i] for i in sorted(set(keep.tolist()))]
        target_equity = min(policy.target_equity, equity_cap)
        menu_hash = _hash_parts(model.data_hash, context.model_dump_json(), cfg.model_dump_json(), "candidates")
        baseline = self.build_candidate_menu(context, model).baseline
        return PortfolioMenu(
            as_of=model.as_of, risk_class=context.risk_class, target_equity=target_equity,
            baseline=baseline, portfolios=portfolios,
            default_index=self._pick_default_index(target_equity, portfolios),
            infeasible_levels=[], data_quality=model.data_quality, input_hash=menu_hash,
        )

    def run_goal_based(self, context: InvestorContext, as_of: date, goal: gq.GoalSpec,
                       screener: CandidateScreener) -> GoalBasedResult:
        """Candidates -> Agent B keeps those that fit the investor -> simulate each against the goal ->
        choose the highest goal probability. Every limit still binds: the choice is only ever made among
        portfolios that are inside all of them."""
        cfg = self.config
        model = self.prepare_market_model(context, as_of)
        menu = self.build_candidates(context, model)
        if not menu.portfolios:
            raise ValueError("no candidate portfolio satisfies the investor's limits")
        screening = screener.screen_candidates(context.investor_id, menu, cfg.min_behavioral_confidence)
        if not screening.approved:
            raise ValueError("Agent B approved no candidate portfolio")
        approved = [menu.portfolios[i] for i in screening.approved]

        # Long history of exactly the funds the approved portfolios hold. Each fund keeps its OWN history
        # (get_price_history); younger funds are completed by regression on the long-history ones
        # (goal.complete_history) instead of cutting everyone to the youngest fund's start.
        held = sorted({a for p in approved for a, w in p.weights.items() if w > 1e-9})
        # The broad equity market (VT, a core fund) comes along for the hindsight correction's betas.
        market = next((m for m in gq.MARKET_PROXIES if m in model.asset_ids), None) if cfg.goal_hindsight_correction             else None
        # The T-bill fund (a core fund) comes along for the short rate of the rate anchor.
        rate_proxy = gq.SHORT_RATE_PROXY if gq.RATE_ANCHOR and gq.SHORT_RATE_PROXY in model.asset_ids else None
        wanted = sorted(set(held) | ({market} if market else set()) | ({rate_proxy} if rate_proxy else set()))
        if hasattr(self.market_provider, "get_price_history"):
            prices = self.market_provider.get_price_history(wanted, as_of, cfg.goal_history_days)
        else:   # a provider without it (tests, other data sources): the common window, as before
            prices = self.market_provider.get_market_snapshot(wanted, as_of, cfg.goal_history_days).prices
        missing = sorted(set(held) - set(prices.columns))
        if missing:
            raise ValueError(f"no price history for held assets {missing}")
        rate = gq.short_rate(prices[rate_proxy]) if rate_proxy in prices.columns else None
        # The proxy is simulated only when it is held or is the market fund (the history stays as before).
        wanted = [a for a in wanted if a in prices.columns and (a != rate_proxy or a in held or a == market)]
        daily, backfilled, history_note = gq.complete_history(prices[wanted], cfg.goal_block_days, cfg.goal_seed)
        adjustments: list = []
        classes = dict(zip(model.asset_ids, model.asset_classes))
        if market in daily.columns:
            daily, adjustments, _ = gq.equilibrium_means(daily, classes, market)
            if adjustments:
                history_note += (f" Hindsight correction: {len(adjustments)} stock, equity-fund or crypto average(s) "
                                 f"set to beta x the {market} market average ({gq.EQUILIBRIUM_METHOD}).")
        rate_anchor = None
        if rate is not None:
            daily, rate_anchor = gq.anchor_rate_means(daily, classes, rate)
            if rate_anchor.assets:
                history_note += (f" Rate anchor: {len(rate_anchor.assets)} bond or cash average(s) set to the short "
                                 f"rate at the decision date ({rate:.2%} a year, from {rate_proxy}; bonds plus "
                                 f"{gq.BOND_TERM_PREMIUM:.1%}).")
        if len(daily) < cfg.goal_min_history_days:
            raise gq.InsufficientHistory(
                f"only {len(daily)} trading days of price history ({daily.index.min().date()} to "
                f"{daily.index.max().date()}); the goal simulation needs at least {cfg.goal_min_history_days} "
                "(about five years) so that it does not just replay one market phase")
        fees = {a.asset_id: a.expense_ratio for a in model.assets}
        candidates = [(i, menu.portfolios[i].method, menu.portfolios[i].equity_weight or 0.0, menu.portfolios[i].weights)
                      for i in screening.approved]
        vols = {i: menu.portfolios[i].risk.ex_ante_vol for i in screening.approved if menu.portfolios[i].risk}
        # Regime-aware sampler (EGOQUANT_REGIME_MC, on by default; "0": None = the block bootstrap, unchanged).
        # One decision per request: every candidate is ranked on the same draws, so the choice and the reported
        # chance always come from the same sampler.
        regime = regime_hook.decide(daily.index, cfg.goal_block_days, as_of, model.data_source)
        select, selection_info = None, {}
        if v2.portfolio_method() == "v2":        # portfolio method v2: least worst-10% drawdown within the tolerance
            effn = {i: v2_cand.effective_n(np.array(list(menu.portfolios[i].weights.values()))) for i in screening.approved}
            floor = v2_cand.EFFECTIVE_N_FLOOR if self._policy(context).v2_max_fund_weight is not None else None

            n_hold = {i: int(sum(1 for w in menu.portfolios[i].weights.values() if w > v2_selection.MIN_POSITION))
                      for i in screening.approved}

            def select(outcomes, outcome_vols, tie):
                ranked, reason, info = v2_selection.select_goal(outcomes, outcome_vols, effn, floor, n_hold)
                selection_info.update(info)
                return ranked, reason
        analysis = gq.analyze_goal(daily, candidates, fees, vols, goal, n_paths=cfg.goal_paths,
                                   block_days=cfg.goal_block_days, seed=cfg.goal_seed, tie=cfg.goal_tie_tolerance,
                                   backfilled=backfilled, history_note=history_note, mean_adjustments=adjustments,
                                   sampler=regime, rate_anchor=rate_anchor, select=select)
        sampler_parts = [regime_hook.hash_part(analysis.sampler)] if analysis.sampler else []

        chosen = menu.portfolios[analysis.chosen_index]
        final = chosen.model_copy(update={
            "stage": "constrained",
            "method": f"goal_based:{chosen.method}",
            "message": f"{chosen.message} | chosen from {len(approved)} approved of {len(menu.portfolios)} candidates",
            "ignored_constraints": list(screening.ignored),
            "relaxed_constraints": list(dict.fromkeys([*chosen.relaxed_constraints, *screening.relaxed])),
            "input_hash": _hash_parts(menu.input_hash, goal.model_dump_json(), f"chosen:{analysis.chosen_index}",
                                      *sampler_parts),
            "engine_version": regime_hook.engine_version(analysis.sampler, chosen.engine_version)
                              + ("+pv2" if select is not None else ""),
            "diagnostics": {**chosen.diagnostics, "goal_candidates": float(len(menu.portfolios)),
                            "goal_approved": float(len(approved)),
                            "goal_probability": analysis.outcomes[0].prob_goal,
                            **(v2_diag.describe(model, np.array([chosen.weights.get(a, 0.0) for a in model.asset_ids]),
                                                chosen.binding_constraints, resample_n=v2_cand.N_RESAMPLE)
                               if select is not None else {}),
                            **selection_info},
        })
        result = AgentAResult(menu=menu, behavioral_constraints=list(screening.constraints), final=final)
        return GoalBasedResult(result=result, screening=screening, analysis=analysis)

    # ----------------------------------------------------------------------
    # Step 3: re-optimize with Agent B's numeric limits
    # ----------------------------------------------------------------------
    def finalize_proposal(
        self,
        context: InvestorContext,
        model: MarketModel,
        menu: PortfolioMenu | None,
        behavioral_constraints: list[Constraint],
    ) -> PortfolioProposal:
        """Constrained proposal. B's influence stays explicit: used, ignored, binding.

        `menu` may be None (the backtest skips the menu to save time). The search starts at the
        class target equity, capped by every equity limit (B's included), and steps the equity
        weight down in small increments until all constraints can be met. Hard constraints are never
        relaxed; soft ones (for example the dynamic risk budget) are dropped only as a last resort.
        """
        cfg = self.config
        policy = self._policy(context)

        # Weak behavioral evidence is NOT enforced; it is reported as ignored.
        usable = [c for c in behavioral_constraints
                  if c.confidence is None or c.confidence >= cfg.min_behavioral_confidence]
        ignored = [c.label for c in behavioral_constraints if c not in usable]

        constraints = self._base_constraints(context, model) + usable
        if context.max_turnover is not None and context.current_holdings is not None:
            constraints.append(Constraint(type="max_turnover", value=context.max_turnover,
                                          source="investor_preference"))
        final_hash = _hash_parts(menu.input_hash if menu else model.data_hash, context.model_dump_json(),
                                 cfg.model_dump_json(), *(c.model_dump_json() for c in behavioral_constraints))

        if menu is not None and not menu.portfolios:
            return self._infeasible_proposal(model, context, "no feasible menu portfolio to start from",
                                             ignored, final_hash)

        equity_caps = [c.value for c in constraints if c.type == "max_equity"]
        start_level = min([policy.target_equity] + equity_caps)
        use_v2 = v2.portfolio_method() == "v2"
        if use_v2:                               # portfolio method v2: diversified final portfolio, v2 fund cap
            constraints = v2_solve.cap_constraints(self, context, constraints)
            result, relaxed, used_level = v2_final.solve_final(self, model, context, constraints, start_level)
        else:
            result, relaxed, used_level = self._search_equity_level(model, context, constraints, start_level)
        if result is None or result.weights is None:
            return self._infeasible_proposal(model, context, "no portfolio satisfies all hard constraints",
                                             ignored, final_hash)

        method = cfg.objective
        v2_style = result.message.split(":")[1] if use_v2 and result.message.startswith("v2:") else None
        if v2_style:
            method = f"v2:{v2_style}"
        holdings = self._current_weights(model, context)
        if holdings is not None and cfg.turnover_penalty > 0:
            method += "+cost_aware"
        note = f"equity target {used_level:.0%}"
        if used_level < start_level - 1e-9:
            note += f" (lowered from {start_level:.0%} to satisfy constraints)"
        if model.risk_budget_scale < 1.0:
            note += f" | volatility regime {model.regime_label}, risk budget scaled to {model.risk_budget_scale:.0%}"

        active = [c for c in constraints if c.label not in relaxed]
        budget = next((c for c in constraints if c.source == "system" and c.type == "max_vol"), None)
        proposal = self._make_proposal(
            model, context, result.weights, stage="constrained", method=method,
            status="fallback" if relaxed else "optimal", active_constraints=active,
            binding=result.binding, relaxed=relaxed, ignored=ignored, message=note, input_hash=final_hash,
            diagnostics={**({"risk_budget_vol": budget.value} if budget else {}),
                         **(v2_diag.describe(model, result.weights, result.binding, resample_n=v2_cand.N_RESAMPLE)
                            if v2_style else {})} or None,
        )
        if v2_style:
            proposal = proposal.model_copy(update={"engine_version": proposal.engine_version + "+pv2"})
        if used_level < start_level - 1e-9:
            proposal.limiting_constraints = self._limiting_constraints(model, context, active, used_level, start_level)
        if cfg.run_sensitivity and not use_v2:          # v2: the closed resampling already is the robustness check
            proposal.sensitivity = self._sensitivity(model, context, active, used_level)
        return proposal

    def _limiting_constraints(self, model: MarketModel, context: InvestorContext, constraints: list[Constraint],
                              level: float, start_level: float) -> list[str]:
        """Labels of the limits that stopped the next equity level up (one step above `level`).

        A limit can stop the search without being binding at the chosen level (the level moves in
        steps). Each kind of limit (type + scenario + asset) is dropped in turn; if the higher level
        then becomes feasible, the tightest limit of that kind is reported. Empty when only a
        combination of limits blocks it.
        """
        next_level = min(level + self.config.equity_step_down, start_level)
        groups: dict[tuple, list[Constraint]] = {}
        for c in constraints:
            if c.type != "max_equity":           # equity caps set the starting level, not the step down
                groups.setdefault((c.type, c.scenario, c.asset_class, c.asset_id), []).append(c)
        limiting: list[str] = []
        for group in groups.values():
            dropped = {id(c) for c in group}
            others = [c for c in constraints if id(c) not in dropped]
            if self._solve(model, context, others, next_level, with_turnover=True).weights is None:
                continue
            values = [c.value for c in group]
            tightest = max(values) if group[0].type == "min_cash" else min(values)
            limiting += [c.label for c in group if abs(c.value - tightest) <= 1e-12]
        return limiting

    # ======================================================================
    # Constraint translation
    # ======================================================================
    def _policy(self, context: InvestorContext) -> RiskClassPolicy:
        return self.config.risk_class_policies[context.risk_class]

    def _baseline_mix(self, policy: RiskClassPolicy) -> dict[str, float]:
        if self.config.baseline_class_mix is not None:
            return self.config.baseline_class_mix
        return {"equity": policy.target_equity, "bond": 1.0 - policy.target_equity, "cash": 0.0}

    def _base_constraints(self, context: InvestorContext, model: MarketModel) -> list[Constraint]:
        """Hard limits from the risk class, suitability and the investor, plus the soft dynamic budget."""
        cfg = self.config
        policy = self._policy(context)
        constraints = [
            # From the questionnaire class (C1..C5)
            Constraint(type="max_equity", value=policy.max_equity, source="risk_class"),
            Constraint(type="max_vol", value=policy.max_vol, source="risk_class"),
            Constraint(type="max_stress_loss", value=policy.max_stress_loss, source="risk_class",
                       scenario=cfg.capacity_scenario),
            # Regulatory / suitability and the investor's own preferences
            Constraint(type="max_equity", value=context.suitability_max_equity, source="suitability"),
            Constraint(type="max_asset_weight", value=context.max_asset_weight, source="investor_preference"),
        ]
        # Liquidity: hold at least the share that may be needed soon in cash-like assets.
        min_cash = max(context.min_cash_buffer, context.liquidity_need)
        if min_cash > 0:
            constraints.append(Constraint(type="min_cash", value=min_cash, source="suitability"))
        # Class caps (commodities, crypto, cash). The cash cap never undercuts the investor's own cash
        # need: liquidity comes first. A cap is only sent when the universe holds that class.
        for asset_class, field_name in CLASS_CAP_FIELDS.items():
            if asset_class not in model.asset_classes:
                continue
            cap = getattr(policy, field_name)
            if asset_class == "cash":
                cap = max(cap, min_cash)
            if cap < 1.0:
                constraints.append(Constraint(type="max_class_weight", value=cap, source="risk_class",
                                              asset_class=asset_class))
        # The investor chose crypto and may hold it: a small floor, so the advice contains crypto for sure
        # (Agent A's lowest-risk styles would otherwise leave it out). Never above the class cap; SOFT, so a
        # hard limit (the investor's stress loss, Agent B) always wins and the floor is reported as relaxed.
        allowed = set(model.asset_ids) - set(context.excluded_assets)
        chosen_crypto = [a for a in context.interest_assets
                         if a in allowed and model.asset_classes[model.asset_ids.index(a)] == "crypto"]
        crypto_cap = getattr(policy, CLASS_CAP_FIELDS["crypto"]) if "crypto" in CLASS_CAP_FIELDS else 0.0
        if chosen_crypto and crypto_cap > 0 and cfg.interest_crypto_floor > 0:
            constraints.append(Constraint(type="min_class_weight", value=min(cfg.interest_crypto_floor, crypto_cap),
                                          source="investor_preference", hard=False, asset_class="crypto"))
        # Capacity: the loss the investor can afford applies to one named hypothetical scenario.
        if context.max_acceptable_loss is not None:
            constraints.append(Constraint(type="max_stress_loss", value=context.max_acceptable_loss,
                                          source="suitability", scenario=cfg.capacity_scenario))
        # Dynamic risk budget: in a high-volatility regime, cap the portfolio volatility at
        #   (volatility this investor would normally run) x (regime scale, below 1).
        # SOFT, because it is a de-risking preference that must not make the problem unsolvable.
        if model.risk_budget_scale < 1.0:
            normal_vol = self._normal_vol(model, context, constraints)
            constraints.append(Constraint(type="max_vol", value=normal_vol * model.risk_budget_scale,
                                          source="system", hard=False))
        return constraints

    def _normal_vol(self, model: MarketModel, context: InvestorContext,
                    static_constraints: list[Constraint]) -> float:
        """Volatility the investor would run in NORMAL conditions: the class-target portfolio priced
        with the long-run covariance (full lookback). Falls back to the class volatility cap."""
        policy = self._policy(context)
        equity_caps = [c.value for c in static_constraints if c.type == "max_equity"]
        level = min([policy.target_equity] + equity_caps)
        result = self._solve(model, context, static_constraints, level, cov_override=model.cov_long_run)
        if result.weights is None:
            return policy.max_vol
        return qc.portfolio_volatility(result.weights, model.cov_long_run)

    def _current_weights(self, model: MarketModel, context: InvestorContext) -> np.ndarray | None:
        if context.current_holdings is None:
            return None
        return np.array([context.current_holdings.get(a, 0.0) for a in model.asset_ids])

    def _solver_inputs(self, model: MarketModel, context: InvestorContext,
                       constraints: list[Constraint], with_turnover: bool) -> dict:
        """Turn Constraint objects into the arrays core.solve_portfolio understands."""
        cfg = self.config
        n = len(model.asset_ids)
        upper = np.ones(n)                       # long-only, unleveraged: 0 <= w_i <= 1
        labels = ["system:long_only"] * n
        for i, (asset_id, asset) in enumerate(zip(model.asset_ids, model.assets)):
            if asset_id in context.excluded_assets:
                upper[i], labels[i] = 0.0, "investor_preference:excluded_asset"
            elif asset.max_weight is not None and asset.max_weight < upper[i]:
                upper[i], labels[i] = asset.max_weight, "system:instrument_cap"   # e.g. one single stock

        current = self._current_weights(model, context) if with_turnover else None
        rows: list[tuple[str, np.ndarray, float]] = []   # each row means  a . w <= b
        vol_limits: list[tuple[str, float]] = []
        turnover_cap: tuple[str, float] | None = None
        for c in constraints:
            if c.type == "max_asset_weight":
                targets = [model.asset_ids.index(c.asset_id)] if c.asset_id else range(n)
                for i in targets:
                    if c.value < upper[i]:
                        upper[i], labels[i] = c.value, c.label
            elif c.type == "max_equity":
                rows.append((c.label, model.equity_mask.astype(float), c.value))
            elif c.type == "max_class_weight":
                rows.append((c.label, self._class_mask(model, c.asset_class), c.value))
            elif c.type == "min_class_weight":
                rows.append((c.label, -self._class_mask(model, c.asset_class), -c.value))   # class weight >= value
            elif c.type == "min_cash":
                rows.append((c.label, -model.cash_mask.astype(float), -c.value))
            elif c.type == "max_stress_loss":
                rows.append((c.label, -self._shocks(model, c.scenario), c.value))  # loss <= value
            elif c.type == "max_vol":
                vol_limits.append((c.label, c.value))
            elif c.type == "max_turnover" and current is not None:
                turnover_cap = (c.label, c.value)

        expected = None
        if cfg.objective == "mean_variance":
            assumed = cfg.expected_returns or {}
            missing = [a for a in model.asset_ids if a not in assumed]
            if missing:
                raise ValueError(f"expected_returns assumptions missing for: {missing}")
            fees = {a.asset_id: a.expense_ratio for a in model.assets}
            expected = np.array([assumed[a] - fees[a] for a in model.asset_ids])   # net of fees

        return dict(
            cov=model.cov_annual, asset_ids=model.asset_ids, upper_bounds=upper, bound_labels=labels,
            linear_rows=rows, vol_limits=vol_limits, equity_mask=model.equity_mask.astype(float),
            expected_returns=expected, risk_aversion=cfg.risk_aversion, current_weights=current,
            trade_cost_rates=np.array([a.trading_cost_bps for a in model.assets]) / 10_000.0,
            turnover_penalty=cfg.turnover_penalty, turnover_cap=turnover_cap,
        )

    def _solve(self, model: MarketModel, context: InvestorContext, constraints: list[Constraint],
               equity_target: float, cov_override: np.ndarray | None = None,
               with_turnover: bool = False, **solver_options) -> qc.SolveResult:
        inputs = self._solver_inputs(model, context, constraints, with_turnover)
        if cov_override is not None:
            inputs["cov"] = cov_override
        inputs.update(solver_options)     # e.g. ridge, target_weights, or a different level mask
        return qc.solve_portfolio(equity_target=equity_target, **inputs)

    def _solve_with_fallback(self, model: MarketModel, context: InvestorContext,
                             constraints: list[Constraint], equity_target: float,
                             cov_override: np.ndarray | None = None,
                             **solver_options) -> tuple[qc.SolveResult, list[str]]:
        """Try all constraints at ONE equity level; if infeasible, drop SOFT ones and say so."""
        attempts: list[tuple[list[Constraint], list[str]]] = [(constraints, [])]
        soft = [c for c in constraints if not c.hard]
        if soft:
            attempts.append(([c for c in constraints if c.hard], [c.label for c in soft]))
        result = qc.SolveResult(None, "infeasible", [], "no attempt made")
        for active, relaxed in attempts:
            result = self._solve(model, context, active, equity_target, cov_override, **solver_options)
            if result.weights is not None:
                return result, relaxed
        return result, []

    def _search_equity_level(self, model: MarketModel, context: InvestorContext,
                             constraints: list[Constraint], start_level: float
                             ) -> tuple[qc.SolveResult | None, list[str], float]:
        """Walk the equity weight down from `start_level` until the constraints can be met.

        Pass 1 keeps every constraint (soft ones included) at every level. Only if no level works
        does pass 2 drop the soft constraints and report them as relaxed.
        """
        step = self.config.equity_step_down
        n_steps = int(np.floor(start_level / step + 1e-9))
        levels = [max(start_level - k * step, 0.0) for k in range(n_steps + 1)]
        if levels[-1] > 1e-9:
            levels.append(0.0)

        soft = [c for c in constraints if not c.hard]
        passes: list[tuple[list[Constraint], list[str]]] = [(constraints, [])]
        if soft:
            passes.append(([c for c in constraints if c.hard], [c.label for c in soft]))
        for active, relaxed in passes:
            for level in levels:
                result = self._solve(model, context, active, level, with_turnover=True)
                if result.weights is not None:
                    return result, relaxed, level
        return None, [], start_level

    @staticmethod
    def _class_mask(model: MarketModel, asset_class: str | None) -> np.ndarray:
        return np.array([c == asset_class for c in model.asset_classes], dtype=float)

    @staticmethod
    def _shocks(model: MarketModel, scenario: str | None) -> np.ndarray:
        if scenario not in model.scenario_shocks:
            raise ValueError(f"unknown stress scenario: {scenario}")
        return model.scenario_shocks[scenario]

    def _slack(self, c: Constraint, w: np.ndarray, model: MarketModel, context: InvestorContext) -> float:
        """Positive = satisfied with room to spare, negative = violated."""
        if c.type == "max_equity":
            return c.value - float(w @ model.equity_mask)
        if c.type == "max_class_weight":
            return c.value - float(w @ self._class_mask(model, c.asset_class))
        if c.type == "min_class_weight":
            return float(w @ self._class_mask(model, c.asset_class)) - c.value
        if c.type == "max_asset_weight":
            idx = [model.asset_ids.index(c.asset_id)] if c.asset_id else list(range(len(w)))
            return c.value - float(w[idx].max())
        if c.type == "min_cash":
            return float(w @ model.cash_mask) - c.value
        if c.type == "max_vol":
            return c.value - qc.portfolio_volatility(w, model.cov_annual)
        if c.type == "max_turnover":
            current = self._current_weights(model, context)
            return float("inf") if current is None else c.value - qc.turnover(w, current)
        return c.value + float(w @ self._shocks(model, c.scenario))  # max_stress_loss

    # ======================================================================
    # Building outputs
    # ======================================================================
    def _make_proposal(
        self, model: MarketModel, context: InvestorContext, weights: np.ndarray, *, stage: str,
        method: str, status: str, active_constraints: list[Constraint], input_hash: str,
        message: str = "", binding: list[str] | None = None, relaxed: list[str] | None = None,
        ignored: list[str] | None = None, diagnostics: dict | None = None,
    ) -> PortfolioProposal:
        """Attach risk metrics, stress results, costs and constraint checks to a weight vector."""
        cfg = self.config
        policy = self._policy(context)
        violated = [c.label for c in active_constraints
                    if self._slack(c, weights, model, context) < -qc.TOLERANCE]
        if violated and status == "optimal" and stage != "baseline":
            status, message = "fallback", message + " | constraints violated"
        loss_limit = min(policy.max_stress_loss,
                         context.max_acceptable_loss if context.max_acceptable_loss is not None else 1.0)
        return PortfolioProposal(
            stage=stage, method=method, as_of=model.as_of, risk_class=context.risk_class,
            status=status, message=message,
            weights=self._weights_dict(model, weights),
            equity_weight=float(weights @ model.equity_mask),
            risk=self._risk_metrics(model, weights),
            stress={name: qc.scenario_portfolio_return(weights, s) for name, s in model.scenario_shocks.items()},
            reverse_stress_equity_drop=qc.equity_drop_to_reach_loss(weights, model.equity_mask.astype(float), loss_limit),
            binding_constraints=binding or [], relaxed_constraints=relaxed or [],
            ignored_constraints=ignored or [], violated_constraints=violated,
            implementation=self._implementation_plan(model, context, weights),
            diagnostics={
                "cov_estimator": cfg.cov_estimator,
                "shrinkage_intensity": float(model.shrinkage_intensity),
                "cov_condition_number": float(model.cond_number),
                "n_observations": float(len(model.returns_window)),
                "regime_label": model.regime_label,
                "regime_ratio": float(model.regime_ratio),
                "risk_budget_scale": float(model.risk_budget_scale),
                **(diagnostics or {}),
            },
            data_source=model.data_source,
            vol_estimator=f"{cfg.cov_estimator}_{cfg.window_days}d",
            input_hash=input_hash, engine_version=cfg.engine_version,
        )

    def _infeasible_proposal(self, model: MarketModel, context: InvestorContext, message: str,
                             ignored: list[str], input_hash: str) -> PortfolioProposal:
        return PortfolioProposal(
            stage="constrained", method=self.config.objective, as_of=model.as_of,
            risk_class=context.risk_class, status="infeasible", message=message, weights={},
            ignored_constraints=ignored, data_source=model.data_source,
            input_hash=input_hash, engine_version=self.config.engine_version,
        )

    def _risk_metrics(self, model: MarketModel, w: np.ndarray) -> RiskMetrics:
        cfg = self.config
        portfolio_returns = model.returns_window.to_numpy() @ w
        var, cvar, n_tail = qc.historical_var_cvar(portfolio_returns, cfg.var_confidence)
        contributions = qc.risk_contributions(w, model.cov_annual)
        return RiskMetrics(
            ex_ante_vol=qc.portfolio_volatility(w, model.cov_annual),
            stressed_corr_vol=qc.portfolio_volatility(w, model.cov_stressed),
            risk_contributions={a: float(c) for a, c in zip(model.asset_ids, contributions)},
            diversification_ratio=qc.diversification_ratio(w, model.cov_annual),
            effective_n_holdings=qc.effective_number_of_holdings(w),
            hist_var=var, hist_cvar=cvar, n_tail_obs=n_tail,
            hist_max_drawdown=qc.max_drawdown(portfolio_returns), var_confidence=cfg.var_confidence,
        )

    def _implementation_plan(self, model: MarketModel, context: InvestorContext,
                             w: np.ndarray) -> ImplementationPlan | None:
        """Turnover, cost and trade list versus current holdings (None if there are none)."""
        current = self._current_weights(model, context)
        if current is None:
            return None
        cost_bps = np.array([a.trading_cost_bps for a in model.assets])
        return ImplementationPlan(
            turnover=qc.turnover(w, current),
            estimated_cost=qc.transaction_cost(w, current, cost_bps),
            trades={a: float(d) for a, d in zip(model.asset_ids, w - current) if abs(d) > 1e-8},
        )

    @staticmethod
    def _weights_dict(model: MarketModel, w: np.ndarray) -> dict[str, float]:
        return {a: float(x) for a, x in zip(model.asset_ids, w)}

    @staticmethod
    def _pick_default_index(target_equity: float, portfolios: list[PortfolioProposal]) -> int | None:
        """Default = the highest menu level that does not exceed the class target equity."""
        if not portfolios:
            return None
        eligible = [i for i, p in enumerate(portfolios) if (p.equity_weight or 0.0) <= target_equity + 1e-9]
        return eligible[-1] if eligible else 0

    # ======================================================================
    # Sensitivity to estimation choices
    # ======================================================================
    def _sensitivity(self, model: MarketModel, context: InvestorContext, constraints: list[Constraint],
                     equity_level: float) -> list[SensitivityRow]:
        """Re-solve with other windows / estimators and report how far the weights move.

        The reference is the same problem solved WITHOUT trading costs, so the table isolates the
        effect of the covariance estimate alone.
        """
        cfg = self.config
        reference = self._solve(model, context, constraints, equity_level)
        if reference.weights is None:
            return []
        rows: list[SensitivityRow] = []
        for window in cfg.sensitivity_windows:
            if window > len(model.returns_all):
                continue
            for estimator in cfg.sensitivity_estimators:
                cov, _ = qc.estimate_annual_covariance(model.returns_all.tail(window), estimator,
                                                       cfg.ewma_decay, cfg.shrinkage, cfg.periods_per_year)
                result = self._solve(model, context, constraints, equity_level, cov_override=cov)
                if result.weights is None:
                    continue
                rows.append(SensitivityRow(
                    window_days=window, estimator=estimator,
                    max_abs_weight_change=float(np.abs(result.weights - reference.weights).max()),
                    ex_ante_vol=qc.portfolio_volatility(result.weights, cov),
                ))
        return rows


def _hash_parts(*parts: bytes | str) -> str:
    """Short SHA-256 fingerprint of inputs so any result can be reproduced and audited."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part if isinstance(part, bytes) else part.encode("utf-8"))
    return digest.hexdigest()[:16]
