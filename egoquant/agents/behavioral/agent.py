"""Agent B: the Behavioral Twin.

Agent B is a deterministic function, not a chatbot. It never reads free text and uses no LLM.

  Agent A --PortfolioMenu--> Agent B --list[Constraint]--> Agent A
                                     \\--BehavioralReport--> Advisor / audit (via analyze())

Inputs: the investor's trading logs and their Eckel-Grossman game result (from a provider), plus
Agent A's menu (which carries the questionnaire class C1..C5 and each portfolio's stress losses).

Limits sent to Agent A (source="behavioral"):
  1. Panic selling  -> max_stress_loss at the loss where the investor sold before.
  2. Game           -> when there is no informative trading history and the game is 2+ levels more
                       cautious than the questionnaire class: Agent A's own loss limit for the
                       game's level, soft. The app's Risk Game "loss acceptance" score (did the player
                       retreat to safer bets after losing rounds?) is read the same way; when both
                       act, the more cautious level wins. Still soft. The app's Crash Test (sold a
                       hypothetical fund during a fixed fall?) gives a hypothetical capitulation point, read
                       the same way. Game-type evidence alone lowers the limit by at most one class.
  3. Overtrading    -> a soft max_turnover guardrail on the recommended rebalance.
Advisor flags only (never limits): cost of panic, disposition effect, return chasing, hype
chasing, horizon mismatch, concentration, stated-vs-revealed gaps.

Rules it never breaks:
  * B only TIGHTENS. It never raises a limit set by the questionnaire class or suitability.
  * No evidence, no constraint.
  * Real trading evidence outranks the game, and the game never makes a limit hard.
  * B never sends Agent A a limit Agent A could not meet at all (loss limits are clamped to the
    safest menu portfolio, the turnover guardrail is sized so the recommended move stays possible).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from egoquant.agents.behavioral import core as bc
from egoquant.agents.behavioral.models import (
    BehavioralData,
    BehavioralDataProvider,
    BehavioralFeature,
    BehavioralReport,
    BehaviorConfig,
    CandidateAssessment,
    Evidence,
    EvidenceSource,
    CrashTestResult,
    GameResult,
    LossAcceptance,
    ScreeningResult,
    TradingHistory,
)
from egoquant.models import RISK_CLASS_ORDER, Constraint, PortfolioMenu


@dataclass
class _HistoryView:
    """A validated, as_of-truncated history turned into day-aligned arrays (internal only)."""

    history: TradingHistory
    positions: pd.DataFrame
    prices: pd.DataFrame | None                   # aligned to positions dates, NaN = unknown
    dates: pd.DatetimeIndex
    total_value: np.ndarray
    growth: np.ndarray                            # time-weighted growth index I_t
    drawdown: np.ndarray
    risky_cols: list[str]
    risky_value: np.ndarray
    risky_net_sold: np.ndarray
    risky_gross_sold: np.ndarray
    inflows: np.ndarray
    outflows: np.ndarray
    gross_traded: float
    trades: list[tuple[int, str, str, float]] = field(default_factory=list)   # (day, asset, side, value)
    noncash_weights: pd.DataFrame = field(default_factory=pd.DataFrame)


@dataclass
class _Threshold:
    value: float
    hard: bool
    confidence: float
    source: EvidenceSource
    refs: list[str]
    contradicted: bool


class BehavioralTwinAgent:
    """Agent B. Implements the BehavioralAdvisor protocol from egoquant/models.py."""

    def __init__(self, data_provider: BehavioralDataProvider, config: BehaviorConfig | None = None):
        self.data_provider = data_provider
        self.config = config or BehaviorConfig()

    # ======================================================================
    # Public API
    # ======================================================================
    def evaluate_menu(self, investor_id: str, menu: PortfolioMenu) -> list[Constraint]:
        """The BehavioralAdvisor method Agent A calls. Returns numeric limits only."""
        return self.analyze(investor_id, menu).constraints

    def screen_candidates(self, investor_id: str, menu: PortfolioMenu, min_confidence: float) -> ScreeningResult:
        """Goal-based path: keep the candidates that fit this investor; Agent A simulates only those.

        1. B's own limits (trading logs, game), when strong enough for Agent A to apply (confidence at or
           above `min_confidence`, Agent A's threshold): a candidate that breaks one is removed.
        2. Comfort band, the prior from the questionnaire tightened by the evidence: the loss in the stress
           scenario must be above the next safer class's limit and at most the investor's own limit (or
           B's tighter loss limit). A profile with nothing safer below starts the band at 0.
        3. Between screen_min_keep and screen_max_keep are kept: too few -> the nearest candidates outside
           the band are added; too many -> the band is thinned evenly across its loss range.
        B never adds risk: every candidate already respects the questionnaire's limits (Agent A built them).
        """
        cfg = self.config
        report = self.analyze(investor_id, menu)
        used = [c for c in report.constraints if c.confidence is None or c.confidence >= min_confidence]
        ignored = [c.label for c in report.constraints if c not in used]
        scenario = cfg.stress_scenario
        losses = [max(0.0, -float(p.stress.get(scenario, 0.0))) for p in menu.portfolios]

        upper = cfg.risk_class_policies[menu.risk_class].max_stress_loss
        for c in used:
            if c.type == "max_stress_loss" and c.scenario == scenario:
                upper = min(upper, c.value)
        lower = max((pol.max_stress_loss for pol in cfg.risk_class_policies.values()
                     if pol.max_stress_loss < upper - 1e-9), default=0.0)
        tol = cfg.feasibility_slack

        def screen(limits: list[Constraint]) -> tuple[list[int], dict[int, str]]:
            ok, why = [], {}
            for i, portfolio in enumerate(menu.portfolios):
                broken = [c.label for c in limits if not _candidate_meets(c, portfolio)]
                if broken:
                    why[i] = "breaks behavioral limit " + ", ".join(broken)
                else:
                    ok.append(i)
            return ok, why

        passing, rejected = screen(used)
        relaxed: list[str] = []
        if not passing:   # last resort, as Agent A does: drop the SOFT limits, keep the hard ones, and say so
            relaxed = [c.label for c in used if not c.hard]
            used = [c for c in used if c.hard]
            passing, rejected = screen(used)
            upper = cfg.risk_class_policies[menu.risk_class].max_stress_loss
            for c in used:
                if c.type == "max_stress_loss" and c.scenario == scenario:
                    upper = min(upper, c.value)
            lower = max((pol.max_stress_loss for pol in cfg.risk_class_policies.values()
                         if pol.max_stress_loss < upper - 1e-9), default=0.0)
        if not passing:
            raise ValueError("no candidate portfolio respects Agent B's hard limits")

        def in_band(i: int) -> bool:
            return losses[i] <= upper + tol and (lower <= 0.0 or losses[i] > lower + tol)

        band = [i for i in passing if in_band(i)]
        outside = [i for i in passing if not in_band(i)]
        for i in outside:
            rejected[i] = "more cautious than your comfort band" if losses[i] <= lower + tol else "above your comfort band"

        added: list[int] = []
        thinned: list[int] = []
        min_keep, max_keep = cfg.screen_min_keep, max(cfg.screen_min_keep, cfg.screen_max_keep)
        approved = list(band)
        if len(approved) < min_keep and outside:
            def distance(i: int) -> float:
                return lower - losses[i] if losses[i] <= lower + tol else losses[i] - upper
            added = sorted(outside, key=lambda i: (distance(i), i))[: min_keep - len(approved)]
            for i in added:
                rejected.pop(i, None)
            approved = sorted(approved + added)
        elif len(approved) > max_keep:
            by_loss = sorted(approved, key=lambda i: (losses[i], i))
            picks = {by_loss[int(round(k))] for k in np.linspace(0, len(by_loss) - 1, max_keep)}
            thinned = [i for i in approved if i not in picks]
            for i in thinned:
                rejected[i] = "similar risk to a kept candidate (thinned to keep the shortlist varied)"
            approved = sorted(picks)

        n = len(menu.portfolios)
        facts = [f"The behaviour check kept {len(approved)} of {n} candidate portfolios for the goal simulation."]
        band_text = (f"up to {upper:.0%}" if lower <= 0.0 else f"between {lower:.0%} and {upper:.0%}")
        facts.append(f"Comfort band: a loss {band_text} in the severe-fall test "
                     f"({'tightened by your past behavior or the risk game' if upper < cfg.risk_class_policies[menu.risk_class].max_stress_loss - 1e-9 else 'set by your questionnaire class ' + menu.risk_class}).")
        broke = sum(1 for r in rejected.values() if r.startswith("breaks"))
        if broke:
            facts.append(f"{broke} candidates broke a limit drawn from your behavior and were removed.")
        if added:
            facts.append(f"Too few candidates fell inside the band, so the {len(added)} closest outside it were added.")
        if relaxed:
            facts.append("No candidate met every soft limit from your behavior, so these were relaxed: "
                         + ", ".join(relaxed) + ". Hard limits were kept.")
        if ignored:
            facts.append("Some behavioral signals were too weak to apply: " + ", ".join(ignored) + ".")

        return ScreeningResult(
            approved=approved, band_lower=lower, band_upper=upper, stress_scenario=scenario,
            constraints=list(report.constraints), ignored=ignored, relaxed=relaxed, rejected=rejected,
            counts={"candidates": n, "broke_limit": broke, "outside_band": len(outside) - len(added),
                    "added": len(added), "thinned": len(thinned), "approved": len(approved)},
            facts=facts, report=report,
        )

    def analyze(self, investor_id: str, menu: PortfolioMenu) -> BehavioralReport:
        """Full analysis. Deterministic: same data + menu + config -> same report."""
        cfg = self.config
        flags: list[str] = []
        facts: list[str] = []
        data = self.data_provider.get_behavioral_data(investor_id)
        input_hash = self._input_hash(data, menu)

        if data is None:
            return self._empty_report(investor_id, menu, "insufficient_data", "none",
                                      ["no_behavioral_data"], [], input_hash)

        issues: list[str] = []
        if data.investor_id != investor_id:
            issues.append(f"data belongs to investor '{data.investor_id}', not '{investor_id}'")
        if data.history is not None:
            issues += self._validate_history(data.history)
        if issues:
            return self._empty_report(investor_id, menu, "invalid_data", data.data_source,
                                      ["behavioral_data_rejected"], issues, input_hash)

        # ---- Look-ahead protection: nothing after the menu's as_of may be used -----------
        view, history_days = None, 0
        if data.history is not None:
            view, history_days = self._prepare_history(data.history, menu.as_of, flags)
        game = data.game_result
        if game is not None and game.played_on > menu.as_of and not self.config.accept_game_after_as_of:
            flags.append("game_after_as_of_ignored")
            game = None
        loss_acc = data.loss_acceptance
        if loss_acc is not None and loss_acc.played_on > menu.as_of and not self.config.accept_game_after_as_of:
            flags.append("loss_acceptance_after_as_of_ignored")
            loss_acc = None
        crash = data.crash_test
        if crash is not None and crash.played_on > menu.as_of and not self.config.accept_game_after_as_of:
            flags.append("crash_test_after_as_of_ignored")
            crash = None

        if cfg.game_in_profile and (game is not None or loss_acc is not None):
            # The category already rests partly on the game: counting its scores again as a tighter limit would count it twice.
            flags.append("game_part_of_category")
            facts.append("The Risk Game result is already part of the risk category, so it is not counted a second time as "
                         "behavioural evidence here. The crash test and the trading history still are.")
            game, loss_acc = None, None

        features: list[BehavioralFeature] = []
        evidence: list[Evidence] = []
        revealed: float | None = None
        held_depths: list[float] = []
        episodes: list[bc.Episode] = []
        if data.history is not None:
            features.append(BehavioralFeature(name="history_days", value=float(history_days),
                                              unit="trading days", source="real"))

        # ---- The game: compared with the questionnaire class from Agent A's menu ------------
        class_level = RISK_CLASS_ORDER.index(menu.risk_class) + 1
        game_level, game_agrees = None, False
        if game is not None:
            game_level = cfg.game_level_map[game.choice]
            game_agrees = game_level < class_level
            self._game_evidence(game, game_level, class_level, menu.risk_class, evidence, features, flags, facts)
        game_acts = game_level is not None and class_level - game_level >= cfg.game_gap_levels
        la_level, la_acts = None, False
        if loss_acc is not None:
            la_level, la_acts = self._loss_acceptance_evidence(loss_acc, class_level, menu.risk_class, evidence,
                                                               features, flags, facts)
        ct_value, ct_acts = None, False
        if crash is not None:
            ct_value, ct_acts = self._crash_test_evidence(crash, menu.risk_class, evidence, features, flags, facts)

        # ---- Real trading history ------------------------------------------------------------
        threshold: _Threshold | None = None
        if view is not None:
            revealed = self._history_features(view, features, flags, facts)
            episodes, held_depths, threshold = self._panic_evidence(view, game_agrees, evidence, features,
                                                                    flags, facts)
            self._patterns(view, episodes, data.stated_horizon_years, evidence, features, flags, facts)
        real_informative = bool(episodes) or bool(held_depths)
        if held_depths:
            features.append(BehavioralFeature(
                name="max_drawdown_held_through", value=max(held_depths), unit="drawdown fraction",
                source="real", n_evidence=len(held_depths),
                evidence_refs=[e.ref for e in evidence if e.kind == "held_through"]))

        status = ("ok" if (view is not None or game is not None or la_level is not None or crash is not None)
                  else "insufficient_data")
        if status == "insufficient_data":
            flags.append("no_usable_behavioral_data")

        # ---- Limits for Agent A ------------------------------------------------------------------
        losses = self._scenario_losses(menu, flags)
        constraints: list[Constraint] = []
        if threshold is not None:
            constraints += self._loss_limit(threshold, losses, investor_id, input_hash, "capitulation", flags,
                                            cfg.risk_class_policies[menu.risk_class].max_stress_loss)
        elif not real_informative and (game_acts or la_acts or ct_acts):
            # Hypothetical signals only: the most cautious acting one wins, at most one class down; never hard.
            class_limit = lambda lvl: cfg.risk_class_policies[RISK_CLASS_ORDER[lvl - 1]].max_stress_loss
            signals = []        # (name, loss limit, what, basis, evidence ref, confidence)
            if game_acts:
                signals.append(("game", class_limit(game_level), "the cautious game result",
                                f"{RISK_CLASS_ORDER[game_level - 1]}'s limit", "game:eckel_grossman", cfg.conf_game))
            if la_acts:
                signals.append(("loss_acceptance", class_limit(la_level),
                                "the risk game's loss acceptance (safer bets after losing rounds)",
                                f"{RISK_CLASS_ORDER[la_level - 1]}'s limit", "game:loss_acceptance",
                                cfg.conf_loss_acceptance))
            if ct_acts:
                signals.append(("crash_test", ct_value, "where they sold in the crash test",
                                "the loss at which they sold", "game:crash_test", cfg.conf_crash_test))
            strictest = min(signals, key=lambda sig: sig[1])
            value, basis = strictest[1], strictest[3]
            floor_class = RISK_CLASS_ORDER[max(class_level - cfg.game_max_class_drop, 1) - 1]
            floor = cfg.risk_class_policies[floor_class].max_stress_loss
            if value < floor - 1e-9:
                value = floor
                basis = (f"capped at {floor_class}'s limit: game results alone lower the limit by at most "
                         f"{cfg.game_max_class_drop} class")
                flags.append("game_limit_capped_one_class")
            game_limit = _Threshold(
                value=value, hard=False,
                confidence=cfg.conf_game_and_loss_acceptance if len(signals) > 1 else signals[0][5],
                source="game", refs=[sig[4] for sig in signals], contradicted=False)
            constraints += self._loss_limit(game_limit, losses, investor_id, input_hash,
                                            "+".join(sig[0] for sig in signals), flags,
                                            cfg.risk_class_policies[menu.risk_class].max_stress_loss)
            threshold = game_limit
            facts.append(f"No informative trading history, so {' and '.join(sig[2] for sig in signals)} sets a "
                         f"soft limit: at most {game_limit.value:.0%} loss in the crash scenario ({basis}).")
        if game_acts and real_informative:
            flags.append("game_overridden_by_trading_history")
        if la_acts and real_informative:
            flags.append("loss_acceptance_overridden_by_trading_history")
        if ct_acts and real_informative:
            flags.append("crash_test_overridden_by_trading_history")
        if "overtrading" in flags:
            loss_limit = next((c.value for c in constraints if c.type == "max_stress_loss"), None)
            constraints += self._turnover_guardrail(menu, losses, loss_limit, investor_id, input_hash, flags, facts)

        assessment = self._assess_menu(menu, losses, threshold, held_depths, revealed)
        revealed_class = None
        if revealed is not None:
            caps = [(k, cfg.risk_class_policies[k].max_equity) for k in RISK_CLASS_ORDER]
            revealed_class = bc.class_for_equity_share(revealed, caps)
            if menu.target_equity - revealed > cfg.equity_gap_flag:
                flags.append("class_target_above_revealed_equity")
            elif revealed - menu.target_equity > cfg.equity_gap_flag:
                flags.append("class_target_below_revealed_equity")

        return BehavioralReport(
            investor_id=investor_id, as_of=menu.as_of, status=status, data_source=data.data_source,
            constraints=constraints, features=features, evidence=evidence, menu_assessment=assessment,
            advisor_facts=facts, stress_scenario=cfg.stress_scenario, risk_class=menu.risk_class,
            capitulation_threshold=threshold.value if threshold and threshold.source == "real" else None,
            threshold_source=threshold.source if threshold else None, game_level=game_level,
            loss_acceptance_level=la_level, crash_test_threshold=ct_value,
            revealed_equity_share=revealed, revealed_class=revealed_class,
            implied_risk_aversion=(bc.implied_risk_aversion(revealed, cfg.assumed_equity_premium,
                                                            cfg.assumed_equity_vol)
                                   if revealed is not None else None),
            flags=flags, issues=[], input_hash=input_hash, version=cfg.version,
        )

    # ======================================================================
    # Validation and preparation
    # ======================================================================
    @staticmethod
    def _validate_history(history: TradingHistory) -> list[str]:
        """Structural checks. Any issue -> status invalid_data and NO constraints (never a crash)."""
        issues: list[str] = []
        pos = history.positions
        if not isinstance(pos.index, pd.DatetimeIndex):
            return ["positions index is not a DatetimeIndex"]
        if pos.index.tz is not None:
            issues.append("positions index must be timezone-naive dates")
        if len(pos) == 0:
            issues.append("positions are empty")
        if not pos.index.is_monotonic_increasing:
            issues.append("position dates are not sorted ascending")
        if pos.index.has_duplicates:
            issues.append("duplicate position dates")
        if pos.isna().any().any():
            issues.append("positions contain missing values (NaN)")
        elif (pos < 0).any().any():
            issues.append("positions contain negative values (long-only accounts expected)")
        unknown = sorted(set(pos.columns) - set(history.asset_classes))
        if unknown:
            issues.append(f"asset_classes missing for positions: {unknown}")
        if not any(history.asset_classes.get(c) == "cash" for c in pos.columns):
            issues.append("positions need a cash column so trades between positions keep value unchanged")

        flows = history.external_flows
        if len(flows) > 0:
            if not isinstance(flows.index, pd.DatetimeIndex):
                issues.append("external_flows index is not a DatetimeIndex")
            else:
                if flows.isna().any():
                    issues.append("external_flows contain missing values (NaN)")
                if not flows.index.isin(pos.index).all():
                    issues.append("external_flows have dates that are not position dates")

        prices = history.asset_prices
        if prices is not None:
            if not isinstance(prices.index, pd.DatetimeIndex):
                issues.append("asset_prices index is not a DatetimeIndex")
            else:
                extra = sorted(set(prices.columns) - set(pos.columns))
                if extra:
                    issues.append(f"asset_prices have assets that are not positions: {extra}")
                if (prices <= 0).any().any():
                    issues.append("asset_prices contain zero or negative prices")
        unknown_tags = sorted(set(history.fundamentals) - set(pos.columns))
        if unknown_tags:
            issues.append(f"fundamentals given for assets that are not positions: {unknown_tags}")

        index_set = set(pos.index.normalize()) if len(pos) else set()
        for trade in history.trades:
            if trade.asset_id not in pos.columns:
                issues.append(f"trade in unknown asset {trade.asset_id} on {trade.trade_date}")
                break
            if pd.Timestamp(trade.trade_date) not in index_set:
                issues.append(f"trade date {trade.trade_date} is not a position date")
                break
        return issues

    def _prepare_history(self, history: TradingHistory, as_of: date,
                         flags: list[str]) -> tuple[_HistoryView | None, int]:
        cfg = self.config
        cutoff = pd.Timestamp(as_of)
        pos = history.positions
        if pos.index.max() > cutoff:
            flags.append("history_truncated_at_as_of")
        pos = pos.loc[pos.index <= cutoff]
        n = len(pos)
        if n < cfg.min_history_days:
            flags.append("history_too_short")
            return None, n

        flows = history.external_flows
        flows = flows[flows.index <= cutoff].groupby(level=0).sum() if len(flows) else flows
        total = pos.sum(axis=1)
        if (total <= 0).all():
            flags.append("account_always_empty")
            return None, n
        returns = bc.time_weighted_returns(total, flows)
        growth = bc.growth_index(returns)
        drawdown = bc.rolling_drawdown(growth, cfg.peak_window_days).to_numpy()

        classes = history.asset_classes
        risky_cols = [c for c in pos.columns if classes[c] in cfg.risky_classes]
        noncash_cols = [c for c in pos.columns if classes[c] != "cash"]
        risky_value = pos[risky_cols].sum(axis=1).to_numpy() if risky_cols else np.zeros(n)

        net_sold, gross_sold = np.zeros(n), np.zeros(n)
        gross_traded = 0.0
        trades: list[tuple[int, str, str, float]] = []
        day_of = {ts: i for i, ts in enumerate(pos.index.normalize())}
        for trade in history.trades:
            i = day_of.get(pd.Timestamp(trade.trade_date))
            if i is None:
                continue  # after as_of (validation already rejected dates outside the history)
            gross_traded += trade.value
            trades.append((i, trade.asset_id, trade.side, trade.value))
            if classes[trade.asset_id] in cfg.risky_classes:
                if trade.side == "sell":
                    net_sold[i] += trade.value
                    gross_sold[i] += trade.value
                else:
                    net_sold[i] -= trade.value

        aligned = flows.reindex(pos.index, fill_value=0.0).to_numpy() if len(flows) else np.zeros(n)
        prices = None
        if history.asset_prices is not None:
            prices = history.asset_prices.reindex(index=pos.index, columns=pos.columns)
        live = total > 0
        noncash = pos.loc[live, noncash_cols].div(total[live], axis=0) if noncash_cols else pd.DataFrame()
        return _HistoryView(
            history=history, positions=pos, prices=prices, dates=pos.index, total_value=total.to_numpy(),
            growth=growth.to_numpy(), drawdown=drawdown, risky_cols=risky_cols, risky_value=risky_value,
            risky_net_sold=net_sold, risky_gross_sold=gross_sold, inflows=np.clip(aligned, 0.0, None),
            outflows=np.clip(-aligned, 0.0, None), gross_traded=gross_traded, trades=trades,
            noncash_weights=noncash,
        ), n

    # ======================================================================
    # Evidence: history features, panic selling, game
    # ======================================================================
    def _history_features(self, view: _HistoryView, features: list[BehavioralFeature],
                          flags: list[str], facts: list[str]) -> float:
        """Revealed equity share, turnover, concentration. Returns the revealed risky share."""
        cfg = self.config
        live = view.total_value > 0
        revealed = float(np.mean(view.risky_value[live] / view.total_value[live]))
        n_days = len(view.dates)
        turnover = bc.annual_turnover(view.gross_traded, float(view.total_value[live].mean()), n_days)
        per_month = bc.trades_per_month(len(view.trades), n_days)
        concentration = bc.concentration_p90(view.noncash_weights) if not view.noncash_weights.empty else 0.0
        features += [
            BehavioralFeature(name="revealed_equity_share", value=revealed,
                              unit="share of account in equity (time average)", source="real", n_evidence=n_days),
            BehavioralFeature(name="annual_turnover", value=turnover, unit="one-way per year",
                              source="real", n_evidence=len(view.trades)),
            BehavioralFeature(name="trades_per_month", value=per_month, unit="trades", source="real",
                              n_evidence=len(view.trades)),
            BehavioralFeature(name="concentration_p90", value=concentration,
                              unit="largest non-cash position weight, 90th percentile of days",
                              source="real", n_evidence=n_days),
        ]
        if turnover > cfg.overtrading_turnover:
            flags.append("overtrading")
            facts.append(f"Trades a lot: about {turnover:.0%} of the account per year, {per_month:.1f} trades a month.")
        if concentration > cfg.high_concentration:
            flags.append("high_concentration")
            facts.append(f"Often keeps over {concentration:.0%} of the account in a single position.")
        return revealed

    def _panic_evidence(self, view: _HistoryView, game_agrees: bool, evidence: list[Evidence],
                        features: list[BehavioralFeature], flags: list[str], facts: list[str]
                        ) -> tuple[list[bc.Episode], list[float], _Threshold | None]:
        cfg = self.config
        episodes, held = bc.detect_episodes(
            view.drawdown, view.risky_value, view.risky_net_sold, view.risky_gross_sold, view.outflows,
            cfg.spell_floor, cfg.min_trigger_drawdown, cfg.min_sell_fraction, cfg.sell_window_days,
            cfg.max_withdrawal_share, cfg.min_informative_depth,
        )
        day = lambda i: view.dates[i].date()
        refs = []
        for e in episodes:
            ref = f"capitulation:{day(e.trigger_pos).isoformat()}"
            refs.append(ref)
            evidence.append(Evidence(ref=ref, kind="capitulation", source="real", start=day(e.spell_start),
                                     end=day(e.spell_end), drawdown=e.trigger_drawdown,
                                     sold_fraction=e.sold_fraction))
            facts.append(f"Sold {e.sold_fraction:.0%} of equities on {day(e.trigger_pos)} when the account "
                         f"was down {e.trigger_drawdown:.1%}.")
        for h in held:
            evidence.append(Evidence(ref=f"held_through:{day(h.spell_start).isoformat()}", kind="held_through",
                                     source="real", start=day(h.spell_start), end=day(h.spell_end),
                                     drawdown=h.depth))
            facts.append(f"Held through a {h.depth:.1%} drop ({day(h.spell_start)} to {day(h.spell_end)}) "
                         "without panic selling.")
        held_depths = [h.depth for h in held]
        if not episodes:
            if not held:
                flags.append("no_stress_period_in_history")
            return episodes, held_depths, None

        triggers = [e.trigger_drawdown for e in episodes]
        value = float(np.median(triggers))
        n = len(triggers)
        consistent = n >= 2 and max(triggers) - min(triggers) <= cfg.consistency_band
        contradicted = any(d > value + cfg.contradiction_margin for d in held_depths)
        if contradicted:
            hard, conf = False, cfg.conf_contradicted
            flags.append("panic_evidence_conflicts_with_held_through")
        elif consistent:
            hard, conf = True, min(cfg.conf_multi_cap, cfg.conf_multi_base + cfg.conf_multi_step * (n - 1))
        elif n == 1 and game_agrees:
            hard, conf = False, cfg.conf_single_with_game
            refs = refs + ["game:eckel_grossman"]
            flags.append("single_panic_confirmed_by_game")
        else:
            hard, conf = False, cfg.conf_single_episode
            if n >= 2:
                flags.append("panic_episodes_inconsistent")
        threshold = _Threshold(value=value, hard=hard, confidence=round(conf, 4), source="real", refs=refs,
                               contradicted=contradicted)
        features.append(BehavioralFeature(name="capitulation_threshold", value=value, unit="drawdown fraction",
                                          source="real", n_evidence=n, evidence_refs=refs))
        return episodes, held_depths, threshold

    def _game_evidence(self, game: GameResult, game_level: int, class_level: int, risk_class: str,
                       evidence: list[Evidence], features: list[BehavioralFeature], flags: list[str],
                       facts: list[str]) -> None:
        cfg = self.config
        gap = class_level - game_level
        evidence.append(Evidence(ref="game:eckel_grossman", kind="game", source="game", start=game.played_on,
                                 end=game.played_on, value=float(game_level),
                                 note=f"choice {game.choice} of 6 (payoffs {game.payoff_low:g}/{game.payoff_high:g}), hypothetical"))
        features.append(BehavioralFeature(name="game_level", value=float(game_level), unit="level 1-5",
                                          source="game", n_evidence=1, evidence_refs=["game:eckel_grossman"]))
        if gap >= cfg.game_gap_levels:
            flags.append("game_more_cautious_than_class")
            facts.append(f"In the risk game they chose gamble {game.choice} (level {game_level}), "
                         f"much more cautious than their questionnaire class {risk_class}.")
        elif gap >= 1:
            flags.append("game_slightly_more_cautious")
        elif gap < 0:
            flags.append("game_more_aggressive_than_class")
            facts.append(f"In the risk game they chose gamble {game.choice} (level {game_level}), more "
                         f"adventurous than class {risk_class}. Shown only; limits are never loosened.")

    def _loss_acceptance_evidence(self, la: LossAcceptance, class_level: int, risk_class: str,
                                  evidence: list[Evidence], features: list[BehavioralFeature], flags: list[str],
                                  facts: list[str]) -> tuple[int | None, bool]:
        """(level, acts). It acts only when the score shows retreating after losses (below neutral) AND its
        level is game_gap_levels or more below the questionnaire class. It never loosens anything."""
        cfg = self.config
        if la.rounds_after_loss == 0:
            flags.append("loss_acceptance_without_losing_rounds")
            return None, False
        level = cfg.loss_acceptance_level(la.score)
        rounds = f", {la.rounds_after_loss} losing rounds" if la.rounds_after_loss else ""
        evidence.append(Evidence(ref="game:loss_acceptance", kind="loss_acceptance", source="game",
                                 start=la.played_on, end=la.played_on, value=float(level),
                                 note=f"score {la.score:g}/100{rounds}, hypothetical"))
        features.append(BehavioralFeature(name="loss_acceptance_level", value=float(level), unit="level 1-5",
                                          source="game", n_evidence=1, evidence_refs=["game:loss_acceptance"]))
        if la.score >= cfg.loss_acceptance_neutral:
            return level, False
        gap = class_level - level
        if gap >= cfg.game_gap_levels:
            flags.append("loss_acceptance_more_cautious_than_class")
            facts.append(f"In the risk game they moved to safer bets after losing rounds (loss acceptance "
                         f"{la.score:g}/100, level {level}), much more cautious than their questionnaire class "
                         f"{risk_class}.")
            return level, True
        if gap >= 1:
            flags.append("loss_acceptance_slightly_more_cautious")
        return level, False

    def _crash_test_evidence(self, ct: CrashTestResult, risk_class: str, evidence: list[Evidence],
                             features: list[BehavioralFeature], flags: list[str],
                             facts: list[str]) -> tuple[float | None, bool]:
        """(hypothetical capitulation point, acts). A sale of at least min_sell_fraction marks the loss at which
        the player sold; it acts when that loss is below the class's own limit. Holding on is a fact only."""
        cfg = self.config
        if ct.sold_at_drawdown is None or ct.sold_fraction < cfg.min_sell_fraction:
            if ct.sold_fraction > 0:
                flags.append("crash_test_small_sale")
            if ct.max_drawdown_seen >= cfg.min_informative_depth:
                flags.append("crash_test_held_through")
                facts.append(f"In the crash test they held on through a hypothetical {ct.max_drawdown_seen:.0%} "
                             "fall. Shown only; limits are never loosened.")
            evidence.append(Evidence(ref="game:crash_test", kind="crash_test", source="game", start=ct.played_on,
                                     end=ct.played_on, drawdown=ct.max_drawdown_seen,
                                     sold_fraction=ct.sold_fraction, note="held on, hypothetical"))
            return None, False
        value = float(ct.sold_at_drawdown)
        evidence.append(Evidence(ref="game:crash_test", kind="crash_test", source="game", start=ct.played_on,
                                 end=ct.played_on, drawdown=value, sold_fraction=ct.sold_fraction,
                                 note="sold, hypothetical"))
        features.append(BehavioralFeature(name="crash_test_sell_drawdown", value=value, unit="drawdown fraction",
                                          source="game", n_evidence=1, evidence_refs=["game:crash_test"]))
        facts.append(f"In the crash test they sold {ct.sold_fraction:.0%} of the fund when it was down "
                     f"{value:.0%} (hypothetical).")
        acts = value < cfg.risk_class_policies[risk_class].max_stress_loss - 1e-9
        flags.append("crash_test_sold_below_class_limit" if acts else "crash_test_sold_above_class_limit")
        return value, acts

    # ======================================================================
    # The five Advisor-only patterns
    # ======================================================================
    def _patterns(self, view: _HistoryView, episodes: list[bc.Episode], horizon: float | None,
                  evidence: list[Evidence], features: list[BehavioralFeature], flags: list[str],
                  facts: list[str]) -> None:
        cfg = self.config
        hist = view.history
        n = len(view.dates)
        no_prices = "needs asset prices in the trading history"

        # 1. Cost of panic: holding the pre-sale positions vs what actually happened
        if episodes and view.prices is not None:
            costs, refs = [], []
            for e in episodes:
                start = e.trigger_pos - 1
                end = min(start + cfg.panic_cost_window_days, n - 1)
                cost = bc.panic_cost(view.positions.iloc[start].to_numpy(dtype=float),
                                     view.prices.iloc[start].to_numpy(dtype=float),
                                     view.prices.iloc[end].to_numpy(dtype=float),
                                     float(view.growth[end] / view.growth[start]))
                costs.append(cost)
                refs.append(f"capitulation:{view.dates[e.trigger_pos].date().isoformat()}")
                facts.append(f"The sale on {view.dates[e.trigger_pos].date()} left the account about "
                             f"{abs(cost):.1%} {'behind' if cost >= 0 else 'ahead of'} simply holding on, "
                             f"measured over the next {end - start} trading days.")
            mean_cost = float(np.mean(costs))
            features.append(BehavioralFeature(name="cost_of_panic", value=mean_cost,
                                              unit="share of account value vs holding (average per sale)",
                                              source="real", n_evidence=len(costs), evidence_refs=refs))
            if mean_cost > cfg.panic_cost_flag:
                flags.append("panic_selling_was_costly")
        elif episodes:
            features.append(BehavioralFeature(name="cost_of_panic", value=None, unit="", source="real",
                                              note=no_prices))

        # 2. Disposition effect: realize gains, keep losses (Odean 1998)
        if view.prices is not None and view.risky_cols:
            cols = view.risky_cols
            col_idx = {c: i for i, c in enumerate(cols)}
            counts = bc.disposition_counts(
                view.prices[cols].to_numpy(dtype=float), view.positions[cols].iloc[0].to_numpy(dtype=float),
                [(d, col_idx[a], s, v) for d, a, s, v in view.trades if a in col_idx])
            value = counts.effect if counts.sell_days >= cfg.disposition_min_sell_days else None
            if value is not None:
                note = f"PGR {counts.pgr:.2f}, PLR {counts.plr:.2f}, {counts.sell_days} sell days"
            elif counts.sell_days < cfg.disposition_min_sell_days:
                note = f"only {counts.sell_days} days with equity sales; needs {cfg.disposition_min_sell_days}+"
            else:
                note = "never held both a winning and a losing position on a sell day"
            features.append(BehavioralFeature(name="disposition_effect", value=value,
                                              unit="PGR - PLR (share of gains realized minus share of losses realized)",
                                              source="real", n_evidence=counts.sell_days, note=note))
            if value is not None and value > cfg.disposition_flag:
                flags.append("disposition_effect")
                facts.append(f"Tends to sell winners and keep losers: realized {counts.pgr:.0%} of gains "
                             f"but only {counts.plr:.0%} of losses when selling.")
        else:
            features.append(BehavioralFeature(name="disposition_effect", value=None, unit="", source="real",
                                              note=no_prices))

        # 3 + 4. Buying after rallies (return chasing) and hype buys
        risky_buys = [(d, a, v) for d, a, s, v in view.trades
                      if s == "buy" and a in view.risky_cols and view.inflows[d] <= 0]   # deposit-day buys are routine
        if view.prices is not None and risky_buys:
            trailing = bc.trailing_returns(view.prices, cfg.rally_lookback_days)
            pct = bc.rally_percentiles(trailing)
            spike = bc.trailing_returns(view.prices, cfg.hype_lookback_days)
            values = np.array([v for _, _, v in risky_buys])
            chased = np.array([bool(pct.iloc[d][a] >= cfg.rally_percentile) if np.isfinite(pct.iloc[d][a]) else False
                               for d, a, _ in risky_buys])
            share = bc.chase_share(values, chased) if len(risky_buys) >= cfg.chase_min_buys else None
            features.append(BehavioralFeature(
                name="return_chasing_share", value=share,
                unit=f"share of discretionary equity buys made after a top-20% {cfg.rally_lookback_days}-day rally "
                     "(about 0.20 by chance)",
                source="real", n_evidence=len(risky_buys),
                note="" if share is not None else f"needs {cfg.chase_min_buys}+ discretionary buys"))
            if share is not None and share >= cfg.chase_flag_share:
                flags.append("return_chasing")
                facts.append(f"{share:.0%} of discretionary equity purchases came right after a strong rally "
                             "(about 20% would be expected by chance).")

            weak = {a for a, f in hist.fundamentals.items()
                    if f.instrument == "stock" and (f.profitable is False or
                                                    (f.debt_to_equity or 0.0) > cfg.weak_debt_to_equity)}
            hype = np.array([a in weak and np.isfinite(spike.iloc[d][a]) and spike.iloc[d][a] >= cfg.hype_spike_return
                             for d, a, _ in risky_buys])
            n_hype = int(hype.sum())
            features.append(BehavioralFeature(
                name="hype_buy_share", value=bc.chase_share(values, hype) if hist.fundamentals else None,
                unit=f"share of discretionary equity buys in weak-fundamental stocks after a "
                     f"{cfg.hype_spike_return:.0%}+ {cfg.hype_lookback_days}-day spike",
                source="real", n_evidence=n_hype,
                note="" if hist.fundamentals else "needs fundamentals tags in the trading history"))
            for (d, a, v), is_hype in zip(risky_buys, hype):
                if is_hype and sum(e.kind == "hype_buy" for e in evidence) < 5:
                    f = hist.fundamentals[a]
                    evidence.append(Evidence(
                        ref=f"hype_buy:{view.dates[d].date().isoformat()}:{a}", kind="hype_buy", source="real",
                        start=view.dates[d].date(), end=view.dates[d].date(), asset_id=a, value=v,
                        note=f"up {spike.iloc[d][a]:.0%} in {cfg.hype_lookback_days} days; profitable="
                             f"{f.profitable}, debt/equity={f.debt_to_equity}"))
            if n_hype >= cfg.hype_flag_min_buys:
                flags.append("hype_chasing")
                names = sorted({a for (_, a, _), h in zip(risky_buys, hype) if h})
                facts.append(f"Bought {', '.join(names)} {n_hype} times right after sharp price spikes, although "
                             "the company is unprofitable or heavily indebted.")
        else:
            note = no_prices if view.prices is None else "no discretionary equity buys"
            features.append(BehavioralFeature(name="return_chasing_share", value=None, unit="", source="real", note=note))
            features.append(BehavioralFeature(name="hype_buy_share", value=None, unit="", source="real", note=note))

        # 5. Horizon mismatch: how long positions are actually kept vs the stated horizon
        live = view.total_value > 0
        years = bc.implied_holding_years(float(view.risky_value[live].mean()), float(view.risky_gross_sold.sum()), n)
        note = "never sold equity: holding period longer than the history" if years is None else ""
        if horizon is None:
            note = (note + "; " if note else "") + "no stated horizon to compare with"
        features.append(BehavioralFeature(name="implied_holding_years", value=years, unit="years (selling speed)",
                                          source="real", n_evidence=int((view.risky_gross_sold > 0).sum()), note=note))
        if years is not None and horizon is not None and years < cfg.horizon_mismatch_ratio * horizon:
            flags.append("horizon_mismatch")
            facts.append(f"Positions are kept about {years:.1f} years on average, far shorter than the stated "
                         f"{horizon:g}-year horizon.")

    # ======================================================================
    # Limits for Agent A
    # ======================================================================
    def _scenario_losses(self, menu: PortfolioMenu, flags: list[str]) -> list[float] | None:
        """Positive loss of every menu portfolio in the configured scenario (menu order kept)."""
        scenario = self.config.stress_scenario
        if not menu.portfolios:
            flags.append("empty_menu")
            return None
        if any(scenario not in p.stress or p.equity_weight is None for p in menu.portfolios):
            flags.append("stress_scenario_missing_from_menu")
            return None
        return [max(0.0, -float(p.stress[scenario])) for p in menu.portfolios]

    def _loss_limit(self, threshold: _Threshold, losses: list[float] | None, investor_id: str,
                    input_hash: str, kind: str, flags: list[str], class_limit: float | None = None) -> list[Constraint]:
        """max_stress_loss at the evidence value; clamped if even the safest menu portfolio loses more.
        Evidence at or above the class's own limit (class_limit) tightens nothing: no constraint is sent.

        Agent A (v0.2) steps the equity weight down until every limit holds, soft ones included, so
        no extra equity cap is needed to make this limit effective.
        """
        if losses is None:
            return []
        cfg = self.config
        value = threshold.value
        if class_limit is not None and value >= class_limit - 1e-9:
            flags.append("behavioral_limit_not_below_class")     # no stricter than the class's own limit: B only tightens
            return []
        safest = min(losses)
        if safest > value - cfg.feasibility_slack:
            value = bc.ceil_to(safest + cfg.feasibility_slack, 1e-4)
            flags.append("behavioral_limit_clamped_to_menu")
        return [Constraint(type="max_stress_loss", value=value, scenario=cfg.stress_scenario, source="behavioral",
                           hard=threshold.hard, confidence=threshold.confidence,
                           evidence_ref=f"{investor_id}:{input_hash}:{kind}")]

    def _turnover_guardrail(self, menu: PortfolioMenu, losses: list[float] | None, loss_limit: float | None,
                            investor_id: str, input_hash: str, flags: list[str],
                            facts: list[str]) -> list[Constraint]:
        """Soft max_turnover for overtraders: change the portfolio gradually instead of all at once.

        Value = turnover_limit (20%): Agent A moves at most that share of the portfolio per rebalance.
        Exception: when B also sends a loss limit, a tight turnover cap could make that loss limit
        impossible to meet, and Agent A would then drop EVERY soft limit together. So the cap is widened
        to what reaching the loss-limit level needs: the larger turnover of the two menu portfolios that
        bracket the highest level meeting the loss limit (turnover is convex, so every level in between
        can meet it), plus slack.
        """
        cfg = self.config
        portfolios = menu.portfolios
        if not portfolios or any(p.implementation is None for p in portfolios):
            flags.append("turnover_limit_needs_current_holdings")
            return []
        value = cfg.turnover_limit
        if loss_limit is not None and losses is not None:
            order = sorted(range(len(portfolios)), key=lambda i: portfolios[i].equity_weight or 0.0)
            fits = [i for i in order if (portfolios[i].equity_weight or 0.0) <= menu.target_equity + 1e-9
                    and losses[i] <= loss_limit]
            low = fits[-1] if fits else order[0]
            position = order.index(low)
            needed = max(portfolios[i].implementation.turnover for i in order[position:position + 2])
            if needed + cfg.turnover_slack > value:
                value = min(1.0, needed + cfg.turnover_slack)
                flags.append("turnover_limit_widened_for_loss_limit")
        facts.append(f"Because of frequent trading, this rebalance changes at most {value:.0%} of the portfolio.")
        return [Constraint(type="max_turnover", value=value, source="behavioral", hard=False,
                           confidence=cfg.conf_turnover, evidence_ref=f"{investor_id}:{input_hash}:overtrading")]

    def _assess_menu(self, menu: PortfolioMenu, losses: list[float] | None, threshold: _Threshold | None,
                     held_depths: list[float], revealed: float | None) -> list[CandidateAssessment]:
        cfg = self.config
        held_max = max(held_depths) if held_depths else None
        out: list[CandidateAssessment] = []
        for i, p in enumerate(menu.portfolios):
            loss = losses[i] if losses is not None else None
            if loss is None:
                fit, reasons = "no_evidence", ["stress_scenario_unavailable"]
            elif threshold is not None:
                if loss > threshold.value:
                    fit, reasons = "misfit", ["loss_exceeds_limit"]
                elif loss > (1.0 - cfg.caution_margin) * threshold.value:
                    fit, reasons = "caution", ["loss_near_limit"]
                else:
                    fit, reasons = "fit", ["loss_within_limit"]
                reasons.append("source_trading_history" if threshold.source == "real" else "source_game_only")
                if threshold.contradicted:
                    reasons.append("evidence_conflicting")
            elif held_max is not None:
                if loss <= held_max:
                    fit, reasons = "fit", ["loss_within_drawdown_held_through"]
                else:
                    fit, reasons = "no_evidence", ["loss_beyond_observed_experience"]
            else:
                fit, reasons = "no_evidence", ["no_behavioral_evidence"]
            equity = float(p.equity_weight or 0.0)
            if revealed is not None and equity > revealed + cfg.equity_gap_flag:
                reasons.append("equity_above_revealed_share")
            out.append(CandidateAssessment(menu_index=i, equity_weight=equity, scenario_loss=loss,
                                           fit=fit, reasons=reasons))
        return out

    # ======================================================================
    # Helpers
    # ======================================================================
    def _empty_report(self, investor_id: str, menu: PortfolioMenu, status: str, source: str,
                      flags: list[str], issues: list[str], input_hash: str) -> BehavioralReport:
        losses = self._scenario_losses(menu, flags)
        return BehavioralReport(
            investor_id=investor_id, as_of=menu.as_of, status=status, data_source=source,
            constraints=[], features=[], evidence=[],
            menu_assessment=self._assess_menu(menu, losses, None, [], None), advisor_facts=[],
            stress_scenario=self.config.stress_scenario, risk_class=menu.risk_class,
            capitulation_threshold=None, threshold_source=None, game_level=None,
            revealed_equity_share=None, revealed_class=None, implied_risk_aversion=None,
            flags=flags, issues=issues, input_hash=input_hash, version=self.config.version,
        )

    def _input_hash(self, data: BehavioralData | None, menu: PortfolioMenu) -> str:
        """Fingerprint of everything B used, so any report can be reproduced."""
        digest = hashlib.sha256()
        digest.update(menu.input_hash.encode())
        digest.update(self.config.model_dump_json().encode())
        if data is None:
            digest.update(b"no-data")
            return digest.hexdigest()[:16]
        digest.update(data.model_dump_json(exclude={"history"}).encode())
        if data.history is not None:
            h = data.history
            digest.update(pd.util.hash_pandas_object(h.positions, index=True).to_numpy().tobytes())
            if len(h.external_flows):
                digest.update(pd.util.hash_pandas_object(h.external_flows, index=True).to_numpy().tobytes())
            if h.asset_prices is not None:
                digest.update(pd.util.hash_pandas_object(h.asset_prices, index=True).to_numpy().tobytes())
            digest.update(json.dumps([t.model_dump(mode="json") for t in h.trades]).encode())
            digest.update(json.dumps({k: v.model_dump(mode="json") for k, v in sorted(h.fundamentals.items())}).encode())
            digest.update(json.dumps(h.asset_classes, sort_keys=True).encode())
            digest.update(h.base_currency.encode())
        return digest.hexdigest()[:16]


def _candidate_meets(c: Constraint, portfolio) -> bool:
    """Does one candidate portfolio respect one of B's limits? Unknown limit types never remove anything."""
    tol = 1e-6
    if c.type == "max_stress_loss":
        return c.scenario not in portfolio.stress or -portfolio.stress[c.scenario] <= c.value + tol
    if c.type == "max_equity":
        return (portfolio.equity_weight or 0.0) <= c.value + tol
    if c.type == "max_turnover":
        return portfolio.implementation is None or portfolio.implementation.turnover <= c.value + tol
    if c.type == "max_vol":
        return portfolio.risk is None or portfolio.risk.ex_ante_vol <= c.value + tol
    return True
