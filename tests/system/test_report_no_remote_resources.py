"""The downloaded report (and the sealed report built from it) is one self-contained page: opening the file makes no request.

Found on 20 Oct: a restyled report asked Google Fonts for two typefaces, which would tell a third party every time a saved or sealed report
is opened, and would let a remote stylesheet change what a sealed page looks like. Whatever the styling, the page must fetch nothing by itself.
"""
import re

from egoquant.report import render_html
from tests.helpers import make_answers
from tests.system.test_goal_based import run as run_goal

# anything a browser fetches by itself when it opens the file (a plain <a href> is only followed on a click and is not listed)
AUTOMATIC = re.compile(r"<link\b|<script\b|<iframe\b|<embed\b|<object\b|<(?:img|source|audio|video)\b[^>]*\bsrc\s*=|@import|url\(", re.I)


def test_the_download_makes_no_request_when_it_is_opened():
    page = render_html(run_goal(make_answers(gl_score=25)).report)
    found = AUTOMATIC.search(page)
    assert not found, found.group(0)
    assert "googleapis" not in page and "gstatic" not in page
