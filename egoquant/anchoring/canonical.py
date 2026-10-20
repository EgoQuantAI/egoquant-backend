"""Canonical JSON: the same bytes from Python and from the browser (src/lib/proof.ts), for the same value.

Rules (docs/ON_CHAIN_PLAN.md 3.1, docs/ANCHORING.md):
- compact (",", ":"), UTF-8, no trailing newline, NaN / Infinity refused;
- object keys sorted by UTF-16 code unit (what JavaScript's default sort does; for ASCII keys it is plain order);
- integers only up to 2**53 - 1 (what JavaScript can hold exactly).
Two modes:
- "record": the advisory record. No floats at all and ASCII keys only; the record builder converts weights to
  ppm and money to cents first, so a float here is a bug and raises.
- "report": the saved report, which holds floats. A number with an integral value (1.0, 3) is written as an
  integer; any other number as a string with exactly six decimals (exact value of the double, ties to even).
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any, Literal

MAX_SAFE_INT = 2**53 - 1
Mode = Literal["record", "report"]


class CanonicalError(ValueError):
    """The value cannot be written canonically (a float in a record, NaN, a non-ASCII record key, ...)."""


def _key_order(key: str) -> bytes:
    return key.encode("utf-16-be")


def fixed6(x: float) -> str:
    """Six decimals of the double's exact value, ties to even (proof.ts does the same with BigInt)."""
    with localcontext() as ctx:
        ctx.prec = 400                           # enough digits for any double's exact value
        return format(Decimal(x).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN), "f")


def _norm(value: Any, mode: Mode, path: str) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            raise CanonicalError(f"integer too large at {path or '/'}")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalError(f"NaN or Infinity at {path or '/'}")
        if mode == "record":
            raise CanonicalError(f"float at {path or '/'}: use ppm, cents or a 6-place string")
        if value.is_integer() and abs(value) <= MAX_SAFE_INT:
            return int(value)
        return fixed6(value)
    if isinstance(value, dict):
        out = {}
        for key in sorted(value, key=lambda k: _key_order(k) if isinstance(k, str) else b""):
            if not isinstance(key, str):
                raise CanonicalError(f"non-string key at {path or '/'}")
            if mode == "record" and not key.isascii():
                raise CanonicalError(f"non-ASCII key at {path or '/'}")
            out[key] = _norm(value[key], mode, f"{path}/{key}")
        return out
    if isinstance(value, (list, tuple)):
        return [_norm(v, mode, f"{path}/{i}") for i, v in enumerate(value)]
    raise CanonicalError(f"unsupported type {type(value).__name__} at {path or '/'}")


def canonical_text(value: Any, mode: Mode = "record") -> str:
    return json.dumps(_norm(value, mode, ""), separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_bytes(value: Any, mode: Mode = "record") -> bytes:
    return canonical_text(value, mode).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def report_sha256(saved_report: dict) -> str:
    """The fingerprint of the whole saved report (versions.report_sha256 in the record)."""
    return sha256_hex(canonical_bytes(saved_report, "report"))
