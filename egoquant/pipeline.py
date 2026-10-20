"""End-to-end run for one investor: the orchestrator wiring in one function.

    Answers --build_risk_profile--> RiskProfile (C1..C5, max R-level, loss capacity)
    RiskProfile --build_form--> PreferenceForm (only the investment types this investor may choose)
    PreferenceAnswers --to_interest_fields--> what Agent C looks at, in priority order
    RiskProfile --to_agent_c_limits--> Agent C: approved universe (no product above max R-level)
    Agent C --fetch_universe--> prices for that universe, most relevant first, within the data budget
    RiskProfile + Agent C selection --to_investor_context_limits--> InvestorContext suitability fields
    Agent A (calls Agent C for prices and Agent B for behavioral limits) --> AgentAResult
    Risk Engine: independent re-check of the final proposal --> RiskReview (final say)
    build_client_report --> ClientReport (carries the Risk Engine verdict)
    optional, after the verdict: Agent D market briefing + "What changed" (read-only, never changes a number)

Deterministic: same inputs, same outputs. No LLM sets any number here (the optional briefing's tone labels
only feed the briefing itself; see CLAUDE.md). The questionnaire score never leaves the risk
profile; Agent A and Agent B get the C-level and numeric limits only.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone

from egoquant.agents.market_intel import (
    InvestorInterests,
    MarketIntelligenceAgent,
    PriceSource,
    SnapshotReport,
    UniverseSelection,
)
from egoquant.agents.behavioral import BehavioralTwinAgent, NoBehavioralData, NoBehavioralEvidence
from egoquant.agents.market_intel.directory import Directory
from egoquant.agents.quant import QuantAgent, v2
from egoquant.agents.quant.goal import GoalAnalysis, GoalSpec, InsufficientHistory
from egoquant.models import (
    CLASS_CAP_FIELDS,
    AgentAResult,
    AgentConfig,
    BehavioralAdvisor,
    Constraint,
    InvestorContext,
)
from egoquant.preferences import (
    PreferenceAnswers,
    PreferenceBank,
    PreferenceForm,
    build_form,
    load_preference_bank,
    to_interest_fields,
)
from egoquant.briefing.models import Briefing
from egoquant.report import ClientReport, attach_briefing, attach_what_changed, build_client_report
from egoquant.risk_engine import RiskEngine, RiskReview
from egoquant.risk_profile import (
    Answers,
    RiskProfile,
    RiskProfileConfig,
    build_risk_profile,
    is_expired,
    load_all,
    to_agent_c_limits,
    to_constraints,
    to_investor_context_limits,
)
from egoquant.risk_profile.models import GameInput, QuestionBank
from egoquant import settings

# InvestorContext fields that come from the risk profile. Callers may not set them: a looser value
# would undo the suitability check. excluded_assets is merged instead (the investor may exclude more).
_SUITABILITY_FIELDS = frozenset({"risk_class", "suitability_max_equity", "max_acceptable_loss", "max_asset_weight"})

logger = logging.getLogger(__name__)

# Builds the optional market briefing from (interests, selection, final proposal, as_of, horizon_years).
# Runs after the Risk Engine and only reads its inputs, so it can never change a number.
BriefingBuilder = Callable[..., "Briefing | None"]


@dataclass
class PipelineResult:
    profile: RiskProfile
    selection: UniverseSelection
    context: InvestorContext
    result: AgentAResult
    review: RiskReview
    report: ClientReport
    review_constraints: list[Constraint] = field(default_factory=list)   # what the Risk Engine checked
    market_report: SnapshotReport | None = None                           # Agent C's data notes and alerts
    profile_expired: bool = False                                         # valid_until before as_of: re-assess
    preference_form: PreferenceForm | None = None                         # what the investor could choose
    agent_c: MarketIntelligenceAgent | None = None                        # for briefings (watchlist, crypto)
    screening: object | None = None                                       # goal path: Agent B's ScreeningResult
    goal_analysis: GoalAnalysis | None = None                             # goal path: the simulation
    goal_unavailable: str | None = None                                   # goal given but not simulated: why

    @property
    def approved(self) -> bool:
        return self.review.approved


def _accepts(fn: Callable, name: str) -> bool:
    """Does the builder take this keyword (by name or **kwargs)? Older builders do not know briefing_period."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def _v2_views(preferences, context, agent_c, as_of, config) -> list:
    """Deterministic Black-Litterman views from the investor's interests and preferences (egoquant/preferences/views.py).
    Any problem means no views: the portfolio is then built from the prior alone, never an error."""
    try:
        from egoquant.preferences.views import build_views
    except ImportError:
        return []
    try:
        assets = agent_c.get_market_snapshot(agent_c.selection.tickers, as_of, config.lookback_days).assets   # served from memory
        return list(build_views(preferences, list(context.interest_assets), [], assets))
    except Exception as exc:                                    # views are an optional refinement
        logger.warning("portfolio v2 views unavailable: %s", type(exc).__name__)
        return []


