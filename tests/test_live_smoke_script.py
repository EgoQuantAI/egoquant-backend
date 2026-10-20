"""supabase/tests/live_smoke.py (run by hand against the real project) keeps accounts it did not create.

The script is not collected by pytest and needs a real Supabase project, so this test loads it with a stub in place of `requests`
and checks only what must never regress: with pre-created users (SMOKE_A_*, SMOKE_B_*) it logs in, never signs up and never deletes
an account; self-created users are still signed up and deleted; no secret is ever printed."""

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "supabase" / "tests" / "live_smoke.py"
SECRETS = {"SMOKE_A_EMAIL": "a-user@example.org", "SMOKE_A_PASSWORD": "pw-a-0123456789", "SMOKE_B_EMAIL": "b-user@example.org",
           "SMOKE_B_PASSWORD": "pw-b-0123456789"}
BASE = {"SUPABASE_URL": "https://project.example.test", "SUPABASE_ANON_KEY": "anon-key-0123456789", "BACKEND_URL": "https://backend.example.test"}


class Reply:
    status_code = 200

    def json(self):
        return {"access_token": "token-0123456789", "username": "somebody", "consent_version": "v1", "trades": []}


class Rows(Reply):
    def json(self):                                  # Supabase's REST API answers a list of rows
        return []


def run_script(monkeypatch, capsys, env):
    for name in (*SECRETS, *BASE):
        monkeypatch.delenv(name, raising=False)
    for name, value in {**BASE, **env}.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("live_smoke_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def fake(method, url, **kwargs):
        calls.append((method, url))
        return Rows() if "/rest/v1/" in url else Reply()

    module.requests = SimpleNamespace(request=fake, get=lambda url, **kw: fake("GET", url, **kw),
                                      post=lambda url, **kw: fake("POST", url, **kw))
    module.main()
    return calls, capsys.readouterr().out


def deleted_accounts(calls):
    return [c for c in calls if c[0] == "DELETE" and c[1].endswith("/account")]


def test_pre_created_users_log_in_and_are_never_deleted_or_signed_up(monkeypatch, capsys):
    calls, out = run_script(monkeypatch, capsys, SECRETS)
    assert not deleted_accounts(calls)
    assert not any(url.endswith("/auth/v1/signup") for _, url in calls)               # no sign-up, so no e-mail, no spent quota
    assert sum(url.endswith("/auth/v1/token?grant_type=password") for _, url in calls) == 2
    assert any(m == "DELETE" and u.endswith("/account/data") for m, u in calls)        # their data is erased, the accounts stay
    assert "Pre-created accounts are kept" in out
    for secret in SECRETS.values():
        assert secret not in out
    assert "token-0123456789" not in out and BASE["SUPABASE_ANON_KEY"] not in out


def test_self_created_users_are_still_signed_up_and_deleted(monkeypatch, capsys):
    calls, out = run_script(monkeypatch, capsys, {})
    assert sum(url.endswith("/auth/v1/signup") for _, url in calls) == 3               # A, B and the taken-username attempt
    assert len(deleted_accounts(calls)) == 2


@pytest.mark.parametrize("missing", list(SECRETS))
def test_a_partial_set_of_pre_created_variables_is_not_used(monkeypatch, capsys, missing):
    env = {k: v for k, v in SECRETS.items() if k != missing}
    calls, _ = run_script(monkeypatch, capsys, env)
    assert len(deleted_accounts(calls)) == 2                                          # falls back to self-created users
    assert sum(url.endswith("/auth/v1/signup") for _, url in calls) == 3
