"""Portfolio method v2, stage 3: candidate portfolios that do not collapse onto the calmest fund.

Along the same stock-share axis as the default engine, at each level, six styles (all inside the same hard limits, the 30% per-fund
cap of C3 to C5, the group caps and the single-stock cap; the nearest feasible portfolio to a raw target is found by the existing
solve_portfolio(target_weights=...)):
  erc          equal risk contribution inside each sleeve (Maillard, Roncalli, Teiletche 2010; Spinu 2013)
  hrp          hierarchical risk parity (Lopez de Prado 2016)
  max_div      maximum diversification (Choueifaty and Coignard 2008)
  bl_mv        Black-Litterman (1992) mean-variance: expected returns from reverse optimisation of a reference allocation plus
               small deterministic views from the investor's interests; NEVER historical means
  resampled    Michaud (1989) resampled efficiency: the average of N bl_mv solutions on resampled expected returns, then projected
               back onto the feasible set. The draws come from a PRNG seeded with SHA-256(data, investor, views, level):
               the same client always gets the same portfolio, different clients get different draws.
  diversified  the default engine's minimum variance plus a concentration penalty (kept as a reference candidate)

Pure functions of the model and the investor; no I/O. docs/PORTFOLIO_V2.md.
"""

from __future__ import annotations

import hashlib

import numpy as np

from egoquant.agents.quant import core as qc
from egoquant.agents.quant.v2 import solve as v2solve
from egoquant.models import PortfolioMenu

V2_LEVELS = 19                 # PROVISIONAL: points on the stock-share axis (the default engine uses 20)
N_RESAMPLE = 24                # PROVISIONAL: resampled draws per level (cheap closed-form targets, one projection; Michaud used hundreds)
RESAMPLE_YEARS = 10.0          # effective years of information behind the expected returns (sampling error = cov / years)
RISK_AVERSION = 3.0            # PROVISIONAL: delta of the Black-Litterman prior and of bl_mv
RIDGE_SMALL = 0.05             # concentration penalty in units of the average asset variance, for bl_mv and resampled
EFFECTIVE_N_FLOOR = 6.0        # PROVISIONAL: C3 to C5
MIN_KEEP = 0.01                # PROVISIONAL: positions below 1% of the account are not worth holding (dust)
MAX_POSITIONS = 20             # PROVISIONAL: a practical portfolio holds at most this many funds (cardinality limit)
TIDY_ABOVE = MAX_POSITIONS     # a candidate with more positions than this is tidied once
STYLES = ("erc", "hrp", "max_div", "bl_mv", "resampled", "diversified", "equal_weight", "inverse_vol", "interest_tilt")
STYLE_LABELS = {
    "erc": "Equal risk contribution", "hrp": "Hierarchical risk parity", "max_div": "Maximum diversification",
    "bl_mv": "Black-Litterman mean-variance", "resampled": "Resampled efficiency", "diversified": "Diversified minimum risk",
    # the default engine's styles stay in the family, under the v2 limits, so the best chance of v2 can never fall below v1's
    "min_variance": "Lowest risk", "equal_weight": "Equal weight", "inverse_vol": "Risk-balanced (inverse volatility)",
    "interest_tilt": "Interest tilt",
}


def effective_n(w: np.ndarray) -> float:
    """1 / sum(w^2): how many equally weighted funds would be as concentrated."""
    s = float(np.sum(np.square(w)))
    return 1.0 / s if s > 0 else 0.0


def seed_for(model, context, level: float, tag: str) -> int:
    """Deterministic seed: the same inputs give the same portfolio; any change of the investor, data or views gives other draws."""
    views = ";".join(f"{v.kind}:{v.key}:{v.annual_excess:.6f}:{v.confidence:.3f}" for v in getattr(context, "views", []))
    text = f"{model.data_hash}|{context.model_dump_json()}|{views}|{tag}|{level:.6f}"
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")


