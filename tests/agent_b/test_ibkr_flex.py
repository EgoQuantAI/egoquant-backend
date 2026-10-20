"""IBKR Flex Web Service client: network mocked, fake clock. Envelopes follow IBKR's documented v3 format."""

from __future__ import annotations

import pytest
import requests

from egoquant.agents.behavioral import ibkr_flex
from egoquant.agents.behavioral.ibkr_flex import BASE_URL, FlexError, fetch_flex_report

TOKEN = "123456789012345678901234"          # invented
QUERY = "987654"
REF = "1234567890"
REPORT = '<FlexQueryResponse queryName="trades" type="AF"><FlexStatements count="1"/></FlexQueryResponse>'


def ok_send(url=f"{BASE_URL}/GetStatement"):
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<FlexStatementResponse timestamp="18 October, 2026 08:00 PM EDT">'
            f"<Status>Success</Status><ReferenceCode>{REF}</ReferenceCode><Url>{url}</Url></FlexStatementResponse>")


def fail(code, msg="IBKR text"):
    return (f'<FlexStatementResponse timestamp="x"><Status>Fail</Status><ErrorCode>{code}</ErrorCode>'
            f"<ErrorMessage>{msg}</ErrorMessage></FlexStatementResponse>")


class Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code, self.content = text, status, text.encode()


class FakeSession:
    """Answers in order; records every call with the fake time it was made."""

    def __init__(self, answers, clock):
        self.answers, self.clock, self.calls = list(answers), clock, []

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append({"url": url, "params": params, "headers": headers, "t": clock_now(self.clock),
                           "redirects": allow_redirects})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, Resp) else Resp(answer)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        assert s >= 0
        self.now += s


def clock_now(clock):
    return clock.now


def run(answers, **kw):
    clock = Clock()
    session = FakeSession(answers, clock)
    try:
        return fetch_flex_report(kw.pop("token", TOKEN), kw.pop("query_id", QUERY), session=session,
                                 clock=clock, sleep=clock.sleep, **kw), session
    except FlexError as err:
        err.session = session
        raise


def assert_no_token(err: BaseException):
    text = " ".join([str(err), repr(err), str(err.__cause__ or ""), str(err.__context__ or "")])
    assert TOKEN not in text
    assert err.__cause__ is None and err.__context__ is None


def test_send_request_then_get_statement_returns_the_report():
    report, s = run([ok_send(), REPORT])
    assert report == REPORT
    assert [c["url"] for c in s.calls] == [f"{BASE_URL}/SendRequest", f"{BASE_URL}/GetStatement"]
    assert s.calls[0]["params"] == {"t": TOKEN, "q": QUERY, "v": "3"}
    assert s.calls[1]["params"] == {"t": TOKEN, "q": REF, "v": "3"}
    assert all(c["url"].startswith("https://") for c in s.calls)
    assert all(c["headers"]["User-Agent"] for c in s.calls)          # IBKR asks for a User-Agent
    assert not any(c["redirects"] for c in s.calls)


def test_csv_report_is_returned_as_is():
    csv = '"ClientAccountID","Symbol","Quantity"\n"U0000000","VTI","10"\n'
    assert run([ok_send(), csv])[0] == csv


def test_polls_politely_while_the_statement_is_being_generated():
    report, s = run([ok_send(), fail(1019), fail(1019), REPORT])
    assert report == REPORT
    times = [c["t"] for c in s.calls]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps) >= 1.0                                          # IBKR: 1 request per second
    assert len(s.calls) <= 10                                        # IBKR: 10 per minute
    assert times[-1] - times[0] <= 30


def test_gives_up_with_still_generating_inside_the_time_cap():
    with pytest.raises(FlexError) as e:
        run([ok_send()] + [fail(1019)] * 20)
    err = e.value
    assert err.kind == "still_generating" and err.code == 1019
    calls = err.session.calls
    assert len(calls) <= 10
    assert calls[-1]["t"] - 1000.0 <= 30
    assert "try again" in str(err).lower()