def run_pipeline(
    answers: Answers,
    investor_id: str,
    interest_areas: list[str] | None,
    goal_type: str,
    horizon_years: float,
    as_of: date,
    *,
    preferences: PreferenceAnswers | None = None,
    preference_bank: PreferenceBank | None = None,
    trading_platforms: list[str] | None = None,
    behavioral_agent: BehavioralAdvisor | None = None,
    price_source: PriceSource | None = None,
    agent_config: AgentConfig | None = None,
    risk_engine: RiskEngine | None = None,
    bank: QuestionBank | None = None,
    profile_config: RiskProfileConfig | None = None,
    assessed_at: datetime | None = None,
    goal: GoalSpec | None = None,
    fetch_budget: dict[str, int] | None = None,
    briefing_builder: BriefingBuilder | None = None,
    briefing: bool = False,
    briefing_period: str = "week",
    directory: Directory | None | str = "auto",
    max_interest_areas: int | None = None,
    fetch_seconds: float | None = None,
    risk_game: GameInput | None = None,
    **context_fields,
) -> PipelineResult:
    """Run questionnaire -> Agent C -> Agent A (+ Agent B) -> Risk Engine -> report for one investor.

    interest_areas / preferences: what Agent C should look at. Give exactly one: either interest areas
        directly (the chatbot's validated fields), or the investor's answers to the preference form,
        which are checked against what their risk profile allows and ordered by priority.
    behavioral_agent: Agent B (e.g. BehavioralTwinAgent). None = no behavioral evidence.
    price_source: Agent C's price feed. None = Agent C's live feeds (needs network and API keys);
        pass SyntheticPriceSource() for an offline demo.
    assessed_at: when the investor completed the questionnaire (timezone-aware). Default: midnight UTC on
        as_of, so a fresh run is never expired and there is no hidden clock. An expired profile
        (valid_until before as_of) is FLAGGED in the result and the report, not silently used or blocked.
    goal: when given, the GOAL-BASED path runs: Agent A builds many distinct candidates inside the limits,
        Agent B keeps those that fit the investor (screen_candidates), Agent A simulates each against the
        goal on resampled past returns and proposes the one most likely to reach it. Without a goal the
        original path runs (one menu, Agent B's limits, one re-optimized proposal).
    fetch_budget: most NEW price requests per market for this run (default: Agent C's
        DEFAULT_FETCH_BUDGET). Cached prices are free; see MarketIntelligenceAgent.fetch_universe.
    directory: Agent C's investment directory. "auto" (default) lets Agent C load it; None = the curated
        catalog only (the web app passes None while its background load is still running).
    briefing / briefing_builder: the optional Agent D market briefing, off by default. briefing=True without a
        builder uses egoquant.briefing.web.build_web_briefing (mode from EGOQUANT_BRIEFING); a builder given
        on its own also switches it on. It runs AFTER the Risk Engine verdict and only reads its inputs: a
        rejected proposal sends no weights (proposal=None). A failure only drops the briefing (logged), never
        the portfolio. With a briefing the report also gets "What changed" (Agent C drawdown alerts + Agent D
        tone alerts; the only action is offer_risk_profile_recheck).
    briefing_period: "week" (default) | "month": the window the briefing summarises and compares. Passed to
        builders that accept it.
    max_interest_areas: use only the first N interest areas, in the investor's own (priority) order, for prices and
        news; the report then says so in one sentence. None = all (the web app caps it to keep a build fast).
    fetch_seconds: Agent C's time limit for NEW price requests (see MarketIntelligenceAgent.fetch_universe).
    context_fields: other InvestorContext fields (min_cash_buffer, liquidity_need, current_holdings,
        excluded_assets, max_turnover, ...). Suitability fields are rejected (see _SUITABILITY_FIELDS).
    """
    clash = sorted(_SUITABILITY_FIELDS & set(context_fields))
    if clash:
        raise ValueError(f"{clash} come from the risk questionnaire and cannot be set by the caller")
    if bank is None or profile_config is None:
        loaded_bank, loaded_config = load_all()
        bank, profile_config = bank or loaded_bank, profile_config or loaded_config
    config = agent_config or AgentConfig()
    if profile_config.adapter.stress_scenario != config.capacity_scenario:
        raise ValueError("risk profile stress scenario and Agent A's capacity_scenario differ")

    if (interest_areas is None) == (preferences is None):
        raise ValueError("give either interest_areas or preferences (exactly one)")

    # 1. Questionnaire -> risk profile
    if assessed_at is None:
        assessed_at = datetime.combine(as_of, time(0), tzinfo=timezone.utc)
    # The Risk Game (docs/RISK_GAME_BLEND.md) is one third of the tolerance part of the category; none = exactly today.
    game_args = {"game": risk_game, **settings.risk_game_options()} if risk_game is not None else {}
    profile = build_risk_profile(answers, bank, profile_config, assessed_at=assessed_at, **game_args)
    profile_expired = is_expired(profile, as_of)

    # 2. Preferences: only what this profile allows is offered, and only offered options are accepted
    form = None
    interest_fields: dict = {"interest_areas": interest_areas}
    if preferences is not None:
        pref_bank = preference_bank or load_preference_bank()
        form = build_form(pref_bank, profile, profile_config, trading_platforms)
        interest_fields = to_interest_fields(preferences, form, pref_bank)
    chosen_areas = list(interest_fields.get("interest_areas") or [])
    capped_areas: list[str] = []
    if max_interest_areas is not None and len(chosen_areas) > max_interest_areas:
        capped_areas = chosen_areas[max_interest_areas:]
        interest_fields = {**interest_fields, "interest_areas": chosen_areas[:max_interest_areas]}

    # 3. Agent C: universe limited by the profile's R-level and product restrictions
    holdings = context_fields.get("current_holdings") or {}
    interests = InvestorInterests(investor_id=investor_id, trading_platforms=trading_platforms or [],
                                  held_assets=sorted(holdings), **interest_fields,
                                  **to_agent_c_limits(profile, profile_config))
    agent_c = MarketIntelligenceAgent(interests, source=price_source, directory=directory)
    # Prices first, most relevant first, within the data budget; the universe shrinks to what arrived,
    # and Agent A's snapshot below is then served from memory (no further requests).
    # With a goal, the ~10-year history the simulation needs comes in the SAME requests (no second fetch).
    # Always the ~20-year window: a request costs the same for any range, and every price a trader's build
    # fetches stays in the shared cache for later builds and the goal simulation.
    agent_c.fetch_universe(as_of, config.lookback_days, fetch_budget, history_days=config.goal_history_days,
                           max_seconds=fetch_seconds)

    # 4. Agent A context: the profile's suitability fields, plus the caller's other facts
    suitability = to_investor_context_limits(profile, profile_config, agent_c.selection)
    extra_excluded = context_fields.pop("excluded_assets", [])
    suitability["excluded_assets"] = list(dict.fromkeys([*suitability["excluded_assets"], *extra_excluded]))
    # No stocks chosen (no equity area picked, none held): the portfolio holds none (Umut, 20 Oct). The core world-stock
    # fund stays in the universe for the model, but the stock cap is 0, the path C1/C2 already take.
    no_stocks = preferences is not None and suitability["suitability_max_equity"] > 0 and not any(
        i.asset_class == "equity" and any(r == "held" or r.startswith("interest:") for r in i.reasons)
        for i in agent_c.selection.instruments)
    if no_stocks:
        suitability["suitability_max_equity"] = 0.0
    if v2.portfolio_method() == "v2":         # v2 sends every eligible product: only the 3 most relevant of each area are forced through screening
        context_fields.setdefault("interest_assets", sorted(agent_c.interest_assets(per_area=3)))
    context_fields.setdefault("interest_assets", [
        i.ticker for i in agent_c.selection.instruments if any(r.startswith("interest:") for r in i.reasons)])
    context = InvestorContext(investor_id=investor_id, risk_class=profile.c_level, goal_type=goal_type,
                              horizon_years=horizon_years, **suitability, **context_fields)
    if v2.portfolio_method() == "v2" and preferences is not None:      # portfolio method v2: small views from the interests
        context = context.model_copy(update={"views": _v2_views(preferences, context, agent_c, as_of, config)})

    # 5. Agent A (asks Agent C for prices and Agent B for behavioral limits, or for its screening)
    agent_a = QuantAgent(agent_c, behavioral_agent or NoBehavioralEvidence(), config)
    screening, goal_analysis, goal_unavailable = None, None, None
    result = None
    if goal is not None:
        screener = (behavioral_agent if hasattr(behavioral_agent, "screen_candidates")
                    else BehavioralTwinAgent(NoBehavioralData()))
        try:
            goal_run = agent_a.run_goal_based(context, as_of, goal, screener)
            result, screening, goal_analysis = goal_run.result, goal_run.screening, goal_run.analysis
        except InsufficientHistory as exc:
            # No misleading probabilities: build the portfolio the standard way and say why.
            goal_unavailable = f"The goal simulation was not run: {exc}. It runs once more price history is cached."
    if result is None:
        result = agent_a.run(context, as_of)
    assets = agent_c.get_market_snapshot(agent_c.selection.tickers, as_of, config.lookback_days).assets

    # 6. Risk Engine: every hard limit Agent A had to respect, re-checked independently
    review_constraints = _review_constraints(profile, profile_config, config, context, result)
    engine = risk_engine or RiskEngine(catalog=agent_c.catalog, scenarios=agent_a.scenarios)   # incl. listed coins
    review = engine.review(result.final, assets, review_constraints, profile.max_r_level)

    # 7. Client report, carrying the Risk Engine verdict
    names = {i.ticker: i.name for i in agent_c.selection.instruments}
    names.update({i.ticker: i.name for i in agent_c.selection.watchlist})
    report = build_client_report(result, context, assets, config, agent_a.scenarios, asset_names=names,
                                 risk_review=review, selection=agent_c.selection, risk_profile=profile,
                                 goal_analysis=goal_analysis, screening=screening)
    market = agent_c.last_report
    if market is not None and market.price_note and market.prices_as_of is not None:
        if market.snapshot_first:
            # Snapshot price mode (chosen, not a failure): say what the prices are, calmly.
            report.banners.append(f"Prices: {market.price_note}. Markets after {market.prices_as_of:%d %b %Y} are not in it.")
        else:
            # The live feed failed and the committed snapshot of REAL prices was used: say so where the investor reads
            # (the screen otherwise shows "real market data" and the request date, as if the prices were current).
            report.banners.append(f"LIVE PRICES UNAVAILABLE: this portfolio uses saved {market.price_note}. Markets after "
                                  f"{market.prices_as_of.isoformat()} are not in it.")
        report.prices_as_of, report.price_note = market.prices_as_of, market.price_note
    if no_stocks:
        report.banners.append("You did not choose stocks, so the portfolio holds none. Add Stocks under Investments to "
                              "include them.")
    if capped_areas:
        used = ", ".join(_area_name(a) for a in chosen_areas[:max_interest_areas])
        left_out = ", ".join(_area_name(a) for a in chosen_areas[max_interest_areas:])
        report.banners.append(f"Your choices cover {len(chosen_areas)} interest areas; to keep this fast we used {max_interest_areas} "
                              f"of them ({used}) for prices and news and left out {left_out}. Choose fewer areas to include them all.")
    if briefing and briefing_builder is None:
        from egoquant.briefing.web import build_web_briefing
        briefing_builder = build_web_briefing
    if briefing_builder is not None:
        # 8. Optional market briefing, after the verdict. No weights for a rejected proposal.
        kwargs = dict(interests=interests, selection=agent_c.selection,
                      proposal=result.final if review.approved else None, as_of=as_of, horizon_years=horizon_years)
        if _accepts(briefing_builder, "briefing_period"):
            kwargs["briefing_period"] = briefing_period
        try:
            market_briefing = briefing_builder(**kwargs)
        except Exception:                                       # the briefing is optional; the portfolio is not
            logger.exception("Market briefing failed; report sent without it")
            market_briefing = None
        report = attach_briefing(market_briefing, report)
        last = agent_c.last_report
        # Drawdowns: holdings (weight > 0 in an APPROVED proposal; largest first), then the investor's
        # interest/watchlist instruments; the rest of the universe is one count line.
        weights = result.final.weights if review.approved else {}
        holdings = [t for t, w in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0])) if w > 1e-9]
        areas = set(interests.interest_areas)
        interest_tickers = ({i.ticker for i in agent_c.catalog if areas & set(i.areas)}
                            | set(agent_c.selection.watchlist_tickers))
        report = attach_what_changed(report, list(last.alerts) if last is not None else [], market_briefing,
                                     names=names, data_to=last.last_date if last is not None else None,
                                     holdings=holdings, interest_tickers=interest_tickers,
                                     drawdown_threshold=getattr(agent_c, "drawdown_alert_threshold", None))
    return PipelineResult(profile=profile, selection=agent_c.selection, context=context, result=result,
                          review=review, report=report, review_constraints=review_constraints,
                          profile_expired=profile_expired, screening=screening, goal_analysis=goal_analysis,
                          goal_unavailable=goal_unavailable,
                          market_report=agent_c.last_report, preference_form=form, agent_c=agent_c)


