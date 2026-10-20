"""RiskProfile -> what the other agents may see. Numbers only; never the score or the C-level.

    to_agent_c_limits          -> Agent C's InvestorInterests (max_r_level, blocked_product_types).
                                  Agent C drops every instrument above max_r_level from the universe.
    to_investor_context_limits -> Agent A's InvestorContext fields (equity cap, loss limit, exclusions,
                                  per-asset cap), built from the profile and Agent C's UniverseSelection.
    to_constraints             -> two hard suitability Constraints, re-checked by the Risk Engine.

R-levels come from Agent C's catalog only (Instrument.r_level); this module keeps no second list.
Constraint is the SHARED class from egoquant/models.py (also used by Agent A and Agent B); it is not
redefined here. If that class changes, to_constraints is the only place in this module to update.
"""

from __future__ import annotations

from egoquant.agents.market_intel import UniverseSelection
from egoquant.models import Constraint
from egoquant.risk_profile.models import RiskProfile, RiskProfileConfig


def _stress_limit(profile: RiskProfile, config: RiskProfileConfig) -> float:
    return config.gate.max_stress_loss if profile.no_loss_gate else profile.loss_capacity


def to_constraints(profile: RiskProfile, config: RiskProfileConfig) -> list[Constraint]:
    """Exactly two hard suitability constraints: max_equity and max_stress_loss."""
    evidence = profile.questionnaire_version
    return [
        Constraint(type="max_equity", value=profile.suitability_max_equity, source="suitability",
                   hard=True, evidence_ref=evidence),
        Constraint(type="max_stress_loss", value=_stress_limit(profile, config), source="suitability",
                   hard=True, evidence_ref=evidence, scenario=config.adapter.stress_scenario),
    ]


def to_agent_c_limits(profile: RiskProfile, config: RiskProfileConfig) -> dict:
    """Fields for Agent C's InvestorInterests."""
    blocked = list(config.knowledge.complex_product_types) if profile.product_access == "blocked_complex" else []
    return {"max_r_level": profile.max_r_level, "blocked_product_types": blocked}


def to_investor_context_limits(profile: RiskProfile, config: RiskProfileConfig,
                               selection: UniverseSelection) -> dict:
    """Suitability fields for Agent A's InvestorContext, given Agent C's selection for this investor.

    excluded_assets: Agent C's assets_to_exclude (held but not allowed), plus, as a backstop, any
        selected instrument rated above max_r_level (Agent C should already have dropped those).
    max_asset_weight: only set for R1-only investors (config.adapter.r1_max_asset_weight); otherwise
        Agent A's own default applies. With two R1 funds a 40% cap per asset cannot reach 100%.
    max_acceptable_loss: the loss capacity, or 0 for investors who accept no loss (the gate).
    """
    above = [i.ticker for i in selection.instruments if i.r_level > profile.max_r_level]
    excluded = list(dict.fromkeys([*selection.assets_to_exclude, *above]))
    fields: dict = {"suitability_max_equity": profile.suitability_max_equity, "excluded_assets": excluded,
                    "max_acceptable_loss": _stress_limit(profile, config)}
    if profile.max_r_level == 1:
        fields["max_asset_weight"] = config.adapter.r1_max_asset_weight
    return fields