def test_short_time_cap_is_respected():
    with pytest.raises(FlexError) as e:
        run([ok_send()] + [fail(1019)] * 20, timeout_s=5)
    assert e.value.kind == "still_generating"
    assert e.value.session.calls[-1]["t"] - 1000.0 <= 5


@pytest.mark.parametrize("code,kind", [(1012, "token_expired"), (1015, "token_invalid"), (1013, "ip_not_allowed"),
                                       (1018, "rate_limited"), (1014, "query_invalid"), (1011, "service_inactive"),
                                       (1010, "legacy_query"), (1020, "invalid_request")])
def test_send_request_errors_are_user_safe(code, kind):
    with pytest.raises(FlexError) as e:
        run([fail(code, f"secret detail {TOKEN}")])
    err = e.value
    assert err.kind == kind and err.code == code
    assert len(err.session.calls) == 1                               # no retry on a final error
    assert "secret detail" not in str(err)                           # IBKR's text is never echoed
    assert_no_token(err)


def test_final_error_while_polling_stops_at_once():
    with pytest.raises(FlexError) as e:
        run([ok_send(), fail(1019), fail(1012)])
    assert e.value.kind == "token_expired" and len(e.value.session.calls) == 3


def test_unknown_error_code_keeps_the_code_with_a_generic_message():
    with pytest.raises(FlexError) as e:
        run([fail(1099)])
    assert e.value.kind == "ibkr_error" and e.value.code == 1099


@pytest.mark.parametrize("exc,kind", [(requests.Timeout(f"https://x/?t={TOKEN}"), "ibkr_timeout"),
                                      (requests.ConnectionError(f"https://x/?t={TOKEN}"), "ibkr_unreachable")])
def test_network_errors_never_carry_the_token(exc, kind):
    with pytest.raises(FlexError) as e:
        run([exc])
    assert e.value.kind == kind and e.value.code is None
    assert_no_token(e.value)


def test_http_error_status_is_user_safe():
    with pytest.raises(FlexError) as e:
        run([Resp("<html>down</html>", 503)])
    assert e.value.kind == "ibkr_unavailable" and "503" in str(e.value)


def test_unexpected_answer_is_rejected():
    with pytest.raises(FlexError) as e:
        run(["<html>login page</html>"])
    assert e.value.kind == "ibkr_error"


def test_oversized_report_is_refused(monkeypatch):
    monkeypatch.setattr(ibkr_flex, "MAX_REPORT_BYTES", 100)
    with pytest.raises(FlexError) as e:
        run([ok_send(), REPORT * 10])
    assert e.value.kind == "report_too_large"


@pytest.mark.parametrize("url", ["http://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement",
                                 "https://evil.example.com/GetStatement",
                                 "https://interactivebrokers.com.evil.example/GetStatement"])
def test_statement_url_must_be_https_on_an_ibkr_host(url):
    _, s = run([ok_send(url), REPORT])
    assert s.calls[1]["url"] == f"{BASE_URL}/GetStatement"


def test_statement_url_from_ibkr_is_used_when_safe():
    other = "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
    _, s = run([ok_send(other), REPORT])
    assert s.calls[1]["url"] == other


@pytest.mark.parametrize("token,query,kind", [("", QUERY, "token_format"), ("abc", QUERY, "token_format"),
                                              (TOKEN + "&q=1", QUERY, "token_format"), (TOKEN, "", "query_id_format"),
                                              (TOKEN, "12a", "query_id_format")])
def test_bad_input_is_refused_before_any_request(token, query, kind):
    with pytest.raises(FlexError) as e:
        run([], token=token, query_id=query)
    assert e.value.kind == kind and e.value.session.calls == []
    assert token == "" or token not in str(e.value)


def test_token_is_never_logged(caplog):
    caplog.set_level("DEBUG")
    with pytest.raises(FlexError):
        run([ok_send(), fail(1012)])
    run([ok_send(), REPORT])
    assert TOKEN not in caplog.text
