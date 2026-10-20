"""HTTP for the What-If Agent and the privacy panel. Both need a signed-in user (Supabase token).

    POST /what-if                    run one scenario directly (testing and the demo); the SAME validator and agent
                                     as the chat's run_what_if tool. The sealed what_if_context must belong to the
                                     signed-in user (it comes from /account/portfolio); otherwise 403.
    GET  /privacy-log/{session_id}   how many sensitive values were hidden from the AI in one chat session (counts
                                     per kind, never the values); only the user's own rows.

Every run is stored in the user's own `what_if_runs` table (RLS, 180-day retention; supabase/migrations/002).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

from egoquant.accounts.routes import _call, _own, _signed_in
from egoquant.accounts.store import get_store
from egoquant.agents import what_if as wi

router = APIRouter()
logger = logging.getLogger(__name__)


class WhatIfIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    what_if_context: str = Field(min_length=1, max_length=120_000)   # from /account/portfolio
    scenario: dict = Field(max_length=10)                            # WhatIfScenario fields; validated by the agent


def store_run(token: str, user_id: str, result: wi.WhatIfResult, context: str) -> None:
    """Audit row (scenario, summary, seed, path count, engine version, status). Never fails the request."""
    try:
        get_store().insert(token, "what_if_runs", [{
            "user_id": user_id, "report_id": result.base_report_id, "context_hash": wi.context_hash(context),
            "scenario": result.scenario or {}, "summary": wi.audit_summary(result), "status": result.status,
            "seed": result.seed, "n_paths": result.n_paths, "engine_version": result.engine_version}])
    except Exception:
        logger.warning("what-if audit row not stored")


@router.post("/what-if", response_model=wi.WhatIfResult)
def what_if(body: WhatIfIn, authorization: str | None = Header(None)):
    token, user = _signed_in(authorization)
    try:
        base = wi.open_what_if_context(body.what_if_context)
    except wi.WhatIfInvalid as err:
        raise HTTPException(422, err.reason) from None
    if base.get("user_id") != user.id:
        raise HTTPException(403, "This portfolio belongs to another account (or to no account). Build it from your "
                                 "account page first.")
    result = wi.run_what_if(body.what_if_context, body.scenario, user_id=user.id, account_token=token)
    store_run(token, user.id, result, body.what_if_context)
    return result


@router.get("/privacy-log/{session_id}")
def privacy_log(session_id: str = Path(pattern=r"^[A-Za-z0-9-]{8,64}$"), authorization: str | None = Header(None)):
    token, user = _signed_in(authorization)
    rows = _call(get_store().select, token, "privacy_log",
                 _own(user, session_id=f"eq.{session_id}", select="counts"))
    totals: dict[str, int] = {}
    for row in rows:
        for kind, n in (row.get("counts") or {}).items():
            if isinstance(n, int) and n >= 0:
                totals[str(kind)] = totals.get(str(kind), 0) + n
    return {"session_id": session_id, "hidden": sum(totals.values()), "by_kind": totals}
