"""Deterministic scoring of the risk questionnaire. Pure functions: no I/O, no LLM, no clock.

Flow:
    validate_answers  -> every required item answered with a known option
    tolerance         -> Grable & Lytton sum (13-47) -> level 1-5 via published bands
    capacity          -> B3 (money needed back) is a hard cap; B2, B4, B5, B6 are averaged (points -> level);
                         capacity_level = min(hard caps, level of the average)
    knowledge         -> Big Three score 0-3 + experience -> product_access (never the C-level)
    gate              -> D1 = "no" forces C1, lowest category
    combine           -> c_level = min(tolerance_level, capacity_level) unless the gate fired
    consistency       -> flag "review_suggested" only; scores are never changed

Every number comes from RiskProfileConfig (config.yaml).
"""

from __future__ import annotations

import calendar
import hashlib
import json
from datetime import date, datetime, timezone

from egoquant.risk_profile.models import (
    AnswerAuditRecord,
    Answers,
    GameInput,
    ProductAccess,
    Question,
    QuestionBank,
    RiskProfile,
    RiskProfileConfig,
    ToleranceBand,
)

B3_ID = "B3"                                         # binding-question id reported for loss capacity
B3_INVESTED_ID = "B3_invested"
B3_NEEDED_ID = "B3_needed"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def validate_answers(answers: Answers, bank: QuestionBank) -> None:
    """Raise ValueError unless every single/multi item is answered with a known option."""
    if answers.questionnaire_version != bank.questionnaire_version:
        raise ValueError(
            f"questionnaire version mismatch: answers are for {answers.questionnaire_version}, "
            f"question bank is {bank.questionnaire_version}"
        )
    known = {q.id for q in bank.questions}
    unknown = (set(answers.choices) | set(answers.multi_choices)) - known
    if unknown:
        raise ValueError(f"answers to unknown questions: {sorted(unknown)}")
    for q in bank.questions:
        if q.type == "single":
            if q.id not in answers.choices:
                raise ValueError(f"{q.id} is not answered")
            q.option(answers.choices[q.id])
        elif q.type == "multi":
            picked = answers.multi_choices.get(q.id)
            if not picked:
                raise ValueError(f"{q.id} is not answered (select at least one option)")
            for option_id in picked:
                q.option(option_id)
            if "none" in picked and len(picked) > 1:
                raise ValueError(f"{q.id}: 'none' cannot be combined with other options")


# --------------------------------------------------------------------------
# Section GL: risk tolerance
# --------------------------------------------------------------------------
def _points(q: Question, option_id: str) -> int:
    points = q.option(option_id).points
    if points is None:
        raise ValueError(f"{q.id}: option '{option_id}' has no points")
    return points


def tolerance_score(answers: Answers, bank: QuestionBank) -> int:
    return sum(_points(q, answers.choices[q.id]) for q in bank.section("GL"))


def tolerance_band(score: int, config: RiskProfileConfig) -> ToleranceBand:
    for band in config.tolerance.bands:
        if band.min <= score <= band.max:
            return band
    raise ValueError(f"tolerance score {score} is outside every configured band")


def _normalized(q: Question, option_id: str) -> float:
    """Item score rescaled to 0-1, so 2-, 3- and 4-option items are comparable."""
    all_points = [o.points for o in q.options if o.points is not None]
    lo, hi = min(all_points), max(all_points)
    return (_points(q, option_id) - lo) / (hi - lo)


def consistency_gap(answers: Answers, bank: QuestionBank, config: RiskProfileConfig) -> float:
    """Self-description (0-1) minus the mean of the scenario items (0-1). Positive = says bolder than acts."""
    cfg = config.tolerance.consistency
    self_value = _normalized(bank.get(cfg.self_item), answers.choices[cfg.self_item])
    scenario = [_normalized(bank.get(qid), answers.choices[qid]) for qid in cfg.scenario_items]
    return self_value - sum(scenario) / len(scenario)