def _area_name(area: str) -> str:
    """'us_stocks' -> 'US stocks', 'long_term_bonds' -> 'long term bonds': an interest area as the investor reads it."""
    return " ".join(w.upper() if w in {"us", "etf", "ai", "uk", "eu"} else w for w in area.replace("_", " ").split())


def _review_constraints(profile: RiskProfile, profile_config: RiskProfileConfig, config: AgentConfig,
                        context: InvestorContext, result: AgentAResult) -> list[Constraint]:
    """Suitability limits, the class limits and Agent B's hard limits that Agent A applied."""
    policy = config.risk_class_policies[profile.c_level]
    class_limits = [
        Constraint(type="max_equity", value=policy.max_equity, source="risk_class"),
        Constraint(type="max_stress_loss", value=policy.max_stress_loss, source="risk_class",
                   scenario=config.capacity_scenario),
    ]
    min_cash = max(context.min_cash_buffer, context.liquidity_need)
    for asset_class, field_name in CLASS_CAP_FIELDS.items():
        cap = getattr(policy, field_name)
        if asset_class == "cash":
            cap = max(cap, min_cash)          # the investor's own cash need comes first (as in Agent A)
        if cap < 1.0:
            class_limits.append(Constraint(type="max_class_weight", value=cap, source="risk_class",
                                           asset_class=asset_class))
    ignored = set(result.final.ignored_constraints)
    behavioral = [c for c in result.behavioral_constraints if c.hard and c.label not in ignored]
    return to_constraints(profile, profile_config) + class_limits + behavioral