def _reference_weights(agent, model, context) -> np.ndarray:
    """The class's baseline mix spread over the near-duplicate groups of each class (a group counts once)."""
    mix = agent._baseline_mix(agent._policy(context))
    state = getattr(model, "v2", None)
    group_of = state.group_of if state is not None else {}
    w = np.zeros(len(model.asset_ids))
    excluded = set(context.excluded_assets)
    vol = np.sqrt(np.maximum(np.diag(model.cov_annual), 1e-8))
    for cls, share in mix.items():
        members = [i for i, (a, c) in enumerate(zip(model.asset_ids, model.asset_classes)) if c == cls and a not in excluded]
        if not members or share <= 0:
            continue
        # The market portfolio is mostly broad funds: sector funds and single stocks together get only 15% of a class's mass.
        sat = [v2solve.is_satellite(model.assets[i]) for i in members]
        broad = [i for i, s in zip(members, sat) if not s]
        niche = [i for i, s in zip(members, sat) if s]
        split = ([(broad, 0.85), (niche, 0.15)] if broad and niche else [(broad or niche, 1.0)])
        for group_members, part in split:
            labels = [group_of.get(model.asset_ids[i], f"solo:{i}") for i in group_members]
            count = {lab: labels.count(lab) for lab in set(labels)}
            raw = np.array([1.0 / vol[i] / count[lab] for i, lab in zip(group_members, labels)])     # a group counts once
            for i, r in zip(group_members, raw / raw.sum()):
                w[i] = share * part * r
    total = w.sum()
    return w / total if total > 0 else np.full(len(w), 1.0 / len(w))


def prior_returns(agent, model, context) -> np.ndarray:
    """Black-Litterman expected returns (annual, net of fees). Uses v2/returns_bl.py when it exists, else the plain prior."""
    w_ref = _reference_weights(agent, model, context)
    try:
        from egoquant.agents.quant.v2.returns_bl import black_litterman_returns
    except ImportError:
        pi = RISK_AVERSION * (model.cov_annual @ w_ref)
        fees = np.array([a.expense_ratio for a in model.assets])
        return pi - fees
    return np.asarray(black_litterman_returns(
        model.cov_annual, model.asset_ids, model.asset_classes, None, w_ref, list(getattr(context, "views", [])),
        model.assets, delta=RISK_AVERSION), dtype=float)


def _targets():
    try:
        from egoquant.agents.quant.v2 import targets
    except ImportError:
        return None
    return targets


def _proposal(agent, model, context, constraints, w, style, level, axis_name, result, relaxed, groups_dropped, note=""):
    from egoquant.agents.quant.agent import _hash_parts
    return agent._make_proposal(
        model, context, w, stage="optimized", method=style, status="fallback" if relaxed else "optimal",
        active_constraints=constraints, binding=result.binding, relaxed=relaxed,
        message=f"{STYLE_LABELS[style]} · {axis_name} {level:.0%}{note}",
        input_hash=_hash_parts(model.data_hash, f"v2:{style}:{axis_name}:{level:.6f}"),
        diagnostics={"v2_style": style, "v2_level": float(level), "v2_effective_n": float(effective_n(w)),
                     "v2_largest": float(np.max(w)), "v2_group_caps_dropped": float(groups_dropped)})


def _fit_to_caps(raw: np.ndarray, cap: np.ndarray, total: float) -> np.ndarray:
    try:
        from egoquant.agents.quant.v2.targets import fit_to_caps
    except ImportError:
        w = total * raw / raw.sum()
        return np.minimum(w, cap)
    return fit_to_caps(raw / raw.sum(), cap, total)


