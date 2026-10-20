"""Risk Engine: independent re-check of Agent A's final proposal. It has the final say.

    Agent A --PortfolioProposal--> Risk Engine --RiskReview (approved / violations)--> Advisor

Independent on purpose: it does not call Agent A, Agent B or Agent A's math core. It recomputes everything
from the proposal's weights, the assets' metadata and the limits, so a bug in the optimizer cannot
hide a breach. Checks, per proposal:

  1. weights      long-only and sum to 1
  2. R-level      every holding's product risk level <= the investor's max_r_level.
                  R-levels come from Agent C's catalog (the single source). An unknown ticker or a
                  missing max_r_level is a violation: the engine fails closed.
                  Also: no holding above its instrument cap from Agent C (e.g. 10% per single stock).
  3. equity       total equity weight <= every hard max_equity constraint
  4. class caps   total weight of a class (commodity, crypto, cash) <= every hard max_class_weight
  5. stress loss  loss in each named HYPOTHETICAL scenario <= every hard max_stress_loss constraint

Soft constraints are not enforced here (Agent A may relax them by design).
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from egoquant.agents.market_intel import DEFAULT_CATALOG, Instrument
from egoquant.models import AssetInfo, Constraint, PortfolioProposal, StressScenario
from egoquant.scenarios import DEFAULT_STRESS_SCENARIOS

ENGINE_VERSION = "0.2.0"


class RiskCheck(BaseModel):
    name: str                      # e.g. "r_level[SHY]", "suitability:max_equity"
    passed: bool
    detail: str


_CLASS_WORDS = {"equity": "stocks", "bond": "bonds", "cash": "cash", "commodity": "gold and commodities", "crypto": "crypto"}
_NAME = r"[A-Za-z0-9.\-^=]+"


def _num(text: str) -> str:
    shown = f"{float(text):.1f}"
    return shown[:-2] if shown.endswith(".0") else shown


_PLAIN = [
    (re.compile(r"^proposal has no holdings \(status .+\)$"),
     lambda m: "no portfolio fits all of your limits at once (a high cash or liquidity need is the usual cause)"),
    (re.compile(rf"^({_NAME}) \([\d.]+%\) is R(\d); investor maximum is R(\d)$"),
     lambda m: f"{m[1]} is a higher-risk product (risk level R{m[2]}); your category allows products up to R{m[3]}"),
    (re.compile(rf"^({_NAME}) \([\d.]+%\) has no R-level in Agent C's catalog$"),
     lambda m: f"{m[1]} is not in our product list, so its risk level cannot be checked"),
    (re.compile(rf"^({_NAME}) weight ([\d.]+)% vs instrument cap ([\d.]+)%$"),
     lambda m: f"{m[1]} would be {_num(m[2])}% of the portfolio, above the {_num(m[3])}% allowed in one product of this kind"),
    (re.compile(r"^equity weight ([\d.]+)% vs limit ([\d.]+)% \(.*\)$"),
     lambda m: f"your share in stocks would be {_num(m[1])}%, above your limit of {_num(m[2])}%"),
    (re.compile(r"^(\w+) weight ([\d.]+)% vs limit ([\d.]+)% \(.*\)$"),
     lambda m: f"your share in {_CLASS_WORDS.get(m[1], m[1])} would be {_num(m[2])}%, above your limit of {_num(m[3])}%"),
    (re.compile(r"^hypothetical loss ([\d.]+)% in (.+?) vs limit ([\d.]+)% \(.*\)$"),
     lambda m: f"in the \"{m[2]}\" scenario the portfolio could lose {_num(m[1])}%, more than your limit of {_num(m[3])}%"),
]


def plain_violations(violations: list[str]) -> list[str]:
    """The failed checks' details for the investor: internal labels such as (suitability:max_equity) are dropped, the same
    sentence is said once, and anything not recognised is passed on unchanged. The review itself keeps the exact details."""
    out: list[str] = []
    for text in violations:
        for pattern, build in _PLAIN:
            m = pattern.match(text)
            if m:
                text = build(m)
                break
        if text not in out:
            out.append(text)
    return out


class RiskReview(BaseModel):
    approved: bool
    violations: list[str]          # details of the failed checks, in check order
    checks: list[RiskCheck]
    engine_version: str = ENGINE_VERSION
    label: str = "independent re-check against hypothetical stress scenarios; not a forecast"


class RiskEngine:
    def __init__(self, catalog: list[Instrument] | None = None, scenarios: list[StressScenario] | None = None,
                 weight_tolerance: float = 1e-6, limit_tolerance: float = 1e-6):
        catalog = catalog if catalog is not None else DEFAULT_CATALOG
        self.r_levels = {i.ticker: i.r_level for i in catalog}
        self.scenarios = {s.name: s for s in (scenarios or DEFAULT_STRESS_SCENARIOS)}
        self.weight_tolerance = weight_tolerance
        self.limit_tolerance = limit_tolerance

    def review(self, proposal: PortfolioProposal, assets: list[AssetInfo], constraints: list[Constraint],
               max_r_level: int | None) -> RiskReview:
        checks: list[RiskCheck] = []
        holdings = {a: w for a, w in proposal.weights.items() if abs(w) > self.weight_tolerance}
        info = {a.asset_id: a for a in assets}

        checks.append(self._check_weights(proposal, holdings))
        missing = sorted(set(holdings) - set(info))
        if missing:
            checks.append(RiskCheck(name="asset_metadata", passed=False,
                                    detail=f"no metadata for holdings {missing}"))
        else:
            checks += self._check_r_levels(holdings, max_r_level)
            checks += self._check_instrument_caps(holdings, info)
            checks += self._check_equity(holdings, info, constraints)
            checks += self._check_class_weights(holdings, info, constraints)
            checks += self._check_stress(holdings, info, constraints)

        violations = [c.detail for c in checks if not c.passed]
        return RiskReview(approved=not violations, violations=violations, checks=checks)

    # ------------------------------------------------------------------
    def _check_weights(self, proposal: PortfolioProposal, holdings: dict[str, float]) -> RiskCheck:
        if not holdings:
            return RiskCheck(name="weights", passed=False, detail=f"proposal has no holdings (status {proposal.status})")
        negative = sorted(a for a, w in holdings.items() if w < 0)
        total = sum(holdings.values())
        ok = not negative and abs(total - 1.0) <= 1e-4
        detail = f"weights sum to {total:.6f}" + (f"; negative weights {negative}" if negative else "")
        return RiskCheck(name="weights", passed=ok, detail=detail)

    def _check_instrument_caps(self, holdings: dict[str, float], info: dict[str, AssetInfo]) -> list[RiskCheck]:
        """Per-instrument caps from Agent C's metadata (e.g. at most 10% in one single stock)."""
        return [
            RiskCheck(name=f"instrument_cap[{a}]", passed=w <= info[a].max_weight + self.limit_tolerance,
                      detail=f"{a} weight {w:.2%} vs instrument cap {info[a].max_weight:.2%}")
            for a, w in sorted(holdings.items()) if info[a].max_weight is not None
        ]

    def _check_r_levels(self, holdings: dict[str, float], max_r_level: int | None) -> list[RiskCheck]:
        if max_r_level is None:
            return [RiskCheck(name="r_level", passed=False, detail="investor max_r_level not provided")]
        checks = []
        for asset_id, w in sorted(holdings.items()):
            r = self.r_levels.get(asset_id)
            if r is None:
                checks.append(RiskCheck(name=f"r_level[{asset_id}]", passed=False,
                                        detail=f"{asset_id} ({w:.1%}) has no R-level in Agent C's catalog"))
            else:
                checks.append(RiskCheck(name=f"r_level[{asset_id}]", passed=r <= max_r_level,
                                        detail=f"{asset_id} ({w:.1%}) is R{r}; investor maximum is R{max_r_level}"))
        return checks

    def _check_equity(self, holdings: dict[str, float], info: dict[str, AssetInfo],
                      constraints: list[Constraint]) -> list[RiskCheck]:
        equity = sum(w for a, w in holdings.items() if info[a].asset_class == "equity")
        return [
            RiskCheck(name=c.label, passed=equity <= c.value + self.limit_tolerance,
                      detail=f"equity weight {equity:.2%} vs limit {c.value:.2%} ({c.label})")
            for c in constraints if c.hard and c.type == "max_equity"
        ]

    def _check_class_weights(self, holdings: dict[str, float], info: dict[str, AssetInfo],
                             constraints: list[Constraint]) -> list[RiskCheck]:
        checks = []
        for c in constraints:
            if not (c.hard and c.type == "max_class_weight"):
                continue
            weight = sum(w for a, w in holdings.items() if info[a].asset_class == c.asset_class)
            checks.append(RiskCheck(name=c.label, passed=weight <= c.value + self.limit_tolerance,
                                    detail=f"{c.asset_class} weight {weight:.2%} vs limit {c.value:.2%} ({c.label})"))
        return checks

    def _check_stress(self, holdings: dict[str, float], info: dict[str, AssetInfo],
                      constraints: list[Constraint]) -> list[RiskCheck]:
        checks = []
        for c in constraints:
            if not (c.hard and c.type == "max_stress_loss"):
                continue
            scenario = self.scenarios.get(c.scenario or "")
            if scenario is None:
                checks.append(RiskCheck(name=c.label, passed=False, detail=f"unknown stress scenario '{c.scenario}'"))
                continue
            loss = -sum(w * self._asset_shock(info[a], scenario) for a, w in holdings.items())
            checks.append(RiskCheck(name=c.label, passed=loss <= c.value + self.limit_tolerance,
                                    detail=f"hypothetical loss {loss:.2%} in {scenario.name} vs limit "
                                           f"{c.value:.2%} ({c.label})"))
        return checks

    @staticmethod
    def _asset_shock(asset: AssetInfo, scenario: StressScenario) -> float:
        """Asset return in the scenario: class shock, plus duration/convexity rate effect for bonds."""
        shock = scenario.asset_class_shocks.get(asset.asset_class, 0.0)
        if asset.asset_class == "bond" and asset.duration_years is not None and scenario.rate_shock_bp:
            dy = scenario.rate_shock_bp / 10_000.0
            shock += -asset.duration_years * dy + 0.5 * (asset.convexity or 0.0) * dy * dy
        return shock
