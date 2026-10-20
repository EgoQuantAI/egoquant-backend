"""No public endpoint ever shows a key, a token or even a key's fingerprint (a hash prefix lets anyone confirm a
guessed key). Every GET route without a path parameter is called WITHOUT sign-in, with known fake secrets in the
environment, and its whole answer is searched for them. /data/status says only "set" / "not set"."""

import hashlib

import pytest
from fastapi.testclient import TestClient

from egoquant.anchoring import routes as anchoring

FAKE = {
    "TIINGO_API_KEY": "tiingo-FAKE-traders-0123456789abcdef",
    "TIINGO_WARM_API_KEY": "tiingo-FAKE-builder-0123456789abcdef",
    "OPENAI_API_KEY": "sk-FAKE-openai-0123456789abcdefghij",
    "SUPABASE_ANON_KEY": "sb-FAKE-anon-0123456789abcdefghij",
    "EGOQ_SEAL_PRIVATE_KEY": "0x" + "5" * 64,
    "EGOQUANT_CONTEXT_SECRET": "FAKE-context-secret-0123456789",
}
SKIP = {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


def _needles() -> list[str]:
    out = []
    for value in FAKE.values():
        out += [value, value.removeprefix("0x"), hashlib.sha256(value.encode()).hexdigest()[:10]]
    return out


@pytest.fixture
def client(monkeypatch):
    for k, v in FAKE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("EGOQUANT_WARM", "off")
    monkeypatch.setenv("EGOQUANT_NEWS_WORKER", "off")
    anchoring.set_anchor_client(None)
    from main import app
    yield TestClient(app, raise_server_exceptions=False)
    anchoring.set_anchor_client()


def test_data_status_says_only_set_or_not_set(client):
    data = client.get("/data/status").json()
    assert data["tiingo_traders_key"]["key"] == "set"
    assert data.get("tiingo_builder_key", {"key": "set"})["key"] == "set"


def test_no_public_get_endpoint_shows_a_key_a_token_or_a_fingerprint(client):
    from main import app
    paths = [p for p, ops in app.openapi()["paths"].items() if "get" in ops and "{" not in p and p not in SKIP]
    assert "/data/status" in paths and "/anchoring/status" in paths and len(paths) >= 8
    for path in paths:
        body = client.get(path).text
        for needle in _needles():
            assert needle not in body, f"{path} shows a secret or its fingerprint"
