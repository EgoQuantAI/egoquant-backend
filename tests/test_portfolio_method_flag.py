"""settings.portfolio_method(): "v2" only for the exact value "v2" (stripped, any case); everything else is "v1"."""

import pytest

from egoquant import settings


@pytest.mark.parametrize("value", ["v2", "V2", " v2 ", "\tV2\n", "  v2"])
def test_only_v2_switches_the_new_engine_on(monkeypatch, value):
    monkeypatch.setenv("EGOQUANT_PORTFOLIO_METHOD", value)
    assert settings.portfolio_method() == "v2"


@pytest.mark.parametrize("value", ["", " ", "v1", "V1", "v3", "v2.0", "v 2", "2", "true", "on", "1", "yes", "v2v2", "vv2", "v2;"])
def test_everything_else_is_v1(monkeypatch, value):
    monkeypatch.setenv("EGOQUANT_PORTFOLIO_METHOD", value)
    assert settings.portfolio_method() == "v1"


def test_unset_is_v1(monkeypatch):
    monkeypatch.delenv("EGOQUANT_PORTFOLIO_METHOD", raising=False)
    assert settings.portfolio_method() == "v1"


def test_it_is_read_at_call_time_not_import_time(monkeypatch):
    monkeypatch.delenv("EGOQUANT_PORTFOLIO_METHOD", raising=False)
    assert settings.portfolio_method() == "v1"
    monkeypatch.setenv("EGOQUANT_PORTFOLIO_METHOD", "v2")
    assert settings.portfolio_method() == "v2"
    monkeypatch.setenv("EGOQUANT_PORTFOLIO_METHOD", "v1")
    assert settings.portfolio_method() == "v1"