# --------------------------------------------------------------------------
# Section B: risk capacity
# --------------------------------------------------------------------------
def loss_capacity(invested: float, needed: float) -> float:
    """Share of the invested amount the investor can lose and still meet the goal, in [0, 1]."""
    if invested <= 0:
        raise ValueError("amount invested must be greater than 0")
    if needed < 0:
        raise ValueError("amount needed cannot be negative")
    if needed > invested:
        raise ValueError("amount needed cannot be larger than the amount invested")
    return min(1.0, max(0.0, (invested - needed) / invested))


def loss_capacity_cap(value: float, config: RiskProfileConfig) -> int:
    for band in config.capacity.loss_capacity_bands:
        if value < band.below:
            return band.cap
    return config.capacity.loss_capacity_max_cap


def capacity_caps(answers: Answers, config: RiskProfileConfig) -> dict[str, int]:
    """Level of each HARD-capped capacity question (config.capacity.hard_caps) and of each answer that is capped on its own
    (config.capacity.answer_caps), in question order."""
    cfg = config.capacity
    caps: dict[str, int] = {}
    for qid in cfg.question_order:
        if qid in cfg.answer_caps and qid not in cfg.hard_caps:
            answer_cap = cfg.answer_caps[qid].get(answers.choices[qid])
            if answer_cap is not None:
                caps[qid] = answer_cap
            continue
        if qid not in cfg.hard_caps:
            continue
        if qid == B3_ID:
            caps[qid] = loss_capacity_cap(loss_capacity(answers.amount_invested, answers.amount_needed), config)
        else:
            option_id = answers.choices[qid]
            if option_id not in cfg.option_caps[qid]:
                raise ValueError(f"{qid}: no cap configured for option '{option_id}'")
            caps[qid] = cfg.option_caps[qid][option_id]
    return caps


def capacity_points(answers: Answers, config: RiskProfileConfig) -> dict[str, int]:
    """Points (1-5) of each AVERAGED capacity question, in question order."""
    cfg = config.capacity
    points: dict[str, int] = {}
    for qid in cfg.question_order:
        if qid not in cfg.aggregate.questions:
            continue
        if qid == B3_ID:                              # the money-needed-back band, used as points
            points[qid] = loss_capacity_cap(loss_capacity(answers.amount_invested, answers.amount_needed), config)
        else:
            option_id = answers.choices[qid]
            if option_id not in cfg.aggregate.option_points[qid]:
                raise ValueError(f"{qid}: no points configured for option '{option_id}'")
            points[qid] = cfg.aggregate.option_points[qid][option_id]
    return points


def aggregate_level(points: dict[str, int], config: RiskProfileConfig) -> tuple[float | None, int]:
    """(average points, level) of the averaged capacity answers; no averaged question = (None, max level)."""
    agg = config.capacity.aggregate
    if not points:
        return None, agg.max_level
    average = sum(points.values()) / len(points)
    for band in agg.level_bands:
        if average < band.below:
            return average, band.level
    return average, agg.max_level


def capacity_level(caps: dict[str, int], points: dict[str, int], config: RiskProfileConfig,
                   ) -> tuple[int, str, float | None]:
    """(capacity level, the question that limits it, average points).

    level = min(hard caps, level of the average of the averaged answers). The limiting question is the hard-capped one
    when a hard cap is at least as low as the average's level (first in order on a tie), otherwise the averaged answer
    with the fewest points (first in order on a tie): the one that pulled the average down most."""
    average, average_level = aggregate_level(points, config)
    hard_level = min(caps.values()) if caps else config.capacity.aggregate.max_level
    level = min(hard_level, average_level)
    if caps and hard_level <= average_level:
        return level, next(qid for qid, cap in caps.items() if cap == hard_level), average
    weakest = min(points.values())
    return level, next(qid for qid, pts in points.items() if pts == weakest), average


# --------------------------------------------------------------------------
# Section K: knowledge and experience
# --------------------------------------------------------------------------
def knowledge_score(answers: Answers, bank: QuestionBank, config: RiskProfileConfig) -> int:
    """Number of Big Three items answered correctly. 'Do not know' and 'Refuse' count as wrong."""
    return sum(answers.choices[qid] == bank.get(qid).correct_option for qid in config.knowledge.big_three)


def has_experience(answers: Answers, config: RiskProfileConfig) -> bool:
    cfg = config.knowledge
    products = set(answers.multi_choices.get("K4", []))
    return bool(products & set(cfg.experience_products)) or answers.choices["K5"] not in cfg.no_experience_years


