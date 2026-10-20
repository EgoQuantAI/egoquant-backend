"""The advisory record (`egoq-record-1`): what we advised, built from the saved report at confirm time.

Fields: docs/ON_CHAIN_COORDINATION.md section 2. Never in the record: user id, email, username, IBKR data, the
questionnaire score, free text, headlines. The whole saved report is only FINGERPRINTED (versions.report_sha256);
it is never encrypted and never put on-chain. Building a record changes nothing in the report.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from functools import lru_cache
from pathlib import Path

from egoquant.anchoring.canonical import canonical_text, report_sha256

SCHEMA = "egoq-record-1"
SCHEMA_V2 = "egoq-record-2"      # + client {username, report_id} + versions.report_document_sha256 (the sealed document)
SCHEMAS = (SCHEMA, SCHEMA_V2)
PPM = 1_000_000
WEIGHT_SUM_TOLERANCE = Decimal("0.0001")         # PROVISIONAL: weights must sum to 1 within this
_PKG = Path(__file__).resolve().parent.parent


class NotSealable(ValueError):
    """This report cannot be confirmed or sealed. `reason` is a short, user-safe label."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def utc_seconds(value: str | datetime) -> str:
    """'YYYY-MM-DDTHH:MM:SSZ' (UTC, truncated to seconds) from a datetime or a database ISO timestamp."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _instant(value: str | datetime) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _cents(x) -> int:
    return int((Decimal(repr(float(x))) * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def allocation_ppm(rows: list[dict]) -> list[dict]:
    """[{ticker, weight_ppm}] sorted by ticker, weight > 0 only, summing to exactly 1 000 000.

    Largest remainder: each weight is floored to ppm, and the missing ppm go to the largest remainders (ties:
    the larger weight, then the ticker), so the biggest holding absorbs the rounding."""
    weights: dict[str, Decimal] = {}
    for row in rows:
        w = Decimal(repr(float(row.get("weight") or 0)))
        if w <= 0:
            continue
        ticker = str(row.get("asset_id") or "")
        if not ticker or ticker in weights:
            raise NotSealable("allocation is not readable")
        weights[ticker] = w
    total = sum(weights.values(), Decimal(0))
    if not weights or abs(total - 1) > WEIGHT_SUM_TOLERANCE:
        raise NotSealable("allocation does not sum to 100%")
    exact = {t: w / total * PPM for t, w in weights.items()}
    ppm = {t: int(v) for t, v in exact.items()}
    missing = PPM - sum(ppm.values())
    for t in sorted(exact, key=lambda t: (exact[t] - ppm[t], weights[t], t), reverse=True)[:missing]:
        ppm[t] += 1
    return [{"ticker": t, "weight_ppm": ppm[t]} for t in sorted(ppm)]


def _answers(saved: dict) -> dict:
    raw = (saved.get("inputs") or {}).get("answers")
    if not isinstance(raw, dict):
        raise NotSealable("saved before plans could be sealed")
    choices = raw.get("choices") or {}
    multi = raw.get("multi_choices") or {}
    out = {"choices": {str(k): str(v) for k, v in choices.items()},
           "multi_choices": {str(k): sorted(str(x) for x in v) for k, v in multi.items()},
           "amount_invested_cents": _cents(raw.get("amount_invested") or 0),
           "amount_needed_cents": _cents(raw.get("amount_needed") or 0)}
    if isinstance(raw.get("risk_game"), dict):          # only when the Risk Game was played: it is an input of the category
        out["risk_game"] = _risk_game(raw["risk_game"])
    return out


def _ppm(x) -> int:
    return int(round(float(x) * 1_000_000))


def _risk_game(game: dict) -> dict:
    """The Risk Game result as the record keeps it (docs/RISK_GAME_BLEND.md): the category rests on its two scores, Agent B's
    loss limit on the crash test. Whole numbers only (the crash test's fractions as ppm); the time it was played is not an input."""
    out: dict = {"tolerance": int(game.get("tolerance") or 0)}
    if game.get("loss_acceptance") is not None:
        out["loss_acceptance"] = int(game["loss_acceptance"])
    crash = game.get("crash_test")
    if isinstance(crash, dict):
        out["crash_test"] = {"max_drawdown_seen_ppm": _ppm(crash.get("max_drawdown_seen") or 0),
                             "sold_fraction_ppm": _ppm(crash.get("sold_fraction") or 0),
                             "sold_at_drawdown_ppm": None if crash.get("sold_at_drawdown") is None else _ppm(crash["sold_at_drawdown"])}
    return out


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@lru_cache(maxsize=1)
def code_versions() -> dict[str, str]:
    """Versions of the code that produced the advice (read only; nothing here is edited)."""
    import yaml
    from egoquant.risk_engine import ENGINE_VERSION
    briefing_cfg = yaml.safe_load((_PKG / "briefing" / "config.yaml").read_text()) or {}
    return {"risk_engine": ENGINE_VERSION,
            "risk_profile_config": _sha256_file(_PKG / "risk_profile" / "config.yaml"),
            "agent_d_prompt_sha256": _sha256_file(_PKG / "briefing" / "prompts" / "fund_aware_tone.txt"),
            "agent_d_model": str((briefing_cfg.get("tone") or {}).get("llm_model") or "")}


def check_sealable(saved: dict) -> None:
    """Only a Risk-Engine-approved report with no violations is ever confirmed or sealed."""
    report = saved.get("report") or {}
    if saved.get("approved") is not True or saved.get("violations") or report.get("risk_review_approved") is False:
        raise NotSealable("not approved")
    if report.get("status") == "infeasible" or not report.get("allocation"):
        raise NotSealable("not approved")


def build_record(saved: dict, *, report_id: str, log_rows: list[dict], consent_version: str,
                 confirmed_at: datetime, record_version: int = 1, versions: dict | None = None,
                 username: str | None = None, document_sha256: str | None = None) -> dict:
    """The record for one confirmed report version. `saved` is the `reports.report` value (the FINAL saved
    report, after Agent D's news update); `log_rows` are the user's operation_log rows ({at, action, details})."""
    check_sealable(saved)
    report = saved.get("report") or {}
    answers = _answers(saved)
    stamp = utc_seconds(confirmed_at)
    # In real order: by the database's full timestamp, then truncated to seconds; plan_confirmed is always last.
    mine = sorted((r for r in log_rows if (r.get("details") or {}).get("report_id") == report_id
                   and r.get("action") != "plan_confirmed" and r.get("at")), key=lambda r: _instant(r["at"]))
    log = [{"at": utc_seconds(r["at"]), "action": str(r["action"])} for r in mine]
    log.append({"at": stamp, "action": "plan_confirmed"})
    checks = [{"name": str(c.get("name", "")), "passed": bool(c.get("passed")), "detail": str(c.get("detail", ""))}
              for c in saved.get("checks") or []]
    checks.sort(key=lambda c: (c["name"], c["detail"]))
    risk_class = str(report.get("risk_class") or saved.get("c_level") or "")
    if risk_class not in {"C1", "C2", "C3", "C4", "C5"}:
        raise NotSealable("risk class missing")
    record = {
        "schema": SCHEMA_V2 if document_sha256 else SCHEMA,
        "report_id": report_id,
        "record_version": int(record_version),
        "confirmed_at": stamp,
        "consent_version": consent_version,
        "investor": {"risk_class": risk_class,
                     "questionnaire_version": str((saved.get("inputs") or {}).get("answers", {})
                                                  .get("questionnaire_version") or _bank_version()),
                     "answers": answers},
        "allocation": allocation_ppm(report.get("allocation") or []),
        "risk_engine": {"approved": True, "violations": [], "checks": checks},
        "versions": {**(versions if versions is not None else code_versions()),
                     "engine": str(report.get("engine_version") or ""),
                     "input_hash": str(report.get("input_hash") or ""),
                     "report_sha256": report_sha256(saved),
                     **({"report_document_sha256": document_sha256} if document_sha256 else {})},
        "operation_log": log,
    }
    if document_sha256:            # v2: who it was sealed for (the account's username only: never an email or user id)
        if not username:
            raise NotSealable("username missing")
        record["client"] = {"username": str(username), "report_id": report_id}
    canonical_text(record)                       # raises now if anything is not canonical
    return record


@lru_cache(maxsize=1)
def _bank_version() -> str:
    """The questionnaire version from the risk-profile config (it must match questions.json)."""
    import yaml
    cfg = yaml.safe_load((_PKG / "risk_profile" / "config.yaml").read_text()) or {}
    return str(cfg.get("questionnaire_version") or "")
