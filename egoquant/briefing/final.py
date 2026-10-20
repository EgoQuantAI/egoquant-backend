"""One source of truth for Agent D's part of a report: the briefing JOB's final result.

/portfolio builds a quick briefing inside the request (from the rating cache, a few seconds), then the briefing
job rates every headline and the News tab shows its result. Without this module the report (HTML download,
report view, "What changed", the Advisor chat context and the saved account report) kept the quick, half-rated
version. Here the job's final sections replace that version, and nothing else changes:

  * AGENT_D_KEYS are the only report keys that are replaced (the briefings and "What changed" of both periods).
  * report_digest() fingerprints everything ELSE in the report (Agent A's and the Risk Engine's numbers). It is
    sealed into the briefing context at /portfolio time, so a browser copy can be refreshed only if its numbers
    are exactly the ones /portfolio produced (the briefing job carries the digest; a mismatch is refused).
  * the Advisor's sealed chat context is reopened (it is ours, signed) and only its Agent D facts are replaced.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

AGENT_D_KEYS = ("market_briefing", "market_briefing_month", "what_changed", "what_changed_action",
                "what_changed_month", "what_changed_month_action")
_NOT_DIGESTED = (*AGENT_D_KEYS, "banners")      # banners include the briefing's own (e.g. the DEMO notice)


class FinalBriefingError(ValueError):
    """User-safe message; `status` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _normalize(value: Any) -> Any:
    """Numbers as floats, so a browser round trip (1.0 -> 1) gives the same digest; everything else as is."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return str(value)


def report_digest(report: dict) -> str:
    """SHA-256 of the report WITHOUT its Agent D sections (and banners): the numbers this module never touches."""
    rest = {k: v for k, v in report.items() if k not in _NOT_DIGESTED}
    text = json.dumps(_normalize(rest), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def final_sections(job_result: dict) -> dict:
    """The job's final Agent D sections, in the report's own keys."""
    return {"market_briefing": job_result.get("week"), "market_briefing_month": job_result.get("month"),
            "what_changed": list(job_result.get("what_changed") or []),
            "what_changed_action": job_result.get("what_changed_action"),
            "what_changed_month": list(job_result.get("what_changed_month") or []),
            "what_changed_month_action": job_result.get("what_changed_month_action")}


def apply_final(report: dict, sections: dict) -> dict:
    """A copy of the report with the Agent D sections replaced (every other key unchanged)."""
    out = copy.deepcopy(report)
    for key in AGENT_D_KEYS:
        out[key] = copy.deepcopy(sections.get(key))
    return out


def render_report_html(report: dict) -> str:
    """The downloadable HTML page for a (refreshed) report JSON, rendered exactly as /portfolio renders it."""
    from egoquant.report import ClientReport, render_html
    fields = set(ClientReport.model_fields)
    return render_html(ClientReport.model_validate({k: v for k, v in report.items() if k in fields}))


def refresh_chat_context(token: str | None, report: dict) -> str | None:
    """The Advisor's sealed context with its Agent D facts replaced by the refreshed report's. None when there is
    no context (or it no longer opens: the chat then falls back to its own message, as before)."""
    if not token:
        return None
    from egoquant.advisor import context as ctx
    try:
        facts = ctx.open_context(token)
    except Exception:
        return None
    facts = {k: v for k, v in facts.items() if k not in ("v", "exp", "iat", *AGENT_D_KEYS)}
    if report.get("what_changed"):
        facts["what_changed"] = [ctx._clip(x) for x in report["what_changed"][:10]]
        facts["what_changed_action"] = report.get("what_changed_action")
    if report.get("market_briefing"):
        facts["market_briefing"] = ctx._briefing_facts(report["market_briefing"])
    if report.get("market_briefing_month"):
        facts["market_briefing_month"] = ctx._briefing_facts(report["market_briefing_month"], with_items=False)
        facts["what_changed_month"] = [ctx._clip(x) for x in (report.get("what_changed_month") or [])[:10]]
    return ctx.seal_context(facts)


def news_is_final(report: dict | None) -> bool:
    """True when the report's news section is the briefing job's finished version, or the report has no news section
    at all (news switched off, or dropped for size). The quick in-request version is not final: every way of getting a
    report (portfolio page, account build, open, download) finalizes it first, and only a final one may be sealed."""
    briefing = (report or {}).get("market_briefing")
    if not briefing:
        return True
    return bool(((briefing.get("metadata") if isinstance(briefing, dict) else None) or {}).get("final_job"))


def check_job(job, report: dict | None = None) -> dict:
    """The job's final result, after the checks: it exists, it finished, and (with a report) it belongs to it."""
    if job is None:
        raise FinalBriefingError(404, "Unknown or expired news briefing. Please open the News tab again.")
    if job.state == "running":
        raise FinalBriefingError(409, f"The news is still being rated ({job.rated} of {job.total}).")
    if job.state != "done" or not job.result or job.result.get("week") is None:
        raise FinalBriefingError(409, "The news briefing could not be finished; the report keeps its first version.")
    if report is not None:
        if not job.report_digest or job.report_digest != report_digest(report):
            raise FinalBriefingError(409, "This report does not belong to this news briefing.")
    return job.result


__all__ = ["AGENT_D_KEYS", "FinalBriefingError", "apply_final", "check_job", "final_sections", "news_is_final",
           "refresh_chat_context", "render_report_html", "report_digest"]