def product_access(score: int, experienced: bool, config: RiskProfileConfig) -> ProductAccess:
    cfg = config.knowledge
    if score >= cfg.full_access_min_score and (experienced or not cfg.full_access_requires_experience):
        return "full"
    if score >= cfg.warning_min_score:
        return "warning"
    return "blocked_complex"


# --------------------------------------------------------------------------
# Combine
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Compliance record: answers fingerprint, validity period
# --------------------------------------------------------------------------
ANSWERS_HASH_SCHEME = "eq-answers-v1"


def answers_hash(answers: Answers, game: GameInput | None = None) -> str:
    """Stable fingerprint of the normalized answers and questionnaire version.

    Normalized: choices sorted by question id, multi-select options sorted (order does not matter),
    amounts written with two decimals (50000 and 50000.0 are the same answer). Any change to an answer,
    an amount or the questionnaire version changes the hash.
    """
    payload = {
        "scheme": ANSWERS_HASH_SCHEME,
        "questionnaire_version": answers.questionnaire_version,
        "choices": dict(sorted(answers.choices.items())),
        "multi_choices": {q: sorted(set(opts)) for q, opts in sorted(answers.multi_choices.items())},
        "amount_invested": f"{answers.amount_invested:.2f}",
        "amount_needed": f"{answers.amount_needed:.2f}",
    }
    if game is not None:                              # a profile built with a game fingerprints the game too; none = as before
        payload["game"] = {"tolerance": game.tolerance, "loss_acceptance": game.loss_acceptance}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def add_months(dt: datetime, months: int) -> datetime:
    """Calendar months; the day is clamped to the month's length (29 Feb + 12 months -> 28 Feb)."""
    years, month0 = divmod(dt.month - 1 + months, 12)
    year, month = dt.year + years, month0 + 1
    return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1]))


def is_expired(profile: RiskProfile, on: date) -> bool:
    """True when the profile's validity ended before `on` (valid through the whole valid_until day)."""
    return profile.valid_until.date() < on


# --------------------------------------------------------------------------
# The Risk Game inside the category (docs/RISK_GAME_BLEND.md)
# --------------------------------------------------------------------------
def game_score(game: GameInput) -> float:
    """Mean of the game's tolerance and loss acceptance (0-100); the tolerance alone when there is no loss acceptance."""
    if game.loss_acceptance is None:
        return float(game.tolerance)
    return (game.tolerance + game.loss_acceptance) / 2.0