def resampled_weights(agent, model, context, constraints, level, level_mask, mu, rng, n=N_RESAMPLE, axis=None, noise=True):
    """Michaud (1989): the average of mean-variance portfolios on resampled expected returns. Each draw is the closed-form
    long-only mean-variance direction of every sleeve ((delta*cov + 2*ridge*I)^-1 mu_k, negatives closed, scaled to the sleeve's share
    and its per-fund bounds), so no solver runs per draw; the average is then projected ONCE onto all the limits by the caller."""
    cov = model.cov_annual
    n_assets = len(mu)
    ridge = RIDGE_SMALL * float(np.mean(np.diag(cov)))
    chol = np.linalg.cholesky(cov + 1e-10 * np.eye(n_assets))
    mask = level_mask if axis is None else axis.mask
    allowed = np.ones(n_assets, dtype=bool) if axis is None else axis.allowed
    upper = agent._solver_inputs(model, context, constraints, False)["upper_bounds"] if axis is None else axis.upper
    sleeves = [(np.flatnonzero(mask & allowed), level), (np.flatnonzero(~mask & allowed), 1.0 - level)]
    inverses = []
    for idx, share in sleeves:
        if share > 1e-12 and idx.size:
            inverses.append((idx, share, np.linalg.inv(RISK_AVERSION * cov[np.ix_(idx, idx)] + 2.0 * ridge * np.eye(idx.size))))
    if not inverses:
        return None
    draws = []
    for _ in range(n):
        mu_k = mu + chol @ rng.standard_normal(n_assets) / np.sqrt(RESAMPLE_YEARS) if noise else mu
        w = np.zeros(n_assets)
        for idx, share, inv in inverses:
            raw = np.maximum(inv @ mu_k[idx], 0.0)
            if raw.sum() <= 1e-12:
                raw = np.ones(idx.size)
            w[idx] = _fit_to_caps(raw, upper[idx], share)
        draws.append(w)
    return np.mean(draws, axis=0)


class Axis:
    """Where the level lives: the stock share (stocks), or the bond share for a profile allowed no stocks."""

    def __init__(self, agent, context, model, constraints, force_stocks: bool = False):
        self.equity_cap = min(c.value for c in constraints if c.type == "max_equity")
        bond_mask = np.array([c == "bond" for c in model.asset_classes])
        if self.equity_cap > 1e-9 or force_stocks:
            self.name, self.mask = "stocks", model.equity_mask
        else:
            self.name, self.mask = "bonds", bond_mask
        self.allowed = np.array([a not in context.excluded_assets for a in model.asset_ids])
        self.upper = agent._solver_inputs(model, context, constraints, False)["upper_bounds"]
        self.options = {"equity_mask": self.mask.astype(float)} if self.name == "bonds" else {}

    def levels(self, cfg, n: int) -> list[float]:
        if self.name == "stocks":
            return [float(x) for x in np.linspace(cfg.menu_min_equity, self.equity_cap, n)]
        return [float(x) for x in np.linspace(0.0, 1.0, n)]


def tidy(agent, model, context, constraints, axis: Axis, level: float, result, relaxed, dropped):
    """One re-solve for a candidate with many small positions: those below MIN_KEEP are closed (upper bound 0) and the nearest
    feasible portfolio to the rest is taken. If that is infeasible the original stays."""
    w = result.weights
    if int(np.sum(w > MIN_KEEP / 2)) <= TIDY_ABOVE:
        return result, relaxed, dropped
    classes = np.array(model.asset_classes)
    for limit in (MAX_POSITIONS, MAX_POSITIONS + 10, MAX_POSITIONS + 25):      # cardinality limit; a looser one if the limits need more funds
        keep = np.zeros(len(w), dtype=bool)
        keep[np.argsort(-w, kind="stable")[:limit]] = True
        keep &= w >= MIN_KEEP
        for cls in set(classes[w > 1e-9]):                 # every asset class that is held keeps its largest fund (cash need, class mins)
            members = np.flatnonzero((classes == cls) & (w > 1e-9))
            keep[members[np.argmax(w[members])]] = True
        target = np.where(keep, w, 0.0)
        if target.sum() <= 0:
            break
        upper = np.where(keep, axis.upper, 0.0)
        res, rel, drp = v2solve.solve_level(agent, model, context, constraints, level, level_mask=axis.mask,
                                            target_weights=target / target.sum(), upper_bounds=upper)
        if res.weights is not None:
            return res, rel, drp
    return result, relaxed, dropped


def solutions_at_level(agent, model, context, constraints, axis: Axis, level: float, mu: np.ndarray,
                       n_resample: int = N_RESAMPLE, exact_bl: bool = True):
    """Yield (style, SolveResult, relaxed, group_caps_dropped, note) for every v2 style at one level (tidied)."""
    for style, result, relaxed, dropped, note in _raw_solutions(agent, model, context, constraints, axis, level, mu, n_resample,
                                                                exact_bl):
        if result is not None and result.weights is not None:
            result, relaxed, dropped = tidy(agent, model, context, constraints, axis, level, result, relaxed, dropped)
        yield style, result, relaxed, dropped, note


