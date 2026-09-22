"""Grounding check. No LLM, no database."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from grounding import ungrounded  # noqa: E402


def step(metric, value):
    return {"tool": "query_metric", "args": {"metrics": [metric]},
            "result": {"columns": [metric], "rows": [[value]], "row_count": 1, "governed": True}}


TRACE = [step("gross_revenue", 22633.57), step("net_revenue", 20486.14),
         step("refund_rate", 0.08333333333333333)]


def test_returned_figures_pass():
    assert ungrounded("Net revenue is $20,486.14.", "net revenue?", TRACE) == []
    assert ungrounded("Gross revenue was 22,633.57", "gross?", TRACE) == []


def test_invented_product_is_flagged():
    # the real failure: refund rate multiplied by gross revenue, reported as if queried
    bad = ungrounded("Refunded amount: $1,885.63", "how much refunded?", TRACE)
    assert bad == ["$1,885.63"], bad


def test_difference_across_separate_queries_is_allowed():
    assert ungrounded("$2,147.43 was refunded.", "refunded?", TRACE) == []


def test_percent_from_fraction():
    assert ungrounded("The refund rate is 8.3%.", "refund rate?", TRACE) == []
    assert ungrounded("The refund rate is 8.33%.", "refund rate?", TRACE) == []
    assert ungrounded("The refund rate is 9.1%.", "refund rate?", TRACE) == ["9.1%"]


def test_percent_over_100_is_not_wrongly_flagged():
    # net_revenue_growth_mom of 1.6617333... (166.2%) was wrongly flagged: the x100 conversion
    # only applied to a raw value within +/-1, which a growth metric can legitimately exceed
    growth = [step("net_revenue_growth_mom", 1.6617333480753738)]
    assert ungrounded("Growth was +166.2%.", "growth?", growth) == []
    assert ungrounded("Growth was -63.5%.", "growth?",
                      [step("net_revenue_growth_mom", -0.6354522945816778)]) == []
    assert ungrounded("Growth was 200.0%.", "growth?", growth) == ["200.0%"]  # still catches a wrong one


def test_rounding_and_suffix():
    assert ungrounded("Roughly $20,486 net.", "q", TRACE) == []
    assert ungrounded("About $20.5k net.", "q", TRACE) == []
    assert ungrounded("About $25k net.", "q", TRACE) == ["$25k"]


def test_noise_is_not_a_claim():
    text = "In 2026, since 2026-04-01, the top 3 are:\n1. first\n2. second. Ran `limit 200`."
    assert ungrounded(text, "top 3 customers", TRACE) == []


def test_numbers_from_the_question_are_allowed():
    assert ungrounded("A 10% discount is not in the data.", "Would a 10% discount help?", []) == []
    assert ungrounded("There are 500 of them.", "Would a 10% discount help?", []) == ["500"]


def test_column_total_and_multirow():
    months = {"tool": "query_metric", "args": {},
              "result": {"columns": ["m", "v"], "rows": [["a", 100.0], ["b", 250.5]], "row_count": 2}}
    assert ungrounded("Total 350.50, best month 250.50.", "q", [months]) == []


def test_errors_and_no_trace():
    err = {"tool": "run_sql", "args": {}, "result": {"error": "nope"}}
    assert ungrounded("It was 12,345.", "q", [err]) == ["12,345"]
    assert ungrounded("No numbers here.", "q", []) == []