def game_level(score: float) -> int:
    """1 + floor(score / 20), at most 5: equal bands 0-19.99 -> 1, 20-39.99 -> 2, 40-59.99 -> 3, 60-79.99 -> 4, 80-100 -> 5."""
    return min(5, 1 + int(score // 20))


def blended_level(tolerance_level: int, level_of_game: int, weight: float) -> int:
    """The tolerance part of the category: (1 - weight) * questionnaire level + weight * game level, rounded to the nearest
    class (a half rounds up), kept between 1 and 5. weight 1/3 is (2 * t + game) / 3; weight 0 is the questionnaire alone."""
    blend = (1.0 - weight) * tolerance_level + weight * level_of_game
    return max(1, min(5, int(blend + 0.5 + 1e-9)))


def build_risk_profile(answers: Answers, bank: QuestionBank, config: RiskProfileConfig, *,
                       assessed_at: datetime, game: GameInput | None = None, game_weight: float | None = None,
                       may_raise: bool | None = None) -> RiskProfile:
    """Score a completed questionnaire. Same inputs always give the same RiskProfile.

    assessed_at: when the investor completed the questionnaire. The caller supplies it (there is no clock
    in here); it must be timezone-aware and is stored in UTC.

    game: the Risk Game's result, or None (not played: the profile is exactly what it was before the game existed).
    game_weight / may_raise: None = config.game (weight 1/3, may raise). weight 0 ignores the game everywhere.
    The category is the LOWEST of the capacity level and the blended tolerance level, where
    blended = (1 - weight) * questionnaire level + weight * game level, rounded to the nearest class; the gate still forces C1.
    may_raise False: the game can only lower the tolerance part (class = lowest of cap, questionnaire level and blend).
    """
    if assessed_at.tzinfo is None or assessed_at.utcoffset() is None:
        raise ValueError("assessed_at must be timezone-aware (e.g. datetime.now(timezone.utc))")
    assessed_at = assessed_at.astimezone(timezone.utc)
    if bank.questionnaire_version != config.questionnaire_version:
        raise ValueError("question bank and config are for different questionnaire versions")
    validate_answers(answers, bank)
    flags: list[str] = []

    t_score = tolerance_score(answers, bank)
    t_level = tolerance_band(t_score, config).level

    caps, points = capacity_caps(answers, config), capacity_points(answers, config)
    c_cap, binding_question, c_average = capacity_level(caps, points, config)
    loss_cap = loss_capacity(answers.amount_invested, answers.amount_needed)

    k_score = knowledge_score(answers, bank, config)
    access = product_access(k_score, has_experience(answers, config), config)
    if access == "warning":
        flags.append("complex_products_ack_required")
    elif access == "blocked_complex":
        flags.append("complex_products_blocked")

    weight = config.game.weight if game_weight is None else game_weight
    if not 0.0 <= weight <= 0.5:
        raise ValueError("game_weight must be between 0 and 0.5")
    raises = config.game.may_raise if may_raise is None else may_raise
    played = game is not None and weight > 0.0       # weight 0: the game is ignored everywhere
    g_score = g_level = blended = None
    tolerance_part = t_level
    if played:
        g_score = game_score(game)
        g_level = game_level(g_score)
        blended = blended_level(t_level, g_level, weight)
        tolerance_part = blended if raises else min(t_level, blended)

    gate = answers.choices[config.gate.question] == config.gate.no_loss_option
    questionnaire_level = 1 if gate else min(c_cap, t_level)
    if gate:
        level, binding = 1, "gate"
        flags.append("no_loss_gate")
    else:
        level = min(c_cap, tolerance_part)
        if played and level != questionnaire_level:
            binding = "game"                          # the game moved the category (lower, or higher where the cap allows)
        elif t_level <= c_cap:
            binding = "tolerance"
        else:
            binding = "capacity"

    if abs(consistency_gap(answers, bank, config)) >= config.tolerance.consistency.threshold:
        flags.append("review_suggested")

    c_level = f"C{level}"
    policy = config.c_levels[c_level]
    return RiskProfile(
        tolerance_score=t_score,
        tolerance_level=t_level,
        capacity_level=c_cap,
        capacity_binding_question=binding_question,
        capacity_average=c_average,
        loss_capacity=loss_cap,
        knowledge_score=k_score,
        product_access=access,
        no_loss_gate=gate,
        c_level=c_level,
        lowest_category=gate,
        binding_dimension=binding,
        max_r_level=policy.max_r_level,
        suitability_max_equity=policy.max_equity,
        flags=flags,
        questionnaire_version=bank.questionnaire_version,
        assessed_at=assessed_at,
        valid_until=add_months(assessed_at, config.compliance.validity_months),
        answers_hash=answers_hash(answers, game if played else None),
        game_played=played, game_score=g_score, game_level=g_level, blended_level=blended,
        questionnaire_class=f"C{questionnaire_level}" if played else None,
    )


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------
def audit_records(answers: Answers, bank: QuestionBank, timestamp: datetime) -> list[AnswerAuditRecord]:
    """One record per answered item, in question-bank order. The caller supplies the timestamp."""
    records: list[AnswerAuditRecord] = []
    version = answers.questionnaire_version

    def add(question_id: str, option_id: str | None, value: float | None = None) -> None:
        records.append(AnswerAuditRecord(question_id=question_id, questionnaire_version=version,
                                         option_id=option_id, value=value, timestamp=timestamp))

    for q in bank.questions:
        if q.type == "single" and q.id in answers.choices:
            add(q.id, answers.choices[q.id])
        elif q.type == "multi":
            for option_id in answers.multi_choices.get(q.id, []):
                add(q.id, option_id)
        elif q.id == B3_INVESTED_ID:
            add(q.id, None, answers.amount_invested)
        elif q.id == B3_NEEDED_ID:
            add(q.id, None, answers.amount_needed)
    return records
