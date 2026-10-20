"""IBKR Flex Web Service client: token + Flex Query ID -> the raw Flex report text (XML or CSV).

Flow (IBKR's public Flex Web Service docs, version 3):
    1. SendRequest?t=<token>&q=<query id>&v=3   -> <FlexStatementResponse> with a ReferenceCode
    2. GetStatement?t=<token>&q=<reference>&v=3 -> the report, or a Fail envelope (1019 = still generating)

IBKR allows 1 request/s and 10 requests/min per token, so the client waits >= 1.1 s between calls, makes at most
10 calls per fetch and gives up after about 30 s in total. The report itself is parsed elsewhere (ibkr.py).

Security: the token is used for this one fetch only. It is never logged, stored, or put into an error message,
an exception or its chained cause (requests exceptions quote the URL, which holds the token, so they are
never chained: the FlexError is raised outside the except block). HTTPS only; the GetStatement URL IBKR returns is used only if it is an
https://*.interactivebrokers.com address. Response envelopes are read with regular expressions, never with an
XML parser, so no entity expansion is possible.
"""

from __future__ import annotations

import re
import time
from collections import deque
from typing import Callable
from urllib.parse import urlsplit

import requests

BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
API_VERSION = "3"
USER_AGENT = "EgoQuant/1.0 (Python requests)"   # IBKR asks programmatic clients to set a User-Agent

MIN_INTERVAL_S = 1.1            # IBKR: 1 request per second per token (small margin)
MAX_CALLS = 10                  # IBKR: 10 requests per minute per token; a fetch never needs more
MAX_REPORT_BYTES = 5_000_000    # a trades report is far smaller; refuse anything larger
POLL_WAITS_S = (2.0, 3.0, 5.0)  # wait before each GetStatement poll (last value repeats)

# IBKR error codes that mean "not ready yet, try again shortly": keep polling until the time cap.
RETRY_CODES = frozenset({1001, 1004, 1005, 1006, 1007, 1008, 1009, 1019, 1021})

# code -> (short safe label, user-safe message). Messages never quote IBKR's text or the token.
_ERRORS: dict[int, tuple[str, str]] = {
    1003: ("statement_not_available", "IBKR says this statement is not available. Check the Flex Query's period."),
    1010: ("legacy_query", "This is a legacy Flex Query. Create an Activity Flex Query in IBKR and use its ID."),
    1011: ("service_inactive", "The Flex Web Service is not active for this account. Enable it in IBKR."),
    1012: ("token_expired", "Your Flex token has expired. Generate a new token in IBKR and try again."),
    1013: ("ip_not_allowed", "Your Flex token is restricted to other IP addresses. Remove or update the "
                             "IP restriction in IBKR, or upload the report file instead."),
    1014: ("query_invalid", "IBKR does not recognise this Flex Query ID. Check the ID and try again."),
    1015: ("token_invalid", "IBKR does not recognise this Flex token. Check the token and try again."),
    1016: ("account_invalid", "IBKR reports a problem with the account behind this token."),
    1017: ("reference_invalid", "IBKR lost track of the statement request. Please try again."),
    1018: ("rate_limited", "Too many requests to IBKR with this token. Wait a minute and try again."),
    1020: ("invalid_request", "IBKR could not validate the request. Check the token and Flex Query ID."),
}
_STILL_GENERATING = ("still_generating", "IBKR is still generating your statement. Try again in a minute.")


