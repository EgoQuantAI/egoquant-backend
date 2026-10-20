"""The request-body size limit in main.py: an oversized declared body is refused before it is read, and nothing is echoed."""

import pytest
from fastapi.testclient import TestClient

from main import _body_limit, app

client = TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path,limit", [("/portfolio", 4_500_000), ("/trade-check", 4_500_000), ("/account/trades/import", 4_500_000),
                                        ("/chat", 512_000), ("/chat/stream", 512_000), ("/what-if", 512_000), ("/other", 1_000_000)])
def test_limits_by_route(path, limit):
    assert _body_limit(path) == limit


@pytest.mark.parametrize("path", ["/portfolio", "/trade-check", "/chat", "/what-if", "/account/consent", "/anything"])
def test_oversized_body_is_413_and_not_echoed(path):
    out = client.post(path, content=b"A" * 4_600_000, headers={"Content-Type": "application/json"})
    assert out.status_code == 413
    assert out.json() == {"detail": "Request is too large."}


def test_a_chat_body_over_its_own_limit_is_413_but_fine_for_a_portfolio_route():
    body = b'{"x": "' + b"A" * 600_000 + b'"}'
    assert client.post("/chat", content=body, headers={"Content-Type": "application/json"}).status_code == 413
    assert client.post("/portfolio", content=body, headers={"Content-Type": "application/json"}).status_code != 413


def test_normal_requests_and_gets_are_untouched():
    assert client.get("/health").status_code == 200
    assert client.post("/chat", json={}).status_code != 413


def test_bad_content_length_is_refused():
    out = client.post("/chat", content=b"{}", headers={"Content-Type": "application/json", "Content-Length": "abc"})
    assert out.status_code in (400, 413)