def _raw_solutions(agent, model, context, constraints, axis: Axis, level: float, mu: np.ndarray, n_resample: int,
                   exact_bl: bool = True):
    cfg = agent.config
    mean_var = float(np.mean(np.diag(model.cov_annual)))
    tg = _targets()
    seen: list[np.ndarray] = []                     # what the methods above produce at this level: the default styles use it
    sleeves = [(axis.mask & axis.allowed, level), (~axis.mask & axis.allowed, 1.0 - level)]
    sleeves = [(m, s) for m, s in sleeves if s > 1e-12]
    if tg is not None:
        for style, fn in (("erc", tg.erc_target), ("hrp", tg.hrp_target), ("max_div", tg.max_diversification_target)):
            target = fn(model.cov_annual, axis.allowed, sleeves, axis.upper)
            if target is None:
                continue
            res, relaxed, dropped = v2solve.solve_level(agent, model, context, constraints, level, level_mask=axis.mask,
                                                        target_weights=target)
            _note(seen, res)
            yield style, res, relaxed, dropped, ""
    if exact_bl:        # one exact constrained solve (the no-goal final portfolio: a single level)
        res, relaxed, dropped = v2solve.solve_level(agent, model, context, constraints, level, level_mask=axis.mask,
                                                    expected_returns=mu, risk_aversion=RISK_AVERSION,
                                                    ridge=RIDGE_SMALL * mean_var, **axis.options)
        _note(seen, res)
        yield "bl_mv", res, relaxed, dropped, ""
    else:               # the menu (many levels): the closed-form mean-variance direction, then ONE projection onto the limits
        direction = resampled_weights(agent, model, context, constraints, level, axis.mask, mu, np.random.default_rng(0), 1,
                                      axis=axis, noise=False)
        if direction is not None:
            res, relaxed, dropped = v2solve.solve_level(agent, model, context, constraints, level, level_mask=axis.mask,
                                                        target_weights=direction)
            _note(seen, res)
            yield "bl_mv", res, relaxed, dropped, ""
    rng = np.random.default_rng(seed_for(model, context, level, "resample"))
    avg = resampled_weights(agent, model, context, constraints, level, axis.mask, mu, rng, n_resample, axis=axis)
    if avg is not None:
        avg = np.where(avg < MIN_KEEP, 0.0, avg)                      # dust from averaging many solutions is dropped
        avg = avg / avg.sum() if avg.sum() > 0 else avg
        res, relaxed, dropped = v2solve.solve_level(agent, model, context, constraints, level, level_mask=axis.mask,
                                                    target_weights=avg)
        _note(seen, res)
        yield "resampled", res, relaxed, dropped, f" · {n_resample} draws"
    res, relaxed, dropped = v2solve.solve_level(agent, model, context, constraints, level, level_mask=axis.mask,
                                                ridge=cfg.candidate_ridge * mean_var, **axis.options)
    _note(seen, res)
    yield "diversified", res, relaxed, dropped, ""
    yield from _default_styles(agent, model, context, constraints, axis, level, cfg, seen)


def _note(seen: list, res) -> None:
    if res is not None and res.weights is not None:
        seen.append(res.weights)


def _support(model, seen: list, floor: float = 0.02):
    """The funds the other methods agree on at this level: the MAX_POSITIONS with the largest weight in any of them (at least
    `floor`), plus the largest fund of every asset class that is held. The default styles (equal weight, inverse volatility,
    interest tilt) are then spread over THESE funds, not over all the representatives (80 equal positions of 1% are not a
    portfolio). None when the methods agree on fewer than 4 funds."""
    if not seen:
        return None
    score = np.max(np.vstack(seen), axis=0)
    support = np.zeros(len(score), dtype=bool)
    support[np.argsort(-score, kind="stable")[:MAX_POSITIONS]] = True
    support &= score >= floor
    classes = np.array(model.asset_classes)
    for cls in set(classes[score > floor]):
        members = np.flatnonzero((classes == cls) & (score > 0))
        support[members[np.argmax(score[members])]] = True
    return support if int(support.sum()) >= 4 else None


