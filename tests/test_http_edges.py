"""The HTTP surface as a hostile client sees it (the live probes of 20 Oct, offline): every error status carries the security
headers, the CORS rules hold, an unauthenticated caller gets a plain refusal from every account route, and no odd method or path
ends in a 5xx. Complements tests/test_body_limit.py, tests/test_no_echo_422.py and tests/accounts/test_extreme_inputs.py."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import egoquant_routes

SECURITY_HEADERS = {"x-content-type-options": "nosniff", "x-frame-options": "DENY",
                    "referrer-policy": "strict-origin-when-cross-origin", "permissions-policy": "camera=(), microphone=(), geolocation=()"}
VERCEL = "https://fintechaton-frontend.vercel.app"


@pytest.fixture(scope="module")
def client():
    from main import app
    return TestClient(app, raise_server_exceptions=False)


def assert_headers(response, what):
    got = {k: response.headers.get(k) for k in SECURITY_HEADERS}
    assert got == SECURITY_HEADERS, (what, response.status_code, got)


def test_every_error_status_carries_the_security_headers(client, monkeypatch):
    egoquant_routes._portfolio_timestamps.clear()
    monkeypatch.setattr(egoquant_routes, "_PORTFOLIO_LIMIT", 0)                 # the bucket is "full": the next build is a 429
    valid = {"answers": {"choices": {}, "amount_invested": 1000.0, "amount_needed": 0.0}, "interest_areas": ["stocks"], "horizon_years": 5.0}
    seen = {}
    probes = {
        "404": lambda: client.get("/nope"),
        "405": lambda: client.put("/profile", json={}),
        "411 (chunked body)": lambda: client.post("/profile", content=iter([b"{}"]), headers={"Content-Type": "application/json"}),
        "413 (declared too large)": lambda: client.post("/profile", content=b"x" * 1_100_000, headers={"Content-Type": "application/json"}),
        "422": lambda: client.post("/profile", json={}),
        "401": lambda: client.get("/account/me"),
        "429": lambda: client.post("/portfolio", json=valid),
        "200": lambda: client.get("/health"),
    }
    for name, make in probes.items():
        r = make()
        seen[name] = r.status_code
        assert_headers(r, name)
        assert "Traceback" not in r.text and r.status_code < 500, (name, r.status_code, r.text[:120])
    assert seen["411 (chunked body)"] == 411 and seen["413 (declared too large)"] == 413 and seen["401"] == 401, seen
    assert seen["404"] == 404 and seen["405"] == 405 and seen["422"] == 422 and seen["200"] == 200, seen
    assert seen["429"] in (422, 429), seen                                     # 422 if the answers above are refused before the limiter


def test_the_rate_limit_answer_is_plain_json_with_the_security_headers(client, monkeypatch):
    from tests.system.test_risk_game_blend import random_answers
    monkeypatch.setenv("EGOQUANT_DATA", "synthetic")
    monkeypatch.setenv("EGOQUANT_BRIEFING", "off")
    egoquant_routes._portfolio_timestamps.clear()
    egoquant_routes._portfolio_timestamps.extend([time.monotonic()] * egoquant_routes._PORTFOLIO_LIMIT)    # 20 builds in the last minute
    try:
        r = client.post("/portfolio", json={"answers": random_answers(1), "interest_areas": ["stocks"], "horizon_years": 5.0})
        assert r.status_code == 429 and r.headers["content-type"].startswith("application/json"), r.text[:120]
        assert r.json() == {"detail": "Too many portfolio requests. Please try again later."}
        assert_headers(r, "429")
    finally:
        egoquant_routes._portfolio_timestamps.clear()


def test_cors_allows_only_the_vercel_origin_and_never_credentials(client):
    ok = client.options("/profile", headers={"Origin": VERCEL, "Access-Control-Request-Method": "POST",
                                             "Access-Control-Request-Headers": "content-type"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == VERCEL
    assert "access-control-allow-credentials" not in ok.headers
    for origin in ("https://evil.example", "null", "http://fintechaton-frontend.vercel.app", VERCEL + ".evil.example"):
        refused = client.options("/profile", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
        assert refused.status_code == 400 and "access-control-allow-origin" not in refused.headers, (origin, refused.headers)
        plain = client.get("/health", headers={"Origin": origin})
        assert "access-control-allow-origin" not in plain.headers and "access-control-allow-credentials" not in plain.headers, origin
    assert client.options("/profile", headers={"Origin": VERCEL, "Access-Control-Request-Method": "PUT"}).status_code == 400
    assert client.options("/profile", headers={"Origin": VERCEL, "Access-Control-Request-Method": "POST",
                                               "Access-Control-Request-Headers": "x-custom-header"}).status_code == 400


def operations(app):
    """(METHOD, path) for every documented operation (the router objects of this FastAPI version hide their routes)."""
    return [(method.upper(), path) for path, ops in app.openapi()["paths"].items() for method in ops
            if method in ("get", "post", "put", "patch", "delete")]


def fill(path):
    for name in ("report_id", "check_id"):
        path = path.replace("{" + name + "}", "00000000-0000-0000-0000-000000000000")
    return path.replace("{session_id}", "abcdefgh12").replace("{job_id}", "x").replace("{tx_hash}", "x")


def test_an_unauthenticated_caller_gets_a_plain_refusal_from_every_account_route(client):
    """No sign-in: never a 200, never a 5xx. 401 from the check on the token; a body route may answer 422 first (FastAPI validates the body
    before the function runs, so the caller learns the field names, which /openapi.json shows anyway)."""
    from main import app
    ops = [(m, p) for m, p in operations(app) if p.startswith("/account") or p == "/what-if" or p.startswith("/privacy-log")]
    assert len(ops) >= 25, len(ops)
    public = {"/account/consent-version", "/account/username-available"}
    for method, path in ops:
        if path in public:
            continue
        r = client.request(method, fill(path), json={} if method in ("POST", "PUT", "PATCH") else None)
        off = r.status_code == 503 and "not available on this server" in r.text         # sealing switched off: said plainly, before sign-in
        assert r.status_code in (401, 422) or off, (method, path, r.status_code, r.text[:100])
        assert "Traceback" not in r.text
        assert_headers(r, f"{method} {path}")


def test_no_method_and_no_path_ends_in_a_5xx(client):
    from main import app
    have = {}
    for method, path in operations(app):
        have.setdefault(path, set()).add(method)
    for path, methods in have.items():
        for method in ("HEAD", "OPTIONS", "TRACE", "PATCH", "DELETE", "PUT"):
            if method in methods:
                continue                                                         # a method the route has is tested elsewhere
            r = client.request(method, fill(path))
            assert r.status_code < 500 and "Traceback" not in r.text, (method, path, r.status_code)
    for odd in ("//", "/%2e%2e/%2e%2e/etc/passwd", "/health/", "/?q=%3Cscript%3E", "/" + "a" * 5000, "/%00", "/profile/", "/portfolio/",
                "/account//me", "/é🙂"):
        r = client.get(odd)
        assert r.status_code < 500 and "Traceback" not in r.text, (odd, r.status_code)
        assert "<script>" not in r.text
