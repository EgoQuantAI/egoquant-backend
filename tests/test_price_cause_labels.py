"""The plain labels /portfolio gives for why live prices were not used (egoquant_routes._cause_label): fixed words, never
the vendor's message."""

import pytest

from egoquant_routes import _cause_label, _market_data_cause

SLOW = ("the price feed answered too slowly (30 s for 2 request(s), 2 of them slow): this build uses the saved snapshot "
        "instead")


@pytest.mark.parametrize("reason, label", [
    ("TIINGO_API_KEY is not set. Real ETF prices come from Tiingo", "Tiingo key not configured"),
    ("Tiingo refused the API key (HTTP 401)", "Tiingo key rejected"),
    ("Tiingo rate limit reached (HTTP 429); try again later", "rate limit reached"),
    ("Binance refused the request from this location (HTTP 451)", "crypto feed blocked in this region"),
    (SLOW, "price feed too slow"),
    ("Tiingo error for VT: HTTP 500", "data vendor error"),
])
def test_each_reason_has_its_plain_label(reason, label):
    assert _cause_label(reason) == label


def test_the_slow_feed_label_is_fixed_words_without_numbers_or_names():
    label = _cause_label(SLOW + "; Tiingo could not be reached (network error: ReadTimeout)")
    assert label == "price feed too slow" and not any(ch.isdigit() for ch in label)
    assert _market_data_cause(RuntimeError(SLOW)) == "price feed too slow"
