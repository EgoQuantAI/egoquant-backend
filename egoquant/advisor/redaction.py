"""Redaction gateway: personal identifiers are replaced by placeholders BEFORE any text reaches OpenAI, and put
back AFTER the reply comes in. Every OpenAI call in chat.py goes through `Redactor.redact_params`.

    IBKR account ids (U1234567, DU1234567, F1234567)  -> [ACCOUNT_1]
    email addresses                                   -> [EMAIL_1]
    phone numbers (international +.., 555-123-4567,
    (555) 123-4567, 0532 123 45 67)                   -> [PHONE_1]
    the signed-in user's own username                 -> [NAME_1]

The same value always gets the same placeholder within one request. The mapping lives only in this object (one
per request, server side) and is never logged; only the COUNTS per kind (distinct values hidden, however many OpenAI
calls the request made) are logged and returned (privacy panel).
Amounts are not replaced: they are engine facts the advisor must quote, and the number-grounding check
(grounding.py) verifies every one of them against the engine instead.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from typing import Any

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("ACCOUNT", re.compile(r"\b(?:DU|U|F)\d{5,9}\b")),
    ("PHONE", re.compile(
        r"(?<![\w+])\+\d{1,3}(?:[\s.-]?\d){7,13}(?!\d)"                     # +90 532 123 45 67, +1 555 123 4567
        r"|(?<!\w)\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"                  # 555-123-4567, (555) 123-4567
        r"|(?<!\w)0\d{3}[\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}(?!\d)")),      # 0532 123 45 67
]
_PLACEHOLDER = re.compile(r"\[(ACCOUNT|EMAIL|PHONE|NAME)_(\d+)\]")


class Redactor:
    def __init__(self, names: list[str] | None = None):
        self._names = [n for n in (names or []) if n and len(n.strip()) >= 3]
        self._to_placeholder: dict[tuple[str, str], str] = {}
        self._to_value: dict[str, str] = {}

    @property
    def counts(self) -> Counter:
        """Distinct values hidden, per kind (a name in different capitalisation counts once)."""
        return Counter(kind for kind, _ in {(k, v.lower() if k == "NAME" else v) for k, v in self._to_placeholder})

    def name_placeholder(self, name: str) -> str:
        """The token the model writes where it would use this person's first name; `restore` turns it back. The model never
        sees the name itself (the placeholder is the only thing that goes out)."""
        return self._placeholder("NAME", name)

    def _placeholder(self, kind: str, value: str) -> str:
        key = (kind, value)                       # exact spelling: restore gives back exactly what was written
        if key not in self._to_placeholder:
            n = sum(1 for k in self._to_placeholder if k[0] == kind) + 1
            ph = f"[{kind}_{n}]"
            self._to_placeholder[key] = ph
            self._to_value[ph] = value
        return self._to_placeholder[key]

    def redact(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return text
        for kind, rx in _PATTERNS:
            text = rx.sub(lambda m, k=kind: self._placeholder(k, m.group(0)), text)
        for name in self._names:
            text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", lambda m: self._placeholder("NAME", m.group(0)),
                          text, flags=re.IGNORECASE)
        return text

    def restore(self, text: str) -> str:
        if not isinstance(text, str) or not self._to_value:
            return text
        return _PLACEHOLDER.sub(lambda m: self._to_value.get(m.group(0), m.group(0)), text)

    def _walk(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, list):
            return [self._walk(x) for x in obj]
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                # ids, reasoning blobs and tool-call metadata are not user text
                out[k] = v if k in ("id", "call_id", "encrypted_content", "type", "role", "name", "status") else self._walk(v)
            return out
        return obj

    def redact_params(self, params: dict) -> dict:
        """A copy of an OpenAI Responses request with every text field redacted (instructions and input)."""
        out = copy.copy(params)
        if "instructions" in out:
            out["instructions"] = self.redact(out["instructions"])
        if "input" in out:
            out["input"] = self._walk(copy.deepcopy(out["input"]))
        return out

    def summary(self) -> dict[str, int]:
        """Counts per kind (never the values)."""
        return {k.lower(): int(v) for k, v in sorted(self.counts.items())}


__all__ = ["Redactor"]
