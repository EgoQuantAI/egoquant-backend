"""The saved-report list shows the day the prices of a snapshot build run to (QA, 20 Oct): `summary.prices_as_of`, only when the
build used the committed snapshot. Supabase is the FakeStore of test_accounts.py; SYNTHETIC prices, no network, no LLM."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import egoquant_routes
from tests.accounts.test_account_risk_game import app_client, build, request, saved_row  # noqa: F401
from tests.accounts.test_accounts import A, store  # noqa: F401


def test_a_build_without_a_snapshot_day_saves_the_summary_it_always_had(app_client, store):          # noqa: F811
    out = build(app_client, request())
    assert out.status_code == 200, out.text
    summary = saved_row(store, out.json()["report_id"])["summary"]
    assert "prices_as_of" not in summary
    assert set(summary) == {"approved", "as_of", "data_source", "equity_weight", "n_trades", "prob_goal"}


def test_a_snapshot_build_saves_the_day_its_prices_run_to(app_client, store):                          # noqa: F811
    original = egoquant_routes._portfolio_out

    def with_snapshot_day(*args, **kwargs):
        built = original(*args, **kwargs)
        built.prices_as_of = date(2026, 10, 16)
        return built

    with patch.object(egoquant_routes, "_portfolio_out", with_snapshot_day):
        out = build(app_client, request())
    assert out.status_code == 200, out.text
    assert saved_row(store, out.json()["report_id"])["summary"]["prices_as_of"] == "2026-10-16"