class FlexError(Exception):
    """A user-safe Flex Web Service failure. `str(err)` can be shown to the user as is.

    kind: short safe label (token_expired, ip_not_allowed, rate_limited, still_generating, ...).
    code: IBKR's numeric error code when IBKR returned one, else None. Never holds the token.
    """

    def __init__(self, kind: str, message: str, code: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.code = code


def _error_for(code: int | None) -> FlexError:
    if code in RETRY_CODES:
        return FlexError(*_STILL_GENERATING, code=code)
    kind, message = _ERRORS.get(code, ("ibkr_error", "IBKR could not provide the statement. Please try again later."))
    return FlexError(kind, message, code=code)


def _tag(text: str, name: str) -> str | None:
    m = re.search(rf"<{name}>\s*([^<]*?)\s*</{name}>", text, re.IGNORECASE)
    return m.group(1) if m else None


def _envelope(text: str) -> dict | None:
    """The fields of a <FlexStatementResponse> status envelope, or None if `text` is not one (i.e. a report)."""
    head = re.sub(r"^\s*(<\?xml[^>]*\?>\s*)?", "", text[:2000])
    if not head.startswith("<FlexStatementResponse"):
        return None
    code = _tag(text, "ErrorCode")
    return {"status": (_tag(text, "Status") or "").lower(), "code": int(code) if code and code.isdigit() else None,
            "reference": _tag(text, "ReferenceCode"), "url": _tag(text, "Url")}


def _safe_statement_url(url: str | None) -> str:
    """GetStatement URL: IBKR's own if it is https on an interactivebrokers.com host, else the documented one."""
    default = f"{BASE_URL}/GetStatement"
    if not url:
        return default
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and (host == "interactivebrokers.com" or host.endswith(".interactivebrokers.com")):
        return f"https://{parts.netloc}{parts.path}"
    return default


class _Throttle:
    """At most one call per MIN_INTERVAL_S, MAX_CALLS per fetch, nothing past the deadline."""

    def __init__(self, deadline: float, clock: Callable[[], float], sleep: Callable[[float], None]):
        self.deadline, self.clock, self.sleep = deadline, clock, sleep
        self.calls: deque[float] = deque()

    def remaining(self) -> float:
        return self.deadline - self.clock()

    def wait(self, at_least: float = 0.0) -> bool:
        """Sleep until the next call is allowed; False if that would pass the deadline or the call cap."""
        if len(self.calls) >= MAX_CALLS:
            return False
        now = self.clock()
        start = now + at_least
        if self.calls:
            start = max(start, self.calls[-1] + MIN_INTERVAL_S)
        if start >= self.deadline - 1.0:     # leave at least 1 s for the request itself
            return False
        if start > now:
            self.sleep(start - now)
        self.calls.append(self.clock())
        return True


def _call(session: requests.Session, url: str, token: str, q: str, throttle: _Throttle) -> str:
    timeout = max(1.0, min(10.0, throttle.remaining()))
    failure = None
    try:
        resp = session.get(url, params={"t": token, "q": q, "v": API_VERSION},
                           headers={"User-Agent": USER_AGENT}, timeout=timeout, allow_redirects=False)
    except requests.Timeout:
        failure = FlexError("ibkr_timeout", "IBKR did not answer in time. Please try again later.")
    except requests.RequestException:
        failure = FlexError("ibkr_unreachable", "Could not reach IBKR. Check your connection and try again.")
    if failure is not None:     # raised outside the except block: no __context__ holding the URL with the token
        raise failure
    if resp.status_code != 200:
        raise FlexError("ibkr_unavailable", f"IBKR's Flex service is unavailable right now (HTTP {resp.status_code}). "
                                            "Please try again later.")
    if len(resp.content) > MAX_REPORT_BYTES:
        raise FlexError("report_too_large", "The IBKR report is too large. Narrow the Flex Query's period.")
    return resp.text


def fetch_flex_report(token: str, query_id: str, *, timeout_s: float = 30,
                      session: requests.Session | None = None,
                      clock: Callable[[], float] = time.monotonic,
                      sleep: Callable[[float], None] = time.sleep) -> str:
    """Fetch an IBKR Flex Query report with a Flex Web Service token. Returns the raw report text.

    Raises FlexError (user-safe message, short `kind`, IBKR `code` if any) on every failure; the token never
    appears in it. `session`, `clock` and `sleep` are for tests.
    """
    token = (token or "").strip()
    query_id = (query_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{8,64}", token):
        raise FlexError("token_format", "The Flex token should be the string of digits IBKR shows under "
                                        "Flex Web Service (letters and digits only).")
    if not re.fullmatch(r"\d{1,20}", query_id):
        raise FlexError("query_id_format", "The Flex Query ID should be a number (shown next to the query in IBKR).")

    session = session or requests.Session()
    throttle = _Throttle(clock() + timeout_s, clock, sleep)

    throttle.wait()
    sent = _envelope(_call(session, f"{BASE_URL}/SendRequest", token, query_id, throttle))
    if sent is None:
        raise FlexError("ibkr_error", "IBKR sent an unexpected answer. Please try again later.")
    if sent["status"] != "success" or not sent["reference"]:
        raise _error_for(sent["code"])
    reference = sent["reference"]
    if not re.fullmatch(r"\d{1,30}", reference):
        raise FlexError("ibkr_error", "IBKR sent an unexpected answer. Please try again later.")
    statement_url = _safe_statement_url(sent["url"])

    last: FlexError = FlexError(*_STILL_GENERATING)
    for attempt in range(MAX_CALLS):
        if not throttle.wait(POLL_WAITS_S[min(attempt, len(POLL_WAITS_S) - 1)]):
            break
        text = _call(session, statement_url, token, reference, throttle)
        status = _envelope(text)
        if status is None:
            return text                                   # the report itself (XML <FlexQueryResponse> or CSV)
        if status["status"] == "success":                 # an envelope without a report: not ready yet
            continue
        last = _error_for(status["code"])
        if status["code"] not in RETRY_CODES:
            raise last
    raise last
