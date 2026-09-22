"""Result table rendering. No LLM, no database."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tables import render  # noqa: E402


def data(tool, columns, rows, **extra):
    return {"tool": tool, "args": {}, "result": {"columns": columns, "rows": rows,
                                                 "row_count": len(rows), **extra}}


def test_single_row_gets_no_table():
    assert render([data("query_metric", ["net_revenue"], [[1.0]], governed=True)]) == ""


def test_formatting():
    t = render([data("query_metric", ["order__status", "refund_rate", "net_revenue", "n", "order__customer"],
                     [["a|b", 0.0833, 1234.5, 1200, 7], ["c", None, 2.0, 3, 1234]], governed=True)],
               {"refund_rate"})
    assert "| a\\|b | 8.3% | 1,234.50 | 1,200 | 7 |" in t, t
    assert "| c | – | 2.00 | 3 | 1234 |" in t, t            # ids get no thousands separator
    assert "| --- | ---: | ---: | ---: | ---: |" in t, t
    assert t.startswith("Returned by the governed metric query:")


def test_ungoverned_is_labelled():
    t = render([data("run_sql", ["x", "y"], [[1, 2], [3, 4]], governed=False)])
    assert t.startswith("Returned by ad-hoc SQL (not a governed metric):")


def test_a_trailing_all_null_row_is_dropped_as_a_join_artifact():
    # net_revenue_growth_mom: the first period has no prior period (genuinely null) and
    # MetricFlow's time-spine join can add one spurious trailing period past the last real one.
    # Neither is informative in a table, so both edges are trimmed; the middle is never touched.
    rows = [["2026-01-01", None], ["2026-02-01", -0.15], ["2026-03-01", -0.23], ["2026-09-01", None]]
    t = render([data("query_metric", ["metric_time__month", "net_revenue_growth_mom"], rows, governed=True)],
               {"net_revenue_growth_mom"})
    assert "2026-01-01" not in t and "2026-09-01" not in t
    assert "2026-02-01" in t and "2026-03-01" in t


def test_a_null_in_the_middle_of_the_table_is_kept_as_dash():
    rows = [["2026-01-01", -0.15], ["2026-02-01", None], ["2026-03-01", -0.23]]
    t = render([data("query_metric", ["metric_time__month", "net_revenue_growth_mom"], rows, governed=True)],
               {"net_revenue_growth_mom"})
    assert "| 2026-02-01 | – |" in t, t


def test_trimming_never_drops_below_two_rows():
    rows = [["2026-01-01", None], ["2026-02-01", None]]
    t = render([data("query_metric", ["metric_time__month", "x"], rows, governed=True)])
    assert "2026-01-01" in t and "2026-02-01" in t


def test_errors_skipped_duplicates_removed_and_capped():
    err = {"tool": "run_sql", "args": {}, "result": {"error": "boom"}}
    a = data("query_metric", ["k"], [[1], [2]], governed=True)
    assert render([err, a, a]).count("Returned by") == 1
    big = data("query_metric", ["k"], [[i] for i in range(40)], governed=True)
    out = render([big])
    assert "…and 15 more rows." in out and "| 24 |" in out and "| 25 |" not in out
