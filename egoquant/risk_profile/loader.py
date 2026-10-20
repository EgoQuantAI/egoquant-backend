"""File I/O for the risk questionnaire. Kept separate so scoring.py stays pure (no I/O)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from egoquant.risk_profile.models import QuestionBank, RiskProfileConfig

_HERE = Path(__file__).parent
QUESTIONS_PATH = _HERE / "questions.json"
CONFIG_PATH = _HERE / "config.yaml"


def load_question_bank(path: Path | str = QUESTIONS_PATH) -> QuestionBank:
    with open(path, encoding="utf-8") as fh:
        return QuestionBank.model_validate(json.load(fh))


def load_config(path: Path | str = CONFIG_PATH) -> RiskProfileConfig:
    with open(path, encoding="utf-8") as fh:
        return RiskProfileConfig.model_validate(yaml.safe_load(fh))


def load_all() -> tuple[QuestionBank, RiskProfileConfig]:
    """Load the bank and config and check they belong to the same questionnaire version."""
    bank, config = load_question_bank(), load_config()
    if bank.questionnaire_version != config.questionnaire_version:
        raise ValueError(
            f"questions.json is {bank.questionnaire_version} but config.yaml is {config.questionnaire_version}"
        )
    return bank, config
