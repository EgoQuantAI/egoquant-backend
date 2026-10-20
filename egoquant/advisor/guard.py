"""Compliance guard for chat replies: withholds sentences that read like trade instructions or forecasts.

Works on whole sentences so it can run while a reply streams: `feed` releases only complete sentences,
already checked, and keeps the unfinished tail until more text (or `finish`) arrives. A hit is logged by
pattern only, never with the text.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).with_name("config.yaml")

# A sentence ends at . ! ? followed by whitespace (so "14.2%" is not split), or at a line break.
_SENTENCE = re.compile(r".*?(?:[.!?]+(?=\s)|[\u3002\uff01\uff1f]+|\n|$)\s*", re.S)    # 。！？ end a Chinese sentence with no space after
_BOUNDARY = re.compile(r"[.!?]+\s|[\u3002\uff01\uff1f]+|\n")


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class ReplyGuard:
    def __init__(self, patterns: list[str], withheld_note: str):
        self.patterns = [(p, re.compile(p, re.IGNORECASE)) for p in patterns]
        self.withheld_note = withheld_note
        self.hits: list[str] = []
        self._buffer = ""

    @classmethod
    def from_config(cls, path: Path = CONFIG_PATH) -> "ReplyGuard":
        cfg = load_config(path)
        return cls(cfg["forbidden_patterns"], cfg["withheld_note"])

    def _check(self, text: str) -> str:
        out = []
        for sentence in (m.group(0) for m in _SENTENCE.finditer(text)):
            if not sentence:
                continue
            found = [p for p, rx in self.patterns if rx.search(sentence)]
            if found:
                self.hits.extend(found)
                logger.warning("advisor guard withheld a sentence (patterns: %s)", found)
                # keep the line structure so the rest of a list still renders
                out.append("\n" if sentence.endswith("\n") else "")
            else:
                out.append(sentence)
        return "".join(out)

    def feed(self, delta: str) -> str:
        """Add streamed text; return the part that is complete and checked (may be empty)."""
        self._buffer += delta
        last = None
        for last in _BOUNDARY.finditer(self._buffer):
            pass
        if last is None:
            return ""
        ready, self._buffer = self._buffer[: last.end()], self._buffer[last.end():]
        return self._check(ready)

    def finish(self) -> str:
        """Release the checked tail, plus the withheld note if anything was removed."""
        tail, self._buffer = self._check(self._buffer), ""
        if self.hits:
            tail = tail.rstrip() + "\n\n_" + self.withheld_note + "_"
        return tail

    def clean(self, text: str) -> str:
        """Whole reply at once (non-streaming path)."""
        return (self.feed(text) + self.finish()).strip()
