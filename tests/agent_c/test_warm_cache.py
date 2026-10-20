"""Cache warm-up: priority order, skipping cached tickers, waiting on Tiingo's hourly limit, the monthly guard."""

from datetime import date

import pandas as pd
import pytest

from egoquant.agents.market_intel import SyntheticPriceSource
from egoquant.agents.market_intel.sources import CachedPriceSource, RateLimitError, TiingoUsage
from egoquant.agents.market_intel.warm import warm_cache, warm_plan

AS_OF, START = date(2026, 9, 30), date(2016, 1, 4)


def test_plan_puts_core_first_then_rank_one_of_every_cluster():
    plan = warm_plan(None, per_cluster=2)
    assert plan[:4] == ["BIL", "AGG", "SHY", "VT"]
    assert len(plan) == len(set(plan))
    assert "IAU" in plan[:40] and "TLT" in plan[:40]                   # rank 1 of gold and long bonds, early


class _Limited:
    """A Tiingo-like feed: the third request answers 429 once."""
    name = "tiingo"
    data_source = "historical"

    def __init__(self):
        self.inner, self.calls, self.limited_once = SyntheticPriceSource(), 0, False

    def needs_request(self, ticker, start, end):
        return True

    def fetch_adjusted_close(self, tickers, start, end):
        self.calls += 1
        if self.calls == 3 and not self.limited_once:
            self.limited_once = True
            raise RateLimitError("Tiingo rate limit reached (HTTP 429)")
        return self.inner.fetch_adjusted_close(tickers, start, end)


def test_warm_cache_skips_cached_waits_on_the_hourly_limit_and_resumes(tmp_path):
    cache = CachedPriceSource(_Limited(), tmp_path / "c", today=lambda: date(2026, 10, 1))
    usage = TiingoUsage(tmp_path / "u.json", today=lambda: date(2026, 10, 1))
    slept = []
    report = warm_cache(["BIL", "AGG", "SHY", "VT"], AS_OF, START, source=cache, usage=usage,
                        log=lambda *_: None, sleep=slept.append)
    assert report.fetched == ["BIL", "AGG", "SHY", "VT"] and report.waits == 1 and slept == [15 * 60]
    again = warm_cache(["BIL", "AGG"], AS_OF, START, source=cache, usage=usage, log=lambda *_: None)
    assert again.already_cached == ["BIL", "AGG"] and again.fetched == []


def test_warm_cache_stops_at_the_monthly_guard(tmp_path):
    cache = CachedPriceSource(_Limited(), tmp_path / "c", today=lambda: date(2026, 10, 1))
    usage = TiingoUsage(tmp_path / "u.json", limit=0, today=lambda: date(2026, 10, 1))
    report = warm_cache(["BIL"], AS_OF, START, source=cache, usage=usage, log=lambda *_: None)
    assert report.fetched == [] and "monthly" in report.stopped


# ---- the continuous background builder ----
def test_builder_waits_while_the_hourly_share_is_in_use(monkeypatch):
    from egoquant.agents.market_intel import warm

    counts = iter([30, 30, 29])                                       # two checks over the share, then room
    monkeypatch.setattr(warm, "tiingo_requests_in_last", lambda seconds, key=None: next(counts))
    slept = []
    builder = warm.CacheWarmer(per_hour=30, per_cluster=1, sleep=slept.append)
    builder._throttle()
    assert slept == [60, 60] and builder.status["state"] == "fetching"


def test_builder_cycle_warms_the_plan(monkeypatch, tmp_path):
    from egoquant.agents.market_intel import warm

    calls = {}

    def fake_warm(plan, as_of, start, **kw):
        calls.update(plan=plan, as_of=as_of, start=start, throttle=kw["before_request"])
        return warm.WarmReport(planned=len(plan), fetched=plan[:3], already_cached=plan[3:])

    monkeypatch.setattr(warm, "warm_cache", fake_warm)
    monkeypatch.setattr("egoquant.agents.market_intel.directory.load_directory", lambda *a, **k: None)
    builder = warm.CacheWarmer(per_hour=30, per_cluster=2, today=lambda: date(2026, 10, 19))
    builder.cycle()
    assert calls["as_of"] == date(2026, 10, 16) and calls["plan"][:4] == ["BIL", "AGG", "SHY", "VT"]
    assert builder.status["state"] == "idle" and builder.status["warm"] == len(calls["plan"])


def test_builder_does_not_start_when_off_synthetic_or_without_a_key(monkeypatch):
    from egoquant.agents.market_intel import warm

    monkeypatch.setattr(warm, "_WARMER", None)
    monkeypatch.setattr(warm.CacheWarmer, "start", lambda self: self)      # never a real thread in tests
    monkeypatch.delenv("TIINGO_WARM_API_KEY", raising=False)
    monkeypatch.setenv("TIINGO_API_KEY", "x")
    monkeypatch.setenv("EGOQUANT_WARM", "off")
    assert warm.start_background_warmer() is None
    monkeypatch.setenv("EGOQUANT_WARM", "on")
    monkeypatch.setenv("EGOQUANT_DATA", "synthetic")
    assert warm.start_background_warmer() is None
    monkeypatch.setenv("EGOQUANT_DATA", "")
    monkeypatch.delenv("TIINGO_API_KEY")
    assert warm.start_background_warmer() is None
    assert warm.warmer_status()["state"] == "off"