def _default_styles(agent, model, context, constraints, axis: Axis, level: float, cfg, seen=None):
    """The default engine's other four styles (QuantAgent.build_candidates), now under the v2 per-fund and group caps."""
    vols = np.sqrt(np.diag(model.cov_annual))
    interest = np.array([a in set(context.interest_assets) for a in model.asset_ids]) & axis.allowed

    support = _support(model, seen or [])

    def target(per_asset: np.ndarray):
        t = np.zeros(len(model.asset_ids))
        for group, share in ((axis.mask & axis.allowed, level), (~axis.mask & axis.allowed, 1.0 - level)):
            if share <= 1e-12:
                continue
            if support is not None and (group & support).any():
                group = group & support
            if not group.any() or per_asset[group].sum() <= 0:
                return None
            t[group] = share * per_asset[group] / per_asset[group].sum()
        return t

    plans = {"equal_weight": {"target_weights": target(np.ones(len(vols)))},
             "inverse_vol": {"target_weights": target(1.0 / np.maximum(vols, 1e-6))}}
    if interest.any():
        plans["interest_tilt"] = {"target_weights": target(np.where(interest, cfg.candidate_interest_tilt, 1.0))}
    for style, options in plans.items():
        if "target_weights" in options and options["target_weights"] is None:
            continue
        res, relaxed, dropped = v2solve.solve_level(agent, model, context, constraints, level, level_mask=axis.mask,
                                                    **options, **axis.options)
        _note(seen, res)
        yield style, res, relaxed, dropped, ""


def build_menu(agent, context, model) -> PortfolioMenu:
    """Hook H2 (QuantAgent.build_candidates under v2)."""
    from egoquant.agents.quant.agent import _hash_parts
    cfg = agent.config
    policy = agent._policy(context)
    constraints = v2solve.v2_constraints(agent, context, model)
    axis = Axis(agent, context, model, constraints)
    mu = prior_returns(agent, model, context)
    portfolios, kept = [], []
    for level in axis.levels(cfg, V2_LEVELS):
        for style, result, relaxed, dropped, note in solutions_at_level(agent, model, context, constraints, axis, level, mu,
                                                                       exact_bl=False):
            if result is None or result.weights is None:
                continue
            w = result.weights
            if any(np.max(np.abs(w - other)) <= cfg.candidate_min_distance for other in kept):
                continue
            kept.append(w)
            portfolios.append(_proposal(agent, model, context, constraints, w, style, level, axis.name, result, relaxed,
                                        dropped, note))
    portfolios.sort(key=lambda p: (p.equity_weight or 0.0, p.method))
    if len(portfolios) > cfg.candidate_target:
        keep = np.linspace(0, len(portfolios) - 1, cfg.candidate_target).round().astype(int)
        portfolios = [portfolios[i] for i in sorted(set(keep.tolist()))]
    target_equity = min(policy.target_equity, axis.equity_cap)
    menu_hash = _hash_parts(model.data_hash, context.model_dump_json(), cfg.model_dump_json(), "candidates:v2")
    baseline_weights = qc.build_baseline_weights(
        model.asset_classes, [a in context.excluded_assets for a in model.asset_ids],
        agent._baseline_mix(policy), axis.equity_cap)
    baseline = agent._make_proposal(
        model, context, baseline_weights, stage="baseline", method="fixed_weight", status="optimal",
        message="naive fixed class mix, equal weight within each class; constraints not enforced",
        active_constraints=constraints, input_hash=_hash_parts(model.data_hash, "baseline"))
    return PortfolioMenu(
        as_of=model.as_of, risk_class=context.risk_class, target_equity=target_equity, baseline=baseline,
        portfolios=portfolios, default_index=agent._pick_default_index(target_equity, portfolios),
        infeasible_levels=[], data_quality=model.data_quality, input_hash=menu_hash)
