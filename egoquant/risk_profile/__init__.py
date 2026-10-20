"""Formal risk questionnaire: Answers -> RiskProfile (C1..C5) -> suitability Constraints.

Deterministic code only. See README.md in this folder for sources and every cutoff's justification.
"""

from egoquant.risk_profile.adapter import to_agent_c_limits, to_constraints, to_investor_context_limits
from egoquant.risk_profile.explain import explain
from egoquant.risk_profile.loader import load_all, load_config, load_question_bank
from egoquant.risk_profile.models import AnswerAuditRecord, Answers, GameInput, RiskProfile, RiskProfileConfig
from egoquant.risk_profile.scoring import answers_hash, audit_records, build_risk_profile, is_expired

__all__ = [
    "AnswerAuditRecord", "Answers", "GameInput", "RiskProfile", "RiskProfileConfig",
    "answers_hash", "audit_records", "build_risk_profile", "is_expired", "explain",
    "load_all", "load_config", "load_question_bank",
    "to_agent_c_limits", "to_constraints", "to_investor_context_limits",
]