# ---- two keys: the builder's and the traders' ----
def test_each_key_has_its_own_request_count_and_ledger(tmp_path):
    from egoquant.agents.market_intel import sources

    before_a = sources.tiingo_requests_in_last(3600, "key-a")
    sources._log_tiingo_request("key-a")
    sources._log_tiingo_request("key-a")
    sources._log_tiingo_request("key-b")
    assert sources.tiingo_requests_in_last(3600, "key-a") == before_a + 2
    assert sources.tiingo_requests_in_last(3600, "key-b") >= 1
    a = sources.TiingoUsage(api_key="key-a")
    b = sources.TiingoUsage(api_key="key-b")
    assert a.path != b.path and "key-a" not in a.path.name             # the file name never holds the key


def test_traders_use_the_traders_key_and_the_builder_its_own(monkeypatch):
    from egoquant import settings
    from egoquant.agents.market_intel.sources import build_live_source

    monkeypatch.setenv("TIINGO_API_KEY", "traders-key")
    monkeypatch.setenv("TIINGO_WARM_API_KEY", "builder-key")
    live = build_live_source({"us_listed"})
    assert live.sources["us_listed"].inner.api_key == "traders-key"
    assert settings.tiingo_warm_api_key() == "builder-key" and settings.warm_has_own_key()
    assert settings.warm_per_hour() == 45
    monkeypatch.delenv("TIINGO_WARM_API_KEY")                          # one key only: shared, smaller share
    assert settings.tiingo_warm_api_key() == "traders-key" and not settings.warm_has_own_key()
    assert settings.warm_per_hour() == 30


# ---- Oct 19: one shared key ran out after every redeploy ----
class _Response:
    def __init__(self, status, text="", rows=None):
        self.status_code, self.text, self._rows = status, text, rows or []

    def json(self):
        return self._rows


class _Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), 0

    def get(self, url, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


def test_after_a_429_the_key_sends_no_request_until_the_cooldown_ends():
    from egoquant.agents.market_intel import sources

    rows = [{"date": "2026-10-16T00:00:00.000Z", "adjClose": 10.0}]
    session = _Session(_Response(429, "Error: You have run over your hourly request allocation. Token key-z"),
                       _Response(200, rows=rows))
    src = sources.TiingoPriceSource(api_key="key-z", session=session)
    for _ in range(3):
        with pytest.raises(RateLimitError):
            src.fetch_adjusted_close(["VT"], date(2026, 10, 1), date(2026, 10, 16))
    assert session.calls == 1                                         # only the first one reached Tiingo
    status = sources.tiingo_limit_status("key-z")
    assert "hourly request allocation" in status["last_429"]["message"]
    assert "key-z" not in str(status) and sources.key_fingerprint("key-z") not in str(status)
    assert status["key"] == "set" and sources.tiingo_limit_status(None) == {"key": "not set"}
    assert 0 < status["cooldown_seconds_left"] <= sources.TIINGO_COOLDOWN_SECONDS
    sources._TIINGO_COOLDOWN.clear()                                  # the window has passed
    assert list(src.fetch_adjusted_close(["VT"], date(2026, 10, 1), date(2026, 10, 16)).columns) == ["VT"]
    assert session.calls == 2


def test_a_cooldown_on_one_key_leaves_the_other_key_alone():
    from egoquant.agents.market_intel import sources

    sources._note_tiingo_429("traders-key", "")
    assert sources.tiingo_cooldown_left("traders-key") > 0 and sources.tiingo_cooldown_left("builder-key") == 0


def test_the_builder_does_not_share_the_traders_key_unless_asked(monkeypatch):
    from egoquant.agents.market_intel import warm

    monkeypatch.setattr(warm.CacheWarmer, "start", lambda self: self)      # never a real thread in tests
    monkeypatch.setenv("TIINGO_API_KEY", "traders-key")
    monkeypatch.delenv("TIINGO_WARM_API_KEY", raising=False)
    monkeypatch.setenv("EGOQUANT_DATA", "")
    for value in ("", "on"):
        monkeypatch.setattr(warm, "_WARMER", None)
        monkeypatch.setenv("EGOQUANT_WARM", value)
        assert warm.start_background_warmer() is None
        status = warm.warmer_status()
        assert status["state"] == warm.SHARED_KEY_OFF and "traders-key" not in str(status)
        assert status["tiingo_traders_key"]["key"]
    monkeypatch.setattr(warm, "_WARMER", None)
    monkeypatch.setenv("EGOQUANT_WARM", "shared")                           # the old behavior, on request
    assert warm.start_background_warmer() is not None and warm.warmer_status()["key"] == "shared with traders"
    monkeypatch.setattr(warm, "_WARMER", None)
    monkeypatch.setenv("EGOQUANT_WARM", "")
    monkeypatch.setenv("TIINGO_WARM_API_KEY", "builder-key")                # its own key: runs as before
    assert warm.start_background_warmer() is not None
    status = warm.warmer_status()
    assert status["key"] == "own" and set(status) >= {"tiingo_traders_key", "tiingo_builder_key"}
