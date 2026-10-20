"""Number grounding: every quantitative claim in an advisor reply must come from the engine (or the user).

    claims(text)                      -> the reply's quantitative claims: percentages, "X in 100" / "X out of 100",
                                         money amounts ($1,234 / 1,234 USD / $283k / 2.5 million dollars) and
                                         multipliers (2x, 3 times)
    allowed_numbers(*sources)         -> Allowed: every number in the engine facts, this turn's tool results and
                                         the user's own messages (numbers inside strings too), plus the CHANCE-type
                                         subset (probability / chance / frequency fields, "X in 100" phrases)
    ungrounded(text, allowed)         -> the claims no allowed number supports (rounding tolerated)

Tolerance (rounding, never invention): a percentage may round an allowed share or percentage to a whole number or
one decimal (|x - v| <= 0.55 points; a share 0.6578 supports 65.8% and 66%); "X in 100" is a statement about a
CHANCE, so only chance-type values support it (within 1 of a frequency, or of 100 x a probability): a portfolio
weight of 0.61 never supports "61 in 100"; a money amount within 0.6% or 1 unit (the report rounds to three
significant figures); a multiplier within 0.05. Bare counts and years ("20 years", "10,000 futures") are not claims.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_NUM = r"\d[\d,]*(?:\.\d+)?"
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
_PERCENT = re.compile(rf"(?<![\w.])(-?{_NUM})\s?(?:%|percent\b|per cent\b)", re.I)
_IN100 = re.compile(r"(?<![\w.])(\d{1,3})\s+(?:in|out of|of)\s+(?:every\s+)?100(?!\d)", re.I)
_MONEY_PRE = re.compile(rf"(?:US\$|\$|USD\s?|EUR\s?|€|£|¥|CNY\s?|RMB\s?)\s?({_NUM})\s?(k|thousand|m|mn|million|bn|b|billion)?\b",
                        re.I)
_MONEY_POST = re.compile(rf"(?<![\w.$])({_NUM})\s?(k|thousand|m|mn|million|bn|billion)?\s?(?:USD|US dollars|dollars|EUR|"
                         rf"euros|GBP|CNY|RMB|yuan)\b", re.I)
_MULT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s?(?:x|×|times)\b", re.I)
_ANY_NUMBER = re.compile(rf"-?{_NUM}")


@dataclass(frozen=True)
class Claim:
    kind: str           # percent | in100 | money | multiplier
    value: float
    text: str


def _f(s: str) -> float:
    return float(s.replace(",", ""))


def claims(text: str) -> list[Claim]:
    out: list[Claim] = []
    spans: list[tuple[int, int]] = []

    def free(m) -> bool:
        return not any(a < m.end() and m.start() < b for a, b in spans)

    for kind, rx in (("in100", _IN100), ("percent", _PERCENT), ("money", _MONEY_PRE), ("money", _MONEY_POST),
                     ("multiplier", _MULT)):
        for m in rx.finditer(text or ""):
            if not free(m):
                continue
            try:
                value = _f(m.group(1))
            except ValueError:
                continue
            if kind == "money" and m.lastindex and m.lastindex >= 2 and m.group(2):
                value *= _SCALE[m.group(2).lower()]
            spans.append((m.start(), m.end()))
            out.append(Claim(kind, value, m.group(0)))
    return out


_CHANCE_KEY = re.compile(r"prob|chance|frequency|in_100|odds", re.I)


@dataclass
class Allowed:
    any: set[float]           # every number from the sources
    chance: set[float]        # probabilities and frequencies only (what may support "X in 100")


def _numbers(obj: Any, out: Allowed, chance: bool) -> None:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        out.any.add(float(obj))
        if chance:
            out.chance.add(float(obj))
    elif isinstance(obj, str):
        for c in claims(obj):                       # "283k", "68 in 100", "2.5 million" inside strings
            out.any.add(c.value)
            if c.kind == "in100" or (chance and c.kind == "percent"):
                out.chance.add(c.value)
        for m in _ANY_NUMBER.finditer(obj):
            try:
                out.any.add(_f(m.group(0)))
            except ValueError:
                pass
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _numbers(v, out, chance or bool(_CHANCE_KEY.search(str(k))))
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            _numbers(v, out, chance)


def allowed_numbers(*sources: Any, user_text: Any = None) -> Allowed:
    """Engine facts and tool results as `sources`; the user's own messages as `user_text` (every number the user
    wrote is allowed in any role, per the grounding rule)."""
    out = Allowed(set(), set())
    for s in sources:
        _numbers(s, out, False)
    if user_text is not None:
        _numbers(user_text, out, True)
    return out


def _supported(c: Claim, allowed: Allowed) -> bool:
    for v in (allowed.chance if c.kind == "in100" else allowed.any):
        if c.kind == "percent":
            if abs(c.value - v) <= 0.55 or (abs(v) <= 1.5 and abs(c.value - 100 * v) <= 0.55):
                return True
        elif c.kind == "in100":
            if abs(c.value - v) <= 1.0 or (0 <= v <= 1.0 and abs(c.value - 100 * v) <= 1.0):
                return True
        elif c.kind == "money":
            if abs(c.value - v) <= max(1.0, 0.006 * abs(v)):
                return True
        elif c.kind == "multiplier":
            if abs(c.value - v) <= 0.05:
                return True
    return False


def ungrounded(text: str, allowed: Allowed) -> list[Claim]:
    return [c for c in claims(text) if not _supported(c, allowed)]


CORRECTION = ("Your previous reply contained numbers that are not in the ENGINE FACTS, the run_what_if results or "
              "the investor's own messages: {items}. Rewrite the reply using ONLY numbers that appear there, quoted as "
              "given (rounding is fine). Do not estimate, extrapolate or compute new numbers. If a number is not "
              "available, say the engine has not computed it.")
FALLBACK = ("I can only quote numbers that EgoQuant has calculated for you, and I could not phrase this answer "
            "safely. Please ask about a specific figure, or open the Portfolio page to see it.")

__all__ = ["Allowed", "CORRECTION", "Claim", "FALLBACK", "allowed_numbers", "claims", "ungrounded"]
