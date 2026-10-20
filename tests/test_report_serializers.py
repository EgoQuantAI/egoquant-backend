"""ClientReport leaves its optional fields out of the JSON when they are empty (v2 diagnostics, snapshot price fields).

Pydantic keeps only the LAST @model_serializer of a model: with two of them the first one's work silently disappears and
the default report grows a "final_diagnostics": null (the regime golden hash then changes). So: exactly one serializer,
and both behaviours checked on a real report."""

import json

from egoquant.report import ClientReport
from tests.helpers import make_answers
from tests.system.test_goal_based import run as run_goal


def test_the_report_has_exactly_one_model_serializer():
    assert len(ClientReport.__pydantic_decorators__.model_serializers) == 1


def test_empty_optional_fields_are_left_out_and_set_ones_are_kept():
    report = run_goal(make_answers(gl_score=25)).report
    plain = json.loads(report.model_dump_json())
    assert "final_diagnostics" not in plain and "prices_as_of" not in plain and "price_note" not in plain

    shown = json.loads(report.model_copy(update={"final_diagnostics": {"v2_effective_n": 8.5},
                                                 "price_note": "real prices as of 2026-10-16, cached",
                                                 "prices_as_of": report.as_of}).model_dump_json())
    assert shown["final_diagnostics"] == {"v2_effective_n": 8.5}
    assert shown["price_note"].startswith("real prices as of") and shown["prices_as_of"] == str(report.as_of)
