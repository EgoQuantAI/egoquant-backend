"""Rule-based advice / forecast filter. Runs on every LLM summary and again on the final rendered lines.

Patterns come from config (case-insensitive regular expressions with word boundaries). A hit means the
text is never shown: an LLM summary falls back to the template; a template whose headline itself
contains advice wording is replaced by a neutral "withheld" line pointing to the original link.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


class AdviceFilter:
    def __init__(self, patterns: list[str]):
        self.patterns = [(p, re.compile(p, re.IGNORECASE)) for p in patterns]

    def hits(self, text: str) -> list[str]:
        """The patterns that match, in config order (empty = clean)."""
        return [p for p, rx in self.patterns if rx.search(text or "")]

    def is_clean(self, text: str) -> bool:
        return not self.hits(text)

    def safe_line(self, text: str, replacement: str) -> str:
        """Last safety net for rendered text: return the line, or the replacement if it matches."""
        found = self.hits(text)
        if found:
            logger.warning("advice filter replaced a rendered line (patterns: %s)", found)
            return replacement
        return text


def withheld_line(source_name: str) -> str:
    return f"{source_name} published an item whose wording reads like investment advice; it is not repeated here (see the link)."


SAFE_REPLACEMENT = "This line was removed because it read like investment advice."
