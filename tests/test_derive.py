"""Generic period-over-period / share-of-total transforms, computed in code over a result the
agent already has. No LLM, no database: pure functions over plain dicts."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import derive  # noqa: E402


def step(tool, columns, rows, error=None):
    result = {"error": error} if error else {"columns": columns, "rows": rows, "row_count": len(rows)}
    return {"tool": tool, "args": {}, "result": result}


MONTHS = {"columns": ["metric_time__month", "net_revenue"],
          "rows": [["2026-01-01", 2820.8], ["2026-02-01", 2396.41], ["2026-03-01", 1856.89]]}


def test_last_result_skips_errors_and_single_row_results():
    trace = [step("query_metric", ["x"], [[1]]),                       # single row: not a series
             step("run_sql", ["x"], [[1], [2]], error="boom"),         # errored: not usable
             step("describe_metrics", ["x"], [[1], [2]]),              # not a data tool
             step("query_metric", MONTHS["columns"], MONTHS["rows"])]  # the real one
    assert derive.last_result(trace) == trace[-1]["result"]


def test_last_result_is_none_with_nothing_to_derive_from():
    assert derive.last_result([]) is None
    assert derive.last_result([step("query_metric", ["x"], [[1]])]) is None  # only one row


def test_period_over_period_pct_matches_hand_computed_values():
    out = derive.period_over_period(MONTHS, columns=None, pct=True)
    assert out["columns"] == ["metric_time__month", "net_revenue", "net_revenue_change_pct"]
    assert out["rows"][0][2] is None  # no prior row
    assert abs(out["rows"][1][2] - (2396.41 - 2820.8) / 2820.8) < 1e-9
    assert abs(out["rows"][2][2] - (1856.89 - 2396.41) / 2396.41) < 1e-9


def test_period_over_period_change_is_an_amount_not_a_percentage():
    out = derive.period_over_period(MONTHS, columns=None, pct=False)
    assert out["columns"][-1] == "net_revenue_change"
    assert out["rows"][1][2] == 2396.41 - 2820.8


def test_period_over_period_over_100_percent_is_not_special_cased():
    big = {"columns": ["m", "v"], "rows": [["a", 100.0], ["b", 300.0]]}  # +200%
    out = derive.period_over_period(big, None, pct=True)
    assert out["rows"][1][2] == 2.0


def test_share_of_total_sums_to_one():
    methods = {"columns": ["payment_method", "net_revenue"],
              "rows": [["credit_card", 5554.65], ["paypal", 4730.67],
                       ["bank_transfer", 5371.32], ["gift_card", 4829.50]]}
    out = derive.share_of_total(methods, columns=None)
    shares = [r[2] for r in out["rows"]]
    assert abs(sum(shares) - 1.0) < 1e-9
    assert out["columns"][-1] == "net_revenue_share_pct"


def test_a_named_column_that_does_not_exist_is_a_clear_error():
    try:
        derive.period_over_period(MONTHS, columns=["nope"], pct=True)
        raise AssertionError("should have raised")
    except ValueError as e:
        assert "nope" in str(e)


def test_a_result_with_no_numeric_column_is_a_clear_error():
    labels_only = {"columns": ["status"], "rows": [["placed"], ["shipped"]]}
    try:
        derive.share_of_total(labels_only, columns=None)
        raise AssertionError("should have raised")
    except ValueError:
        pass


def test_apply_dispatches_and_rejects_an_unknown_operation():
    assert derive.apply("share_of_total", MONTHS, None)["columns"][-1] == "net_revenue_share_pct"
    try:
        derive.apply("nonsense", MONTHS, None)
        raise AssertionError("should have raised")
    except ValueError:
        pass
