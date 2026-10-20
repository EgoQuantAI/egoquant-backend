"""A sealed report no longer changes. The wording the reader sees when something tries to change one anyway.

The only thing that writes into a saved report after it was built is the final news briefing
(POST /account/reports/{id}/briefing). Once the investor has confirmed the plan and it was sealed, that save must
answer 409 with a plain sentence, not a generic error. The route calls `require_unsealed(rows)` with whatever it
read for "is there a seal record for this report" (the advisory_records row of ON_CHAIN_PLAN.md, section 5):

    rows = _call(get_store().select, token, "advisory_records", _own(user, report_id=f"eq.{report_id}", select="id", limit="1"))
    require_unsealed(rows)

If that lookup itself fails (the seal tables are not migrated yet), treat the report as not sealed: the save works as
before. Tests: tests/accounts/test_sealed_briefing.py.
"""

from __future__ import annotations

from fastapi import HTTPException

REPORT_SEALED_MESSAGE = ("This plan is already confirmed and sealed, so its saved report can no longer change. "
                         "The sealed version stays exactly as you confirmed it.")


def require_unsealed(seal_rows) -> None:
    """409 with the plain sentence when the report has a seal record; nothing otherwise."""
    if seal_rows:
        raise HTTPException(409, REPORT_SEALED_MESSAGE)
